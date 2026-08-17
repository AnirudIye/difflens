from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://difflens:difflens@localhost:55432/difflens"
    redis_url: str = "redis://localhost:6379/0"
    frontend_origin: str = "http://localhost:3000"

    github_client_id: str = ""
    github_client_secret: str = ""
    session_secret: str = "dev-session-secret-change-me"
    token_encryption_key: str = ""


settings = Settings()
