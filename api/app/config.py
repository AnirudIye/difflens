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

    # The public demo: a seeded pull request anyone can review with no
    # account. Off by default, so a deployment opts in rather than out.
    demo_mode: bool = False
    # Per-IP, on the demo rerun only. The live-review index already caps the
    # demo at one job at a time, so this is fair sharing rather than the
    # safety floor; see docs/THREAT_MODEL.md.
    demo_rate_limit: int = 5
    demo_rate_limit_window_s: int = 3600

    # The public contact form. Messages always land in Postgres; forwarding
    # by email happens only when both Resend values are set, and a failure to
    # forward never fails the request. Rate limit is per-IP; 0 disables it.
    contact_rate_limit: int = 5
    contact_rate_limit_window_s: int = 3600
    resend_api_key: str = ""
    contact_forward_to: str = ""

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
