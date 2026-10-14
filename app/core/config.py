from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    app_base_url: str = "http://localhost:8000"
    frontend_origin: str = "http://localhost:3000"

    jwt_secret_key: str = "dev-only-secret"
    fernet_key: str = ""

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "overwatch"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    # GitHub App (sign-in via user authorization, repo access via installations)
    github_app_client_id: str = ""
    github_app_client_secret: str = ""
    github_app_slug: str = ""
    github_app_private_key_path: str = ""
    github_webhook_secret: str = ""

    jira_client_id: str = ""
    jira_client_secret: str = ""

    gemini_api_key: str = ""
    # AI chat limits; keep the global budget below the Gemini project's daily quota
    chat_user_hourly_limit: int = 30
    chat_workspace_daily_limit: int = 200
    chat_daily_budget: int = 500

    repos_dir: str = "./repos"

    ticket_sync_interval_seconds: int = 300

    # Every git invocation is bounded; a hung clone must not pin a worker.
    git_command_timeout_seconds: int = 300
    # A parse lock older than this is assumed to belong to a crashed run and
    # can be reclaimed, so a restart mid-parse cannot wedge a repository.
    parse_lock_stale_minutes: int = 15

    # Where uploaded files (doc images) live: "local" (a folder, for dev) or "s3".
    # S3 credentials come from the standard AWS sources (env vars, instance role), never from here.
    storage_backend: str = "local"
    uploads_dir: str = "./uploads"
    s3_bucket: str = ""
    s3_region: str = ""
    # only for S3-compatible stores (MinIO, R2); empty for AWS
    s3_endpoint_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
