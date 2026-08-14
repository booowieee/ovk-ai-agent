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

    async def _check_and_register_auto_block(self, owner_id: int, e: Exception):
        if owner_id and owner_id > 0 and owner_id != self.client.user_id:
            import httpx
            if isinstance(e, httpx.HTTPStatusError):
                if e.response.status_code in (400, 401, 403, 404):
                    from src.repositories.blacklist_repo import BlacklistRepository
                    await BlacklistRepository.add_to_auto_blocked(owner_id)
                    logger.warning(f"[Responder] Detected block from user {owner_id} (HTTP {e.response.status_code}). Added to auto-blocked.")

    async def reply_to_comment(self, owner_id: int, post_id: int, comment_id: int,
                               message: str, guid: Optional[int] = None,
                               attachments: Optional[str] = None) -> Optional[int]:
        try:
            return await self.client.create_comment(
                owner_id, post_id, message,
                reply_to_comment=comment_id, guid=guid,
                attachments=attachments
            )
        except Exception as e:
            logger.error(f"Error replying to comment {comment_id}: {e}")
            await self._check_and_register_auto_block(owner_id, e)
            raise

    async def reply_to_post(self, owner_id: int, post_id: int,
                            message: str, guid: Optional[int] = None,
                            attachments: Optional[str] = None) -> Optional[int]:
        try:
            return await self.client.create_comment(
                owner_id, post_id, message, guid=guid,
                attachments=attachments
            )
        except Exception as e:
            logger.error(f"Error replying to post {post_id}: {e}")
            await self._check_and_register_auto_block(owner_id, e)
            raise

    async def add_like(self, type: str, owner_id: int, item_id: int):
        """Ставит лайк на пост или комментарий."""
        try:
            logger.info(f"[Responder] Adding like to {type} {owner_id}_{item_id}...")
            await self.client.call_method("likes.add", {
                "type": type,
                "owner_id": owner_id,
                "item_id": item_id
            })
        except Exception as e:
            logger.error(f"Failed to add like to {type} {owner_id}_{item_id}: {e}")
            await self._check_and_register_auto_block(owner_id, e)
