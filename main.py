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

    # Initialize DB
    await init_db()

    # Initialize external clients
    http_client = httpx.AsyncClient()
    redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)

    # Setup application state
    app_state = AppState(
        is_running=True,
        http_client=http_client,
        redis=redis_client,
        gemini_semaphore=asyncio.Semaphore(3),
        poll_interval=settings.POLL_INTERVAL,
        use_notifications_api=False
    )

    # Initialize services
    gemini_service = GeminiService(app_state)
    openvk_client = OpenVKClient(app_state)
    openvk_responder = OpenVKResponder(app_state, openvk_client, gemini_service)
    openvk_poller = OpenVKPoller(app_state, openvk_client, openvk_responder)

    # Initialize Telegram Control Bot
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(admin_router)
    
    # Inject redis into dispatcher for handlers
    dp['redis'] = redis_client

    # Setup graceful shutdown
    setup_signal_handlers(app_state)

    async def run_poller():
        """Supervised poller execution"""
        while app_state.is_running:
            try:
                await openvk_poller.start_polling()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Ошибка в поллере OpenVK: {e}")
                if app_state.is_running:
                    await asyncio.sleep(5) # backoff

    async def run_telegram_bot():
        """Supervised bot execution"""
        try:
            await dp.start_polling(bot)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Ошибка в Telegram боте: {e}")

    try:
        # Run both tasks concurrently
        await asyncio.gather(
            run_poller(),
            run_telegram_bot()
        )
    except asyncio.CancelledError:
        logger.info("Main tasks cancelled.")
    finally:
        logger.info("Выполняю очистку ресурсов...")
        app_state.is_running = False
        await http_client.aclose()
        await redis_client.aclose()
        await bot.session.close()
        logger.info("Ресурсы очищены. Завершение работы.")

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа приложения прервана пользователем.")
