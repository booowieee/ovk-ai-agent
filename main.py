import asyncio
import sys

from aiogram import Bot, Dispatcher
from redis.asyncio import Redis
import httpx

from src.config import settings
from src.utils.logger import logger
from src.core.app_state import AppState
from src.database.connection import init_db

from src.services.gemini_service import GeminiService
from src.openvk.client import OpenVKClient
from src.openvk.responder import OpenVKResponder
from src.openvk.poller import OpenVKPoller
from src.control_bot.handlers.admin import router as admin_router
from src.utils.shutdown import setup_signal_handlers

async def main():
    logger.info(
        "==================================================\n"
        "    Starting OpenVK AI Agent Application\n"
        "=================================================="
    )

    # Инициализация базы данных
    await init_db()

    # Настройка состояния приложения
    app_state = AppState()
    await app_state.init_connections(settings.REDIS_URL)
    app_state.poll_interval = settings.POLL_INTERVAL

    # Инициализация сервисов
    gemini_service = GeminiService(app_state)
    openvk_client = OpenVKClient(
        state=app_state,
        instance_url=settings.OVK_INSTANCE_URL,
        token=settings.OVK_ACCESS_TOKEN,
        user_id=settings.OVK_USER_ID
    )
    openvk_responder = OpenVKResponder(client=openvk_client, redis=app_state.redis)
    openvk_poller = OpenVKPoller(
        state=app_state,
        client=openvk_client,
        responder=openvk_responder,
        gemini_service=gemini_service
    )

    # Диагностика доступных моделей
    try:
        models = [m.name for m in gemini_service.client.models.list()]
        logger.info(f"[Gemini] Available models for this API key: {models}")
    except Exception as e:
        logger.error(f"[Gemini] Failed to list available models: {e}")

    # Инициализация Telegram-бота управления
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(admin_router)
    
    # Передача Redis в диспетчер для обработчиков
    dp['redis'] = app_state.redis

    # Настройка корректного завершения
    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop, app_state)

    async def run_poller():
        """Запуск поллера с автовосстановлением"""
        while app_state.is_running:
            try:
                await openvk_poller.run()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in OpenVK poller: {e}")
                if app_state.is_running:
                    await asyncio.sleep(5) # backoff

    async def run_telegram_bot():
        """Запуск Telegram-бота с автовосстановлением"""
        while app_state.is_running:
            try:
                await dp.start_polling(bot)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Telegram bot: {e}")
                if app_state.is_running:
                    await asyncio.sleep(5)

    try:
        # Параллельный запуск задач
        await asyncio.gather(
            run_poller(),
            run_telegram_bot()
        )
    except asyncio.CancelledError:
        logger.info("Main tasks cancelled.")
    finally:
        logger.info("Cleaning up resources...")
        app_state.is_running = False
        await app_state.close()
        await bot.session.close()
        logger.info("Resources cleaned up. Shutdown complete.")

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Application execution interrupted by user.")
