"""Configuration centralisée, chargée depuis .env."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    llm_provider: str = "groq"

    ollama_model: str = "qwen2.5:1.5b-instruct-q4_K_M"
    ollama_base_url: str = "http://localhost:11434"

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"


settings = Settings()
