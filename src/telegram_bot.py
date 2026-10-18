import html
import asyncio
import logging
from typing import List, Optional, Callable, Awaitable, Dict, Any, Union
import httpx

from src.models import KworkProject, TelegramConfig, FilterConfig

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Telegram bot for sending project notifications, handling commands and generating AI proposals.
    """

    def __init__(
        self,
        config: TelegramConfig,
        filter_config: Optional[FilterConfig] = None,
        storage: Optional[Any] = None,
        parser: Optional[Any] = None,
        profi_parser: Optional[Any] = None,
        ai_assistant: Optional[Any] = None,
        on_manual_check: Optional[Callable[[], Awaitable[None]]] = None,
        get_status_info: Optional[Callable[[], Dict[str, Any]]] = None,
        proxy: Optional[str] = None,
    ):
        self.config = config
        self.filter_config = filter_config or FilterConfig()
        self.storage = storage
        self.parser = parser
        self.profi_parser = profi_parser
        self.ai_assistant = ai_assistant
        self.on_manual_check = on_manual_check
        self.get_status_info = get_status_info
        self.proxy = proxy
        self.api_url = f"https://api.telegram.org/bot{self.config.bot_token}"
        self._last_update_id = 0
        self._running = False

    def is_configured(self) -> bool:
        return bool(self.config.bot_token and self.config.bot_token.strip() != "YOUR_BOT_TOKEN_HERE")

    def _format_project_message(self, project: KworkProject, include_attachments: bool = True) -> str:
        """Formats project data into Telegram HTML message with full text and attachments."""
        title_safe = html.escape(project.title)
        cat_name = project.category_name or f"ID {project.category_id}"
        
        # Full project description (preserving paragraphs, not truncated)
        desc_preview = ""
        if project.description and project.description.strip():
            clean_desc = project.description.strip()
            desc_preview = f"\n\n📝 <b>Полное описание:</b>\n<i>{html.escape(clean_desc)}</i>"

        client_info = ""
        if project.client_name:
            client_hired = f" (нанято {project.client_hired_percent}%)" if project.client_hired_percent else ""
            if project.client_url:
                client_info = f"\n👤 <b>Заказчик:</b> <a href=\"{project.client_url}\">{html.escape(project.client_name)}</a>{client_hired}"
            else:
                client_info = f"\n👤 <b>Заказчик:</b> {html.escape(project.client_name)}{client_hired}"

        time_info = f"\n⏳ <b>Осталось:</b> {html.escape(project.time_left)}" if project.time_left else ""
        offers_badge = "🔥 Без предложений" if project.offers_count == 0 else f"{project.offers_count} предл."

        files_info = ""
        if project.files:
            file_names = ", ".join(html.escape(f.name) for f in project.files[:3])
            more = f" и еще {len(project.files) - 3}" if len(project.files) > 3 else ""
            files_info = f"\n📎 <b>Вложения:</b> {len(project.files)} шт. (<i>{file_names}{more}</i>)"

        att_text_block = ""
        if include_attachments and project.attachments_text and project.attachments_text.strip():
            att_text_block = f"\n\n📄 <b>Текст прикрепленных файлов / ТЗ:</b>\n<pre>{html.escape(project.attachments_text.strip())}</pre>"

        platform_name = "Profi.ru" if getattr(project, "source", "kwork") == "profi" else "Kwork"
        platform_emoji = "🔴" if getattr(project, "source", "kwork") == "profi" else "🟢"

        resp_price_line = ""
        if getattr(project, "response_price", None):
            resp_price_line = f"🏷 <b>Цена отклика:</b> {html.escape(project.response_price)}\n"

        message = (
            f"{platform_emoji} <b>Биржа: {platform_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💼 <b><a href=\"{project.url}\">{title_safe}</a></b>\n\n"
            f"💰 <b>Бюджет:</b> {project.display_price}\n"
            f"{resp_price_line}"
            f"📂 <b>Категория:</b> {cat_name}\n"
            f"📬 <b>Откликов:</b> {offers_badge}"
            f"{client_info}"
            f"{files_info}"
            f"{time_info}"
            f"{desc_preview}"
            f"{att_text_block}"
        )
        return message

    def _get_project_inline_keyboard(self, project: KworkProject) -> Dict[str, Any]:
        """Builds action buttons for project card."""
        platform_name = "Profi.ru" if getattr(project, "source", "kwork") == "profi" else "Kwork"
        buttons = [
            [
                {"text": f"🔗 Открыть проект на {platform_name}", "url": project.url}
            ]
        ]
        if self.ai_assistant and self.ai_assistant.is_available():
            buttons.extend([
                [
                    {"text": "⚡ Отклик (Оптимальный)", "callback_data": f"prop:optimal:{project.id}"},
                    {"text": "📉 Демпинг (на отзывы)", "callback_data": f"prop:dumping:{project.id}"},
                ],
                [
                    {"text": "💼 По бюджету (Эксперт)", "callback_data": f"prop:expert:{project.id}"},
                ]
            ])
        return {"inline_keyboard": buttons}

    async def send_project_notification(self, project: KworkProject) -> None:
        """Sends new project alert to all configured chat IDs and active subscribers."""
        if not self.is_configured():
            logger.warning("Telegram Bot Token не настроен. Уведомление пропущено.")
            return

        recipients = set(self.config.chat_ids)
        if self.storage:
            recipients.update(self.storage.get_subscribers())
        recipients = {cid for cid in recipients if cid and cid != 0}

        if not recipients:
            logger.warning("Нет активных получателей уведомлений (chat_ids пуст). Отправьте /start боту в Telegram.")
            return

        # Pre-fetch attached files text automatically so it's always included in the alert and cached
        if self.parser and project.files and not project.attachments_text:
            try:
                project.attachments_text = await self.parser.read_project_attachments(project)
            except Exception as e:
                logger.warning(f"Ошибка загрузки вложений для проекта #{project.id}: {e}")

        # Update cache in SQLite with extracted attachments
        if self.storage:
            self.storage.mark_seen(project, notified=True)

        full_text = self._format_project_message(project, include_attachments=True)
        inline_keyboard = self._get_project_inline_keyboard(project)

        # Telegram limit is 4096 chars. If message fits into single message, send directly.
        if len(full_text) <= 3900:
            for chat_id in recipients:
                await self.send_message(
                    chat_id,
                    full_text,
                    reply_markup=inline_keyboard,
                    parse_mode="HTML",
                    retries=3,
                )
                await asyncio.sleep(0.05)
        else:
            # If full text exceeds Telegram limit, send main card with full description,
            # and send attachments text in follow-up message(s)
            card_text = self._format_project_message(project, include_attachments=False)
            if len(card_text) > 3900:
                card_text = card_text[:3850] + "...\n[описание продолжается]</i>"

            for chat_id in recipients:
                msg_id = await self.send_message(
                    chat_id,
                    card_text,
                    reply_markup=inline_keyboard,
                    parse_mode="HTML",
                    retries=3,
                )
                if project.attachments_text and project.attachments_text.strip():
                    att_body = project.attachments_text.strip()
                    chunk_size = 3500
                    for chunk_idx in range(0, len(att_body), chunk_size):
                        part = att_body[chunk_idx : chunk_idx + chunk_size]
                        part_num = f" (часть {chunk_idx // chunk_size + 1})" if len(att_body) > chunk_size else ""
                        att_msg = (
                            f"📎 <b>Вложения к проекту #{project.id}{part_num}:</b>\n"
                            f"<pre>{html.escape(part)}</pre>"
                        )
                        await self.send_message(
                            chat_id,
                            att_msg,
                            reply_to_message_id=msg_id,
                            parse_mode="HTML",
                            retries=3,
                        )
                        await asyncio.sleep(0.1)
                await asyncio.sleep(0.05)


    @staticmethod
    def get_main_reply_keyboard() -> Dict[str, Any]:
        """Returns persistent reply keyboard located under the chat text input."""
        return {
            "keyboard": [
                [
                    {"text": "📋 Заказы Kwork"},
                    {"text": "📋 Заказы Profi.ru"},
                ],
                [
                    {"text": "🔎 Проверить Kwork"},
                    {"text": "🔎 Проверить Profi.ru"},
                ],
                [
                    {"text": "📊 Статус"},
                    {"text": "🎯 Фильтры"},
                ],
                [
                    {"text": "💼 Портфолио"},
                    {"text": "ℹ️ Справка"},
                ],
                [
                    {"text": "🛑 Пауза/Стоп"},
                ],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    async def send_recent_projects(self, chat_id: int, limit: int = 10, source: Optional[str] = None) -> None:
        """Sends the last N projects with full cards and AI proposal generation buttons."""
        projects: List[KworkProject] = []
        if self.storage:
            projects = self.storage.get_recent_projects(limit=limit, source=source)

        # If not enough cached in DB and source is kwork (or all), fetch live from Kwork
        if len(projects) < limit and self.parser and source in (None, "kwork"):
            cat_id = self.filter_config.categories[0] if self.filter_config.categories else None
            live_projects = await self.parser.fetch_projects(category_id=cat_id)
            if self.storage:
                for lp in live_projects:
                    self.storage.mark_seen(lp, notified=True)
            seen_ids = {p.id for p in projects}
            for lp in live_projects:
                if lp.id not in seen_ids:
                    projects.append(lp)
        # If not enough cached in DB and source is profi (or all), fetch live from Profi.ru
        if len(projects) < limit and self.profi_parser and source in (None, "profi"):
            try:
                live_profi = await self.profi_parser.fetch_orders()
                if self.storage:
                    for lp in live_profi:
                        self.storage.mark_seen(lp, notified=False)
                seen_ids = {p.id for p in projects}
                for lp in live_profi:
                    if lp.id not in seen_ids:
                        projects.append(lp)
                    if len(projects) >= limit:
                        break
            except Exception as e:
                logger.warning(f"Ошибка получения свежих заказов Profi.ru для списка: {e}")

        platform_label = "Profi.ru" if source == "profi" else ("Kwork" if source == "kwork" else "всех бирж")
        if not projects:
            btn_hint = f"«🔎 Проверить {platform_label}»" if source else "«🔎 Проверить Kwork» или «🔎 Проверить Profi.ru»"
            await self.send_message(
                chat_id,
                f"📭 <b>Список заказов ({platform_label}) пока пуст.</b>\nЗапустите поиск кнопкой {btn_hint}.",
            )
            return

        projects = projects[:limit]
        await self.send_message(
            chat_id,
            f"📋 <b>Последние заказы ({platform_label}):</b>\n"
            "<i>(Используйте кнопки под карточкой заказа для подготовки отклика)</i>",
        )

        for proj in projects:
            text = self._format_project_message(proj)
            keyboard = self._get_project_inline_keyboard(proj)
            await self.send_message(
                chat_id,
                text,
                reply_markup=keyboard,
            )
            await asyncio.sleep(0.35)


    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        reply_to_message_id: Optional[int] = None,
        parse_mode: Optional[str] = "HTML",
        retries: int = 3,
    ) -> Optional[int]:
        """Sends a plain or formatted message to a specific chat ID with retries and fallback."""
        if not self.is_configured():
            return None

        # Default to main reply keyboard for user-facing command responses
        if reply_markup is None:
            reply_markup = self.get_main_reply_keyboard()

        timeout = httpx.Timeout(25.0, connect=10.0)
        async with httpx.AsyncClient(proxy=self.proxy, timeout=timeout) as client:
            current_parse_mode = parse_mode
            for attempt in range(1, retries + 1):
                payload: Dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                }
                if current_parse_mode:
                    payload["parse_mode"] = current_parse_mode
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                if reply_to_message_id:
                    payload["reply_to_message_id"] = reply_to_message_id

                try:
                    resp = await client.post(f"{self.api_url}/sendMessage", json=payload)
                    if resp.status_code == 200:
                        return resp.json().get("result", {}).get("message_id")

                    # Handle 429 Too Many Requests
                    if resp.status_code == 429:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
                        logger.warning(f"Telegram 429 rate limit (chat_id={chat_id}). Ожидание {retry_after}s...")
                        await asyncio.sleep(retry_after)
                        continue

                    # Handle 400 Bad Request with entity parsing errors -> retry as plain text
                    if resp.status_code == 400 and current_parse_mode:
                        err_text = resp.text
                        if "can't parse entities" in err_text.lower() or "entity" in err_text.lower():
                            logger.warning(f"Сбой парсинга HTML в Telegram, повтор без parse_mode: {err_text}")
                            current_parse_mode = None
                            continue

                    logger.error(f"Ошибка отправки сообщения (попытка {attempt}/{retries}, chat_id={chat_id}): {resp.status_code} {resp.text}")
                    if attempt < retries:
                        await asyncio.sleep(1.0 * attempt)

                except (httpx.TimeoutException, httpx.TransportError) as e:
                    err_name = type(e).__name__
                    logger.warning(
                        f"Сетевой сбой при отправке сообщения в Telegram ({err_name}: {e or 'сетевой таймаут/разрыв соединения'}) "
                        f"[попытка {attempt}/{retries}, chat_id={chat_id}]"
                    )
                    if attempt < retries:
                        await asyncio.sleep(1.5 * attempt)
                    else:
                        logger.error(f"Не удалось отправить сообщение в Telegram после {retries} попыток (chat_id={chat_id}): {err_name}")
                except Exception as e:
                    logger.error(f"Непредвиденный сбой отправки сообщения (chat_id={chat_id}): {type(e).__name__}: {e}", exc_info=True)
                    break
        return None

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: Optional[str] = "HTML",
        retries: int = 3,
    ) -> bool:
        """Edits an existing Telegram message in place with retries and fallback."""
        if not self.is_configured():
            return False

        timeout = httpx.Timeout(25.0, connect=10.0)
        async with httpx.AsyncClient(proxy=self.proxy, timeout=timeout) as client:
            current_parse_mode = parse_mode
            for attempt in range(1, retries + 1):
                payload: Dict[str, Any] = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "disable_web_page_preview": True,
                }
                if current_parse_mode:
                    payload["parse_mode"] = current_parse_mode
                if reply_markup:
                    payload["reply_markup"] = reply_markup

                try:
                    resp = await client.post(f"{self.api_url}/editMessageText", json=payload)
                    if resp.status_code == 200:
                        return True

                    # Message is not modified is considered a success
                    if resp.status_code == 400 and "message is not modified" in resp.text.lower():
                        return True

                    if resp.status_code == 429:
                        retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
                        logger.warning(f"Telegram 429 rate limit в editMessageText. Ожидание {retry_after}s...")
                        await asyncio.sleep(retry_after)
                        continue

                    # Fallback on entity parsing failure
                    if resp.status_code == 400 and current_parse_mode:
                        err_text = resp.text
                        if "can't parse entities" in err_text.lower() or "entity" in err_text.lower():
                            logger.warning(f"Сбой парсинга HTML в editMessageText, повтор без parse_mode: {err_text}")
                            current_parse_mode = None
                            continue

                    logger.error(f"Ошибка editMessageText (попытка {attempt}/{retries}, chat_id={chat_id}): {resp.status_code} {resp.text}")
                    if attempt < retries:
                        await asyncio.sleep(1.0 * attempt)

                except (httpx.TimeoutException, httpx.TransportError) as e:
                    err_name = type(e).__name__
                    logger.warning(
                        f"Сетевой сбой в editMessageText ({err_name}: {e or 'сетевой таймаут/разрыв соединения'}) "
                        f"[попытка {attempt}/{retries}, chat_id={chat_id}]"
                    )
                    if attempt < retries:
                        await asyncio.sleep(1.5 * attempt)
                    else:
                        logger.error(f"Не удалось отредактировать сообщение в Telegram после {retries} попыток (chat_id={chat_id}): {err_name}")
                except Exception as e:
                    logger.error(f"Непредвиденный сбой editMessageText: {type(e).__name__}: {e}", exc_info=True)
                    break
        return False

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> None:
        """Acknowledges a callback button click in Telegram."""
        if not self.is_configured():
            return
        async with httpx.AsyncClient(proxy=self.proxy, timeout=10.0) as client:
            payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
                payload["show_alert"] = show_alert
            try:
                await client.post(f"{self.api_url}/answerCallbackQuery", json=payload)
            except Exception as e:
                logger.debug(f"Ошибка answerCallbackQuery: {e}")

    async def handle_proposal_generation(
        self,
        chat_id: int,
        project_id: Union[int, str],
        mode: str = "optimal",
        reply_to_msg_id: Optional[int] = None,
        force_regenerate: bool = False,
    ) -> None:
        """Handles proposal generation flow with DB caching and resilient delivery."""
        if not self.ai_assistant or not self.ai_assistant.is_available():
            await self.send_message(
                chat_id,
                "⚠️ <b>ИИ-генератор недоступен.</b>\n"
                "Генерация откликов без подключенного ИИ отключена.\n"
                "Задайте <code>AI_API_KEY</code> и <code>AI_BASE_URL</code> в файле <code>.env</code>.",
                reply_to_message_id=reply_to_msg_id,
            )
            return

        # 1. Retrieve project details (DB cache -> Live fetch)
        project: Optional[KworkProject] = None
        if self.storage:
            project = self.storage.get_project(project_id)

        if not project and self.parser:
            if isinstance(project_id, int) or (isinstance(project_id, str) and project_id.isdigit()):
                project = await self.parser.fetch_project_by_id(int(project_id))
                if project and self.storage:
                    self.storage.mark_seen(project, notified=True)

        if not project:
            await self.send_message(
                chat_id,
                f"❌ <b>Проект #{project_id} не найден.</b>\nВозможно, он был архивирован или удален.",
                reply_to_message_id=reply_to_msg_id,
            )
            return

        # Check cached proposal to prevent repeated billing on network errors or double clicks
        cached_proposal: Optional[Dict[str, Any]] = None
        if not force_regenerate and self.storage:
            cached_proposal = self.storage.get_cached_proposal(project.id, mode)

        status_msg_id: Optional[int] = None
        proposal: Optional[Dict[str, Any]] = None

        if cached_proposal:
            proposal = cached_proposal
            logger.info(f"Отклик для проекта #{project.id} ({mode}) загружен из кэша (без расхода API).")
        else:
            # Notify progress when initiating AI call
            action_title = "Перегенерирую" if force_regenerate else "Анализирую"
            status_msg_id = await self.send_message(
                chat_id,
                f"⏳ <i>{action_title} проект <b>«{html.escape(project.title)}»</b>, профиль заказчика и ТЗ...</i>",
                reply_to_message_id=reply_to_msg_id,
            )

            # Fetch client profile & read attached files concurrently
            client_profile = None
            attachments_text = ""

            is_kwork = getattr(project, "source", "kwork") == "kwork"
            profile_task = (
                self.parser.fetch_client_profile(project.client_name)
                if (self.parser and project.client_name and is_kwork)
                else asyncio.sleep(0, result=None)
            )
            if project.attachments_text and project.attachments_text.strip():
                att_task = asyncio.sleep(0, result=project.attachments_text.strip())
            else:
                att_task = (
                    self.parser.read_project_attachments(project)
                    if (self.parser and project.files and is_kwork)
                    else asyncio.sleep(0, result="")
                )

            results = await asyncio.gather(profile_task, att_task, return_exceptions=True)
            if len(results) > 0 and not isinstance(results[0], Exception):
                client_profile = results[0]
                if client_profile and not project.client_profile:
                    project.client_profile = client_profile
            if len(results) > 1 and not isinstance(results[1], Exception):
                attachments_text = results[1] or ""

            # Generate proposal via AI
            try:
                proposal = await self.ai_assistant.generate_proposal(
                    project=project,
                    mode=mode,
                    attachments_text=attachments_text,
                    client_profile=client_profile,
                )
            except Exception as e:
                logger.error(f"Ошибка вызова ИИ для проекта #{project.id}: {e}")
                err_text = (
                    f"❌ <b>Не удалось сгенерировать отклик через ИИ:</b>\n"
                    f"<code>{html.escape(str(e))}</code>\n\n"
                    "<i>Генерация шаблонного текста без ИИ отключена. Проверьте статус ИИ-сервера и повторите попытку.</i>"
                )
                if status_msg_id:
                    await self.edit_message_text(chat_id, status_msg_id, err_text)
                else:
                    await self.send_message(chat_id, err_text, reply_to_message_id=reply_to_msg_id)
                return

            # CRITICAL: Save to DB immediately after generation before sending to Telegram
            if self.storage and proposal:
                self.storage.save_proposal(project.id, mode, proposal)

        price = proposal["suggested_price"]
        days = proposal["suggested_days"]
        proposal_text = proposal["proposal_text"]
        mode_title = proposal.get("mode_name", mode.capitalize())

        client_note = ""
        cp = project.client_profile
        if cp:
            parts = []
            if cp.full_name:
                parts.append(html.escape(cp.full_name))
            if cp.profession:
                parts.append(html.escape(cp.profession))
            if cp.description:
                parts.append("есть «О себе»")
            if parts:
                client_note = f"\n👤 <i>Профиль заказчика: {', '.join(parts)}</i>"

        att_note = ""
        if project.files:
            att_note = f"\n📎 <i>Учтено вложений ТЗ: {len(project.files)} шт.</i>"

        cache_hint = " 💾" if proposal.get("is_cached") else ""

        platform_name = "Profi.ru" if getattr(project, "source", "kwork") == "profi" else "Kwork"
        platform_emoji = "🔴" if getattr(project, "source", "kwork") == "profi" else "🟢"

        resp_price_note = ""
        if getattr(project, "response_price", None):
            resp_price_note = f"\n🏷 <i>Цена отклика: {html.escape(project.response_price)}</i>"

        reply_text = (
            f"{platform_emoji} <b>Биржа: {platform_name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🤖 <b>Пример отклика (Режим: {html.escape(mode_title)}){cache_hint}</b>\n"
            f"💼 Проект: <b><a href=\"{project.url}\">{html.escape(project.title)}</a></b>\n"
            f"💰 <b>Рекомендуемая цена:</b> {price:,} ₽  |  ⏱ <b>Срок:</b> {days} дн."
            f"{resp_price_note}"
            f"{client_note}"
            f"{att_note}\n\n"
            f"📋 <b>Текст для отправки:</b>\n"
            f"<code>{html.escape(proposal_text)}</code>\n\n"
            f"<i>Нажмите на блок с текстом выше, чтобы скопировать его и вставить в отклик на {platform_name}.</i>"
        )

        # Inline buttons under proposal
        switch_mode = "optimal" if mode == "dumping" else "dumping"
        switch_title = "⚡ В оптимальный" if mode == "dumping" else "📉 В демпинг"

        proposal_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🔄 Перегенерировать", "callback_data": f"prop:{mode}:{project.id}:regen"},
                    {"text": switch_title, "callback_data": f"prop:{switch_mode}:{project.id}"},
                ],
                [
                    {"text": "💼 В экспертный", "callback_data": f"prop:expert:{project.id}"},
                    {"text": f"🔗 Открыть проект на {platform_name}", "url": project.url},
                ]
            ]
        }

        # Deliver message: edit in place if we have status_msg_id, otherwise send new message
        delivered = False
        if status_msg_id:
            delivered = await self.edit_message_text(
                chat_id,
                status_msg_id,
                reply_text,
                reply_markup=proposal_keyboard,
            )

        if not delivered:
            await self.send_message(
                chat_id,
                reply_text,
                reply_markup=proposal_keyboard,
                reply_to_message_id=reply_to_msg_id,
            )

    async def handle_callback_query(self, callback_query: Dict[str, Any]) -> None:
        """Processes inline button callback events."""
        cq_id = callback_query.get("id", "")
        data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        msg_id = message.get("message_id")

        if not chat_id or not data:
            return

        # Pattern: prop:<mode>:<project_id>[:regen]
        if data.startswith("prop:"):
            parts = data.split(":")
            if len(parts) >= 3:
                mode = parts[1]
                force_regen = (len(parts) >= 4 and parts[3] == "regen")
                try:
                    raw_id = parts[2]
                    project_id: Union[int, str] = int(raw_id) if raw_id.isdigit() else raw_id
                    ans_text = f"Перегенерирую отклик ({mode})..." if force_regen else f"Загружаю отклик ({mode})..."
                    await self.answer_callback_query(
                        cq_id,
                        ans_text,
                    )
                    # Run generation
                    asyncio.create_task(
                        self.handle_proposal_generation(
                            chat_id=chat_id,
                            project_id=project_id,
                            mode=mode,
                            reply_to_msg_id=msg_id,
                            force_regenerate=force_regen,
                        )
                    )
                except Exception:
                    await self.answer_callback_query(cq_id, "Ошибка ID проекта")
            return

        await self.answer_callback_query(cq_id)

    async def handle_command(self, chat_id: int, command: str) -> None:
        """Processes incoming bot commands and reply keyboard button clicks."""
        clean_text = command.strip()
        if not clean_text:
            return

        cmd_parts = clean_text.split()
        cmd_lower = clean_text.lower()
        cmd_first = cmd_parts[0].lower()

        # 1. Start / Subscribe
        if cmd_first.startswith("/start") or "возобновить" in cmd_lower:
            if self.storage:
                self.storage.add_subscriber(chat_id)
            if chat_id not in self.config.chat_ids:
                self.config.chat_ids.append(chat_id)

            msg = (
                "👋 <b>Добро пожаловать в бот мониторинга бирж (Kwork & Profi.ru)!</b>\n\n"
                "✅ <b>Вы успешно подписаны на уведомления о новых заказах!</b>\n"
                "Мониторинг бирж: <b>Kwork</b> (Разработка и IT) и <b>Profi.ru</b> (Бэкофис специалиста).\n"
                f"Ваш Chat ID: <code>{chat_id}</code> сохранён в базу данных.\n\n"
                "🤖 <b>Интеграция с ИИ для автооткликов активна:</b>\n"
                "Под каждой карточкой заказа доступны кнопки генерации отклика по 3 режимам:\n"
                "• <b>Оптимальный</b> — сбалансированная ставка под вилку заказчика\n"
                "• <b>Демпинг</b> — минимальная ставка для набора отзывов и рейтинга\n"
                "• <b>Эксперт</b> — по верхней планке бюджета заказчика\n"
                "ИИ автоматически читает приложенные ТЗ (.docx, .pdf, .txt) и портфолио.\n\n"
                "⌨️ <i>Для быстрого управления используйте кнопки под клавиатурой.</i>"
            )
            await self.send_message(chat_id, msg)

        # 2. Stop / Unsubscribe
        elif cmd_first.startswith("/stop") or "стоп" in cmd_lower or "пауза" in cmd_lower or "отписаться" in cmd_lower:
            if self.storage:
                self.storage.remove_subscriber(chat_id)
            if chat_id in self.config.chat_ids:
                self.config.chat_ids.remove(chat_id)
            await self.send_message(
                chat_id,
                "🛑 <b>Вы отписались от уведомлений.</b>\nЧтобы возобновить получение заказов, нажмите кнопку или отправьте /start."
            )

        # 3. Status
        elif cmd_first.startswith("/status") or "статус" in cmd_lower:
            stats = self.get_status_info() if self.get_status_info else {}
            uptime = stats.get("uptime", "Неизвестно")
            last_check = stats.get("last_check", "Ещё не выполнялась")
            seen_today = stats.get("seen_today", 0)
            total_seen = stats.get("total_seen", 0)
            total_notified = stats.get("total_notified", 0)
            ai_status = "Включен" if (self.ai_assistant and self.ai_assistant.is_available()) else "Выключен"

            profi_total = stats.get("profi_count", 0)
            kwork_total = stats.get("kwork_count", total_seen - profi_total)
            source_breakdown = f" (Kwork: {kwork_total}, Profi.ru: {profi_total})" if profi_total > 0 else ""

            msg = (
                "📊 <b>Статус Kwork & Profi.ru Parser:</b>\n\n"
                f"⏱ <b>Аптайм:</b> {uptime}\n"
                f"🕒 <b>Последняя проверка:</b> {last_check}\n"
                f"📅 <b>Новых заказов за сегодня:</b> {seen_today}\n"
                f"📩 <b>Всего отправлено уведомлений:</b> {total_notified}\n"
                f"📦 <b>Всего обработано заказов:</b> {total_seen}{source_breakdown}\n"
                f"🤖 <b>ИИ-генератор:</b> {ai_status} (модель: {getattr(self.ai_assistant.config, 'model', 'н/д') if self.ai_assistant else 'н/д'})"
            )
            await self.send_message(chat_id, msg)

        # 4. Filters
        elif cmd_first.startswith("/filters") or "фильтр" in cmd_lower:
            cats = ", ".join(map(str, self.filter_config.categories)) or "Все категории"
            kws = ", ".join(f"'{k}'" for k in self.filter_config.keywords) or "Не заданы (все)"
            stops = ", ".join(f"'{w}'" for w in self.filter_config.stop_words) or "Не заданы"
            min_p = f"{int(self.filter_config.min_price):,} ₽" if self.filter_config.min_price else "0 ₽"
            max_p = f"{int(self.filter_config.max_price):,} ₽" if self.filter_config.max_price else "Без ограничения"
            max_off = str(self.filter_config.max_offers) if self.filter_config.max_offers is not None else "Без ограничения"

            msg = (
                "🎯 <b>Текущие фильтры поиска:</b>\n\n"
                f"🟢 <b>Категории ID (Kwork):</b> {cats}\n"
                f"🔴 <b>Profi.ru:</b> Лента бэкофиса специалиста\n"
                f"🔍 <b>Ключевые слова:</b> {kws}\n"
                f"⛔ <b>Стоп-слова:</b> {stops}\n"
                f"💰 <b>Мин. бюджет:</b> {min_p}\n"
                f"💵 <b>Макс. бюджет:</b> {max_p}\n"
                f"📬 <b>Макс. откликов:</b> {max_off}"
            )
            await self.send_message(chat_id, msg)

        # 5. Check commands
        elif "проверить profi" in cmd_lower or cmd_first == "/check_profi":
            await self.send_message(chat_id, "🔎 <i>Запускаю проверку бэкофиса Profi.ru...</i>")
            if self.on_manual_check:
                asyncio.create_task(self.on_manual_check("profi"))

        elif "проверить kwork" in cmd_lower or cmd_first == "/check_kwork":
            await self.send_message(chat_id, "🔎 <i>Запускаю проверку биржи Kwork...</i>")
            if self.on_manual_check:
                asyncio.create_task(self.on_manual_check("kwork"))

        elif cmd_first.startswith("/check") or "проверить" in cmd_lower:
            await self.send_message(chat_id, "🔎 <i>Запускаю внеочередную проверку всех бирж (Kwork и Profi.ru)...</i>")
            if self.on_manual_check:
                asyncio.create_task(self.on_manual_check("all"))

        # 6. Portfolio
        elif cmd_first.startswith("/portfolio") or "портфолио" in cmd_lower:
            if self.ai_assistant:
                text = self.ai_assistant.load_portfolio_context()
                if len(text) > 3000:
                    text = text[:3000] + "... [сокращено]"
                await self.send_message(
                    chat_id,
                    f"💼 <b>Текущий контекст портфолио (<code>{self.ai_assistant.config.context_file}</code>):</b>\n\n"
                    f"<pre>{html.escape(text)}</pre>\n\n"
                    "<i>Файл можно редактировать на сервере/диске без перезапуска бота.</i>"
                )
            else:
                await self.send_message(chat_id, "⚠️ ИИ-ассистент не настроен.")

        # 7. Recent projects
        elif "заказы profi" in cmd_lower or cmd_first == "/recent_profi":
            asyncio.create_task(self.send_recent_projects(chat_id, limit=10, source="profi"))

        elif "заказы kwork" in cmd_lower or cmd_first == "/recent_kwork":
            asyncio.create_task(self.send_recent_projects(chat_id, limit=10, source="kwork"))

        elif (
            cmd_first.startswith("/recent")
            or cmd_first.startswith("/last")
            or "последн" in cmd_lower
            or "10 заказ" in cmd_lower
            or "заказы" in cmd_lower
        ):
            asyncio.create_task(self.send_recent_projects(chat_id, limit=10, source=None))

        # 8. Proposal on demand
        elif cmd_first.startswith("/proposal") or cmd_first.startswith("/ai"):
            if len(cmd_parts) < 2:
                await self.send_message(
                    chat_id,
                    "ℹ️ <b>Формат команды:</b> <code>/proposal &lt;ID_проекта_или_ссылка&gt; [режим]</code>\n"
                    "Режимы: <code>optimal</code> (по умолчанию), <code>dumping</code>, <code>expert</code>.\n\n"
                    "Пример: <code>/proposal 3256851 dumping</code>"
                )
                return

            raw_arg = cmd_parts[1]
            match = re.search(r"(\d+)", raw_arg)
            if not match:
                await self.send_message(chat_id, "❌ Не удалось распознать ID проекта из аргумента.")
                return

            proj_id = int(match.group(1))
            mode = cmd_parts[2].lower() if len(cmd_parts) > 2 else "optimal"
            if mode not in ("optimal", "dumping", "expert"):
                mode = "optimal"

            asyncio.create_task(
                self.handle_proposal_generation(
                    chat_id=chat_id,
                    project_id=proj_id,
                    mode=mode,
                )
            )

        # 9. Help
        elif cmd_first.startswith("/help") or "справка" in cmd_lower or "помощь" in cmd_lower:
            msg = (
                "ℹ️ <b>Справка по боту мониторинга бирж</b>\n\n"
                "Сервис автоматически опрашивает биржи <b>Kwork</b> и <b>Profi.ru</b>, отбирая подходящие заказы по вашим фильтрам.\n\n"
                "<b>Кнопки быстрого доступа:</b>\n"
                "📋 <b>Заказы Kwork</b> — показать последние 10 проектов с Kwork\n"
                "📋 <b>Заказы Profi.ru</b> — показать последние 10 заказов с Profi.ru\n"
                "🔎 <b>Проверить Kwork</b> — запустить мгновенную проверку Kwork\n"
                "🔎 <b>Проверить Profi.ru</b> — запустить мгновенную проверку Profi.ru\n"
                "📊 <b>Статус</b> — статистика работы сервиса и ИИ\n"
                "🎯 <b>Фильтры</b> — активные критерии отбора\n"
                "💼 <b>Портфолио</b> — просмотр контекста ваших работ\n"
                "ℹ️ <b>Справка</b> — это сообщение\n"
                "🛑 <b>Пауза/Стоп</b> — приостановить уведомления\n\n"
                "<b>Текстовые команды:</b>\n"
                "/start — зарегистрироваться и активировать меню\n"
                "/check — проверить все биржи\n"
                "/check_kwork — проверить только Kwork\n"
                "/check_profi — проверить только Profi.ru\n"
                "/recent — показать последние заказы со всех бирж\n"
                "/proposal &lt;id&gt; [mode] — сгенерировать отклик (optimal, dumping, expert)"
            )
            await self.send_message(chat_id, msg)


        else:
            await self.send_message(
                chat_id,
                "❓ Неизвестная команда. Воспользуйтесь кнопками под клавиатурой или отправьте /help."
            )


    async def start_polling(self) -> None:
        """Long-polling loop for receiving Telegram commands and button callbacks."""
        if not self.is_configured():
            logger.info("Telegram polling пропущен: bot_token не настроен.")
            return

        self._running = True
        logger.info("Telegram bot polling запущен.")

        async with httpx.AsyncClient(proxy=self.proxy, timeout=35.0) as client:
            while self._running:
                try:
                    payload = {
                        "offset": self._last_update_id + 1,
                        "timeout": 20,
                        "allowed_updates": ["message", "callback_query"],
                    }
                    resp = await client.post(f"{self.api_url}/getUpdates", json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        for update in data.get("result", []):
                            self._last_update_id = update["update_id"]
                            
                            # Handle incoming text messages
                            msg = update.get("message")
                            if msg and "text" in msg:
                                chat_id = msg["chat"]["id"]
                                text = msg["text"]
                                await self.handle_command(chat_id, text)

                            # Handle inline button callbacks
                            callback_query = update.get("callback_query")
                            if callback_query:
                                await self.handle_callback_query(callback_query)

                    elif resp.status_code in (401, 404):
                        logger.error(f"Неверный токен Telegram-бота: {resp.text}")
                        break
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.debug(f"Ошибка в Telegram polling: {e}")
                    await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False

