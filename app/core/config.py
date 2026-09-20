from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "dev"
    app_base_url: str = "http://localhost:8000"
    frontend_origin: str = "http://localhost:3000"
    public_webhook_base_url: str = ""

    jwt_secret_key: str = "dev-only-secret"
    fernet_key: str = ""

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "overwatch"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""

    github_client_id: str = ""
    github_client_secret: str = ""

    jira_client_id: str = ""
    jira_client_secret: str = ""

    gemini_api_key: str = ""

    repos_dir: str = "./repos"

    ticket_sync_interval_seconds: int = 300

    # Every git invocation is bounded; a hung clone must not pin a worker.
    git_command_timeout_seconds: int = 300
    # A parse lock older than this is assumed to belong to a crashed run and
    # can be reclaimed, so a restart mid-parse cannot wedge a repository.
    parse_lock_stale_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    return Settings()
