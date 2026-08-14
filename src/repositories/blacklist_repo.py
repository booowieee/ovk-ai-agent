from sqlalchemy import select, delete
from src.database.connection import async_session_factory
from src.database.models import BlacklistedUser, AutoBlockedUser

class BlacklistRepository:
    @staticmethod
    async def is_blacklisted(vk_id: int) -> bool:
        async with async_session_factory() as session:
            result = await session.execute(select(BlacklistedUser).where(BlacklistedUser.vk_id == vk_id))
            return result.scalar_one_or_none() is not None

    @staticmethod
    async def is_auto_blocked(vk_id: int) -> bool:
        async with async_session_factory() as session:
            result = await session.execute(select(AutoBlockedUser).where(AutoBlockedUser.vk_id == vk_id))
            return result.scalar_one_or_none() is not None

    @staticmethod
    async def add_to_blacklist(vk_id: int, reason: str = None) -> bool:
        from src.config import settings
        if vk_id == settings.OVK_USER_ID:
            return False
        async with async_session_factory() as session:
            # Check if already exists
            result = await session.execute(select(BlacklistedUser).where(BlacklistedUser.vk_id == vk_id))
            exists = result.scalar_one_or_none()
            if not exists:
                new_user = BlacklistedUser(vk_id=vk_id, reason=reason)
                session.add(new_user)
                await session.commit()
                return True
            else:
                if exists.reason != reason:
                    exists.reason = reason
                    await session.commit()
                return False

    @staticmethod
    async def remove_from_blacklist(vk_id: int) -> bool:
        async with async_session_factory() as session:
            result = await session.execute(delete(BlacklistedUser).where(BlacklistedUser.vk_id == vk_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    async def add_to_auto_blocked(vk_id: int) -> bool:
        from src.config import settings
        if vk_id == settings.OVK_USER_ID:
            return False
        async with async_session_factory() as session:
            result = await session.execute(select(AutoBlockedUser).where(AutoBlockedUser.vk_id == vk_id))
            exists = result.scalar_one_or_none()
            if not exists:
                new_user = AutoBlockedUser(vk_id=vk_id)
                session.add(new_user)
                await session.commit()
                return True
            return False

    @staticmethod
    async def remove_from_auto_blocked(vk_id: int) -> bool:
        async with async_session_factory() as session:
            result = await session.execute(delete(AutoBlockedUser).where(AutoBlockedUser.vk_id == vk_id))
            await session.commit()
            return result.rowcount > 0

    @staticmethod
    async def get_blacklist() -> list[dict]:
        async with async_session_factory() as session:
            result = await session.execute(select(BlacklistedUser))
            users = result.scalars().all()
            return [{"vk_id": u.vk_id, "reason": u.reason} for u in users]

    @staticmethod
    async def get_auto_blocked() -> list[int]:
        async with async_session_factory() as session:
            result = await session.execute(select(AutoBlockedUser))
            users = result.scalars().all()
            return [u.vk_id for u in users]
