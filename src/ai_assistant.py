import os
import re
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
import httpx
from openai import AsyncOpenAI

from src.models import KworkProject, AIConfig, KworkClientProfile

logger = logging.getLogger(__name__)


PROPOSAL_SYSTEM_PROMPT = """Ты — опытный разработчик и профессиональный фрилансер на бирже Kwork.ru.
Твоя задача — составить персонализированный, убедительный и лаконичный отклик на заказ клиента.

ПРАВИЛА И СТИЛЬ ОТКЛИКА:
1. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ клише и "вода":
   - НЕ начинай с "Здравствуйте! Меня зовут...", "Я профессионал с 10-летним стажем", "Готов выполнить ваше задание качественно и в срок".
   - С первой же строки покажи, что ты ВНИМАТЕЛЬНО изучил ТЗ и понял суть задачи.
2. РАБОТА С ПРОФИЛЕМ ЗАКАЗЧИКА И ВЛОЖЕНИЯМИ:
   - Если в профиле заказчика указано реальное имя (full_name), обратись к нему вежливо по имени (например, "Здравствуйте, Антон!"). Если имя неизвестно, обратись нейтрально.
   - Если в профиле указана специфика бизнеса («О себе», сайт компании, отрасль), покажи понимание контекста его деятельности.
   - Если к заказу приложены файлы или ссылки (docx, pdf, скриншоты, ТЗ), ОБЯЗАТЕЛЬНО упомяни ключевую конкретную деталь из них (например: конкретный экран, кнопку, формат выгрузки, доработку). Это доказывает заказчику, что ты живой профи, прочитавший вложения.
3. ПОРТФОЛИО И ОПЫТ:
   - Выбери из предоставленного портфолио ТОЛЬКО 1 наиболее релевантный кейс (максимум 2), покажи аналогию с текущей задачей. Не перечисляй весь свой стек подряд.
4. ЭКСПЕРТИЗА И ВОПРОСЫ:
   - Задай 1-2 точных технических вопроса по реализации или уточнения по ТЗ.
5. СТРАТЕГИЯ И РЕЖИМ ЦЕНООБРАЗОВАНИЯ:
   - Строго следуй заданному режиму генерации (Оптимальный, Демпинг для отзывов, или Экспертный).
6. ФОРМАТ ОТВЕТА:
   - Твой ответ должен быть валидным JSON-объектом следующего вида:
   {
     "suggested_price": <число в рублях>,
     "suggested_days": <срок выполнения в днях, целое число>,
     "proposal_text": "<готовый текст отклика для отправки заказчику>"
   }
   Не оборачивай JSON в лишние комментарии вне блока json.
"""


class AIAssistant:
    """
    AI Service for generating targeted Kwork proposals using OpenAI-compatible APIs.
    Supports reading attached order materials and personalized portfolio context.
    """

    def __init__(self, config: AIConfig, proxy: Optional[str] = None):
        self.config = config
        self.proxy = proxy
        self._client: Optional[AsyncOpenAI] = None
        self._init_client()

    def is_available(self) -> bool:
        """Returns True if AI generation is enabled and API client is initialized."""
        return bool(self.config.enabled and self._client is not None)

    def _init_client(self) -> None:
        if not self.config.api_key or not self.config.api_key.strip():
            logger.info("AI API ключ не задан. Генерация откликов через ИИ отключена.")
            self._client = None
            return

        base_url = self.config.base_url.strip().rstrip("/") if self.config.base_url else None
        if base_url and not base_url.endswith("/v1") and "/v1" not in base_url:
            base_url = f"{base_url}/v1"
        
        # Configure httpx client with optional proxy
        http_client = httpx.AsyncClient(
            proxy=self.proxy,
            timeout=httpx.Timeout(60.0, connect=15.0),
        )

        self._client = AsyncOpenAI(
            api_key=self.config.api_key.strip(),
            base_url=base_url,
            http_client=http_client,
        )
        logger.info(f"AI Assistant инициализирован (модель: {self.config.model}, base_url: {base_url or 'api.openai.com'})")

    def load_portfolio_context(self) -> str:
        """Loads and returns developer context/portfolio markdown."""
        path = Path(self.config.context_file)
        if not path.is_file():
            # Fallback candidate paths
            candidates = [Path("config/portfolio.md"), Path("portfolio.md")]
            for c in candidates:
                if c.is_file():
                    path = c
                    break

        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception as e:
                logger.error(f"Не удалось прочитать файл портфолио {path}: {e}")
        return "Разработчик на Python (боты, парсеры, бэкенд, автоматизация)."

    def load_instructions_context(self) -> str:
        """Loads and returns general AI proposal instructions markdown."""
        path = Path(getattr(self.config, "instructions_file", "config/instructions.md"))
        if not path.is_file():
            candidates = [
                Path("config/instructions.md"),
                Path("instructions.md"),
                Path("config/prompts/instructions.md"),
            ]
            for c in candidates:
                if c.is_file():
                    path = c
                    break

        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception as e:
                logger.error(f"Не удалось прочитать файл инструкций {path}: {e}")
        return (
            "Пиши лаконичный, персонализированный отклик без воды и клише. "
            "Используй релевантный кейс из портфолио, обоснуй подход и задай 1-2 технических вопроса."
        )

    def load_system_prompt(self) -> str:
        """Loads system prompt from prompts_dir/system.md or defaults to PROPOSAL_SYSTEM_PROMPT."""
        prompts_dir = Path(getattr(self.config, "prompts_dir", "config/prompts"))
        candidates = [
            prompts_dir / "system.md",
            Path("config/prompts/system.md"),
            Path("config/system.md"),
        ]
        for path in candidates:
            if path.is_file():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                        if content:
                            return content
                except Exception as e:
                    logger.error(f"Не удалось прочитать системный промпт {path}: {e}")
        return PROPOSAL_SYSTEM_PROMPT

    def build_system_prompt_prefix(self) -> str:
        """
        Builds static system prompt prefix (~95,000+ tokens) for prompt caching.
        Combines system role, proposal instructions, and developer portfolio.
        Placed at token index 0 in system message to achieve 100% prefix cache hits.
        """
        system_prompt = self.load_system_prompt()
        instructions_context = self.load_instructions_context()
        portfolio_context = self.load_portfolio_context()
        return (
            f"{system_prompt}\n\n"
            f"ОБЩИЕ ИНСТРУКЦИИ И ПРАВИЛА СОСТАВЛЕНИЯ ОТКЛИКА:\n"
            f"{instructions_context}\n\n"
            f"ПОРТФОЛИО, РЕПОЗИТОРИИ И ОПЫТ РАЗРАБОТЧИКА:\n"
            f"{portfolio_context}"
        )

    def load_mode_instructions(self, mode: str, price: int, days: int) -> str:
        """
        Loads prompt instructions for specified mode from config/prompts/mode_{mode}.md.
        Substitutes {price}, {price_formatted}, and {days}.
        Falls back to built-in instructions if file is missing.
        """
        mode_lower = mode.lower()
        prompts_dir = Path(getattr(self.config, "prompts_dir", "config/prompts"))
        candidates = [
            prompts_dir / f"mode_{mode_lower}.md",
            prompts_dir / f"{mode_lower}.md",
            Path(f"config/prompts/mode_{mode_lower}.md"),
        ]
        price_formatted = f"{price:,}".replace(",", " ")

        for path in candidates:
            if path.is_file():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        template = f.read().strip()
                        if template:
                            return template.format(
                                price=price,
                                price_formatted=price_formatted,
                                days=days,
                            )
                except Exception as e:
                    logger.error(f"Ошибка загрузки промпта режима {path}: {e}")

        # Fallback in-code instructions
        return self._build_mode_instructions(mode, price, days)

    def calculate_pricing_baseline(self, project: KworkProject, mode: str) -> Tuple[int, int]:
        """
        Calculates baseline price (RUB) and delivery time (days) for chosen mode.
        Modes:
          - 'optimal': balanced price based on freelancer rate & client price range
          - 'dumping': aggressive low price to secure fast reviews and rating
          - 'expert': top of client's budget with comprehensive delivery
        """
        min_p = int(project.price_limit) if project.price_limit > 0 else 1000
        max_p = int(project.possible_price_limit) if project.possible_price_limit > 0 else min_p
        if max_p < min_p:
            max_p = min_p

        mode_lower = mode.lower()

        if mode_lower == "dumping":
            discount = 0.20
            price = max(500, int(min_p * (1.0 - discount)))
            # Round to hundreds without exceeding the customer's minimum budget.
            price = max(500, min(min_p, round(price / 100) * 100))
            # Default fast turnaround for dumping mode (1 day) to win first reviews.
            days = 1
        elif mode_lower == "expert":
            price = max_p if max_p > 0 else min_p
            price = max(1000, round(price / 500) * 500)
            days = 3 if price > 5000 else 2
        else:  # optimal
            if max_p > min_p:
                price = int(min_p + (max_p - min_p) * 0.5)
            else:
                price = min_p
            price = max(750, round(price / 100) * 100)
            days = 2 if price > 3000 else 1

        return price, days

    def _build_mode_instructions(self, mode: str, price: int, days: int) -> str:
        mode_lower = mode.lower()
        if mode_lower == "dumping":
            return (
                f"РЕЖИМ: 'ДЕМПИНГ ДЛЯ ОТЗЫВОВ'.\n"
                f"Целевая цена предложения: {price:,} ₽ (готовность сделать по минимальной ставке).\n"
                f"Срок: {days} дн.\n"
                "СТРАТЕГИЯ:\n"
                "- Сделай акцент на готовности приступить прямо сейчас.\n"
                "- Вежливо подчеркни, что только развиваешь профиль/нарабатываешь рейтинг на бирже, "
                "поэтому отдаёшь приоритет отзыву на 5 звезд и делаешь работу по очень приятной стартовой цене с максимальной отдачей."
            )
        elif mode_lower == "expert":
            return (
                f"РЕЖИМ: 'ЭКСПЕРТНЫЙ / ПО ВЕРХНЕМУ БЮДЖЕТУ КЛИЕНТА'.\n"
                f"Целевая цена предложения: {price:,} ₽.\n"
                f"Срок: {days} дн.\n"
                "СТРАТЕГИЯ:\n"
                "- Предложи комплексное, надежное решение с запасом прочности (архитектура, обработка ошибок, логирование, инструкция, развертывание).\n"
                "- Обоснуй стоимость высоким качеством, тестами и дальнейшей гарантией поддержки."
            )
        else:
            return (
                f"РЕЖИМ: 'ОПТИМАЛЬНЫЙ (БАЛАНС С УЧЕТОМ ВИЛКИ ЗАКАЗЧИКА)'.\n"
                f"Целевая цена предложения: {price:,} ₽.\n"
                f"Срок: {days} дн.\n"
                "СТРАТЕГИЯ:\n"
                "- Сбалансированный деловой тон, оптимальное соотношение скорости, цены и качества.\n"
                "- Предложи понятные этапы реализации и комфортные условия сотрудничества."
            )

    async def generate_proposal(
        self,
        project: KworkProject,
        mode: str = "optimal",
        attachments_text: str = "",
        client_profile: Optional[KworkClientProfile] = None,
    ) -> Dict[str, Any]:
        """
        Generates proposal draft using configured AI model.
        Returns dict with suggested_price, suggested_days, proposal_text.
        Raises RuntimeError if AI is not configured or if API call fails.
        """
        if not self.is_available():
            raise RuntimeError(
                "ИИ-генератор недоступен (отсутствует AI_API_KEY или сервис отключен). "
                "Генерация откликов без ИИ выключена."
            )

        price_base, days_base = self.calculate_pricing_baseline(project, mode)
        mode_instructions = self.load_mode_instructions(mode, price_base, days_base)
        full_system_prompt = self.build_system_prompt_prefix()

        # Build client profile section
        profile_section = ""
        cp = client_profile or project.client_profile
        if cp:
            details = []
            if cp.full_name:
                details.append(f"- Реальное имя: {cp.full_name}")
            if cp.profession:
                details.append(f"- Специализация / роль: {cp.profession}")
            if cp.payer_level:
                payer_str = cp.payer_level
                if cp.payer_description:
                    payer_str += f" ({cp.payer_description})"
                details.append(f"- Уровень покупателя: {payer_str}")
            if cp.registered_at:
                details.append(f"- На бирже с: {cp.registered_at}")
            if cp.skills:
                details.append(f"- Навыки / стек: {', '.join(cp.skills)}")
            if cp.description:
                details.append(f"- О себе и бизнесе заказчика:\n{cp.description}")

            if details:
                profile_section = "\n\nДАННЫЕ ИЗ ПРОФИЛЯ ЗАКАЗЧИКА:\n" + "\n".join(details)

        # Build attachments section
        att_section = ""
        if attachments_text.strip():
            att_section = f"\n\nПРИЛОЖЕННЫЕ К ЗАКАЗУ МАТЕРИАЛЫ И ТЗ:\n{attachments_text.strip()}"
        elif project.files:
            file_list = ", ".join(f"{f.name} ({f.size} байт)" for f in project.files)
            att_section = f"\n\nПРИЛОЖЕННЫЕ ФАЙЛЫ (без извлеченного текста): {file_list}"

        # Dynamic user message containing only project-specific inputs and mode instructions
        user_content = f"""ДАННЫЕ ЗАКАЗА:
- Название: {project.title}
- Категория: {project.category_name or project.category_id}
- Бюджет заказчика: {project.display_price} (min: {project.price_limit}, max: {project.possible_price_limit})
- Заказчик: {project.client_name} (нанято: {project.client_hired_percent or 'н/д'}%)
- Текущее число откликов: {project.offers_count}
- Ссылка: {project.url}{profile_section}

ОПИСАНИЕ ЗАДАЧИ ОТ ЗАКАЗЧИКА:
{project.description}
{att_section}

СТРАТЕГИЯ И РЕЖИМ ОТКЛИКА:
{mode_instructions}

Составь отклик в формате JSON строго по схеме:
{{
  "suggested_price": <целое число в рублях>,
  "suggested_days": <срок в днях>,
  "proposal_text": "<текст отклика>"
}}
"""

        try:
            # Multi-turn prefix architecture:
            # In Gemini / Antigravity Tools, prefix caching anchors reliably on a completed turn (user + assistant).
            # Turn 1 (~83,000 tokens) is cached as an immutable block, delivering 95%+ Cache Hit on any new order in Turn 2.
            messages = [
                {
                    "role": "user",
                    "content": f"ИНСТРУКЦИИ, ПРАВИЛА И ПОЛНОЕ ПОРТФОЛИО ИСПОЛНИТЕЛЯ НА KWORK:\n\n{full_system_prompt}",
                },
                {
                    "role": "assistant",
                    "content": (
                        "База портфолио, кейсы и инструкции по составлению откликов успешно загружены и зафиксированы. "
                        "Готов формировать персонализированные предложения по новым заказам Kwork строго по ТЗ и в формате JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ]

            response = await self._client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                response_format={"type": "json_object"} if "json_object" in str(self.config.model).lower() or "gpt" in self.config.model else None,
            )

            # Log prompt caching stats if provided by upstream API
            usage = getattr(response, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                cached_tokens = 0
                prompt_details = getattr(usage, "prompt_tokens_details", None)
                if prompt_details:
                    cached_tokens = getattr(prompt_details, "cached_tokens", 0) or 0
                if cached_tokens > 0 and prompt_tokens > 0:
                    pct = (cached_tokens / prompt_tokens) * 100
                    logger.info(
                        f"ИИ ответ получен ({self.config.model}): {prompt_tokens} токенов промпта "
                        f"[CACHE HIT: {cached_tokens} ({pct:.1f}%)], {completion_tokens} токенов ответа"
                    )
                else:
                    logger.info(
                        f"ИИ ответ получен ({self.config.model}): {prompt_tokens} токенов промпта, "
                        f"{completion_tokens} токенов ответа"
                    )

            raw_reply = response.choices[0].message.content or ""
            # Parse JSON from response
            match = re.search(r"\{.*\}", raw_reply, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                s_price = int(data.get("suggested_price") or price_base)
                s_days = int(data.get("suggested_days") or days_base)
                p_text = str(data.get("proposal_text") or "").strip()
                if p_text:
                    mode_title = "Демпинг (для отзывов)" if mode == "dumping" else ("Экспертный" if mode == "expert" else "Оптимальный")
                    return {
                        "suggested_price": s_price,
                        "suggested_days": s_days,
                        "proposal_text": p_text,
                        "is_fallback": False,
                        "mode_name": mode_title,
                    }

            logger.warning(f"Не удалось распарсить JSON из ответа ИИ, возврат сырого текста: {raw_reply[:200]}")
            return {
                "suggested_price": price_base,
                "suggested_days": days_base,
                "proposal_text": raw_reply.strip(),
                "is_fallback": False,
                "mode_name": mode,
            }

        except Exception as e:
            logger.error(f"Ошибка вызова ИИ API: {e}", exc_info=True)
            raise RuntimeError(f"Сбой генерации через ИИ ({self.config.model}): {e}")
