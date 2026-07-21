from app.core.config import Settings


def test_defaults_load_without_env_file():
    settings = Settings(_env_file=None)
    assert settings.app_env == "dev"
    assert settings.mongo_db_name == "overwatch"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("MONGO_DB_NAME", "overwatch_test")
    settings = Settings(_env_file=None)
    assert settings.app_env == "test"
    assert settings.mongo_db_name == "overwatch_test"
