import asyncio
import hashlib
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

    Использует две параллельные стратегии:
    1. Проверка notifications.get (реалтайм, основной триггер упоминаний и реплаев)
    2. Опрос стен (wall polling, резервный фолбек на случай лагов/ограничений API)

    Дедупликация полностью вынесена на уровень глобальных ID комментариев/постов
    в Redis, что исключает дублирование ответов.
    """

    def __init__(self, state: AppState, client: OpenVKClient, responder: OpenVKResponder, gemini_service):
        self.state = state
        self.client = client
        self.responder = responder
        self.gemini_service = gemini_service
        self._monitored_walls: list[int] = []
        self._user_names_cache: dict[int, str] = {}

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

                self._apply_settings(db_settings)

                logger.info(f"[Poller] Tick. walls={self._monitored_walls}")

                # 1. Сначала обрабатываем уведомления (мгновенная реакция)
                await self._process_notifications(db_settings)

                # 2. Опрашиваем стены (резервный фолбек)
                await self._poll_walls(db_settings)

            except Exception as e:
                logger.error(f"Error in poller loop: {e}")

            await asyncio.sleep(self.state.poll_interval)

    def _apply_settings(self, db_settings):
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

    def _add_monitored_wall(self, wall_id: int):
        """Добавляет стену в список мониторинга. Ограничиваем список 5 внешними пользователями."""
        if wall_id in self._monitored_walls:
            return
        self._monitored_walls.append(wall_id)
        external = [w for w in self._monitored_walls if w != self.client.user_id]
        while len(external) > 5:
            oldest = external.pop(0)
            self._monitored_walls.remove(oldest)
        logger.info(f"[Poller] Added wall {wall_id} to monitoring. Active walls: {self._monitored_walls}")

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
                if ntype not in ('mention', 'reply_comment', 'mention_comments'):
                    continue

                feedback = notif.get('feedback', {})
                parent = notif.get('parent', {})
                from_user_id = feedback.get('from_id')

                # Пропускаем уведомления о собственных действиях
                if from_user_id == self.client.user_id:
                    continue

                # Добавляем автора в список мониторинга стен
                if from_user_id:
                    self._add_monitored_wall(from_user_id)

                text = feedback.get('text', '')

                # Определяем ключи блокировки и параметры отправки
                if ntype == 'mention' and not parent:
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

                # Если это не прямой реплай на коммент бота, проверяем наличие упоминания в тексте
                is_reply = (ntype == 'reply_comment')
                if not is_reply and not is_mention_of_user(text, self.client.user_id):
                    continue

                first_name = await self._get_user_first_name(from_user_id) if from_user_id else "Пользователь"
                reply_prefix = f"[id{from_user_id}|{first_name}], " if from_user_id else ""

                await self._process_mention(
                    mention_key, text, owner_id, post_id, comment_id, from_user_id,
                    system_prompt=db_settings.system_prompt,
                    reply_prefix=reply_prefix
                )

        except Exception as e:
            logger.error(f"Error in notifications processing: {e}")

    async def _poll_walls(self, db_settings):
        """Опрашивает стены на предмет новых комментариев с упоминанием бота."""
        for wall_owner_id in list(self._monitored_walls):
            try:
                posts = await self.client.get_wall_posts(wall_owner_id, filter='all', count=5)
                for post in posts:
                    owner_id = post.get('owner_id') or wall_owner_id
                    post_id = post.get('id')
                    comment_info = post.get('comments', {})
                    comment_count = comment_info.get('count', 0)

                    if comment_count == 0:
                        continue

                    # Запрашиваем последние комментарии (без оптимизации по count для обхода кэша API)
                    fetch_count = min(20, comment_count)
                    offset = max(0, comment_count - fetch_count)
                    comments = await self.client.get_comments(owner_id, post_id, count=fetch_count, offset=offset)

                    for comment in comments:
                        cid = comment.get('id')
                        from_user_id = comment.get('from_id')

                        # Пропускаем комментарии от самого бота
                        if from_user_id == self.client.user_id:
                            continue

                        text = comment.get('text', '')
                        
                        # Проверяем, является ли комментарий упоминанием или прямым ответом боту
                        is_mention = is_mention_of_user(text, self.client.user_id)
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
                logger.error(f"Error polling wall {wall_owner_id}: {e}")

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

    async def _process_mention(self, mention_key: str, text: str, owner_id: Optional[int], post_id: Optional[int],
                               comment_id: Optional[int] = None, from_user_id: Optional[int] = None,
                               system_prompt: Optional[str] = None, reply_prefix: Optional[str] = None):
        if await self.responder.is_already_processed(mention_key):
            return

        # Если owner_id или post_id отсутствует, ищем их по стенам
        if (owner_id is None or post_id is None) and comment_id is not None:
            logger.info(f"[Poller] Post/owner ID missing for key {mention_key}. Searching...")
            owner_id, post_id = await self._find_post_id_for_comment(comment_id, from_user_id)
            if owner_id is None or post_id is None:
                logger.warning(f"[Poller] Could not resolve post/owner ID for comment {comment_id}. Releasing lock.")
                await self.responder.release_lock(mention_key)
                return

        clean_text = clean_mention_from_text(text, self.client.user_id)
        logger.info(f"[Bot] Raw text for {mention_key}: '{text}'")
        logger.info(f"[Bot] Clean text for {mention_key}: '{clean_text}'")

        if not clean_text:
            logger.info(f"[Poller] Mention {mention_key} text is empty after cleaning. Marking completed.")
            await self.responder.mark_completed(mention_key)
            return

        logger.info(f"[Bot] Generating response for {mention_key}...")
        try:
            response = await self.gemini_service.generate(clean_text, system_prompt=system_prompt)
            if not response:
                logger.warning(f"[Bot] Gemini returned empty response for {mention_key}. Releasing lock.")
                await self.responder.release_lock(mention_key)
                return

            if reply_prefix:
                response = f"{reply_prefix}{response}"

            guid = int(hashlib.md5(mention_key.encode()).hexdigest()[:8], 16)

            if comment_id is not None:
                result = await self.responder.reply_to_comment(owner_id, post_id, comment_id, response, guid=guid)
            else:
                result = await self.responder.reply_to_post(owner_id, post_id, response, guid=guid)

            if result is not None:
                await self.responder.mark_completed(mention_key)
                logger.info(f"[Bot] Successfully replied to {mention_key}")
            else:
                logger.error(f"[Bot] Failed to send reply to OpenVK for {mention_key}. Releasing lock.")
                await self.responder.release_lock(mention_key)

        except Exception as e:
            logger.error(f"[Bot] Error generating/sending response for {mention_key}: {e}. Releasing lock.")
            await self.responder.release_lock(mention_key)
