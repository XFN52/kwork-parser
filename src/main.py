import os
import sys
import time
import random
import signal
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from src.config import load_config
from src.models import AppConfig
from src.parser import KworkParser
from src.profi_parser import ProfiParser, ProfiAuthError
from src.filters import ProjectFilter
from src.storage import ProjectStorage
from src.telegram_bot import TelegramNotifier
from src.ai_assistant import AIAssistant

logger = logging.getLogger("kwork_parser")



def setup_logging(config: AppConfig) -> None:
    """Configures console and file rotating logging."""
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler with rotation
    if config.log_file:
        Path(config.log_file).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.log_file,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


class KworkService:
    """Main service coordinating parser, filter, database and notifications."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.start_time = time.time()
        self.last_check_time: Optional[datetime] = None
        self.is_running = True
        self._lock = asyncio.Lock()

        self.storage = ProjectStorage(config.database_path)
        self.filter = ProjectFilter(config.filters)
        self.parser = KworkParser(proxy=config.proxy)
        self.profi_parser = (
            ProfiParser(
                cookies=config.profi.cookies,
                url=config.profi.url,
                proxy=config.proxy,
                browser_profile_dir=config.profi.browser_profile_dir,
            )
            if config.profi.enabled
            else None
        )
        self.ai_assistant = AIAssistant(config=config.ai, proxy=config.proxy)
        self.notifier = TelegramNotifier(
            config=config.telegram,
            filter_config=config.filters,
            storage=self.storage,
            parser=self.parser,
            profi_parser=self.profi_parser,
            ai_assistant=self.ai_assistant,
            on_manual_check=self.run_single_check,
            get_status_info=self.get_status_info,
            proxy=config.proxy,
        )


    def get_status_info(self) -> Dict[str, Any]:
        uptime_seconds = int(time.time() - self.start_time)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours} ч. {minutes} мин. {seconds} сек."

        stats = self.storage.get_stats()
        stats["uptime"] = uptime_str
        stats["last_check"] = (
            self.last_check_time.strftime("%H:%M:%S (%d.%m.%Y)")
            if self.last_check_time else "Ещё не выполнялась"
        )
        return stats

    async def run_single_check(self, source: Optional[str] = None) -> None:
        """Executes a single pass over configured categories to discover new projects (Kwork, Profi, or both)."""
        async with self._lock:
            self.last_check_time = datetime.now()
            check_kwork = source in (None, "kwork", "all")
            check_profi = source in (None, "profi", "all")

            found_total = 0
            new_matched = 0

            if check_kwork:
                logger.info("Начало проверки биржи Kwork на новые проекты...")
                categories = self.config.filters.categories
                # If no specific categories set, fetch general list (category_id=None)
                target_categories = categories if categories else [None]

                for cat_id in target_categories:
                    projects = await self.parser.fetch_projects(category_id=cat_id)
                    found_total += len(projects)

                    for project in projects:
                        if self.storage.is_seen(project.id):
                            continue

                        # Evaluate project through filter rules
                        is_match, reason = self.filter.matches(project)
                        if is_match:
                            logger.info(f"Новый подходящий проект [{project.id}]: {project.title} ({project.display_price})")
                            await self.notifier.send_project_notification(project)
                            self.storage.mark_seen(project, notified=True)
                            new_matched += 1
                        else:
                            logger.debug(f"Проект [{project.id}] отклонён фильтром: {reason}")
                            self.storage.mark_seen(project, notified=False)

                    # Small delay between category requests to avoid bursting
                    if len(target_categories) > 1:
                        await asyncio.sleep(2)

            # Check Profi.ru if enabled
            if check_profi and self.profi_parser and self.config.profi.enabled:
                logger.info("Начало проверки бэкофиса Profi.ru на новые заказы...")
                try:
                    profi_orders = await self.profi_parser.fetch_orders()
                    found_total += len(profi_orders)
                    for project in profi_orders:
                        if self.storage.is_seen(project.id):
                            continue

                        is_match, reason = self.filter.matches(project)
                        if is_match:
                            logger.info(f"Новый подходящий заказ Profi [{project.id}]: {project.title} ({project.display_price})")
                            await self.notifier.send_project_notification(project)
                            self.storage.mark_seen(project, notified=True)
                            new_matched += 1
                        else:
                            logger.debug(f"Заказ Profi [{project.id}] отклонён фильтром: {reason}")
                            self.storage.mark_seen(project, notified=False)
                except ProfiAuthError as e:
                    logger.error(f"Ошибка авторизации Profi.ru: {e}")
                except Exception as e:
                    logger.error(f"Ошибка при проверке заказов Profi.ru: {e}", exc_info=True)

            logger.info(
                f"Проверка завершена. Получено: {found_total} заказов, "
                f"новых подходящих: {new_matched}."
            )

    async def run_scheduler(self) -> None:
        """Continuous background loop for checking Kwork."""
        logger.info(
            f"Запущен планировщик Kwork Parser. Интервал: {self.config.poll_interval_seconds}s "
            f"(±{self.config.poll_jitter_seconds}s jitter)."
        )
        # Run cleanup once on startup
        self.storage.cleanup_old_records()

        while self.is_running:
            try:
                await self.run_single_check()
            except Exception as e:
                logger.error(f"Ошибка в цикле сканирования: {e}", exc_info=True)

            jitter = random.uniform(
                -self.config.poll_jitter_seconds,
                self.config.poll_jitter_seconds
            )
            sleep_duration = max(10.0, self.config.poll_interval_seconds + jitter)
            logger.debug(f"Следующая проверка через {sleep_duration:.1f} сек.")
            
            try:
                await asyncio.sleep(sleep_duration)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        """Starts both background scanner and Telegram bot updates handler."""
        tasks = [
            asyncio.create_task(self.run_scheduler()),
            asyncio.create_task(self.notifier.start_polling()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        self.is_running = False
        self.notifier.stop()
        logger.info("Сервис Kwork Parser остановлен.")


async def main() -> None:
    config = load_config()
    setup_logging(config)

    logger.info("Инициализация Kwork Parser...")
    service = KworkService(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, service.stop)
        except NotImplementedError:
            # Signal handlers not fully supported on Windows event loops
            pass

    try:
        await service.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        service.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
