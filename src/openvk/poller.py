import asyncio
import time
from typing import Optional
from src.core.app_state import AppState
from src.openvk.client import OpenVKClient
from src.openvk.responder import OpenVKResponder
from src.repositories.settings_repo import SettingsRepository
from src.utils.logger import logger
from src.openvk.mention_parser import clean_mention_from_text, is_mention_of_user

class OpenVKPoller:
    def __init__(self, state: AppState, client: OpenVKClient, responder: OpenVKResponder, gemini_service):
        self.state = state
        self.client = client
        self.responder = responder
        self.gemini_service = gemini_service
        self._last_notification_time: int = int(time.time())
        self._known_comment_counts: dict[str, int] = {}

    async def run(self):
        logger.info("Starting OpenVK poller...")
        while self.state.is_running:
            try:
                # 1. Check Redis kill switch key 'ovk:bot:paused'
                is_paused = await self.responder.redis.get('ovk:bot:paused')
                if is_paused:
                    await asyncio.sleep(self.state.poll_interval)
                    continue

                # 2. Check if bot is enabled via DB settings and load configuration
                db_settings = await SettingsRepository.get_settings()
                if not db_settings or not db_settings.is_enabled:
                    await asyncio.sleep(self.state.poll_interval)
                    continue

                # Update settings dynamically from database
                if db_settings.openvk_token:
                    self.client.token = db_settings.openvk_token
                if db_settings.openvk_instance_url:
                    self.client.instance_url = db_settings.openvk_instance_url.rstrip("/")
                if db_settings.openvk_user_id:
                    self.client.user_id = db_settings.openvk_user_id
                if db_settings.poll_interval:
                    self.state.poll_interval = db_settings.poll_interval

                # 3. Try notifications strategy
                if self.state.use_notifications_api:
                    try:
                        notifications = await self.client.get_notifications(start_time=self._last_notification_time)
                        if notifications:
                            self._last_notification_time = notifications[0].get('date', int(time.time()))
                            for notif in notifications:
                                ntype = notif.get('type')
                                if ntype in ['reply_comment', 'mention_comments', 'mention']:
                                    feedback = notif.get('feedback', {})
                                    text = feedback.get('text', '')
                                    owner_id = feedback.get('owner_id')
                                    post_id = feedback.get('post_id')
                                    comment_id = feedback.get('id')
                                    from_user_id = feedback.get('from_id')
                                    
                                    mention_id = f"{owner_id}_{post_id}_{comment_id}"
                                    await self._process_mention(
                                        mention_id, text, owner_id, post_id, comment_id, from_user_id,
                                        system_prompt=db_settings.system_prompt
                                    )
                    except Exception as e:
                        error_msg = str(e)
                        if "1289" in error_msg or "EventDB" in error_msg:
                            logger.warning("EventDB disabled, falling back to wall polling.")
                            self.state.use_notifications_api = False
                        else:
                            logger.error(f"Error fetching notifications: {e}")
                
                # 4. Fallback wall polling
                if not self.state.use_notifications_api:
                    posts = await self.client.get_wall_posts(self.client.user_id, filter='others')
                    for post in posts:
                        post_id = post.get('id')
                        owner_id = post.get('owner_id')
                        comments_info = post.get('comments', {})
                        comment_count = comments_info.get('count', 0)
                        
                        post_key = f"{owner_id}_{post_id}"
                        last_count = self._known_comment_counts.get(post_key, 0)
                        
                        if comment_count > last_count:
                            self._known_comment_counts[post_key] = comment_count
                            comments = await self.client.get_comments(owner_id, post_id, count=comment_count - last_count)
                            for comment in comments:
                                text = comment.get('text', '')
                                comment_id = comment.get('id')
                                from_user_id = comment.get('from_id')
                                if is_mention_of_user(text, self.client.user_id):
                                    mention_id = f"{owner_id}_{post_id}_{comment_id}"
                                    await self._process_mention(
                                        mention_id, text, owner_id, post_id, comment_id, from_user_id,
                                        system_prompt=db_settings.system_prompt
                                    )
            
            except Exception as e:
                logger.error(f"Error in poller loop: {e}")

            await asyncio.sleep(self.state.poll_interval)

    async def _process_mention(self, mention_id: str, text: str, owner_id: int, post_id: int, comment_id: Optional[int] = None, from_user_id: Optional[int] = None, system_prompt: Optional[str] = None):
        if await self.responder.is_already_processed(mention_id):
            return

        context_text = text
        if comment_id is not None:
            # We can fetch parent comment context here in the future
            pass
            
        clean_text = clean_mention_from_text(context_text, self.client.user_id)
        
        logger.info(f"Generating response for mention: {mention_id}")
        response = await self.gemini_service.generate(clean_text, system_prompt=system_prompt)
        
        if response:
            if comment_id is not None:
                await self.responder.reply_to_comment(owner_id, post_id, comment_id, response)
            else:
                await self.responder.reply_to_post(owner_id, post_id, response)
            
        await self.responder.mark_completed(mention_id)
