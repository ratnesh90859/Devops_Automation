from flask import Flask, jsonify, request, Response
import os, time, psutil, logging, json as _json, threading, urllib.request as _urllib_req, math, collections
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST
)

# ── Structured logger — 3 log types Loki will label ──────────────────────────
# [INFRA]  →  infrastructure-level events (memory, CPU, container)
# [APP]    →  application-level events (exceptions, slow requests, errors)
# [BIZ]    →  business events (order placed, order failed, payment events)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
)
_log = logging.getLogger("order-api")


def log_infra(msg: str, **kw):
    print(f"[INFRA] {msg} {_json.dumps(kw) if kw else ''}", flush=True)

def log_app(msg: str, **kw):
    print(f"[APP] {msg} {_json.dumps(kw) if kw else ''}", flush=True)

def log_biz(msg: str, **kw):
    print(f"[BIZ] {msg} {_json.dumps(kw) if kw else ''}", flush=True)

# Runtime toggle — set HEAVY_ENABLED=false env var to disable at deploy time,
# or call POST /admin/disable-heavy to disable without redeploying.
_heavy_enabled = os.getenv("HEAVY_ENABLED", "true").lower() != "false"
_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "infraguard-secret-2026")

# ── Threshold Monitor ─────────────────────────────────────────────────────────
# Counters start at ZERO on every app boot. When a counter reaches its
# configured limit the system POSTs an alert to the AI agent which will
# diagnose the issue, suggest a fix, and prompt for approval on Telegram.

_threshold_lock   = threading.Lock()
_COOLDOWN_UNTIL   = 0.0   # Unix timestamp — suppress duplicate count-based alerts
_MEM_COOLDOWN_UNTIL = 0.0 # Separate cooldown for memory threshold alerts
_COOLDOWN_SECS    = int(os.getenv("THRESHOLD_COOLDOWN_SECS", "120"))
_MEM_COOLDOWN_SECS = int(os.getenv("MEMORY_COOLDOWN_SECS", "300"))  # 5 min between memory alerts
_AGENT_WEBHOOK    = os.getenv("AGENT_WEBHOOK_URL", "")
_WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "")
_app_start_time   = time.time()   # used to compute uptime in alert payloads
_MEMORY_ALERT_FIRED = False  # Only fire ONE memory alert per container lifecycle

# Memory threshold — fires when RSS exceeds 80% of the container limit.
# Cloud Run 256Mi container limit ≈ 256 MB process RSS before OOM kill.
# 80% of 256 MB = ~205 MB. Override via MEMORY_THRESHOLD_MB env var.
_MEMORY_THRESHOLD_MB = int(os.getenv("MEMORY_THRESHOLD_MB", "205"))
_MEMORY_LIMIT_MB     = int(os.getenv("MEMORY_LIMIT_MB", "256"))  # current container limit

_THRESHOLDS: dict = {
    "total_requests": {
        "count": 0,
        "limit": int(os.getenv("THRESHOLD_REQUESTS", "100")),
        "fired": False,
        "severity": "medium",
        "description": "Total HTTP request volume has exceeded the configured threshold.",
    },
    "total_errors": {
        "count": 0,
        "limit": int(os.getenv("THRESHOLD_ERRORS", "10")),
        "fired": False,
        "severity": "high",
        "description": "5xx error count has exceeded the configured threshold.",
    },
}


def _post_alert_background(payload: dict) -> None:
    """Fire the threshold alert payload to the agent webhook in a daemon thread."""
    if not _AGENT_WEBHOOK:
        log_infra("threshold_no_webhook_configured", payload=str(payload)[:200])
        return

    def _send():
        try:
            data = _json.dumps(payload).encode()
            req  = _urllib_req.Request(
                f"{_AGENT_WEBHOOK}/webhook",
                data=data,
                headers={"Content-Type": "application/json", "X-Token": _WEBHOOK_SECRET},
                method="POST",
            )
            with _urllib_req.urlopen(req, timeout=10):
                pass
            log_infra(
                "threshold_alert_fired",
                metric=payload["metric"],
                value=payload["value"],
                limit=payload["threshold"],
            )
        except Exception as exc:
            log_infra("threshold_alert_failed", metric=payload.get("metric"), error=str(exc))

    threading.Thread(target=_send, daemon=True).start()


def _increment_threshold(key: str) -> None:
    """Increment a named counter and fire to the agent when its limit is reached."""
    global _COOLDOWN_UNTIL
    with _threshold_lock:
        entry = _THRESHOLDS[key]
        entry["count"] += 1
        current = entry["count"]
        limit   = entry["limit"]
        if entry["fired"] or current < limit:
            return  # nothing to do yet
        # Threshold crossed — check cooldown then fire
        now = time.time()
        if now < _COOLDOWN_UNTIL:
            return  # still in cooldown
        entry["fired"]  = True
        _COOLDOWN_UNTIL = now + _COOLDOWN_SECS

    # Build and dispatch the alert (outside the lock to avoid blocking requests)
    # Collect a snapshot of recent structured logs for all three layers so the
    # AI agent can correlate across infrastructure / application / business events.
    def _collect_recent_logs(n: int = 30) -> dict:
        """Grab the last N lines from each log layer printed to stdout."""
        # In production (Cloud Run) these are read by the agent via GCP Logging.
        # Here we surface basic counters that are always available in-process.
        mem_mb  = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
        uptime  = round(time.time() - _app_start_time, 0)
        return {
            "infra_snapshot":    (
                f"[INFRA] memory_current_mb={mem_mb} "
                f"uptime_secs={uptime} "
                f"total_requests={_THRESHOLDS['total_requests']['count']} "
                f"total_errors={_THRESHOLDS['total_errors']['count']}"
            ),
            "app_snapshot":      (
                f"[APP] error_rate={round(entry['count'] / max(_THRESHOLDS['total_requests']['count'], 1) * 100, 1)}% "
                f"errors={_THRESHOLDS['total_errors']['count']} "
                f"requests={_THRESHOLDS['total_requests']['count']}"
            ),
            "business_snapshot": (
                f"[BIZ] orders_affected={_THRESHOLDS['total_errors']['count']} "
                f"service_degraded={'true' if _THRESHOLDS['total_errors']['count'] >= 5 else 'false'} "
                f"heavy_enabled={_heavy_enabled}"
            ),
        }

    snapshots = _collect_recent_logs()
    payload = {
        "source":            "threshold_monitor",
        "alertname":         f"{key}_threshold_breached",
        "severity":          entry["severity"],
        "service":           "order-api",
        "metric":            key,
        "value":             str(current),
        "threshold":         str(limit),
        "summary":           f"{key} reached {current} (threshold: {limit})",
        "description": (
            f"{entry['description']} "
            f"Counter: {current}/{limit}. "
            f"Automatic AI diagnosis and fix suggestion have been triggered."
        ),
        # Rich context for cross-layer AI correlation
        "infra_logs":        snapshots["infra_snapshot"],
        "app_logs":          snapshots["app_snapshot"],
        "business_logs":     snapshots["business_snapshot"],
        "memory_mb":         round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1),
        "total_requests":    _THRESHOLDS["total_requests"]["count"],
        "total_errors":      _THRESHOLDS["total_errors"]["count"],
        "error_rate_pct":    round(
            _THRESHOLDS["total_errors"]["count"] /
            max(_THRESHOLDS["total_requests"]["count"], 1) * 100, 1
        ),
    }
    log_infra("threshold_breached", key=key, count=current, limit=limit)
    _post_alert_background(payload)


def _check_memory_threshold() -> None:
    """
    Fire ONE alert when process RSS exceeds 80% of the container memory limit.
    Only fires once per container lifecycle to avoid flooding the agent with
    duplicate alerts. The agent handles the fix via PR → merge → terraform.
    """
    global _MEMORY_ALERT_FIRED
    if _MEMORY_ALERT_FIRED:
        return
    mem_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    if mem_mb < _MEMORY_THRESHOLD_MB:
        return
    with _threshold_lock:
        if _MEMORY_ALERT_FIRED:
            return  # double-check under lock
        _MEMORY_ALERT_FIRED = True

    pct = round(mem_mb / _MEMORY_LIMIT_MB * 100, 1)
    log_infra("memory_threshold_breached",
              mem_mb=round(mem_mb, 1),
              threshold_mb=_MEMORY_THRESHOLD_MB,
              limit_mb=_MEMORY_LIMIT_MB,
              pct=pct)
    payload = {
        "source":          "threshold_monitor",
        "alertname":       "memory_high",
        "severity":        "high",
        "service":         "order-api",
        "metric":          "memory_mb",
        "value":           str(round(mem_mb, 1)),
        "threshold":       str(_MEMORY_THRESHOLD_MB),
        "summary":         f"Memory {round(mem_mb,1)}MB = {pct}% of {_MEMORY_LIMIT_MB}MB container limit",
        "description":     (
            f"Process RSS has reached {pct}% of the container memory limit "
            f"({round(mem_mb,1)}MB / {_MEMORY_LIMIT_MB}MB). "
            f"Container will be OOM-killed if memory is not increased. "
            f"Automatic AI diagnosis and fix suggestion have been triggered."
        ),
        "infra_logs":      (
            f"[INFRA] memory_current_mb={round(mem_mb,1)} "
            f"memory_threshold_mb={_MEMORY_THRESHOLD_MB} "
            f"memory_limit_mb={_MEMORY_LIMIT_MB} "
            f"memory_pct={pct} "
            f"uptime_secs={round(time.time()-_app_start_time,0)}"
        ),
        "app_logs":        (
            f"[APP] memory_usage_pct={pct}% "
            f"total_requests={_THRESHOLDS['total_requests']['count']} "
            f"total_errors={_THRESHOLDS['total_errors']['count']}"
        ),
        "business_logs":   (
            f"[BIZ] service_at_risk=true "
            f"oom_kill_imminent=true "
            f"mem_mb={round(mem_mb,1)} "
            f"limit_mb={_MEMORY_LIMIT_MB}"
        ),
        "memory_mb":       round(mem_mb, 1),
        "memory_pct":      pct,
        "memory_limit_mb": _MEMORY_LIMIT_MB,
        "total_requests":  _THRESHOLDS["total_requests"]["count"],
        "total_errors":    _THRESHOLDS["total_errors"]["count"],
        "error_rate_pct":  round(
            _THRESHOLDS["total_errors"]["count"] /
            max(_THRESHOLDS["total_requests"]["count"], 1) * 100, 1
        ),
    }
    _post_alert_background(payload)


app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Anomaly Detector
# ─────────────────────────────────────────────────────────────────────────────
# Tracks a rolling baseline of 3 metrics sampled every second:
#   • request_rate  — requests/sec in the last 1-second window
#   • latency_p95   — 95th-percentile latency of the last 60 observations
#   • error_rate    — fraction of requests returning 5xx in the last 60 obs
#
# A spike is flagged when the current value exceeds:
#   baseline_mean + (Z_THRESHOLD × baseline_stddev)   AND
#   baseline_mean × MIN_SPIKE_RATIO                   (must be a real spike, not just noise)
#
# The detector fires ONE alert per anomaly, with a cooldown between events.
# -----------------------------------------------------------------------------

_ANOMALY_WINDOW    = int(os.getenv("ANOMALY_WINDOW",    "60"))   # baseline samples
_ANOMALY_Z         = float(os.getenv("ANOMALY_Z",       "2.5"))  # z-score threshold (lowered for sub-ms baselines)
_ANOMALY_RATIO     = float(os.getenv("ANOMALY_RATIO",   "3.0"))  # must be ≥ 3× baseline mean
_ANOMALY_COOLDOWN  = int(os.getenv("ANOMALY_COOLDOWN",  "120"))  # seconds between alerts
_ANOMALY_MIN_OBS   = int(os.getenv("ANOMALY_MIN_OBS",   "20"))   # need this many observations before firing

_anomaly_lock = threading.Lock()
_anomaly_cooldown_until: float = 0.0
_anomaly_last_fired: dict = {}   # metric → last fire time
_anomaly_fired_flag: bool = False # single-fire per container lifecycle like memory alert

# Per-metric sliding windows
_anomaly_windows: dict = {
    "latency_p95":   collections.deque(maxlen=_ANOMALY_WINDOW),  # 5-sec interval p95 latency
    "error_rate":    collections.deque(maxlen=_ANOMALY_WINDOW),  # 5-sec interval error fraction
    "request_rate":  collections.deque(maxlen=_ANOMALY_WINDOW),  # 5-sec interval req/s
}

# Per-5s bucket accumulators (filled by after_request, drained by background sampler)
_bucket_lock   = threading.Lock()
_bucket: dict  = {"latencies": [], "errors": 0, "requests": 0, "ts": time.time()}


def _record_request_for_anomaly(latency: float, is_error: bool) -> None:
    """Called after each request to accumulate into the current 5s bucket."""
    with _bucket_lock:
        _bucket["latencies"].append(latency)
        _bucket["requests"]  += 1
        if is_error:
            _bucket["errors"] += 1


def _anomaly_sampler_loop() -> None:
    """
    Background thread — wakes every 5 seconds, drains the bucket into sliding
    windows, then checks each metric for statistical anomalies.
    Runs for the lifetime of the process.
    """
    while True:
        time.sleep(5)
        try:
            with _bucket_lock:
                latencies  = list(_bucket["latencies"])
                errors     = _bucket["errors"]
                requests   = _bucket["requests"]
                _bucket["latencies"] = []
                _bucket["errors"]    = 0
                _bucket["requests"]  = 0
                _bucket["ts"]        = time.time()

            if requests == 0:
                continue  # no traffic in this window, skip

            # Compute 5s-window metrics
            p95_latency  = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0.0
            err_rate     = errors / requests
            req_per_sec  = requests / 5.0

            # Push into sliding windows
            _anomaly_windows["latency_p95"].append(p95_latency)
            _anomaly_windows["error_rate"].append(err_rate)
            _anomaly_windows["request_rate"].append(req_per_sec)

            # Check each metric for anomaly
            _check_anomaly("latency_p95",   p95_latency)
            _check_anomaly("error_rate",    err_rate)
            _check_anomaly("request_rate",  req_per_sec)

        except Exception as exc:
            log_infra("anomaly_sampler_error", error=str(exc))


# Start the background sampler as a daemon thread
_sampler_thread = threading.Thread(target=_anomaly_sampler_loop, daemon=True, name="anomaly-sampler")
_sampler_thread.start()


def _stats(window: collections.deque) -> tuple[float, float]:
    """Return (mean, stddev) of a deque. Returns (0, 0) if < 2 items."""
    data = list(window)
    n = len(data)
    if n < 2:
        return 0.0, 0.0
    mean = sum(data) / n
    variance = sum((x - mean) ** 2 for x in data) / n
    return mean, math.sqrt(variance)


def _check_anomaly(metric: str, current: float) -> None:
    """
    Compare current value against rolling baseline.
    If it's a statistical spike, fire an alert to the agent.
    """
    global _anomaly_cooldown_until
    window = _anomaly_windows[metric]
    if len(window) < _ANOMALY_MIN_OBS:
        return  # not enough baseline data yet

    mean, stddev = _stats(window)
    if mean <= 0:
        return

    # Z-score
    z = (current - mean) / stddev if stddev > 0 else 0.0
    # Spike ratio
    ratio = current / mean if mean > 0 else 0.0

    if z < _ANOMALY_Z or ratio < _ANOMALY_RATIO:
        return  # not a real spike

    now = time.time()
    with _anomaly_lock:
        last = _anomaly_last_fired.get(metric, 0.0)
        if now - last < _ANOMALY_COOLDOWN:
            return  # still in per-metric cooldown
        _anomaly_last_fired[metric] = now

    # Build human-readable description of the anomaly
    labels = {
        "latency_p95":   ("p95 latency",   "s",   "high_latency"),
        "error_rate":    ("5xx error rate", "%",   "error_spike"),
        "request_rate":  ("request rate",  "rps", "traffic_spike"),
    }
    label, unit, atype = labels.get(metric, (metric, "", "anomaly"))
    display_current = round(current * 100, 1) if metric == "error_rate" else round(current, 3)
    display_mean    = round(mean    * 100, 1) if metric == "error_rate" else round(mean,    3)
    unit_str        = "%" if metric == "error_rate" else unit

    mem_mb  = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
    uptime  = round(time.time() - _app_start_time, 0)

    log_infra("anomaly_detected",
              metric=metric,
              current=display_current,
              baseline_mean=display_mean,
              z_score=round(z, 2),
              spike_ratio=round(ratio, 2))

    payload = {
        "source":       "anomaly_detector",
        "alertname":    f"anomaly_{atype}",
        "severity":     "high",
        "service":      "order-api",
        "metric":       metric,
        "value":        str(display_current),
        "threshold":    str(display_mean),
        "summary":      (
            f"Anomaly detected: {label} spiked to {display_current}{unit_str} "
            f"(baseline {display_mean}{unit_str}, z={round(z,2):.1f}σ, {round(ratio,1)}× normal)"
        ),
        "description":  (
            f"Statistical anomaly: {label} is {round(ratio,1)}× above baseline "
            f"and {round(z,2):.1f} standard deviations from normal. "
            f"Baseline (last {_ANOMALY_WINDOW}s) mean={display_mean}{unit_str}, stddev={round(stddev*(100 if metric=='error_rate' else 1),3)}. "
            f"This may indicate a sudden traffic spike, deployment issue, or infrastructure problem."
        ),
        "infra_logs":   (
            f"[INFRA] anomaly_metric={metric} current={display_current}{unit_str} "
            f"baseline={display_mean}{unit_str} z_score={round(z,2)} "
            f"spike_ratio={round(ratio,2)} memory_mb={mem_mb} uptime_secs={uptime}"
        ),
        "app_logs":     (
            f"[APP] anomaly_type={atype} "
            f"total_requests={_THRESHOLDS['total_requests']['count']} "
            f"total_errors={_THRESHOLDS['total_errors']['count']} "
            f"error_rate_pct={round(_THRESHOLDS['total_errors']['count'] / max(_THRESHOLDS['total_requests']['count'],1)*100,1)}"
        ),
        "business_logs": (
            f"[BIZ] spike_detected=true metric={metric} "
            f"deviation={round(z,1)}sigma ratio={round(ratio,1)}x "
            f"service_impact=possible_degradation"
        ),
        "anomaly_metric":  metric,
        "anomaly_z_score": round(z, 2),
        "anomaly_ratio":   round(ratio, 2),
        "baseline_mean":   display_mean,
        "baseline_stddev": round(stddev * (100 if metric == "error_rate" else 1), 3),
        "memory_mb":       mem_mb,
        "total_requests":  _THRESHOLDS["total_requests"]["count"],
        "total_errors":    _THRESHOLDS["total_errors"]["count"],
    }
    _post_alert_background(payload)


REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "http_status"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request latency",
    ["endpoint"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]
)
MEMORY_BYTES = Gauge(
    "app_memory_usage_bytes",
    "RSS memory of this process"
)
ERROR_TOTAL = Counter(
    "http_errors_total",
    "Total 5xx errors",
    ["endpoint"]
)

@app.before_request
def start_timer():
    request._start = time.time()
    mem = psutil.Process(os.getpid()).memory_info().rss
    MEMORY_BYTES.set(mem)
    log_infra("request_start", method=request.method, path=request.path,
              memory_mb=round(mem / 1024 / 1024, 1))

@app.after_request
def record_metrics(response):
    latency = time.time() - getattr(request, '_start', time.time())
    ep = request.path
    is_error = response.status_code >= 500
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=ep,
        http_status=response.status_code
    ).inc()
    REQUEST_LATENCY.labels(endpoint=ep).observe(latency)
    if is_error:
        ERROR_TOTAL.labels(endpoint=ep).inc()
        log_app("http_error", endpoint=ep, status=response.status_code,
                latency_s=round(latency, 3))
        _increment_threshold("total_errors")
    elif latency > 2.0:
        log_app("slow_request", endpoint=ep, latency_s=round(latency, 3))
    # Count every request (skip internal /metrics and /health probes)
    if ep not in ("/metrics", "/health"):
        _increment_threshold("total_requests")
        # Feed anomaly bucket — sampler thread aggregates every 5s and checks
        _record_request_for_anomaly(latency, is_error)
    # Check memory on every request — fires when RSS > 80% of container limit
    _check_memory_threshold()
    return response

@app.errorhandler(Exception)
def handle_exception(e):
    """Catch ALL unhandled exceptions — record metrics then return 500.
    Without this, Flask re-raises exceptions and after_request never fires,
    so ERROR_TOTAL is never incremented for /crash, /leak, etc.
    """
    ep = request.path
    latency = time.time() - getattr(request, '_start', time.time())
    ERROR_TOTAL.labels(endpoint=ep).inc()
    REQUEST_COUNT.labels(method=request.method, endpoint=ep, http_status=500).inc()
    REQUEST_LATENCY.labels(endpoint=ep).observe(latency)
    MEMORY_BYTES.set(psutil.Process(os.getpid()).memory_info().rss)
    log_app("unhandled_exception_500", endpoint=ep, error=str(e),
            error_type=type(e).__name__, latency_s=round(latency, 3))
    return jsonify({"error": "Internal Server Error", "detail": str(e)}), 500

@app.route("/")
def home():
    return jsonify({"status": "ok", "service": "order-api"})

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200

@app.route("/orders")
def orders():
    log_biz("orders_list_requested", count=3, user_ip=request.remote_addr)
    return jsonify({"orders": [1, 2, 3]})

@app.route("/heavy")
def heavy():
    if not _heavy_enabled:
        log_biz("order_rejected", reason="service_degraded", endpoint="/heavy")
        return jsonify({"error": "disabled"}), 503
    size = int(os.getenv("LOAD_SIZE", "500000"))
    mem_before = psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)
    data = [i for i in range(size)]
    mem_after = psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)
    log_infra("memory_spike", size=size, mem_before_mb=mem_before,
              mem_after_mb=mem_after, delta_mb=mem_after - mem_before)
    log_biz("heavy_order_processed", items=len(data), mem_used_mb=mem_after)
    return jsonify({"count": len(data)})


def _check_admin_token():
    token = request.headers.get("X-Admin-Token") or request.args.get("token")
    return token == _ADMIN_TOKEN


# Global list that never gets cleared — simulates memory leak
_leak_store: list = []


@app.route("/admin/disable-heavy", methods=["POST"])
def disable_heavy():
    global _heavy_enabled
    if not _check_admin_token():
        return jsonify({"error": "unauthorized"}), 401
    _heavy_enabled = False
    return jsonify({"heavy_enabled": False, "message": "/heavy endpoint disabled"})


@app.route("/admin/enable-heavy", methods=["POST"])
def enable_heavy():
    global _heavy_enabled
    if not _check_admin_token():
        return jsonify({"error": "unauthorized"}), 401
    _heavy_enabled = True
    return jsonify({"heavy_enabled": True, "message": "/heavy endpoint enabled"})


@app.route("/admin/status", methods=["GET"])
def admin_status():
    if not _check_admin_token():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"heavy_enabled": _heavy_enabled})

@app.route("/slow")
def slow():
    secs = int(os.getenv("SLEEP_SECONDS", "3"))
    log_app("slow_endpoint_called", sleep_seconds=secs)
    log_biz("order_processing_delayed", delay_seconds=secs,
            impact="user_waiting_for_response")
    time.sleep(secs)
    log_biz("order_processing_completed", delay_seconds=secs)
    return jsonify({"done": True})


@app.route("/crash")
def crash():
    log_app("crash_endpoint_called")
    log_biz("order_processing_failed", reason="unhandled_exception",
            endpoint="/crash", impact="order_lost")
    # Explicitly increment error counter BEFORE raising so it's always recorded
    # even if errorhandler is not triggered in some edge case
    ERROR_TOTAL.labels(endpoint="/crash").inc()
    log_app("unhandled_exception", error="division by zero",
            traceback="ZeroDivisionError in /crash", endpoint="/crash")
    log_biz("order_failed", error_type="ZeroDivisionError",
            orders_affected=1, revenue_lost_usd=100)
    data = {"orders": [{"id": 1, "amount": 100}]}
    _ = data["orders"][0]["amount"] / 0  # raises ZeroDivisionError → caught by errorhandler


@app.route("/leak")
def leak():
    chunk_size = int(os.getenv("LEAK_SIZE", "50000"))
    _leak_store.extend(range(chunk_size))
    mem = psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)
    log_infra("memory_leak_growing", leak_items=len(_leak_store),
              current_mem_mb=mem, added_items=chunk_size)
    if mem > 200:
        log_app("memory_high_warning", mem_mb=mem,
                message="Memory growing without release — possible leak")
        log_biz("service_degradation_risk", mem_mb=mem,
                risk="OOMKill imminent if memory exceeds container limit")
    return jsonify({"leak_items": len(_leak_store), "current_mem_mb": mem})


@app.route("/cpu-spike")
def cpu_spike():
    iterations = int(os.getenv("CPU_ITERATIONS", "2000000"))
    start = time.time()
    log_infra("cpu_spike_start", iterations=iterations)
    result = sum(i * i for i in range(iterations))
    duration = round(time.time() - start, 2)
    log_infra("cpu_spike_done", iterations=iterations, duration_s=duration)
    log_biz("compute_job_completed", duration_s=duration,
            impact="cpu_throttling_may_affect_other_requests")
    return jsonify({"result": result % 1000000})


@app.route("/db-error")
def db_error():
    import socket
    log_app("db_connect_attempt", host="127.0.0.1", port=5432)
    try:
        s = socket.create_connection(("127.0.0.1", 5432), timeout=2)
        s.close()
    except (ConnectionRefusedError, OSError) as e:
        ERROR_TOTAL.labels(endpoint="/db-error").inc()
        log_app("db_connection_failed", error=str(e), host="127.0.0.1", port=5432)
        log_biz("order_failed", reason="database_unavailable",
                error="connection_refused_5432",
                impact="all_db_writes_failing_orders_cannot_be_saved")
        return jsonify({
            "error": "Database connection failed",
            "detail": "connection refused: 127.0.0.1:5432 — pool exhausted or DB unreachable"
        }), 503
    return jsonify({"connected": True})


@app.route("/reset", methods=["POST", "GET"])
def reset():
    """
    Reset ALL simulated issues back to normal state.
    - Clears the memory leak store
    - Re-enables the /heavy endpoint
    - Resets LOAD_SIZE and SLEEP_SECONDS env overrides to defaults
    Call this before retesting any scenario.
    """
    global _leak_store, _heavy_enabled, _COOLDOWN_UNTIL, _MEMORY_ALERT_FIRED
    global _anomaly_cooldown_until, _anomaly_last_fired
    cleared_items = len(_leak_store)
    _leak_store = []
    _heavy_enabled = True

    # Also reset threshold counters so the demo can start fresh
    with _threshold_lock:
        for v in _THRESHOLDS.values():
            v["count"] = 0
            v["fired"] = False
    _COOLDOWN_UNTIL = 0.0
    _MEMORY_ALERT_FIRED = False
    log_infra("thresholds_reset_via_reset_endpoint")

    # Reset anomaly detector windows, bucket, and cooldowns
    with _anomaly_lock:
        for w in _anomaly_windows.values():
            w.clear()
        _anomaly_cooldown_until = 0.0
        _anomaly_last_fired.clear()
    with _bucket_lock:
        _bucket["latencies"] = []
        _bucket["errors"]    = 0
        _bucket["requests"]  = 0
    log_infra("anomaly_detector_reset_via_reset_endpoint")

    import gc
    gc.collect()

    mem_after = psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)

    # Also clear the agent's dedup lock so fresh alerts can be processed
    agent_cleared = False
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(
            f"{_AGENT_WEBHOOK.rstrip('/webhook')}/clear",
            method="POST",
            headers={"X-Token": _WEBHOOK_SECRET, "Content-Length": "0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            agent_cleared = (resp.status == 200)
    except Exception as _e:
        print(f"[WARN] /reset: could not clear agent dedup lock: {_e}")

    return jsonify({
        "reset": True,
        "cleared_leak_items": cleared_items,
        "heavy_enabled": True,
        "memory_after_mb": mem_after,
        "thresholds_reset": True,
        "anomaly_detector_reset": True,
        "agent_dedup_cleared": agent_cleared,
        "message": "All simulated issues cleared, threshold counters and anomaly detector reset. Ready to retest.",
    })


@app.route("/spike")
def spike():
    """
    Simulate a sudden latency spike for anomaly detection testing.
    Adds artificial delay to create a statistical anomaly in the latency window.
    Query params:
      - latency=N   (seconds, default 5) — how long to sleep
      - errors=N    (default 0) — inject N synthetic error records into the window
    """
    spike_latency = float(request.args.get("latency", "5"))
    inject_errors = int(request.args.get("errors", "0"))

    log_app("spike_endpoint_called", latency_s=spike_latency, inject_errors=inject_errors)

    # Inject synthetic error observations into the bucket to force error-rate anomaly
    if inject_errors > 0:
        with _bucket_lock:
            for _ in range(inject_errors):
                _bucket["latencies"].append(0.001)
                _bucket["requests"] += 1
                _bucket["errors"]   += 1
        log_infra("anomaly_test_errors_injected", count=inject_errors)

    time.sleep(spike_latency)
    # Also inject the spike latency directly into bucket so sampler picks it up
    with _bucket_lock:
        for _ in range(5):   # inject 5 samples at spike latency for clear signal
            _bucket["latencies"].append(spike_latency)
            _bucket["requests"] += 1
    log_infra("anomaly_test_spike_injected", latency_s=spike_latency, samples=5)
    return jsonify({
        "spike": True,
        "latency_s": spike_latency,
        "errors_injected": inject_errors,
        "message": f"Slept {spike_latency}s — anomaly detector should fire if baseline exists",
    })


@app.route("/anomaly/status")
def anomaly_status():
    """Show current anomaly detector state — windows, baselines, last fire times."""
    result = {}
    for metric, window in _anomaly_windows.items():
        data = list(window)
        mean, stddev = _stats(window) if len(data) >= 2 else (0.0, 0.0)
        multiplier = 100 if metric == "error_rate" else 1
        last_fire = _anomaly_last_fired.get(metric, 0.0)
        result[metric] = {
            "observations":    len(data),
            "required":        _ANOMALY_MIN_OBS,
            "ready":           len(data) >= _ANOMALY_MIN_OBS,
            "baseline_mean":   round(mean * multiplier, 3),
            "baseline_stddev": round(stddev * multiplier, 3),
            "last_fired_secs_ago": round(time.time() - last_fire, 0) if last_fire else None,
            "cooldown_remaining":  max(0, round(last_fire + _ANOMALY_COOLDOWN - time.time(), 0)) if last_fire else 0,
        }
    with _bucket_lock:
        pending = {
            "latencies_count": len(_bucket["latencies"]),
            "requests": _bucket["requests"],
            "errors":   _bucket["errors"],
        }
    return jsonify({
        "anomaly_detector":  result,
        "current_bucket":    pending,
        "z_threshold":       _ANOMALY_Z,
        "ratio_threshold":   _ANOMALY_RATIO,
        "window_size":       _ANOMALY_WINDOW,
        "min_observations":  _ANOMALY_MIN_OBS,
        "sample_interval_s": 5,
    })


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)




# ── Threshold admin endpoints ──────────────────────────────────────────────────

@app.route("/admin/thresholds")
def get_thresholds():
    """Live view of all threshold counters. Shows progress toward each limit."""
    if not _check_admin_token():
        return jsonify({"error": "unauthorized"}), 401
    with _threshold_lock:
        result = {}
        for k, v in _THRESHOLDS.items():
            pct = round((v["count"] / v["limit"]) * 100, 1) if v["limit"] > 0 else 0
            result[k] = {
                "count":    v["count"],
                "limit":    v["limit"],
                "progress": f"{v['count']}/{v['limit']}",
                "percent":  pct,
                "fired":    v["fired"],
                "severity": v["severity"],
            }
        result["cooldown_remaining_secs"] = max(0, round(_COOLDOWN_UNTIL - time.time(), 1))
    return jsonify(result)


@app.route("/admin/reset-thresholds", methods=["POST"])
def reset_thresholds():
    """Reset all threshold counters and cooldown back to zero."""
    global _COOLDOWN_UNTIL
    if not _check_admin_token():
        return jsonify({"error": "unauthorized"}), 401
    with _threshold_lock:
        for v in _THRESHOLDS.values():
            v["count"] = 0
            v["fired"] = False
    _COOLDOWN_UNTIL = 0.0
    log_infra("thresholds_reset_manual")
    return jsonify({
        "reset": True,
        "thresholds": {k: {"count": 0, "limit": v["limit"]} for k, v in _THRESHOLDS.items()},
    })


# ── Demo Engine ────────────────────────────────────────────────────────────────
# A background thread that hammers the app's own endpoints to create real load.
# Triggered via POST /demo/start; stopped via POST /demo/stop.
#
# Scenario progression per run:
#   Phase 1 – Warm-up   : hits /orders (normal traffic)
#   Phase 2 – Stress    : hits /heavy (memory pressure) + /crash (5xx errors)
#   Phase 3 – Leak      : hits /leak  (gradual memory growth)
# The threshold fires either on total_requests or total_errors depending on
# which limit is reached first.  Both counters are logged in real time.

_demo_lock    = threading.Lock()
_demo_running = False
_demo_thread: threading.Thread | None = None
_demo_engine: "_DemoScenario | None" = None
_demo_stats: dict = {}


class _DemoScenario:
    """Encapsulates one complete load-generation run."""

    SCENARIOS = {
        "crash":  {"endpoints": ["/crash"],          "label": "5xx error burst",    "mix": "errors"},
        "memory": {"endpoints": ["/heavy", "/leak"],  "label": "memory pressure",    "mix": "memory"},
        "slow":   {"endpoints": ["/slow"],            "label": "high latency",       "mix": "latency"},
        "mixed":  {"endpoints": ["/orders", "/heavy", "/crash", "/leak"],
                   "label": "mixed realistic load", "mix": "mixed"},
    }

    def __init__(self, scenario: str, target_url: str, req_delay: float, error_threshold: int, req_threshold: int):
        self.scenario    = self.SCENARIOS.get(scenario, self.SCENARIOS["mixed"])
        self.target_url  = target_url.rstrip("/")
        self.req_delay   = req_delay    # seconds between each request
        self.err_limit   = error_threshold
        self.req_limit   = req_threshold
        self.sent        = 0
        self.errors      = 0
        self.start_time  = time.time()
        self._stop       = False

    def stop(self):
        self._stop = True

    def run(self):
        global _demo_running, _demo_stats
        endpoints  = self.scenario["endpoints"]
        mix        = self.scenario["mix"]
        idx        = 0

        log_infra("demo_started",
                  scenario=self.scenario["label"],
                  target=self.target_url,
                  error_threshold=self.err_limit,
                  req_threshold=self.req_limit)
        log_biz("demo_load_generator_activated",
                scenario=self.scenario["label"],
                estimated_requests_to_threshold=self.req_limit)

        while not self._stop:
            ep = endpoints[idx % len(endpoints)]
            idx += 1
            try:
                url = self.target_url + ep
                req = _urllib_req.Request(url, method="GET")
                with _urllib_req.urlopen(req, timeout=15) as resp:
                    status = resp.status
            except Exception as exc:
                # urllib raises HTTPError for 4xx/5xx status codes
                status = getattr(exc, "code", 0)

            self.sent += 1
            if isinstance(status, int) and status >= 500:
                self.errors += 1

            _demo_stats.update({
                "sent":          self.sent,
                "errors":        self.errors,
                "elapsed_secs":  round(time.time() - self.start_time, 1),
                "last_endpoint": ep,
                "last_status":   status,
                "threshold_reqs": self.req_limit,
                "threshold_errs": self.err_limit,
                "progress_reqs": f"{self.sent}/{self.req_limit}",
                "progress_errs": f"{self.errors}/{self.err_limit}",
            })

            # Friendly progress log every 10 requests
            if self.sent % 10 == 0:
                log_infra("demo_progress",
                          sent=self.sent, errors=self.errors,
                          req_pct=round(self.sent / self.req_limit * 100, 1),
                          err_pct=round(self.errors / self.err_limit * 100, 1))
                log_biz("load_test_in_progress",
                        requests_sent=self.sent,
                        errors=self.errors,
                        phase="stress" if self.sent > self.req_limit * 0.3 else "warmup")

            # Stop if BOTH thresholds already fired (no point continuing)
            with _threshold_lock:
                all_fired = all(v["fired"] for v in _THRESHOLDS.values())
            if all_fired:
                log_infra("demo_thresholds_all_fired", sent=self.sent, errors=self.errors)
                log_biz("demo_complete_alert_fired",
                        total_requests=self.sent,
                        total_errors=self.errors)
                break

            time.sleep(self.req_delay)

        _demo_running = False
        log_infra("demo_stopped", sent=self.sent, errors=self.errors,
                  elapsed=round(time.time() - self.start_time, 1))


@app.route("/demo/start", methods=["POST"])
def demo_start():
    """
    Start the automated load generator.

    Body (all optional):
      {
        "scenario":         "mixed|crash|memory|slow",  // default: mixed
        "delay_secs":       0.3,    // pause between requests (default: 0.3s)
        "target_url":       "...",  // self (default: http://localhost:8080)
        "error_threshold":  10,     // override THRESHOLD_ERRORS
        "req_threshold":    100     // override THRESHOLD_REQUESTS
      }
    """
    global _demo_running, _demo_thread, _demo_stats, _demo_engine

    if not _check_admin_token():
        return jsonify({"error": "unauthorized"}), 401

    with _demo_lock:
        if _demo_running:
            return jsonify({"error": "demo already running", "stats": _demo_stats}), 409

        body          = request.get_json(silent=True) or {}
        scenario      = body.get("scenario", "mixed")
        delay         = float(body.get("delay_secs", 0.3))
        target_url    = body.get("target_url", f"http://localhost:{os.getenv('PORT', '8080')}")
        err_threshold = int(body.get("error_threshold",
                                     _THRESHOLDS["total_errors"]["limit"]))
        req_threshold = int(body.get("req_threshold",
                                     _THRESHOLDS["total_requests"]["limit"]))

        # Reset counters so the demo always starts from zero
        with _threshold_lock:
            for v in _THRESHOLDS.values():
                v["count"] = 0
                v["fired"] = False
        global _COOLDOWN_UNTIL
        _COOLDOWN_UNTIL = 0.0

        _demo_stats   = {"status": "running", "sent": 0, "errors": 0}
        _demo_running = True

        _demo_engine  = _DemoScenario(scenario, target_url, delay, err_threshold, req_threshold)
        _demo_thread  = threading.Thread(target=_demo_engine.run, daemon=True,
                                         name="demo-load-generator")
        _demo_thread.start()

        log_infra("demo_engine_launched",
                  scenario=scenario, delay=delay, target=target_url)
        log_biz("enterprise_demo_started",
                scenario=scenario,
                req_threshold=req_threshold,
                err_threshold=err_threshold,
                message="Load generator active — alert will fire on threshold breach")

    return jsonify({
        "status":          "started",
        "scenario":        scenario,
        "target_url":      target_url,
        "delay_secs":      delay,
        "req_threshold":   req_threshold,
        "error_threshold": err_threshold,
        "message": (
            f"Load generator running. "
            f"Alert fires after {req_threshold} requests OR {err_threshold} errors. "
            f"Watch Telegram for the incident report."
        ),
    })


@app.route("/demo/stop", methods=["POST"])
def demo_stop():
    """Stop the running load generator."""
    global _demo_running, _demo_thread, _demo_engine
    if not _check_admin_token():
        return jsonify({"error": "unauthorized"}), 401

    with _demo_lock:
        if not _demo_running:
            return jsonify({"status": "not_running", "stats": _demo_stats})
        if _demo_engine:
            _demo_engine.stop()   # tells the run loop to exit on next iteration
        _demo_running = False
        if _demo_thread:
            _demo_thread.join(timeout=2)

    log_infra("demo_stopped_via_api")
    return jsonify({"status": "stopped", "final_stats": _demo_stats})


@app.route("/demo/status")
def demo_status():
    """Live status of the load generator and threshold counters."""
    with _threshold_lock:
        thresholds = {
            k: {
                "count":    v["count"],
                "limit":    v["limit"],
                "progress": f"{v['count']}/{v['limit']}",
                "percent":  round(v["count"] / v["limit"] * 100, 1) if v["limit"] else 0,
                "fired":    v["fired"],
            }
            for k, v in _THRESHOLDS.items()
        }
        cooldown_secs = max(0, round(_COOLDOWN_UNTIL - time.time(), 1))

    return jsonify({
        "demo_running":   _demo_running,
        "stats":          _demo_stats,
        "thresholds":     thresholds,
        "cooldown_remaining_secs": cooldown_secs,
        "agent_webhook":  bool(_AGENT_WEBHOOK),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
