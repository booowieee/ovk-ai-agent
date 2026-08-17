from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Настройки приложения, загружаемые из .env файла."""
    
    # Telegram-бот управления
    BOT_TOKEN: str
    ADMIN_TELEGRAM_ID: int
    
    # Интеграция Google Gemini
    GEMINI_API_KEY: str
    GEMINI_MODEL: str = "gemini-flash-lite-latest"
    
    # Настройки OpenVK
    OVK_INSTANCE_URL: str = "https://openvk.org"
    OVK_ACCESS_TOKEN: str = ""
    OVK_USER_ID: int = 0
    OVK_STATS_POST_ID: str = ""
    
    # Настройки генерации изображений
    HUGGINGFACE_API_KEY: str = ""
    HUGGINGFACE_SPACE_ID: str = ""
    POLLINATIONS_API_KEY: str = ""
    
    # База данных
    DATABASE_URL: str = "postgresql+asyncpg://ovk_agent:ovk_agent_pass@db:5432/ovk_agent"
    
    # Настройки Redis
    REDIS_URL: str = "redis://redis:6379/0"
    
    # Интервал опроса
    POLL_INTERVAL: int = 10
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
