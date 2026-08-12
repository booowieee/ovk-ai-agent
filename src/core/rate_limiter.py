import asyncio
import time


class RateLimiter:
    """Ограничитель частоты запросов (Token Bucket)."""
    
    def __init__(self, max_per_second: float = 3.0):
        self._interval = 1.0 / max_per_second
        self._last_call = 0.0
        self._lock = asyncio.Lock()
    
    async def acquire(self):
        """Ожидает до момента, когда можно выполнить следующий запрос."""
        async with self._lock:
            now = time.monotonic()
            wait = self._last_call + self._interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
