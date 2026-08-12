from redis.asyncio import Redis
from src.openvk.client import OpenVKClient
from src.utils.logger import logger
from typing import Optional

class OpenVKResponder:
    def __init__(self, client: OpenVKClient, redis: Redis):
        self.client = client
        self.redis = redis

    async def is_already_processed(self, mention_id: str) -> bool:
        key = f"ovk:mention:{mention_id}"
        is_new = await self.redis.set(key, "processing", nx=True, ex=300)
        return not bool(is_new)

    async def mark_completed(self, mention_id: str):
        key = f"ovk:mention:{mention_id}"
        await self.redis.set(key, "completed", ex=604800)

    async def reply_to_comment(self, owner_id: int, post_id: int, comment_id: int, message: str) -> Optional[int]:
        try:
            return await self.client.create_comment(owner_id, post_id, message, reply_to_comment=comment_id)
        except Exception as e:
            logger.error(f"Error replying to comment {comment_id}: {e}")
            return None

    async def reply_to_post(self, owner_id: int, post_id: int, message: str) -> Optional[int]:
        try:
            return await self.client.create_comment(owner_id, post_id, message)
        except Exception as e:
            logger.error(f"Error replying to post {post_id}: {e}")
            return None
