project_id   = "testing-ratnesh"
region       = "asia-south1"
service_name = "order-api"

# ── Cloud Run live values — agent patches these via terraform apply ──
cloudrun_memory        = "256Mi"
cloudrun_cpu           = "1"
cloudrun_timeout       = 30
cloudrun_min_instances = 0
cloudrun_max_instances = 3

# ── Threshold Monitor ──────────────────────────────────────────────────
agent_webhook_url      = "https://infra-agent-546580006264.asia-south1.run.app"
webhook_secret         = "infraguard-secret-2026"
threshold_requests     = 100
threshold_errors       = 10
threshold_cooldown_secs = 120
