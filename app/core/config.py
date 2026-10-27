from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.constants import PRODUCT_NAMES, Product


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    # the brand users see (emails, sender name); internal names such as cookies and the database never change
    product_name: Product = Product.OVERWATCH
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

    # the AI chat's model: any OpenAI-compatible chat API (Groq, OpenRouter, DeepSeek...)
    model_api_key: str = ""
    model_base_url: str = "https://openrouter.ai/api/v1"
    model_name: str = "deepseek/deepseek-v4.1-flash"
    # sent as `reasoning_effort` when set; empty for providers that reject it
    model_reasoning_effort: str = "low"
    # AI chat limits; keep the global budget below the provider's daily quota
    chat_user_hourly_limit: int = 30
    chat_workspace_daily_limit: int = 200
    chat_daily_budget: int = 500
    # "Suggest decisions" runs a workspace may make a day; they also count against chat_daily_budget
    decision_suggest_workspace_daily_limit: int = 20
    # sources a workspace's decision checks may read a day (each one model call), and how often they run
    decision_check_workspace_daily_limit: int = 20
    decision_check_interval_seconds: int = 1800
    # lookups (graph search, reading code) one answer may make before it must answer; 0 turns them off
    chat_max_lookups: int = 20
    # OpenRouter provider routing, empty to send nothing: sort by "latency", "throughput" or "price",
    # or a comma-separated order of provider slugs (an order turns off OpenRouter's cache-warm routing)
    model_provider_sort: str = ""
    model_provider_order: str = ""
    # send each conversation's id as `session_id`, so its calls stay on one provider and its cache
    model_session_routing: bool = False

    repos_dir: str = "./repos"

    doc_sync_interval_seconds: int = 30
    ticket_sync_interval_seconds: int = 120
    # a target sync running longer than this was lost (a restart mid-sync) and may be taken over
    sync_lock_stale_minutes: int = 15
    # a mirrored page's edits go to its source once nobody has typed for this long, as one version
    doc_push_idle_seconds: int = 20
    doc_push_interval_seconds: int = 10
    # archived docs are deleted for good this long after archiving
    doc_archive_retention_days: int = 30

    # Every git invocation is bounded; a hung clone must not pin a worker.
    git_command_timeout_seconds: int = 300
    # A parse lock older than this is assumed to belong to a crashed run and
    # can be reclaimed, so a restart mid-parse cannot wedge a repository.
    parse_lock_stale_minutes: int = 15

    # Where uploaded files (doc images) live: "local" (a folder, for dev), "s3" (AWS) or "r2" (Cloudflare).
    # Bucket credentials come from the standard AWS sources (env vars, instance role), never from here; an R2
    # API token supplies the same AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY pair.
    storage_backend: str = "local"
    uploads_dir: str = "./uploads"
    # the S3_* names are read too, until deploys have moved to STORAGE_*
    storage_bucket: str = Field("", validation_alias=AliasChoices("STORAGE_BUCKET", "S3_BUCKET"))
    storage_region: str = Field("", validation_alias=AliasChoices("STORAGE_REGION", "S3_REGION"))
    # required for R2 (https://<account_id>.r2.cloudflarestorage.com) and other S3-compatible stores; empty for AWS
    storage_endpoint_url: str = Field("", validation_alias=AliasChoices("STORAGE_ENDPOINT_URL", "S3_ENDPOINT_URL"))

    # How emails go out: "console" (logged, for dev and tests) or "smtp" (Gmail: an App Password, port 587 with STARTTLS)
    email_backend: str = "console"
    email_from: str = "noreply@overwatch.local"
    # empty sends as the product name
    email_from_name: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    # new passwords are checked against Have I Been Pwned's breach corpus (k-anonymity: only a hash prefix leaves)
    hibp_enabled: bool = True
    # invite emails a workspace or one admin may send a day; Gmail caps an account at about 500 recipients a day
    invite_email_daily_limit_workspace: int = 50
    invite_email_daily_limit_user: int = 100

    @property
    def product(self) -> str:
        """The product name as users see it."""
        return PRODUCT_NAMES[self.product_name]


@lru_cache
def get_settings() -> Settings:
    return Settings()
