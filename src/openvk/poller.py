import asyncio
from datetime import datetime, timedelta
import hashlib
import httpx
import random
import re
import time
from typing import Optional

from src.config import settings
from src.core.app_state import AppState
from src.openvk.client import OpenVKClient
from src.openvk.responder import OpenVKResponder
from src.openvk.mention_parser import clean_mention_from_text, is_mention_of_user
from src.repositories.settings_repo import SettingsRepository
from src.repositories.blacklist_repo import BlacklistRepository
from src.repositories.stats_repo import StatsRepository
from src.utils.logger import logger


class OpenVKPoller:
    """
    Универсальный поллер OpenVK.

    Использует четыре параллельные стратегии:
    1. Проверка notifications.get (реалтайм, основной триггер упоминаний и реплаев)
    2. Опрос стен (wall polling, резервный фолбек на случай лагов/ограничений API)
    3. Обработка личных сообщений (messages, ответы на входящие ЛС)
    4. Комментирование случайных постов из глобальной ленты (newsfeed.getGlobal)

    Дополнительно поддерживает:
    - Вечный онлайн (обновляется раз в 4 минуты)
    - Автодобавление всех входящих заявок в друзья (проверяется раз в минуту)

    Дедупликация полностью вынесена на уровень глобальных ID комментариев/постов/сообщений
    в Redis, что исключает дублирование ответов.
    """

    def __init__(self, state: AppState, client: OpenVKClient, responder: OpenVKResponder, gemini_service):
        self.state = state
        self.client = client
        self.responder = responder
        self.gemini_service = gemini_service
        self._monitored_walls: list[int] = []
        self._user_names_cache: dict[int, str] = {}
        self._bot_username: Optional[str] = None
        self._last_mark_as_viewed_time: float = 0.0
        self._last_online_time: float = 0.0
        self._last_friend_check_time: float = 0.0
        self._last_global_feed_time: float = 0.0
        self._last_stats_post_update: float = 0.0
        self._existing_friends_to_gift: list[int] = []
        self._last_friends_fetch_time: float = 0.0

    async def run(self):
        logger.info("Starting OpenVK poller...")
        while self.state.is_running:
            try:
                is_paused = await self.responder.redis.get('ovk:bot:paused')
                if is_paused:
                    await asyncio.sleep(self.state.poll_interval)
                    continue

                db_settings = await SettingsRepository.get_settings()
                if not db_settings or not db_settings.is_enabled:
                    await asyncio.sleep(self.state.poll_interval)
                    continue

                await self._apply_settings(db_settings)

                logger.info(f"[Poller] Tick. Active walls: {self._monitored_walls}")

                # Обработка уведомлений
                await self._process_notifications(db_settings)

                # Опрос активных стен
                await self._poll_walls(db_settings)

                # Обработка личных сообщений
                await self._process_private_messages(db_settings)

                # Комментирование постов глобальной ленты
                await self._process_global_feed(db_settings)

                # Поддержание статуса онлайн
                await self._maintain_online()

                # Автодобавление в друзья
                await self._auto_accept_friends()

                # Отправка подарков друзьям по очереди
                await self._process_old_friends_gifting()

                # Обновление поста статистики
                await self._check_and_update_stats_post()

            except Exception as e:
                logger.error(f"Error in poller loop: {e}", exc_info=True)

            await asyncio.sleep(self.state.poll_interval)

    async def _apply_settings(self, db_settings):
        if db_settings.openvk_token:
            self.client.token = db_settings.openvk_token
        if db_settings.openvk_instance_url:
            self.client.instance_url = db_settings.openvk_instance_url.rstrip("/")
        if db_settings.openvk_user_id:
            self.client.user_id = db_settings.openvk_user_id
            if db_settings.openvk_user_id not in self._monitored_walls:
                self._monitored_walls.insert(0, db_settings.openvk_user_id)
        if db_settings.poll_interval:
            self.state.poll_interval = db_settings.poll_interval

        # Резолвим никнейм бота
        if not self._bot_username and self.client.user_id:
            try:
                info = await self.client.get_user_info()
                self._bot_username = info.get('screen_name') or info.get('domain') or f"id{self.client.user_id}"
                logger.info(f"[Poller] Bot username resolved: {self._bot_username}")
            except Exception as e:
                logger.warning(f"Could not resolve bot username: {e}")

    def _add_monitored_wall(self, wall_id: int):
        """Добавляет стену в список мониторинга."""
        if wall_id in self._monitored_walls:
            return
        self._monitored_walls.append(wall_id)
        external = [w for w in self._monitored_walls if w != self.client.user_id]
        while len(external) > 5:
            oldest = external.pop(0)
            self._monitored_walls.remove(oldest)
        logger.info(f"[Poller] Added wall {wall_id} to monitoring. Active walls: {self._monitored_walls}")

    async def _maintain_online(self):
        """Поддерживает «вечный» статус онлайн, отправляя пинг раз в 4 минуты."""
        now = time.time()
        if now - self._last_online_time >= 240.0:
            try:
                logger.info("[Poller:Online] Setting online status...")
                await self.client.call_method("account.setOnline")
                self._last_online_time = now
            except Exception as e:
                logger.error(f"Error setting online status: {e}")

    async def _auto_accept_friends(self):
        """Проверяет и автоматически одобряет все входящие заявки в друзья раз в минуту."""
        now = time.time()
        if now - self._last_friend_check_time >= 60.0:
            self._last_friend_check_time = now
            try:
                raw = await self.client.call_method("friends.getRequests", {"need_viewed": 0, "count": 50})
                response = raw.get('response', {})
                
                # Заявки могут возвращаться как словарь с items, так и плоским списком
                if isinstance(response, dict):
                    items = response.get('items', [])
                elif isinstance(response, list):
                    items = response
                else:
                    items = []

                if not items:
                    return

                logger.info(f"[Poller:Friends] Found {len(items)} incoming friend requests. Accepting...")
                for item in items:
                    if isinstance(item, dict):
                        uid = item.get('id') or item.get('user_id')
                    else:
                        uid = item

                    if not uid:
                        continue

                    try:
                        res = await self.client.call_method("friends.add", {"user_id": uid})
                        logger.info(f"[Poller:Friends] Accepted friend request from user {uid}. Result: {res.get('response')}")
                        
                        try:
                            first_name, last_name = await self._get_user_full_name(uid)
                            friend_info = f"[id{uid}|{first_name} {last_name}]"
                            await self.responder.redis.set('ovk:last_added_friend', friend_info)
                        except Exception as stats_err:
                            logger.error(f"[Stats] Error saving last added friend: {stats_err}")
                            
                        # Отправляем подарок новому другу!
                        await self._send_gift_with_joke(uid, is_new_friend=True)
                            
                    except Exception as e:
                        logger.error(f"Failed to accept friend request from user {uid}: {e}")
            except Exception as e:
                logger.error(f"Error checking friend requests: {e}")

    async def _process_notifications(self, db_settings):
        """Обрабатывает входящие уведомления в реальном времени."""
        try:
            # Запрашиваем 100 уведомлений, чтобы точно не пропустить свежие при лагах отметки о прочтении
            raw = await self.client.call_method("notifications.get", {"count": 100})
            notifications = raw.get('response', {}).get('items', [])
            profiles = raw.get('response', {}).get('profiles', [])

            # Помечаем уведомления как прочитанные не чаще чем раз в 2 минуты (120 секунд),
            # чтобы избежать постоянных 429/400 (You have been rate limited) ошибок от OpenVK.
            if notifications:
                now = time.time()
                if now - self._last_mark_as_viewed_time >= 120.0:
                    try:
                        await self.client.call_method("notifications.markAsViewed")
                        self._last_mark_as_viewed_time = now
                        logger.info("[Poller] Successfully marked notifications as viewed")
                    except Exception as read_err:
                        logger.warning(f"[Poller] Failed to mark notifications as viewed: {read_err}")

            for p in (profiles or []):
                pid = p.get('id')
                pfname = p.get('first_name')
                if pid and pfname:
                    self._user_names_cache[pid] = pfname

            for notif in notifications:
                ntype = notif.get('type')
                feedback = notif.get('feedback') or {}
                parent = notif.get('parent') or {}
                from_user_id = feedback.get('from_id')
                text = feedback.get('text', '')

                # Разрешаем типы уведомлений о комментах под постами/фото
                if ntype not in ('mention', 'reply_comment', 'mention_comments', 'wall', 'comment_post', 'comment_photo'):
                    continue

                if from_user_id == self.client.user_id:
                    continue

                if from_user_id:
                    if await BlacklistRepository.is_blacklisted(from_user_id) or await BlacklistRepository.is_auto_blocked(from_user_id):
                        continue

                # Определяем ключи блокировки и параметры отправки
                if ntype == 'wall':
                    # Пост на стене бота
                    owner_id = feedback.get('to_id') or self.client.user_id
                    post_id = feedback.get('id')
                    comment_id = None
                    mention_key = f"post:{owner_id}_{post_id}"
                elif ntype == 'mention' and not parent:
                    # Упоминание в посте
                    owner_id = feedback.get('to_id') or feedback.get('owner_id')
                    post_id = feedback.get('id')
                    comment_id = None
                    mention_key = f"post:{owner_id}_{post_id}"
                else:
                    # Упоминание или ответ в комментарии
                    comment_id = feedback.get('id')
                    mention_key = f"comment:{comment_id}"
                    
                    # Пытаемся безопасно получить ID поста (из feedback или parent)
                    post_id = feedback.get('post_id') or (parent.get('post_id') if parent else None)
                    if not post_id and parent:
                        post_id = parent.get('id')
                        
                    # Извлекаем ID владельца стены
                    owner_id = (feedback.get('owner_id') or feedback.get('to_id') or 
                                (parent.get('owner_id') if parent else None) or 
                                (parent.get('to_id') if parent else None) or 
                                self.client.user_id)

                # Дедупликация: если этот ключ уже обработан (completed или в процессе), пропускаем!
                # Это полностью убирает спам логов, resolve-имен и thrashing стен на повторных тиках
                lock_state = await self.responder.redis.get(f"ovk:lock:{mention_key}")
                if lock_state in (b"completed", b"processing", "completed", "processing"):
                    continue

                is_reply = (ntype == 'reply_comment')
                is_wall_post = (ntype == 'wall')
                is_mention = is_mention_of_user(text, self.client.user_id, self._bot_username)
                
                # Дополнительно: отвечаем на любые комментарии под постами бота
                is_comment_on_bot_post = (ntype in ('comment_post', 'comment_photo') and owner_id == self.client.user_id)

                # Отвечаем, если это упоминание, реплай, новый пост на нашей стене, или любой коммент под нашим постом
                if not is_reply and not is_wall_post and not is_mention and not is_comment_on_bot_post:
                    continue

                # Добавляем стену, где произошла активность, в мониторинг (для резервного wall polling)
                if owner_id and owner_id != self.client.user_id:
                    self._add_monitored_wall(owner_id)

                first_name = await self._get_user_first_name(from_user_id) if from_user_id else "Пользователь"
                reply_prefix = f"[id{from_user_id}|{first_name}], " if from_user_id else ""

                await self._process_mention(
                    mention_key, text, owner_id, post_id, comment_id, from_user_id,
                    system_prompt=db_settings.system_prompt,
                    reply_prefix=reply_prefix
                )

        except Exception as e:
            logger.error(f"Error in notifications processing: {e}", exc_info=True)

    async def _get_latest_comments(self, owner_id: int, post_id: int, limit=100) -> list:
        """
        Получает последние комментарии к посту.
        
        Сначала делает легкий проверочный запрос (count=1), чтобы узнать
        реальный total_count из базы OpenVK. Затем запрашивает последние limit комментов.
        Это полностью обходит баги кэширования wall.get и оффсетов OpenVK API.
        """
        try:
            raw = await self.client.get_comments_raw(owner_id, post_id, count=1, offset=0)
            response = raw.get('response', {})
            total_count = response.get('count', 0)

            if total_count <= limit:
                offset = 0
                fetch_count = limit
            else:
                offset = total_count - limit
                fetch_count = limit

            raw_data = await self.client.get_comments_raw(owner_id, post_id, count=fetch_count, offset=offset)
            items = raw_data.get('response', {}).get('items', [])
            return items
        except Exception as e:
            logger.error(f"Error fetching latest comments for {owner_id}_{post_id}: {e}", exc_info=True)
            return []

    async def _poll_walls(self, db_settings):
        """Опрашивает стены на предмет новых комментариев с упоминанием бота."""
        for wall_owner_id in list(self._monitored_walls):
            try:
                posts = await self.client.get_wall_posts(wall_owner_id, filter='all', count=5)
                for post in posts:
                    owner_id = post.get('owner_id') or wall_owner_id
                    post_id = post.get('id')
                    
                    # Проверяем, если это пост на стене бота, оставленный другим пользователем
                    is_post_on_bot_wall = (owner_id == self.client.user_id)
                    post_author = post.get('from_id')
                    if post_author:
                        if await BlacklistRepository.is_blacklisted(post_author) or await BlacklistRepository.is_auto_blocked(post_author):
                            continue
                    post_text = post.get('text', '')
                    
                    # Проверяем, есть ли упоминание бота в тексте поста на чужой стене
                    is_mention_in_post = False
                    if not is_post_on_bot_wall and post_author != self.client.user_id:
                        is_mention_in_post = is_mention_of_user(post_text, self.client.user_id, self._bot_username)
                    
                    if (is_post_on_bot_wall and post_author and post_author != self.client.user_id) or is_mention_in_post:
                        mention_key = f"post:{owner_id}_{post_id}"
                        first_name = await self._get_user_first_name(post_author) if post_author else "Пользователь"
                        reply_prefix = f"[id{post_author}|{first_name}], " if post_author else ""
                        
                        await self._process_mention(
                            mention_key, post_text, owner_id, post_id, None, post_author,
                            system_prompt=db_settings.system_prompt,
                            reply_prefix=reply_prefix
                        )

                    comment_info = post.get('comments', {})
                    comment_count = comment_info.get('count', 0)

                    if comment_count == 0:
                        continue

                    # Получаем свежие комментарии напрямую через wall.getComments (без кэша)
                    comments = await self._get_latest_comments(owner_id, post_id, limit=100)

                    for comment in comments:
                        cid = comment.get('id')
                        from_user_id = comment.get('from_id')
                        if from_user_id:
                            if await BlacklistRepository.is_blacklisted(from_user_id) or await BlacklistRepository.is_auto_blocked(from_user_id):
                                continue
                        text = comment.get('text', '')

                        if from_user_id == self.client.user_id:
                            continue

                        is_mention = is_mention_of_user(text, self.client.user_id, self._bot_username)
                        is_reply = (comment.get('reply_to_user') == self.client.user_id)
                        
                        # Если это комментарий под постом на стене бота, то отвечаем на любой коммент от другого пользователя
                        is_comment_on_bot_post = (owner_id == self.client.user_id)

                        if not is_mention and not is_reply and not is_comment_on_bot_post:
                            continue

                        mention_key = f"comment:{cid}"
                        first_name = await self._get_user_first_name(from_user_id) if from_user_id else "Пользователь"
                        reply_prefix = f"[id{from_user_id}|{first_name}], " if from_user_id else ""

                        await self._process_mention(
                            mention_key, text, owner_id, post_id, cid, from_user_id,
                            system_prompt=db_settings.system_prompt,
                            reply_prefix=reply_prefix
                        )
            except Exception as e:
                import httpx
                is_permanent = False
                status_code = None
                if isinstance(e, httpx.HTTPStatusError):
                    status_code = e.response.status_code
                    if status_code in (400, 401, 403, 404):
                        is_permanent = True
                
                if is_permanent:
                    if wall_owner_id in self._monitored_walls:
                        self._monitored_walls.remove(wall_owner_id)
                    await BlacklistRepository.add_to_auto_blocked(wall_owner_id)
                    logger.warning(f"[Poller] Wall {wall_owner_id} is inaccessible (HTTP {status_code}). Removed from monitoring & added to auto-blocked.")
                else:
                    logger.warning(f"[Poller] Temporary error polling wall {wall_owner_id}: {e}")

    async def _process_private_messages(self, db_settings):
        """Обрабатывает входящие личные сообщения (ЛС) от пользователей."""
        try:
            # Запрашиваем непрочитанные диалоги. Пытаемся вызвать messages.getConversations
            try:
                raw = await self.client.call_method("messages.getConversations", {
                    "filter": "unread",
                    "count": 20
                })
                conversations = raw.get('response', {}).get('items', [])
            except Exception as e:
                logger.warning(f"[PM] messages.getConversations failed, trying messages.getDialogs fallback: {e}")
                raw = await self.client.call_method("messages.getDialogs", {
                    "count": 20
                })
                conversations = raw.get('response', {}).get('items', [])

            for item in conversations:
                # В getConversations структура: item.conversation, item.last_message
                # В getDialogs структура: item.message
                msg = item.get('last_message') or item.get('message')
                if not msg:
                    continue

                # Если это getDialogs fallback, проверяем read_state
                # read_state: 0 – непрочитанное, 1 – прочитанное.
                # out: 0 – входящее, 1 – исходящее.
                if 'read_state' in msg:
                    if msg.get('read_state') == 1 or msg.get('out') == 1:
                        continue

                peer_id = msg.get('peer_id') or msg.get('user_id') or msg.get('from_id')
                from_id = msg.get('from_id') or msg.get('user_id')
                text = msg.get('text') or msg.get('body', '')
                msg_id = msg.get('id')

                # Пропускаем сообщения от самого себя
                if from_id == self.client.user_id:
                    continue

                if from_id:
                    if await BlacklistRepository.is_blacklisted(from_id) or await BlacklistRepository.is_auto_blocked(from_id):
                        try:
                            await self.client.call_method("messages.markAsRead", {
                                "peer_id": peer_id,
                                "message_ids": str(msg_id)
                            })
                        except:
                            pass
                        continue

                if not peer_id or not msg_id:
                    continue

                # Проверяем уникальность блокировки в Redis
                mention_key = f"pm:{peer_id}_{msg_id}"
                is_proc = await self.responder.is_already_processed(mention_key)
                if is_proc:
                    continue

                logger.info(f"[PM] Received unread message from user {from_id} (msg_id={msg_id}): '{text}'")

                # Сразу помечаем диалог как прочитанный, чтобы не обрабатывать повторно
                try:
                    await self.client.call_method("messages.markAsRead", {
                        "peer_id": peer_id,
                        "message_ids": str(msg_id)
                    })
                except Exception as e:
                    logger.warning(f"[PM] Failed to mark message as read: {e}")

                # Генерируем ответ
                try:
                    response = await self.gemini_service.generate(text, system_prompt=db_settings.system_prompt)
                    if not response:
                        logger.warning(f"[PM] Gemini returned empty response for message {msg_id}. Releasing lock.")
                        await self.responder.release_lock(mention_key)
                        continue

                    # Отправляем личное сообщение
                    random_id = random.randint(1, 2**31 - 1)
                    res = await self.client.call_method("messages.send", {
                        "peer_id": peer_id,
                        "message": response,
                        "random_id": random_id
                    })

                    if res.get('response') or res.get('error') is None:
                        await self.responder.mark_completed(mention_key)
                        logger.info(f"[PM] Successfully replied to user {from_id} for message {msg_id}")
                        
                        # Записываем статистику ЛС
                        if from_id:
                            try:
                                first_name, last_name = await self._get_user_full_name(from_id)
                                await StatsRepository.increment_user_activity(from_id, first_name, last_name, is_image=False)
                                await StatsRepository.increment_global_stats(text=1)
                            except Exception as stats_err:
                                logger.error(f"[Stats] Error updating PM stats: {stats_err}")
                    else:
                        logger.error(f"[PM] Failed to send reply message to user {from_id}: {res}. Releasing lock.")
                        await self.responder.release_lock(mention_key)

                except Exception as e:
                    logger.error(f"[PM] Error generating/sending response for message {msg_id}: {e}. Releasing lock.", exc_info=True)
                    await self.responder.release_lock(mention_key)
                    if from_id and from_id > 0:
                        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (400, 401, 403, 404):
                            await BlacklistRepository.add_to_auto_blocked(from_id)

        except Exception as e:
            logger.error(f"Error in private messages processing: {e}", exc_info=True)

    async def _process_global_feed(self, db_settings):
        """Периодически комментирует случайные посты из глобальной ленты."""
        now = time.time()
        # Проверяем глобальную ленту раз в 10 минут (600 секунд)
        if now - self._last_global_feed_time < 600.0:
            return

        self._last_global_feed_time = now

        try:
            logger.info("[Poller:GlobalFeed] Fetching global newsfeed...")
            raw = await self.client.call_method("newsfeed.getGlobal", {"count": 15})
            response = raw.get('response', {})
            items = response.get('items', [])

            valid_posts = []
            for item in items:
                if item.get('type') != 'post':
                    continue

                post_id = item.get('post_id') or item.get('id')
                source_id = item.get('source_id') or item.get('owner_id')
                text = item.get('text', '').strip()

                if not post_id or not source_id:
                    continue

                # Игнорируем собственные посты
                if source_id == self.client.user_id:
                    continue

                # Игнорируем пользователей из черного списка
                if source_id:
                    actual_id = abs(source_id)
                    if await BlacklistRepository.is_blacklisted(actual_id) or await BlacklistRepository.is_auto_blocked(actual_id):
                        continue

                # Игнорируем посты без текста (только фото/видео) или слишком короткие
                if not text or len(text) < 10:
                    continue

                # Проверяем, не комментировали ли мы этот пост ранее
                redis_key = f"ovk:global_feed:{source_id}_{post_id}"
                already_commented = await self.responder.redis.get(redis_key)
                if already_commented:
                    continue

                valid_posts.append((source_id, post_id, text, redis_key))

            if not valid_posts:
                logger.info("[Poller:GlobalFeed] No suitable posts found in this tick.")
                return

            # Выбор случайного поста
            source_id, post_id, post_text, redis_key = random.choice(valid_posts)

            # Проверка отсутствия существующего комментария бота
            try:
                comments = await self._get_latest_comments(source_id, post_id, limit=50)
                if any(c.get('from_id') == self.client.user_id for c in comments):
                    logger.info(f"[Poller:GlobalFeed] Bot already has a comment in global post {source_id}_{post_id}. Skipping.")
                    await self.responder.redis.set(redis_key, "1", ex=604800)
                    return
            except Exception as e:
                logger.error(f"Error doing live check on global post {source_id}_{post_id}: {e}")

            # Блокировка в Redis для предотвращения повторной отправки
            await self.responder.redis.set(redis_key, "1", ex=604800)  # 7 дней

            logger.info(f"[Poller:GlobalFeed] Selected post {source_id}_{post_id} for commenting. Text preview: '{post_text[:50]}...'")

            # Генерация комментария к внешнему посту
            custom_system_prompt = (
                "Ты пользователь социальной сети. Напиши короткий, уместный комментарий "
                "к посту другого человека, выразив свое мнение в тему. "
                "Пиши просто, кратко, без смайликов и официоза."
            )
            if db_settings.system_prompt:
                custom_system_prompt = f"{db_settings.system_prompt}\n\n{custom_system_prompt}"

            response = await self.gemini_service.generate(post_text, system_prompt=custom_system_prompt)
            if not response:
                logger.warning(f"[Poller:GlobalFeed] Gemini returned empty response for post {source_id}_{post_id}")
                await self.responder.redis.delete(redis_key)
                return

            guid = int(hashlib.md5(redis_key.encode()).hexdigest()[:8], 16)
            result = await self.responder.reply_to_post(source_id, post_id, response, guid=guid)

            if result is not None:
                logger.info(f"[Poller:GlobalFeed] Successfully commented on post {source_id}_{post_id}")
                try:
                    await StatsRepository.increment_global_stats(text=1, likes=1)
                except Exception as stats_err:
                    logger.error(f"[Stats] Error updating global feed stats: {stats_err}")
                await self.responder.add_like("post", source_id, post_id)
            else:
                logger.error(f"[Poller:GlobalFeed] Failed to send comment to post {source_id}_{post_id}")
                await self.responder.redis.delete(redis_key)

        except Exception as e:
            logger.error(f"Error in global feed processing: {e}", exc_info=True)

    async def _get_user_first_name(self, user_id: int) -> str:
        if user_id in self._user_names_cache:
            return self._user_names_cache[user_id]
        try:
            data = await self.client.call_method("users.get", {"user_ids": user_id})
            items = data.get('response', [])
            if items:
                first_name = items[0].get('first_name')
                if first_name:
                    self._user_names_cache[user_id] = first_name
                    return first_name
        except Exception as e:
            logger.error(f"Error fetching user name for {user_id}: {e}")
        return "Пользователь"

    async def _get_user_full_name(self, user_id: int) -> tuple[str, str]:
        try:
            data = await self.client.call_method("users.get", {"user_ids": user_id})
            items = data.get('response', [])
            if items:
                first_name = items[0].get('first_name', '')
                last_name = items[0].get('last_name', '')
                return first_name, last_name
        except Exception as e:
            logger.error(f"Error fetching full user name for {user_id}: {e}")
        return "Пользователь", ""

    async def _find_post_id_for_comment(self, comment_id: int, from_user_id: Optional[int]) -> tuple[Optional[int], Optional[int]]:
        possible_owners = []
        if from_user_id:
            possible_owners.append(from_user_id)
        possible_owners.append(self.client.user_id)

        for owner in possible_owners:
            try:
                posts = await self.client.get_wall_posts(owner_id=owner, count=10)
                for post in posts:
                    pid = post.get('id')
                    comments = await self.client.get_comments(owner_id=owner, post_id=pid, count=50)
                    for comment in comments:
                        if comment.get('id') == comment_id:
                            return owner, pid
            except Exception as e:
                logger.error(f"Error searching comment {comment_id} on wall {owner}: {e}")
        return None, None

    async def _process_mention(self, mention_key: str, text: str, owner_id: Optional[int], post_id: Optional[int],
                               comment_id: Optional[int] = None, from_user_id: Optional[int] = None,
                               system_prompt: Optional[str] = None, reply_prefix: Optional[str] = None):
        is_proc = await self.responder.is_already_processed(mention_key)
        if is_proc:
            return

        # Живая проверка: если мы комментируем сам пост (а не отвечаем на конкретный комментарий)
        if comment_id is None and owner_id is not None and post_id is not None:
            try:
                comments = await self._get_latest_comments(owner_id, post_id, limit=50)
                if any(c.get('from_id') == self.client.user_id for c in comments):
                    logger.info(f"[Poller:Check] Bot already has a comment in post {owner_id}_{post_id}. Skipping.")
                    await self.responder.mark_completed(mention_key)
                    return
            except Exception as e:
                logger.error(f"Error doing live comment check for post {owner_id}_{post_id}: {e}")

        if (owner_id is None or post_id is None) and comment_id is not None:
            logger.info(f"[Poller] Post/owner ID missing for key {mention_key}. Searching...")
            owner_id, post_id = await self._find_post_id_for_comment(comment_id, from_user_id)
            if owner_id is None or post_id is None:
                logger.warning(f"[Poller] Could not resolve post/owner ID for comment {comment_id}. Releasing lock.")
                await self.responder.release_lock(mention_key)
                return

        # Проверяем статус генерации картинок в Redis
        img_gen = await self.responder.redis.get('ovk:settings:image_generation')
        image_gen_enabled = (img_gen == b'1' or img_gen == '1')

        clean_text = clean_mention_from_text(text, self.client.user_id, self._bot_username)

        if not clean_text:
            await self.responder.mark_completed(mention_key)
            return

        # Inject image generation tag if drawing intent is detected
        if image_gen_enabled:
            drawing_keywords = ["нарисуй", "сгенерируй", "картинку", "картинка", "рисунок", "покажи фото", "скинь фото", "изобрази", "арт", "draw", "paint", "generate"]
            text_lower = clean_text.lower()
            if any(kw in text_lower for kw in drawing_keywords):
                clean_text += (
                    "\n(Системное требование: обязательно добавь в самый конец своего ответа технический тег "
                    "[GENERATE_IMAGE: подробное описание на английском языке для генерации картинки]. "
                    "Не отказывайся и сделай это независимо от твоего характера или мнения о запросе!)"
                )

        logger.info(f"[Bot] Generating response for {mention_key}. User text: '{clean_text[:100]}'. Image gen: {image_gen_enabled}")
        try:
            response = await self.gemini_service.generate(clean_text, system_prompt=system_prompt, image_gen_enabled=image_gen_enabled)
            if not response:
                logger.warning(f"[Bot] Gemini returned empty response for {mention_key}. Marking as completed to prevent infinite loop.")
                await self.responder.mark_completed(mention_key)
                return

            logger.info(f"[Bot] Gemini response: '{response[:200]}'")

            # Вырезаем технический тег и запускаем генерацию картинки
            attachments = None
            if image_gen_enabled and "[GENERATE_IMAGE:" in response:
                match = re.search(r'\[GENERATE_IMAGE:\s*([^\]]+)\]', response)
                if match:
                    image_prompt = match.group(1).strip()
                    response = re.sub(r'\[GENERATE_IMAGE:\s*[^\]]+\]', '', response).strip()
                    
                    logger.info(f"[Bot] Generating AI image for prompt: '{image_prompt}'")
                    try:
                        image_bytes = await self.gemini_service.generate_image(image_prompt)
                        if image_bytes:
                            attachments = await self.client.upload_wall_photo(image_bytes)
                            logger.info(f"[Bot] Successfully uploaded AI image to OpenVK: {attachments}")
                    except Exception as e:
                        logger.error(f"[Bot] Failed to generate/upload AI image: {e}")

            if reply_prefix:
                response = f"{reply_prefix}{response}"

            guid = int(hashlib.md5(mention_key.encode()).hexdigest()[:8], 16)

            if comment_id is not None:
                await self.responder.reply_to_comment(owner_id, post_id, comment_id, response, guid=guid, attachments=attachments)
            else:
                await self.responder.reply_to_post(owner_id, post_id, response, guid=guid, attachments=attachments)

            await self.responder.mark_completed(mention_key)
            logger.info(f"[Bot] Successfully replied to {mention_key}")
            
            # Записываем статистику упоминания
            if from_user_id:
                try:
                    first_name, last_name = await self._get_user_full_name(from_user_id)
                    await StatsRepository.increment_user_activity(from_user_id, first_name, last_name, is_image=bool(attachments))
                    await StatsRepository.increment_global_stats(
                        text=1,
                        images=1 if attachments else 0,
                        likes=1
                    )
                except Exception as stats_err:
                    logger.error(f"[Stats] Error updating stats: {stats_err}")

            if comment_id is not None:
                await self.responder.add_like("comment", owner_id, comment_id)
            else:
                await self.responder.add_like("post", owner_id, post_id)

        except Exception as e:
            is_permanent = False
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (400, 401, 403, 404):
                is_permanent = True
                
            if is_permanent:
                logger.warning(f"[Bot] Permanent error for {mention_key}: {e}. Marking completed to prevent loop.")
                await self.responder.mark_completed(mention_key)
            else:
                logger.error(f"Error generating/sending response for {mention_key}: {e}. Releasing lock.", exc_info=True)
                await self.responder.release_lock(mention_key)

    async def _check_and_update_stats_post(self):
        now = time.time()
        # Обновляем пост раз в 10 минут (600 секунд)
        if now - self._last_stats_post_update < 600:
            return

        self._last_stats_post_update = now

        try:
            stats = await StatsRepository.get_stats()
            g = stats["global"]
            
            # Получение последнего добавленного друга
            last_friend = await self.responder.redis.get('ovk:last_added_friend')
            if last_friend:
                last_friend_text = last_friend.decode('utf-8') if isinstance(last_friend, bytes) else last_friend
            else:
                last_friend_text = "Нет данных"

            # Форматирование текста поста статистики
            text = (
                "📈 Глобальные показатели:\n"
                f"• Всего ответов: {g['total_text_requests']}\n"
                f"• Сгенерировано картинок: {g['total_image_requests']}\n"
                f"• Поставлено лайков: {g['total_likes_count']}\n"
                f"🤝 Последний добавленный друг: {last_friend_text}\n\n"
                
                "🏆 ТОП-5 активных собеседников (Текст):\n"
            )
            
            if not stats["top_text"]:
                text += "— Нет данных\n"
            else:
                for i, u in enumerate(stats["top_text"], 1):
                    text += f"{i}. id{u['vk_id']} ({u['first_name']} {u['last_name']}) — {u['count']} запросов\n"
                    
            text += "\n🖼 ТОП-5 генераторов (Картинки):\n"
            if not stats["top_image"]:
                text += "— Нет данных\n"
            else:
                for i, u in enumerate(stats["top_image"], 1):
                    text += f"{i}. id{u['vk_id']} ({u['first_name']} {u['last_name']}) — {u['count']} картинок\n"
                    
            # Получение московского времени (UTC+3)
            msk_now = datetime.utcnow() + timedelta(hours=3)
            text += f"\nПоследнее обновление: {msk_now.strftime('%d.%m.%Y %H:%M:%S')} MSK"
            
            # Получение ID поста статистики
            post_id_val = await self.responder.redis.get('ovk:stats_post_id')
            if post_id_val:
                post_setting = post_id_val.decode('utf-8') if isinstance(post_id_val, bytes) else str(post_id_val)
            else:
                post_setting = str(settings.OVK_STATS_POST_ID or "").strip()
                if post_setting:
                    await self.responder.redis.set('ovk:stats_post_id', post_setting)

            # Создание нового поста при отсутствии ID
            if not post_setting:
                logger.info("[StatsPost] No stats post ID specified. Creating one dynamically...")
                owner_id = self.client.user_id
                post_res = await self.client.call_method("wall.post", {
                    "owner_id": owner_id,
                    "message": text
                })
                new_post_id = post_res.get("response", {}).get("post_id")
                if not new_post_id:
                    raise Exception(f"Failed to create initial stats post: {post_res}")
                
                post_setting = f"{owner_id}_{new_post_id}"
                await self.responder.redis.set('ovk:stats_post_id', post_setting)
                logger.info(f"[StatsPost] Created initial stats post {post_setting}")
                
                try:
                    await self.client.call_method("wall.pin", {
                        "owner_id": owner_id,
                        "post_id": new_post_id
                    })
                    logger.info(f"[StatsPost] Pinned initial stats post {post_setting}")
                except Exception as pin_err:
                    logger.error(f"[StatsPost] Failed to pin initial post {post_setting}: {pin_err}")
                return

            if "_" in post_setting:
                parts = post_setting.split("_")
                owner_id = int(parts[0])
                post_id = int(parts[1])
            else:
                owner_id = self.client.user_id
                post_id = int(post_setting)

            logger.info(f"[StatsPost] Updating stats post {owner_id}_{post_id}...")
            is_permanent_error = False
            
            try:
                # Передача post_id и id для совместимости с версиями OpenVK
                res = await self.client.call_method("wall.edit", {
                    "owner_id": owner_id,
                    "post_id": post_id,
                    "id": post_id,
                    "message": text
                })
                logger.info(f"[StatsPost] Edit post response: {res}")
            except Exception as edit_err:
                if isinstance(edit_err, httpx.HTTPStatusError):
                    if edit_err.response.status_code in (400, 403, 404):
                        is_permanent_error = True
                elif "OpenVK API error" in str(edit_err):
                    is_permanent_error = True
                
                if not is_permanent_error:
                    # Проброс временных сетевых ошибок (502, таймаут)
                    raise edit_err
                
                logger.warning(
                    f"[StatsPost] Permanent edit error for post {owner_id}_{post_id}: {edit_err}. "
                    f"Attempting to re-create the stats post..."
                )
                
                # Создание нового поста со статистикой
                post_res = await self.client.call_method("wall.post", {
                    "owner_id": owner_id,
                    "message": text
                })
                new_post_id = post_res.get("response", {}).get("post_id")
                if not new_post_id:
                    raise Exception(f"Failed to create new stats post: {post_res}")
                
                logger.info(f"[StatsPost] Created new stats post {owner_id}_{new_post_id}")
                
                # Удаление старого поста
                try:
                    await self.client.call_method("wall.delete", {
                        "owner_id": owner_id,
                        "post_id": post_id,
                        "id": post_id
                    })
                    logger.info(f"[StatsPost] Deleted old stats post {owner_id}_{post_id}")
                except Exception as del_err:
                    logger.error(f"[StatsPost] Failed to delete old post {owner_id}_{post_id}: {del_err}")
                
                # Закрепление нового поста
                try:
                    await self.client.call_method("wall.pin", {
                        "owner_id": owner_id,
                        "post_id": new_post_id
                    })
                    logger.info(f"[StatsPost] Pinned new stats post {owner_id}_{new_post_id}")
                except Exception as pin_err:
                    logger.error(f"[StatsPost] Failed to pin new post {owner_id}_{new_post_id}: {pin_err}")
                
                # Обновление ID поста статистики в памяти и Redis
                new_setting = f"{owner_id}_{new_post_id}" if "_" in post_setting else str(new_post_id)
                settings.OVK_STATS_POST_ID = new_setting
                await self.responder.redis.set('ovk:stats_post_id', new_setting)
                logger.info(f"[StatsPost] Updated stats post ID in Redis and memory to {new_setting}")
            
        except Exception as e:
            logger.error(f"[StatsPost] Failed to update wall stats post: {e}", exc_info=True)

    async def _generate_joke_via_gemini(self) -> str:
        """Генерирует смешной анекдот через Gemini, если внешний API недоступен."""
        try:
            prompt = "Напиши один очень короткий, приличный и смешной анекдот на русском языке. Только сам анекдот, без вступлений и лишних слов."
            resp = await self.gemini_service.client.models.generate_content(
                model='gemini-3.6-flash',
                contents=prompt
            )
            joke = resp.text.strip() if resp.text else ""
            if joke.startswith("```"):
                joke = joke.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
            return joke
        except Exception as e:
            logger.error(f"[Gifts] Failed to generate joke via Gemini: {e}")
            return "Улыбнись! Желаю отличного настроения! 😊"

    async def _fetch_joke_via_api(self) -> str:
        """Получает случайный анекдот с внешнего бесплатного API rzhunemogu."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("http://rzhunemogu.ru/RandJSON.aspx?CType=1", timeout=5.0)
                text = resp.content.decode('cp1251', errors='ignore')
                match = re.search(r'\{"content":"(.*)"\}', text, re.DOTALL)
                if match:
                    joke_text = match.group(1).replace(r'\"', '"').replace(r'\r\n', '\n').replace(r'\n', '\n').strip()
                    if joke_text:
                        return joke_text
                return text.strip()
        except Exception as e:
            logger.error(f"[Gifts] Failed to fetch joke via rzhunemogu: {e}")
            return ""

    def _extract_gift_id_from_url(self, image_url: str) -> Optional[int]:
        """Парсит числовой ID подарка из URL картинки."""
        if not image_url:
            return None
        match = re.search(r'gift(\d+)_', image_url)
        if match:
            return int(match.group(1))
        return None

    async def _get_available_free_gift_ids(self) -> list[int]:
        """Динамически запрашивает все доступные бесплатные ID подарков с ненулевым лимитом usages_left."""
        try:
            now = time.time()
            # Кешируем список на 1 час, чтобы не спамить API на каждом тике
            if hasattr(self, '_cached_free_gifts') and hasattr(self, '_last_gifts_cache_time'):
                if now - self._last_gifts_cache_time < 3600.0:
                    if self._cached_free_gifts:
                        return self._cached_free_gifts

            logger.info("[Gifts] Requesting categories to build free gifts pool...")
            cats_res = await self.client.call_method("gifts.getCategories")
            categories = cats_res.get("response", [])
            
            free_ids = []
            for cat in categories:
                cat_id = cat.get("id")
                if not cat_id:
                    continue
                try:
                    # Запрашиваем подарки в категории
                    gifts_res = await self.client.call_method("gifts.getGiftsInCategory", {"id": cat_id})
                    gifts = gifts_res.get("response", [])
                    for gift in gifts:
                        # Проверяем, бесплатный ли подарок
                        is_free = gift.get("is_free", False) or (gift.get("price") == 0)
                        if is_free:
                            usages_left = gift.get("usages_left")
                            # Если usages_left отсутствует, считаем безлимитным. Иначе проверяем > 0
                            if usages_left is None or (isinstance(usages_left, int) and usages_left > 0):
                                image_url = gift.get("image", "")
                                gift_id = self._extract_gift_id_from_url(image_url)
                                if gift_id is not None:
                                    free_ids.append(gift_id)
                except Exception as cat_err:
                    logger.error(f"[Gifts] Failed to get gifts in category {cat_id}: {cat_err}")
            
            self._cached_free_gifts = free_ids
            self._last_gifts_cache_time = now
            logger.info(f"[Gifts] Free gifts pool updated: {free_ids}")
            return free_ids
        except Exception as e:
            logger.error(f"[Gifts] Failed to build free gifts list: {e}")
            return [1] # Резервный ID на случай сбоя

    async def _send_gift_with_joke(self, target_user_id: int, is_new_friend: bool = True):
        """Отправляет случайный подарок с анекдотом пользователю."""
        try:
            # Динамически получаем доступные бесплатные подарки
            gift_ids = await self._get_available_free_gift_ids()
            if not gift_ids:
                logger.warning(f"[Gifts] No free gifts with usages left available. Gifting to user {target_user_id} skipped.")
                return
                
            gift_id = random.choice(gift_ids)
            
            # Сначала пробуем получить анекдот по API
            joke = await self._fetch_joke_via_api()
            if not joke:
                # Если API лежит, используем Gemini
                joke = await self._generate_joke_via_gemini()
                
            message = joke
            
            logger.info(f"[Gifts] Attempting to send gift {gift_id} to user {target_user_id}...")
            res = await self.client.call_method("gifts.send", {
                "user_ids": str(target_user_id),
                "gift_id": gift_id,
                "message": message
            })
            
            response_data = res.get("response", {})
            success = False
            if isinstance(response_data, dict):
                success = (response_data.get("success") == 1 or response_data.get("withdraw_votes") == 0)
            elif isinstance(response_data, list) and response_data:
                success = (response_data[0].get("success") == 1)
            
            if success:
                logger.info(f"[Gifts] Successfully sent gift {gift_id} to user {target_user_id}")
                # Помечаем в Redis, что подарок отправлен
                await self.responder.redis.sadd("ovk:gifted_friends", str(target_user_id))
                # Сбрасываем кэш, чтобы при следующем запросе обновились usages_left
                self._last_gifts_cache_time = 0.0
            else:
                logger.warning(f"[Gifts] Failed to send gift, response: {res}")
                
        except Exception as e:
            logger.error(f"[Gifts] Error sending gift to {target_user_id}: {e}")

    async def _process_old_friends_gifting(self):
        """Опрашивает список существующих друзей и дарит подарок по одному за тик (чтобы не спамить)."""
        now = time.time()
        
        # Если очередь пуста, пробуем обновить её раз в 1 час
        if not self._existing_friends_to_gift:
            if now - self._last_friends_fetch_time >= 3600.0:
                self._last_friends_fetch_time = now
                try:
                    logger.info("[Gifts] Fetching friends list to check for old friends gifting...")
                    raw = await self.client.call_method("friends.get", {"count": 1000})
                    response = raw.get("response", {})
                    
                    if isinstance(response, dict):
                        friend_ids = response.get("items", [])
                    elif isinstance(response, list):
                        friend_ids = response
                    else:
                        friend_ids = []
                        
                    if friend_ids:
                        # Фильтруем тех, кому мы уже дарили подарок
                        ungifted = []
                        for fid in friend_ids:
                            uid = None
                            if isinstance(fid, dict):
                                uid = fid.get('id') or fid.get('user_id')
                            elif isinstance(fid, (int, str)):
                                uid = int(fid)
                                
                            if not uid:
                                continue
                                
                            is_gifted = await self.responder.redis.sismember("ovk:gifted_friends", str(uid))
                            if not is_gifted:
                                ungifted.append(uid)
                        
                        if ungifted:
                            self._existing_friends_to_gift = ungifted
                            logger.info(f"[Gifts] Found {len(ungifted)} old friends who haven't received a gift yet. Gifting queue loaded.")
                except Exception as e:
                    logger.error(f"[Gifts] Failed to fetch old friends list: {e}")
                    
        # Если в очереди кто-то есть, дарим ОДНОМУ другу за этот тик
        if self._existing_friends_to_gift:
            target_id = self._existing_friends_to_gift.pop(0)
            is_gifted = await self.responder.redis.sismember("ovk:gifted_friends", str(target_id))
            if not is_gifted:
                await self._send_gift_with_joke(target_id, is_new_friend=False)
