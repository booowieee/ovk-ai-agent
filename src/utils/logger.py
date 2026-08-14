import logging
import sys
import os

def setup_logger(name: str = "ovk_agent") -> logging.Logger:
    """Настройка структурированного логирования."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    if not logger.handlers:
        # 1. Вывод в консоль
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # 2. Вывод в файл (монтируется на хост, выживает при пересоздании контейнеров)
        try:
            log_dir = "/app/logs"
            if os.path.exists(log_dir) or os.path.exists("./logs"):
                target_dir = log_dir if os.path.exists(log_dir) else "./logs"
                log_file = os.path.join(target_dir, "bot.log")
                
                from logging.handlers import RotatingFileHandler
                file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
                file_handler.setLevel(logging.INFO)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
        except Exception as e:
            print(f"Warning: Failed to setup file logger: {e}", file=sys.stderr)
    
    return logger


logger = setup_logger()
