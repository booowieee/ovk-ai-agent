from sqlalchemy import select, update, delete
from src.database.connection import async_session_factory
from src.database.models import UserActivity, SystemStats
from datetime import datetime

class StatsRepository:
    @staticmethod
    async def increment_user_activity(vk_id: int, first_name: str | None, last_name: str | None, is_image: bool) -> None:
        async with async_session_factory() as session:
            result = await session.execute(select(UserActivity).where(UserActivity.vk_id == vk_id))
            user = result.scalar_one_or_none()
            if not user:
                user = UserActivity(
                    vk_id=vk_id,
                    first_name=first_name,
                    last_name=last_name,
                    text_requests_count=1 if not is_image else 0,
                    image_requests_count=1 if is_image else 0,
                    last_active_at=datetime.utcnow()
                )
                session.add(user)
            else:
                user.first_name = first_name
                user.last_name = last_name
                user.last_active_at = datetime.utcnow()
                if is_image:
                    user.image_requests_count += 1
                else:
                    user.text_requests_count += 1
            await session.commit()

    @staticmethod
    async def increment_global_stats(text: int = 0, images: int = 0, flux: int = 0, fallback: int = 0, likes: int = 0) -> None:
        async with async_session_factory() as session:
            result = await session.execute(select(SystemStats).where(SystemStats.id == 1))
            stats = result.scalar_one_or_none()
            if stats:
                stats.total_text_requests += text
                stats.total_image_requests += images
                stats.flux_success_count += flux
                stats.fallback_success_count += fallback
                stats.total_likes_count += likes
                await session.commit()

    @staticmethod
    async def get_stats() -> dict:
        async with async_session_factory() as session:
            # 1. Global stats
            result = await session.execute(select(SystemStats).where(SystemStats.id == 1))
            gs = result.scalar_one_or_none()
            
            global_data = {
                "total_text_requests": gs.total_text_requests if gs else 0,
                "total_image_requests": gs.total_image_requests if gs else 0,
                "flux_success_count": gs.flux_success_count if gs else 0,
                "fallback_success_count": gs.fallback_success_count if gs else 0,
                "total_likes_count": gs.total_likes_count if gs else 0,
            }
            
            # 2. Top 5 text users
            res_text = await session.execute(
                select(UserActivity)
                .where(UserActivity.text_requests_count > 0)
                .order_by(UserActivity.text_requests_count.desc())
                .limit(5)
            )
            top_text_users = [
                {
                    "vk_id": u.vk_id,
                    "first_name": u.first_name or "Без имени",
                    "last_name": u.last_name or "",
                    "count": u.text_requests_count
                }
                for u in res_text.scalars().all()
            ]
            
            # 3. Top 5 image users
            res_image = await session.execute(
                select(UserActivity)
                .where(UserActivity.image_requests_count > 0)
                .order_by(UserActivity.image_requests_count.desc())
                .limit(5)
            )
            top_image_users = [
                {
                    "vk_id": u.vk_id,
                    "first_name": u.first_name or "Без имени",
                    "last_name": u.last_name or "",
                    "count": u.image_requests_count
                }
                for u in res_image.scalars().all()
            ]
            
            return {
                "global": global_data,
                "top_text": top_text_users,
                "top_image": top_image_users
            }

    @staticmethod
    async def clear_stats() -> None:
        async with async_session_factory() as session:
            await session.execute(delete(UserActivity))
            await session.execute(
                update(SystemStats)
                .where(SystemStats.id == 1)
                .values(
                    total_text_requests=0,
                    total_image_requests=0,
                    flux_success_count=0,
                    fallback_success_count=0,
                    total_likes_count=0
                )
            )
            await session.commit()
