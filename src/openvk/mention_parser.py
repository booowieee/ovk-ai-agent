import re
from typing import Optional


def extract_vk_mention_ids(text: str) -> list[int]:
    """Извлекает user_id из формата [id123|Name]."""
    return [int(uid) for uid in re.findall(r'\[id(\d+)\|[^\]]+\]', text)]


def extract_at_mentions(text: str) -> list[str]:
    """Извлекает юзернеймы из формата @username."""
    return re.findall(r'@(\w+)', text)


def is_mention_of_user(text: str, user_id: int, username: Optional[str] = None) -> bool:
    """Проверяет, упоминается ли конкретный пользователь в тексте."""
    vk_ids = extract_vk_mention_ids(text)
    if user_id in vk_ids:
        return True
    if username:
        at_mentions = extract_at_mentions(text)
        if username.lower() in [m.lower() for m in at_mentions]:
            return True
    return False


def clean_mention_from_text(text: str, user_id: int, username: Optional[str] = None) -> str:
    """Убирает упоминание бота из текста, чтобы не засорять промпт."""
    # Remove [id123|Name] format
    text = re.sub(rf'\[id{user_id}\|[^\]]+\]', '', text)
    # Remove @username format
    if username:
        text = re.sub(rf'@{re.escape(username)}\b', '', text, flags=re.IGNORECASE)
    return text.strip()
