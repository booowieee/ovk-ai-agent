from sqlalchemy import select
from src.database.connection import async_session_factory
from src.database.models import SystemSettings

class SettingsRepository:
    @staticmethod
    async def get_settings() -> SystemSettings:
        async with async_session_factory() as session:
            result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
            return result.scalar_one_or_none()
            
    @staticmethod
    async def update_settings(**kwargs) -> SystemSettings:
        async with async_session_factory() as session:
            result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
            settings = result.scalar_one_or_none()
            if settings:
                for key, value in kwargs.items():
                    if hasattr(settings, key):
                        setattr(settings, key, value)
                await session.commit()
                await session.refresh(settings)
            return settings
            
    @staticmethod
    async def toggle_enabled() -> bool:
        async with async_session_factory() as session:
            result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
            settings = result.scalar_one_or_none()
            if settings:
                settings.is_enabled = not settings.is_enabled
                await session.commit()
                return settings.is_enabled
            return False

    @staticmethod
    async def is_enabled() -> bool:
        settings = await SettingsRepository.get_settings()
        return settings.is_enabled if settings else False
