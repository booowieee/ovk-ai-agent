import asyncio
import hashlib
import random
import re
import time
from typing import Optional
from src.core.app_state import AppState
from src.openvk.client import OpenVKClient
from src.openvk.responder import OpenVKResponder
from src.repositories.settings_repo import SettingsRepository
from src.utils.logger import logger
from src.openvk.mention_parser import clean_mention_from_text, is_mention_of_user


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
        self._last_online_time: float = 0.0
        self._last_friend_check_time: float = 0.0
        self._last_global_feed_time: float = 0.0

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

                # 1. Сначала обрабатываем уведомления
                await self._process_notifications(db_settings)

                # 2. Опрашиваем стены
                await self._poll_walls(db_settings)

                # 3. Обрабатываем личные сообщения (ЛС)
                await self._process_private_messages(db_settings)

                # 4. Комментируем посты из глобальной новостной ленты
                await self._process_global_feed(db_settings)

                # 5. Вечный статус онлайн
                await self._maintain_online()

                # 6. Автодобавление в друзья
                await self._auto_accept_friends()

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
                    except Exception as e:
                        logger.error(f"Failed to accept friend request from user {uid}: {e}")
            except Exception as e:
                logger.error(f"Error checking friend requests: {e}")

    async def _process_notifications(self, db_settings):
        """Обрабатывает входящие уведомления в реальном времени."""
        try:
            raw = await self.client.call_method("notifications.get", {"count": 15})
            notifications = raw.get('response', {}).get('items', [])
            profiles = raw.get('response', {}).get('profiles', [])

            for p in (profiles or []):
                pid = p.get('id')
                pfname = p.get('first_name')
                if pid and pfname:
                    self._user_names_cache[pid] = pfname

            for notif in notifications:
                ntype = notif.get('type')
                if ntype not in ('mention', 'reply_comment', 'mention_comments', 'wall'):
                    continue

                feedback = notif.get('feedback', {})
                parent = notif.get('parent', {})
                from_user_id = feedback.get('from_id')
                text = feedback.get('text', '')

                if from_user_id == self.client.user_id:
                    continue

                if from_user_id:
                    self._add_monitored_wall(from_user_id)

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
                    if parent:
                        post_id = parent.get('id')
                        owner_id = parent.get('owner_id') or parent.get('to_id')
                    else:
                        owner_id = feedback.get('owner_id') or feedback.get('to_id')
                        post_id = feedback.get('post_id')

                is_reply = (ntype == 'reply_comment')
                is_wall_post = (ntype == 'wall')
                is_mention = is_mention_of_user(text, self.client.user_id, self._bot_username)

                # Отвечаем без проверки на упоминания, если это прямой реплай на коммент бота
                # или новый пост непосредственно на стене бота
                if not is_reply and not is_wall_post and not is_mention:
                    continue

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
                        text = comment.get('text', '')

                        if from_user_id == self.client.user_id:
                            continue

                        is_mention = is_mention_of_user(text, self.client.user_id, self._bot_username)
                        is_reply = (comment.get('reply_to_user') == self.client.user_id)

                        if not is_mention and not is_reply:
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
                logger.error(f"Error polling wall {wall_owner_id}: {e}", exc_info=True)

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
                    "unread": 1,
                    "count": 20
                })
                conversations = raw.get('response', {}).get('items', [])

            for item in conversations:
                # В getConversations структура: item.conversation, item.last_message
                # В getDialogs структура: item.message
                msg = item.get('last_message') or item.get('message')
                if not msg:
                    continue

                peer_id = msg.get('peer_id') or msg.get('user_id') or msg.get('from_id')
                from_id = msg.get('from_id') or msg.get('user_id')
                text = msg.get('text') or msg.get('body', '')
                msg_id = msg.get('id')

                # Пропускаем сообщения от самого себя
                if from_id == self.client.user_id:
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
                    else:
                        logger.error(f"[PM] Failed to send reply message to user {from_id}: {res}. Releasing lock.")
                        await self.responder.release_lock(mention_key)

                except Exception as e:
                    logger.error(f"[PM] Error generating/sending response for message {msg_id}: {e}. Releasing lock.", exc_info=True)
                    await self.responder.release_lock(mention_key)

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

            # Выбираем случайный пост из подходящих (комментируем 1 пост раз в 10 минут)
            source_id, post_id, post_text, redis_key = random.choice(valid_posts)

            # Живая проверка: действительно ли там нет нашего комментария
            try:
                comments = await self._get_latest_comments(source_id, post_id, limit=50)
                if any(c.get('from_id') == self.client.user_id for c in comments):
                    logger.info(f"[Poller:GlobalFeed] Bot already has a comment in global post {source_id}_{post_id}. Skipping.")
                    await self.responder.redis.set(redis_key, "1", ex=604800)
                    return
            except Exception as e:
                logger.error(f"Error doing live check on global post {source_id}_{post_id}: {e}")

            # Ставим блокировку в Redis, чтобы не комментировать повторно
            await self.responder.redis.set(redis_key, "1", ex=604800)  # 7 дней

            logger.info(f"[Poller:GlobalFeed] Selected post {source_id}_{post_id} for commenting. Text preview: '{post_text[:50]}...'")

            # Генерируем ответ. Инструктируем Gemini прокомментировать чужой пост в тему
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

            # Отправляем комментарий к посту
            guid = int(hashlib.md5(redis_key.encode()).hexdigest()[:8], 16)
            result = await self.responder.reply_to_post(source_id, post_id, response, guid=guid)

            if result is not None:
                logger.info(f"[Poller:GlobalFeed] Successfully commented on post {source_id}_{post_id}")
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

        # Инъекция технического требования прямо в текст запроса, чтобы обойти вредный характер бота
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
                logger.warning(f"[Bot] Gemini returned empty response for {mention_key}. Releasing lock.")
                await self.responder.release_lock(mention_key)
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
                result = await self.responder.reply_to_comment(owner_id, post_id, comment_id, response, guid=guid, attachments=attachments)
            else:
                result = await self.responder.reply_to_post(owner_id, post_id, response, guid=guid, attachments=attachments)

            if result is not None:
                await self.responder.mark_completed(mention_key)
                logger.info(f"[Bot] Successfully replied to {mention_key}")
                if comment_id is not None:
                    await self.responder.add_like("comment", owner_id, comment_id)
                else:
                    await self.responder.add_like("post", owner_id, post_id)
            else:
                logger.error(f"[Bot] Failed to send reply to OpenVK for {mention_key}. Releasing lock.")
                await self.responder.release_lock(mention_key)

        except Exception as e:
            logger.error(f"Error generating/sending response for {mention_key}: {e}. Releasing lock.", exc_info=True)
            await self.responder.release_lock(mention_key)
