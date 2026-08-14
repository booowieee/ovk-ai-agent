from typing import Optional
from src.core.app_state import AppState
from src.core.rate_limiter import RateLimiter
from src.core.circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from src.utils.logger import logger

class OpenVKClient:
    def __init__(self, state: AppState, instance_url: str = "", token: str = "", user_id: int = 0):
        self.state = state
        self.instance_url = instance_url.rstrip("/") if instance_url else ""
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
        
        self._breaker.check()
            
        try:
            response = await self.state.http_client.post(url, data=params)
            if response.status_code != 200:
                try:
                    err_json = response.json()
                    logger.error(f"OpenVK error body (HTTP {response.status_code}): {err_json}")
                except Exception:
                    logger.error(f"OpenVK error text (HTTP {response.status_code}): {response.text[:500]}")
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

    async def get_comments_raw(self, owner_id: int, post_id: int, count=100, offset=0) -> dict:
        params = {'owner_id': owner_id, 'post_id': post_id, 'count': count, 'offset': offset}
        return await self.call_method("wall.getComments", params)

    async def get_comment_by_id(self, owner_id: int, comment_id: int) -> dict | None:
        params = {'owner_id': owner_id, 'comment_id': comment_id}
        data = await self.call_method("wall.getComment", params)
        items = data.get('response', {}).get('items', [])
        return items[0] if items else None

    async def create_comment(self, owner_id: int, post_id: int, message: str,
                             reply_to_comment: int = None, guid: int = None,
                             attachments: str = None) -> int:
        logger.info(f"[Client:Comment] create_comment: owner_id={owner_id}, post_id={post_id}, reply_to_comment={reply_to_comment}")
        params = {'owner_id': owner_id, 'post_id': post_id, 'message': message}
        if reply_to_comment is not None:
            params['reply_to_comment'] = reply_to_comment
        if guid is not None:
            params['guid'] = guid
        if attachments is not None:
            params['attachments'] = attachments
        data = await self.call_method("wall.createComment", params)
        return data.get('response', {}).get('comment_id', 0)

    async def get_user_info(self) -> dict:
        params = {'user_ids': self.user_id, 'fields': 'screen_name,domain'}
        data = await self.call_method("users.get", params)
        items = data.get('response', [])
        return items[0] if items else {}

    async def upload_wall_photo(self, file_content: bytes, filename: str = "photo.jpg") -> str:
        """
        Загружает картинку на сервер OpenVK и возвращает строку вложения (например, photo123_456).
        """
        server_data = await self.call_method("photos.getWallUploadServer")
        upload_url = server_data.get('response', {}).get('upload_url')
        if not upload_url:
            raise Exception("Failed to get wall upload server URL")

        files = {'photo': (filename, file_content, 'image/jpeg')}
        response = await self.state.http_client.post(upload_url, files=files)
        response.raise_for_status()
        upload_result = response.json()

        save_params = {
            "server": upload_result.get("server"),
            "photo": upload_result.get("photo"),
            "hash": upload_result.get("hash")
        }
        save_data = await self.call_method("photos.saveWallPhoto", save_params)
        photos = save_data.get('response', [])
        if not photos:
            raise Exception("Failed to save uploaded wall photo")

        photo = photos[0]
        return f"photo{photo.get('owner_id')}_{photo.get('id')}"
