from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql+psycopg://difflens:difflens@localhost:55432/difflens"
    redis_url: str = "redis://localhost:6379/0"
    frontend_origin: str = "http://localhost:3000"

    # Starting a review is the only call that spends GitHub quota, worker
    # time, and AI tokens. 0 disables the limit.
    review_rate_limit: int = 20
    review_rate_limit_window_s: int = 3600

    github_client_id: str = ""
    github_client_secret: str = ""
    session_secret: str = "dev-session-secret-change-me"
    token_encryption_key: str = ""

    # off | mock | anthropic | gemini | openai; mock runs the full pipeline at zero cost
    ai_provider: str = "mock"
    # Empty means the provider's default model
    # (claude-opus-5 / gemini-3.6-flash / gpt-5.6-terra)
    ai_model: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""


settings = Settings()
