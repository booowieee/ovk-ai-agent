from redis.asyncio import Redis
from src.openvk.client import OpenVKClient
from src.utils.logger import logger
from typing import Optional


class OpenVKResponder:
    def __init__(self, client: OpenVKClient, redis: Redis):
        self.client = client
        self.redis = redis

    async def is_already_processed(self, mention_key: str) -> bool:
        """
        Проверяет, обрабатывается ли упоминание.
        Ставит processing-lock с TTL 1 час.
        """
        key = f"ovk:lock:{mention_key}"
        is_new = await self.redis.set(key, "processing", nx=True, ex=3600)
        return not bool(is_new)

    async def release_lock(self, mention_key: str):
        """Снимает временный lock в случае ошибки обработки/отправки."""
        key = f"ovk:lock:{mention_key}"
        await self.redis.delete(key)
        logger.info(f"[Responder] Lock released for key: {mention_key}")

    async def mark_completed(self, mention_key: str):
        """Помечает упоминание как успешно обработанное. TTL 7 дней."""
        key = f"ovk:lock:{mention_key}"
        await self.redis.set(key, "completed", ex=604800)

    async def reply_to_comment(self, owner_id: int, post_id: int, comment_id: int,
                               message: str, guid: Optional[int] = None) -> Optional[int]:
        try:
            return await self.client.create_comment(
                owner_id, post_id, message,
                reply_to_comment=comment_id, guid=guid
            )
        except Exception as e:
            logger.error(f"Error replying to comment {comment_id}: {e}")
            return None

    async def reply_to_post(self, owner_id: int, post_id: int,
                            message: str, guid: Optional[int] = None) -> Optional[int]:
        try:
            return await self.client.create_comment(owner_id, post_id, message, guid=guid)
        except Exception as e:
            logger.error(f"Error replying to post {post_id}: {e}")
            return None
