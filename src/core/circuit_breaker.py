import time
from src.utils.logger import logger


class CircuitBreakerOpen(Exception):
    """Исключение: circuit breaker разомкнут."""
    pass


class CircuitBreaker:
    """Простой Circuit Breaker для защиты от каскадных сбоев."""
    
    def __init__(self, name: str, fail_threshold: int = 5, recovery_timeout: int = 60):
        self.name = name
        self.fail_threshold = fail_threshold
        self.recovery_timeout = recovery_timeout
        self.fail_count = 0
        self.is_open = False
        self.last_failure_time = 0.0
    
    def check(self):
        """Проверяет состояние breaker'а перед вызовом."""
        if self.is_open:
            elapsed = time.time() - self.last_failure_time
            if elapsed > self.recovery_timeout:
                logger.info(f"[CircuitBreaker:{self.name}] Attempting recovery (half-open)...")
                self.is_open = False
                self.fail_count = 0
            else:
                raise CircuitBreakerOpen(
                    f"Circuit breaker '{self.name}' is open. "
                    f"Recovery in {self.recovery_timeout - int(elapsed)}s"
                )
    
    def record_success(self):
        """Записывает успешный вызов."""
        self.fail_count = 0
        if self.is_open:
            self.is_open = False
            logger.info(f"[CircuitBreaker:{self.name}] CLOSED (recovered)")
    
    def record_failure(self):
        """Записывает неудачный вызов."""
        self.fail_count += 1
        self.last_failure_time = time.time()
        if self.fail_count >= self.fail_threshold:
            self.is_open = True
            logger.warning(
                f"[CircuitBreaker:{self.name}] OPENED after {self.fail_count} failures. "
                f"Will retry in {self.recovery_timeout}s"
            )
