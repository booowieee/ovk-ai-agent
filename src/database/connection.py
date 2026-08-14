from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select
from src.config import settings
from src.database.models import Base, SystemSettings, BlacklistedUser, AutoBlockedUser
from src.utils.logger import logger

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async with async_session_factory() as session:
        # Удаляем ID самого бота из черных списков (если он туда попал ранее)
        from sqlalchemy import delete
        if settings.OVK_USER_ID:
            await session.execute(delete(BlacklistedUser).where(BlacklistedUser.vk_id == settings.OVK_USER_ID))
            await session.execute(delete(AutoBlockedUser).where(AutoBlockedUser.vk_id == settings.OVK_USER_ID))
            await session.commit()

        result = await session.execute(select(SystemSettings).where(SystemSettings.id == 1))
        settings_row = result.scalar_one_or_none()
        if not settings_row:
            new_settings = SystemSettings(
                id=1,
                is_enabled=True,
                openvk_instance_url=settings.OVK_INSTANCE_URL,
                openvk_token=settings.OVK_ACCESS_TOKEN,
                openvk_user_id=settings.OVK_USER_ID,
                poll_interval=settings.POLL_INTERVAL
            )
            session.add(new_settings)
            await session.commit()
            logger.info("Created default SystemSettings row.")
