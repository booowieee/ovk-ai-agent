import asyncio
import signal
from src.utils.logger import logger


async def graceful_shutdown(state):
    """Корректное завершение работы приложения."""
    logger.info("Initiating graceful shutdown...")
    state.is_running = False
    
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    logger.info(f"Cancelling {len(tasks)} outstanding tasks...")
    for task in tasks:
        task.cancel()
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    if state.http_client:
        await state.http_client.aclose()
        logger.info("HTTP client closed.")
    if state.redis:
        await state.redis.close()
        logger.info("Redis connection closed.")
    
    logger.info("Shutdown complete.")


def setup_signal_handlers(loop, state):
    """Установка обработчиков сигналов для Docker (SIGTERM/SIGINT)."""
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(graceful_shutdown(state))
            )
    except NotImplementedError:
        # Windows не поддерживает add_signal_handler
        pass
