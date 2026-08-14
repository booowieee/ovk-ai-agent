import asyncio
import sys
import os

# Добавляем корневую директорию в PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import init_db
from src.config import settings
from src.core.app_state import AppState
from src.openvk.client import OpenVKClient
from src.repositories.settings_repo import SettingsRepository

async def test_gifts():
    print("Initializing DB...")
    await init_db()
    
    print("Loading settings...")
    db_settings = await SettingsRepository.get_settings()
    
    app_state = AppState()
    await app_state.init_connections(settings.REDIS_URL)
    
    instance_url = db_settings.openvk_instance_url or settings.OVK_INSTANCE_URL
    token = db_settings.openvk_token or settings.OVK_ACCESS_TOKEN
    user_id = db_settings.openvk_user_id or settings.OVK_USER_ID
    
    client = OpenVKClient(
        state=app_state,
        instance_url=instance_url,
        token=token,
        user_id=user_id
    )
    
    print(f"Bot user_id: {client.user_id}")
    print(f"OpenVK Instance: {client.instance_url}")
    
    # 1. Проверяем categories
    print("\n--- Testing gifts.getCategories ---")
    try:
        cats = await client.call_method("gifts.getCategories")
        print("Categories Response:", cats)
    except Exception as e:
        print("Failed to get gifts categories:", e)
        
    # 2. Проверяем get (какие подарки подарили боту)
    print("\n--- Testing gifts.get ---")
    try:
        my_gifts = await client.call_method("gifts.get", {"user_id": client.user_id})
        print("My Gifts Response:", my_gifts)
    except Exception as e:
        print("Failed to get my gifts:", e)
        
    # 3. Проверяем getGiftsInCategory
    print("\n--- Testing gifts.getGiftsInCategory (category_id=1) ---")
    try:
        gifts_cat = await client.call_method("gifts.getGiftsInCategory", {"category_id": 1})
        print("Gifts in category 1:", gifts_cat)
    except Exception as e:
        print("Failed to get gifts in category 1:", e)
        
    # 4. Пробуем отправить подарок самому себе
    print("\n--- Testing gifts.send ---")
    try:
        # Пытаемся отправить подарок с ID 1
        res = await client.call_method("gifts.send", {
            "user_ids": str(client.user_id),
            "gift_id": 1,
            "message": "Тестовый подарок от ИИ-бота"
        })
        print("Send Gift Response:", res)
    except Exception as e:
        print("Failed to send gift:", e)
        
    await app_state.http_client.aclose()
    await app_state.redis.close()

if __name__ == "__main__":
    asyncio.run(test_gifts())
