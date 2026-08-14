import asyncio
import sys
import os
import httpx
import re

# Добавляем корневую директорию в PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import init_db
from src.config import settings
from src.core.app_state import AppState
from src.openvk.client import OpenVKClient
from src.repositories.settings_repo import SettingsRepository

async def test_gifts_in_category(client, param_name, category_id):
    try:
        print(f"[Test] Trying gifts.getGiftsInCategory with parameter '{param_name}'={category_id}...", end=" ")
        res = await client.call_method("gifts.getGiftsInCategory", {param_name: category_id})
        print("-> SUCCESS!")
        return res
    except Exception as e:
        print(f"-> FAILED ({e})")
        return None

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
    
    # 1. Выясняем правильный параметр для getGiftsInCategory
    print("\n--- Checking getGiftsInCategory parameters ---")
    for param in ["category_id", "id", "category", "cat_id", "cat"]:
        res = await test_gifts_in_category(client, param, 1)
        if res:
            print(f"Found working parameter: '{param}'")
            print("Gifts in category 1:", str(res)[:300] + "...")
            break
            
    # 2. Сканируем широкий диапазон ID подарков (от 1 до 150)
    print("\n--- Scanning Gift IDs 1 to 150 for free ones ---")
    free_ids = []
    
    for gift_id in range(1, 151):
        try:
            print(f"Testing Gift ID {gift_id}...", end=" ", flush=True)
            res = await client.call_method("gifts.send", {
                "user_ids": str(client.user_id),
                "gift_id": gift_id,
                "message": "Тест"
            })
            response_data = res.get("response", {})
            
            success = False
            if isinstance(response_data, dict):
                success = (response_data.get("success") == 1 or response_data.get("withdraw_votes") == 0)
            elif isinstance(response_data, list) and response_data:
                success = (response_data[0].get("success") == 1)
                
            if success:
                print("-> FREE! (Success)")
                free_ids.append(gift_id)
            else:
                error_msg = response_data.get("error", "Unknown error")
                print(f"-> Paid/Failed ({error_msg})")
        except Exception as e:
            err_str = str(e)
            if "enough voices" in err_str or "voices" in err_str:
                print("-> Paid (Voices error)")
            else:
                print(f"-> Error: {err_str}")
        
        # Небольшая пауза, чтобы не спамить API
        await asyncio.sleep(0.3)
        
    print(f"\nScan complete! Found free gift IDs: {free_ids}")
    
    await app_state.http_client.aclose()
    await app_state.redis.close()

if __name__ == "__main__":
    asyncio.run(test_gifts())
