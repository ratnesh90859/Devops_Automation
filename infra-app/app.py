from flask import Flask, jsonify, request, Response
import os, time, psutil
from prometheus_client import (
    Counter, Histogram, Gauge,
    generate_latest, CONTENT_TYPE_LATEST
)

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
    MEMORY_BYTES.set(psutil.Process(os.getpid()).memory_info().rss)

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
    return response

@app.route("/")
def home():
    return jsonify({"status": "ok", "service": "order-api"})

@app.route("/health")
def health():
    return jsonify({"status": "healthy"}), 200

@app.route("/orders")
def orders():
    return jsonify({"orders": [1, 2, 3]})

@app.route("/heavy")
def heavy():
    if not _heavy_enabled:
        return jsonify({"error": "disabled"}), 503
    # simulates memory spike -> triggers OOM alert
    # cost-optimised default: 500000 (4x smaller than original 2000000)
    size = int(os.getenv("LOAD_SIZE", "500000"))
    data = [i for i in range(size)]
    return jsonify({"count": len(data)})


def _check_admin_token():
    token = request.headers.get("X-Admin-Token") or request.args.get("token")
    return token == _ADMIN_TOKEN


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
    # simulates slow endpoint -> triggers latency alert
    # cost-optimised default: 3s (down from 8s)
    secs = int(os.getenv("SLEEP_SECONDS", "3"))
    time.sleep(secs)
    return jsonify({"done": True})


@app.route("/crash")
def crash():
    """Simulates unhandled exception → 500 error rate spike → triggers error_rate alert."""
    # intentionally raises to produce a Python traceback in logs
    data = {"orders": [{"id": 1, "amount": 100}]}
    # divide by zero to create a real traceback
    _ = data["orders"][0]["amount"] / 0
    return jsonify({"done": True})


# Global list that never gets cleared — simulates memory leak
_leak_store: list = []

@app.route("/leak")
def leak():
    """Simulates gradual memory leak — appends to a global list that is never freed."""
    chunk_size = int(os.getenv("LEAK_SIZE", "50000"))
    _leak_store.extend(range(chunk_size))
    mem = psutil.Process(os.getpid()).memory_info().rss // (1024 * 1024)
    return jsonify({"leak_items": len(_leak_store), "current_mem_mb": mem})


@app.route("/cpu-spike")
def cpu_spike():
    """Simulates CPU-bound work — heavy computation → CPU throttling alert."""
    iterations = int(os.getenv("CPU_ITERATIONS", "2000000"))
    result = sum(i * i for i in range(iterations))
    return jsonify({"result": result % 1000000})


@app.route("/db-error")
def db_error():
    """Simulates database connection failure → 503 → DB exhaustion alert."""
    import socket
    try:
        # Try connecting to a non-existent DB port — always fails
        s = socket.create_connection(("127.0.0.1", 5432), timeout=2)
        s.close()
    except (ConnectionRefusedError, OSError):
        ERROR_TOTAL.labels(endpoint="/db-error").inc()
        return jsonify({
            "error": "Database connection failed",
            "detail": "connection refused: 127.0.0.1:5432 — pool exhausted or DB unreachable"
        }), 503
    return jsonify({"connected": True})


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
