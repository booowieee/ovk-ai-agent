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
    Поллер стен OpenVK.

    Единственная стратегия обнаружения упоминаний - опрос стен (wall polling).
    notifications.get используется только для обнаружения новых стен,
    на которых упомянули бота. Обработка идет только через wall polling.

    Дедупликация основана на ID комментариев (хранятся в Redis),
    а не на подсчете количества комментариев.
    """

    def __init__(self, state: AppState, client: OpenVKClient, responder: OpenVKResponder, gemini_service):
        self.state = state
        self.client = client
        self.responder = responder
        self.gemini_service = gemini_service
        self._monitored_walls: list[int] = []
        self._user_names_cache: dict[int, str] = {}
        self._known_counts: dict[str, int] = {}

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

                # Шаг 1: notifications только для обнаружения стен
                await self._discover_walls()

                # Шаг 2: опрос стен на предмет новых упоминаний
                for wall_id in list(self._monitored_walls):
                    try:
                        await self._poll_wall(wall_id, db_settings)
                    except Exception as e:
                        logger.error(f"Error polling wall {wall_id}: {e}")

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
        """Добавляет стену в список для мониторинга. Максимум 5 внешних стен."""
        if wall_id in self._monitored_walls:
            return
        self._monitored_walls.append(wall_id)
        external = [w for w in self._monitored_walls if w != self.client.user_id]
        while len(external) > 5:
            oldest = external.pop(0)
            self._monitored_walls.remove(oldest)
        logger.info(f"[Poller] Added wall {wall_id}. Active: {self._monitored_walls}")

    async def _discover_walls(self):
        """Читает notifications.get только для обнаружения стен с упоминаниями бота."""
        try:
            raw = await self.client.call_method("notifications.get", {"count": 10})
            notifications = raw.get('response', {}).get('items', [])
            profiles = raw.get('response', {}).get('profiles', [])

            for p in (profiles or []):
                pid = p.get('id')
                pfname = p.get('first_name')
                if pid and pfname:
                    self._user_names_cache[pid] = pfname

            for notif in notifications:
                ntype = notif.get('type')
                if ntype in ('mention', 'reply_comment', 'mention_comments'):
                    feedback = notif.get('feedback', {})
                    from_id = feedback.get('from_id')
                    if from_id and from_id != self.client.user_id:
                        self._add_monitored_wall(from_id)

        except Exception as e:
            error_msg = str(e)
            if "1289" in error_msg or "EventDB" in error_msg:
                pass
            else:
                logger.warning(f"Notifications check failed (non-critical): {e}")

    async def _poll_wall(self, wall_owner_id: int, db_settings):
        """Опрашивает стену на предмет новых комментариев с упоминанием бота."""
        posts = await self.client.get_wall_posts(wall_owner_id, filter='all', count=5)

        for post in posts:
            owner_id = post.get('owner_id') or wall_owner_id
            post_id = post.get('id')
            comment_count = post.get('comments', {}).get('count', 0)
            post_key = f"{owner_id}_{post_id}"

            if comment_count == 0:
                self._known_counts[post_key] = 0
                continue

            # Оптимизация: если count не изменился, не дергаем API комментариев
            old_count = self._known_counts.get(post_key)
            self._known_counts[post_key] = comment_count
            if old_count is not None and comment_count <= old_count:
                continue

            # Получаем ID последнего обработанного комментария из Redis
            redis_key = f"ovk:post:{post_key}:last_id"
            raw = await self.responder.redis.get(redis_key)
            last_seen_id = int(raw) if raw else 0

            # Первый раз видим пост: ставим базовую отметку, старые не обрабатываем
            if last_seen_id == 0:
                baseline_comments = await self.client.get_comments(
                    owner_id, post_id,
                    count=1,
                    offset=max(0, comment_count - 1)
                )
                baseline_id = baseline_comments[-1].get('id', 0) if baseline_comments else 0
                await self.responder.redis.set(redis_key, str(baseline_id))
                logger.info(f"[Wall] {post_key}: baseline ID={baseline_id} ({comment_count} comments)")
                continue

            # Забираем последние комментарии
            fetch_count = min(20, comment_count)
            offset = max(0, comment_count - fetch_count)
            comments = await self.client.get_comments(owner_id, post_id, count=fetch_count, offset=offset)

            max_id = last_seen_id
            for comment in comments:
                cid = comment.get('id', 0)

                if cid <= last_seen_id:
                    continue
                if cid > max_id:
                    max_id = cid

                from_user_id = comment.get('from_id')

                # Пропускаем собственные комментарии бота
                if from_user_id == self.client.user_id:
                    continue

                text = comment.get('text', '')
                if not is_mention_of_user(text, self.client.user_id):
                    continue

                mention_id = f"{owner_id}_{post_id}_{cid}"
                first_name = await self._get_user_first_name(from_user_id) if from_user_id else "Пользователь"
                reply_prefix = f"[id{from_user_id}|{first_name}], " if from_user_id else ""

                await self._process_mention(
                    mention_id, text, owner_id, post_id, cid, from_user_id,
                    system_prompt=db_settings.system_prompt,
                    reply_prefix=reply_prefix
                )

            if max_id > last_seen_id:
                await self.responder.redis.set(redis_key, str(max_id))
                logger.info(f"[Wall] {post_key}: last_id {last_seen_id} -> {max_id}")

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

    async def _process_mention(self, mention_id: str, text: str, owner_id: int, post_id: int,
                               comment_id: Optional[int] = None, from_user_id: Optional[int] = None,
                               system_prompt: Optional[str] = None, reply_prefix: Optional[str] = None):
        if await self.responder.is_already_processed(mention_id):
            return

        clean_text = clean_mention_from_text(text, self.client.user_id)
        if not clean_text:
            await self.responder.mark_completed(mention_id)
            return

        logger.info(f"[Bot] Generating reply for {mention_id}")
        response = await self.gemini_service.generate(clean_text, system_prompt=system_prompt)

        if not response:
            logger.warning(f"[Bot] Gemini returned empty for {mention_id}, will retry next tick")
            # НЕ помечаем completed, чтобы повторить попытку в следующем тике
            return

        if reply_prefix:
            response = f"{reply_prefix}{response}"

        # Генерируем guid для защиты от дублей при сетевых ретраях
        guid = int(hashlib.md5(mention_id.encode()).hexdigest()[:8], 16)

        if comment_id is not None:
            result = await self.responder.reply_to_comment(owner_id, post_id, comment_id, response, guid=guid)
        else:
            result = await self.responder.reply_to_post(owner_id, post_id, response, guid=guid)

        if result is not None:
            await self.responder.mark_completed(mention_id)
            logger.info(f"[Bot] Replied to {mention_id}")
        else:
            logger.warning(f"[Bot] Failed to send reply for {mention_id}, will retry")
