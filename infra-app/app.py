from flask import Flask, jsonify, request, Response
import os, time, psutil, logging, json as _json
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

app = Flask(__name__)

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
    latency = time.time() - request._start
    ep = request.path
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=ep,
        http_status=response.status_code
    ).inc()
    REQUEST_LATENCY.labels(endpoint=ep).observe(latency)
    if response.status_code >= 500:
        ERROR_TOTAL.labels(endpoint=ep).inc()
        log_app("http_error", endpoint=ep, status=response.status_code,
                latency_s=round(latency, 3))
    elif latency > 2.0:
        log_app("slow_request", endpoint=ep, latency_s=round(latency, 3))
    return response

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
    try:
        data = {"orders": [{"id": 1, "amount": 100}]}
        _ = data["orders"][0]["amount"] / 0
    except ZeroDivisionError as e:
        log_app("unhandled_exception", error=str(e), traceback="ZeroDivisionError in /crash",
                endpoint="/crash")
        log_biz("order_failed", error_type="ZeroDivisionError",
                orders_affected=1, revenue_lost_usd=100)
        raise


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
    global _leak_store, _heavy_enabled
    cleared_items = len(_leak_store)
    _leak_store = []
    _heavy_enabled = True

    import gc
    gc.collect()

    mem_after = psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)

    return jsonify({
        "reset": True,
        "cleared_leak_items": cleared_items,
        "heavy_enabled": True,
        "memory_after_mb": mem_after,
        "message": "All simulated issues cleared. Ready to retest.",
    })


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
