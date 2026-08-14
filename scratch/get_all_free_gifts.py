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

async def get_all_free_gifts():
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
    
    try:
        # Получаем все категории
        cats_res = await client.call_method("gifts.getCategories")
        categories = cats_res.get("response", [])
        print(f"\nFound {len(categories)} categories. Retrieving gifts...")
        
        free_gifts_list = []
        
        for cat in categories:
            cat_id = cat.get("id")
            cat_name = cat.get("name")
            print(f"\nChecking Category {cat_id} ({cat_name})...")
            
            try:
                # В OpenVK параметр для ID категории - это 'id'
                gifts_res = await client.call_method("gifts.getGiftsInCategory", {"id": cat_id})
                gifts = gifts_res.get("response", [])
                
                for gift in gifts:
                    # Подарок бесплатный, если is_free == True или price == 0
                    is_free = gift.get("is_free", False) or (gift.get("price") == 0)
                    if is_free:
                        gift_info = {
                            "category_name": cat_name,
                            "category_id": cat_id,
                            "gift_id": gift.get("id"),
                            "name": gift.get("name"),
                            "price": gift.get("price"),
                            "usages_left": gift.get("usages_left", "unlimited")
                        }
                        free_gifts_list.append(gift_info)
                        print(f"  [FREE] ID {gift_info['gift_id']}: '{gift_info['name']}' (usages left: {gift_info['usages_left']})")
                    else:
                        print(f"  [PAID] ID {gift.get('id')}: '{gift.get('name')}' (price: {gift.get('price')} votes)")
                        
            except Exception as e:
                print(f"  Failed to get gifts in category {cat_id}: {e}")
                
            await asyncio.sleep(0.2)
            
        print("\n=============================================")
        print("SUMMARY: ALL DETECTED FREE GIFTS FOR THIS BOT:")
        print("=============================================")
        if not free_gifts_list:
            print("No free gifts found.")
        else:
            for g in free_gifts_list:
                print(f"Gift ID: {g['gift_id']} | Name: '{g['name']}' | Usages Left: {g['usages_left']} (Category: {g['category_name']})")
                
    except Exception as e:
        print(f"Error fetching categories: {e}")
        
    await app_state.http_client.aclose()
    await app_state.redis.close()

if __name__ == "__main__":
    asyncio.run(get_all_free_gifts())
