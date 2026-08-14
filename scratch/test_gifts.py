import asyncio
import sys
import os
import random
import httpx
import re

# Добавляем корневую директорию в PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import init_db
from src.config import settings
from src.core.app_state import AppState
from src.openvk.client import OpenVKClient
from src.repositories.settings_repo import SettingsRepository
from src.services.gemini_service import GeminiService

async def fetch_joke_via_api() -> str:
    try:
        print("[Test] Fetching joke from rzhunemogu...")
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
        print(f"[Test] Failed to fetch joke from rzhunemogu: {e}")
        return ""

async def generate_joke_via_gemini(gemini_service) -> str:
    try:
        print("[Test] Generating joke via Gemini...")
        prompt = "Напиши один очень короткий, приличный и смешной анекдот на русском языке. Только сам анекдот, без вступлений и лишних слов."
        resp = await gemini_service.client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        joke = resp.text.strip() if resp.text else ""
        if joke.startswith("```"):
            joke = joke.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
        return joke
    except Exception as e:
        print(f"[Test] Failed to generate joke via Gemini: {e}")
        return "Улыбнись! Желаю отличного настроения! 😊"

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
    
    gemini_service = GeminiService(app_state)
    
    print(f"Bot user_id: {client.user_id}")
    print(f"OpenVK Instance: {client.instance_url}")
    
    # 1. Получаем анекдот
    print("\n--- Getting Joke ---")
    joke = await fetch_joke_via_api()
    if not joke:
        joke = await generate_joke_via_gemini(gemini_service)
        
    print(f"Resulting Joke:\n{joke}\n")
    
    # 2. Выбираем подарок
    gift_ids = [1, 3, 4, 14, 27, 30, 46, 62, 102]
    gift_id = random.choice(gift_ids)
    print(f"Selected random Gift ID: {gift_id}")
    
    # 3. Отправляем самому себе (боту)
    print("\n--- Sending Gift to Self (ID 43657) ---")
    try:
        res = await client.call_method("gifts.send", {
            "user_ids": str(client.user_id),
            "gift_id": gift_id,
            "message": joke
        })
        print("Send Gift Response:", res)
    except Exception as e:
        print("Failed to send gift:", e)
        
    await app_state.http_client.aclose()
    await app_state.redis.close()

if __name__ == "__main__":
    asyncio.run(test_gifts())
