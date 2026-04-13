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

    # Bitbucket — required for code-level fixes pushed via pipeline
    # Create an API token at: https://id.atlassian.com/manage-profile/security/api-tokens
    # Token scopes needed: Repositories (read/write), Pipelines (read/write)
    BITBUCKET_WORKSPACE: str = ""
    BITBUCKET_REPO_SLUG: str = ""
    BITBUCKET_API_TOKEN: str = ""
    BITBUCKET_BRANCH: str = "main"

    class Config:
        env_file = ".env"


settings = Settings()
