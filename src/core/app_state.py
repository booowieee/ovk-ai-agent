import asyncio
import httpx
from redis.asyncio import Redis
from typing import Optional


class AppState:
    """Централизованное состояние приложения и пул соединений."""
    
    def __init__(self):
        self.is_running: bool = True
        self.http_client: Optional[httpx.AsyncClient] = None
        self.redis: Optional[Redis] = None
        self.gemini_semaphore: asyncio.Semaphore = asyncio.Semaphore(3)
        self.poll_interval: int = 10
    
    async def init_connections(self, redis_url: str):
        """Инициализация HTTP клиента и Redis."""
        limits = httpx.Limits(
            max_keepalive_connections=5,
            max_connections=10,
            keepalive_expiry=30.0
        )
        self.http_client = httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
        )
        self.redis = Redis.from_url(redis_url, decode_responses=True)
    
    async def close(self):
        """Закрытие всех соединений."""
        if self.http_client:
            await self.http_client.aclose()
        if self.redis:
            await self.redis.close()
