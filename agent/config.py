from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    GEMINI_API_KEY: str
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    GCP_PROJECT_ID: str
    GCP_REGION: str = "asia-south1"
    CLOUD_RUN_SERVICE: str = "order-api"
    CLOUD_RUN_SERVICE_URL: str
    WEBHOOK_SECRET: str
    BASE_URL: str
    TERRAFORM_DIR: str = "/app/terraform"

    # GitHub — required for code-level fixes pushed via Actions workflow
    # Create a PAT at: https://github.com/settings/tokens
    # Token scopes needed: repo (full), workflow
    GITHUB_OWNER: str = ""
    GITHUB_REPO: str = ""
    GITHUB_TOKEN: str = ""
    GITHUB_BRANCH: str = "main"

    # Loki — local only (docker-compose); leave empty for Cloud Run
    LOKI_URL: str = ""

    # Threshold monitor — infra-app uses these same values via env vars.
    # Documented here for reference; the agent reads them through the alert payload.
    # THRESHOLD_REQUESTS=100   (fire after N non-health/metrics requests)
    # THRESHOLD_ERRORS=10      (fire after N 5xx responses)
    # THRESHOLD_COOLDOWN_SECS=120  (min seconds between consecutive alerts)

    class Config:
        env_file = ".env"


settings = Settings()
