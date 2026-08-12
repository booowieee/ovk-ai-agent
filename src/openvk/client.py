from typing import Optional
from src.core.app_state import AppState
from src.core.rate_limiter import RateLimiter
from src.core.circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from src.utils.logger import logger

class OpenVKClient:
    def __init__(self, state: AppState, instance_url: str, token: str, user_id: int):
        self.state = state
        self.instance_url = instance_url.rstrip("/")
        self.token = token
        self.user_id = user_id
        self._rate_limiter = RateLimiter(3.0)
        self._breaker = CircuitBreaker("openvk")

    async def call_method(self, method: str, params: dict = None) -> dict:
        if params is None:
            params = {}
        params['access_token'] = self.token
        params['v'] = '5.81'
        
        await self._rate_limiter.acquire()
        
        url = f"{self.instance_url}/method/{method}"
        
        if self._breaker.is_open():
            raise CircuitBreakerOpen(f"Circuit breaker for {self._breaker.name} is open")
            
        try:
            response = await self.state.http_client.post(url, data=params)
            response.raise_for_status()
            data = response.json()
            if 'error' in data:
                logger.error(f"OpenVK API error: {data['error']}")
                raise Exception(f"OpenVK API error: {data['error']}")
            
            self._breaker.record_success()
            return data
        except Exception as e:
            self._breaker.record_failure()
            logger.error(f"Failed to call OpenVK method {method}: {e}")
            raise

    async def get_notifications(self, count=50, start_time=0) -> list:
        params = {'count': count, 'start_time': start_time}
        data = await self.call_method("notifications.get", params)
        return data.get('response', {}).get('items', [])

    async def get_wall_posts(self, owner_id: int, count=20, offset=0, filter='all') -> list:
        params = {'owner_id': owner_id, 'count': count, 'offset': offset, 'filter': filter}
        data = await self.call_method("wall.get", params)
        return data.get('response', {}).get('items', [])

    async def get_comments(self, owner_id: int, post_id: int, count=100, offset=0) -> list:
        params = {'owner_id': owner_id, 'post_id': post_id, 'count': count, 'offset': offset}
        data = await self.call_method("wall.getComments", params)
        return data.get('response', {}).get('items', [])

    async def get_comment_by_id(self, owner_id: int, comment_id: int) -> dict | None:
        params = {'owner_id': owner_id, 'comment_id': comment_id}
        data = await self.call_method("wall.getComment", params)
        items = data.get('response', {}).get('items', [])
        return items[0] if items else None

    async def create_comment(self, owner_id: int, post_id: int, message: str, reply_to_comment: int = None) -> int:
        params = {'owner_id': owner_id, 'post_id': post_id, 'message': message}
        if reply_to_comment is not None:
            params['reply_to_comment'] = reply_to_comment
        data = await self.call_method("wall.createComment", params)
        return data.get('response', {}).get('comment_id', 0)

    async def get_user_info(self) -> dict:
        params = {'user_ids': self.user_id}
        data = await self.call_method("users.get", params)
        items = data.get('response', [])
        return items[0] if items else {}
