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
        self._known_comment_counts: dict[str, int] = {}
        self._monitored_walls: list[int] = []

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
                    logger.info(f"[Poller] Bot is disabled in database or settings row is empty.")
                    await asyncio.sleep(self.state.poll_interval)
                    continue

                # Update settings dynamically from database
                if db_settings.openvk_token:
                    self.client.token = db_settings.openvk_token
                if db_settings.openvk_instance_url:
                    self.client.instance_url = db_settings.openvk_instance_url.rstrip("/")
                if db_settings.openvk_user_id:
                    self.client.user_id = db_settings.openvk_user_id
                    if db_settings.openvk_user_id not in self._monitored_walls:
                        self._monitored_walls.append(db_settings.openvk_user_id)
                if db_settings.poll_interval:
                    self.state.poll_interval = db_settings.poll_interval

                logger.info(f"[Poller] Tick. token={'set' if db_settings.openvk_token else 'empty'}, user_id={db_settings.openvk_user_id}, monitored_walls={self._monitored_walls}")

                # 3. Process Notifications Strategy (if enabled)
                if self.state.use_notifications_api:
                    try:
                        logger.info(f"[Poller] Requesting latest notifications...")
                        raw_notif = await self.client.call_method("notifications.get", {"v": "5.81", "count": 10})
                        notifications = raw_notif.get('response', {}).get('items', [])
                        profiles = raw_notif.get('response', {}).get('profiles', [])
                        
                        logger.info(f"[Poller] Notifications response: {len(notifications)} items")
                        if notifications:
                            for notif in notifications:
                                ntype = notif.get('type')
                                logger.info(f"[Poller] Processing notification type '{ntype}'")
                                if ntype in ['reply_comment', 'mention_comments', 'mention']:
                                    feedback = notif.get('feedback', {})
                                    text = feedback.get('text', '')
                                    parent = notif.get('parent')
                                    from_user_id = feedback.get('from_id')
                                    
                                    # Add notification sender to monitored walls to track their comments
                                    if from_user_id and from_user_id != self.client.user_id:
                                        if from_user_id in self._monitored_walls:
                                            self._monitored_walls.remove(from_user_id)
                                        self._monitored_walls.append(from_user_id)
                                        # Keep list limited to bot wall + 2 other walls
                                        external_walls = [w for w in self._monitored_walls if w != self.client.user_id]
                                        if len(external_walls) > 2:
                                            oldest = external_walls[0]
                                            self._monitored_walls.remove(oldest)
                                    
                                    # Try to find sender name in profiles
                                    first_name = None
                                    if from_user_id:
                                        for p in profiles:
                                            if p.get('id') == from_user_id:
                                                first_name = p.get('first_name')
                                                break
                                    reply_prefix = f"[id{from_user_id}|{first_name or 'Пользователь'}], " if from_user_id else ""
                                    
                                    if ntype == 'mention' and not parent:
                                        # Mention in a post
                                        owner_id = feedback.get('to_id')
                                        post_id = feedback.get('id')
                                        comment_id = None
                                        mention_id = f"{owner_id}_{post_id}"
                                    else:
                                        # Mention/reply in a comment
                                        comment_id = feedback.get('id')
                                        if parent:
                                            post_id = parent.get('id')
                                            owner_id = parent.get('owner_id') or parent.get('to_id')
                                        else:
                                            owner_id = feedback.get('owner_id') or feedback.get('to_id')
                                            post_id = feedback.get('post_id')
                                        mention_id = f"comment_{comment_id}"
                                        
                                    await self._process_mention(
                                        mention_id, text, owner_id, post_id, comment_id, from_user_id,
                                        system_prompt=db_settings.system_prompt,
                                        reply_prefix=reply_prefix
                                    )
                    except Exception as e:
                        error_msg = str(e)
                        if "1289" in error_msg or "EventDB" in error_msg:
                            logger.warning("EventDB disabled, falling back to wall polling.")
                            self.state.use_notifications_api = False
                        else:
                            logger.error(f"Error fetching notifications: {e}")
                
                # 4. Wall Polling Strategy (Always run this in addition, to capture wall comment mentions)
                logger.info(f"[Poller] Running Wall Polling for walls: {self._monitored_walls}")
                for wall_owner_id in list(self._monitored_walls):
                    try:
                        logger.info(f"[Poller:Wall] Polling wall of user {wall_owner_id}...")
                        posts = await self.client.get_wall_posts(wall_owner_id, filter='all', count=5)
                        for post in posts:
                            post_id = post.get('id')
                            owner_id = post.get('owner_id') or wall_owner_id
                            comments_info = post.get('comments', {})
                            comment_count = comments_info.get('count', 0)
                            
                            post_key = f"{owner_id}_{post_id}"
                            last_count = self._known_comment_counts.get(post_key, 0)
                            logger.info(f"[Poller:Wall] Post {post_key}: comment count={comment_count}, last tracked count={last_count}")
                            
                            if comment_count > last_count:
                                self._known_comment_counts[post_key] = comment_count
                                logger.info(f"[Poller:Wall] Fetching new comments for post {post_key}...")
                                comments = await self.client.get_comments(owner_id, post_id, count=comment_count - last_count)
                                for comment in comments:
                                    text = comment.get('text', '')
                                    comment_id = comment.get('id')
                                    from_user_id = comment.get('from_id')
                                    is_mention = is_mention_of_user(text, self.client.user_id)
                                    logger.info(f"[Poller:Wall] Comment {comment_id}: mention={is_mention}, text='{text}'")
                                    if is_mention:
                                        mention_id = f"{owner_id}_{post_id}_{comment_id}"
                                        # Since we don't have profiles from wall posts, use safe Пользователь mention
                                        reply_prefix = f"[id{from_user_id}|Пользователь], " if from_user_id else ""
                                        await self._process_mention(
                                            mention_id, text, owner_id, post_id, comment_id, from_user_id,
                                            system_prompt=db_settings.system_prompt,
                                            reply_prefix=reply_prefix
                                        )
                    except Exception as e:
                        logger.error(f"Error polling wall {wall_owner_id}: {e}")
            
            except Exception as e:
                logger.error(f"Error in poller loop: {e}")

            await asyncio.sleep(self.state.poll_interval)

    async def _find_post_id_for_comment(self, comment_id: int, from_user_id: Optional[int]) -> tuple[Optional[int], Optional[int]]:
        """Ищет owner_id и post_id для комментария на стенах."""
        possible_owners = []
        if from_user_id:
            possible_owners.append(from_user_id)
        possible_owners.append(self.client.user_id)
        
        for owner in possible_owners:
            try:
                logger.info(f"[Poller] Searching comment {comment_id} on wall of {owner}...")
                posts = await self.client.get_wall_posts(owner_id=owner, count=10)
                for post in posts:
                    pid = post.get('id')
                    comments = await self.client.get_comments(owner_id=owner, post_id=pid, count=50)
                    for comment in comments:
                        if comment.get('id') == comment_id:
                            logger.info(f"[Poller] Found comment {comment_id} on wall of {owner} under post {pid}")
                            return owner, pid
            except Exception as e:
                logger.error(f"Error searching comment {comment_id} on wall {owner}: {e}")
        return None, None

    async def _process_mention(self, mention_id: str, text: str, owner_id: Optional[int], post_id: Optional[int], comment_id: Optional[int] = None, from_user_id: Optional[int] = None, system_prompt: Optional[str] = None, reply_prefix: Optional[str] = None):
        if await self.responder.is_already_processed(mention_id):
            return

        # Если owner_id или post_id отсутствует, ищем их по стенам
        if (owner_id is None or post_id is None) and comment_id is not None:
            logger.info(f"[Poller] Post/owner ID missing. Searching for comment {comment_id}...")
            owner_id, post_id = await self._find_post_id_for_comment(comment_id, from_user_id)
            if owner_id is None or post_id is None:
                logger.warning(f"[Poller] Could not find post/owner ID for comment {comment_id}. Skipping.")
                return
            # Переопределяем mention_id с реальными ID
            mention_id = f"{owner_id}_{post_id}_{comment_id}"
            # Проверяем реальный ID на случай, если уже обработали
            if await self.responder.is_already_processed(mention_id):
                return

        context_text = text
        if comment_id is not None:
            # Тут можно будет расширить подгрузку родительского контекста
            pass
            
        clean_text = clean_mention_from_text(context_text, self.client.user_id)
        
        logger.info(f"Generating response for mention: {mention_id} (owner={owner_id}, post={post_id})")
        response = await self.gemini_service.generate(clean_text, system_prompt=system_prompt)
        
        if response:
            if reply_prefix:
                response = f"{reply_prefix}{response}"
                
            if comment_id is not None:
                await self.responder.reply_to_comment(owner_id, post_id, comment_id, response)
            else:
                await self.responder.reply_to_post(owner_id, post_id, response)
            
        await self.responder.mark_completed(mention_id)
