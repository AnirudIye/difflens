from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql://difflens:difflens@localhost:5432/difflens"
    redis_url: str = "redis://localhost:6379/0"
    frontend_origin: str = "http://localhost:3000"


settings = Settings()
