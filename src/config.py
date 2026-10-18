import os
import yaml
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

from src.models import AppConfig, TelegramConfig, FilterConfig, AIConfig, ProfiConfig


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """
    Load configuration from YAML file and override with environment variables.
    """
    load_dotenv()

    # Search for config file in order of priority
    candidate_paths = [
        config_path,
        os.getenv("CONFIG_PATH"),
        "config/config.yaml",
        "config.yaml",
    ]

    raw_data = {}
    for p in candidate_paths:
        if p and Path(p).is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw_data = yaml.safe_load(f) or {}
                break
            except Exception as e:
                print(f"Warning: Failed to load config from {p}: {e}")

    # Build config object
    telegram_dict = raw_data.get("telegram", {})
    filters_dict = raw_data.get("filters", {})
    ai_dict = raw_data.get("ai", {})
    profi_dict = raw_data.get("profi", {})

    # Environment overrides for Telegram
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", telegram_dict.get("bot_token", ""))
    chat_ids_env = os.getenv("TELEGRAM_CHAT_IDS")
    if chat_ids_env:
        chat_ids = [int(x.strip()) for x in chat_ids_env.split(",") if x.strip()]
    else:
        chat_ids = telegram_dict.get("chat_ids", [])

    telegram_cfg = TelegramConfig(
        bot_token=bot_token,
        chat_ids=chat_ids,
        send_previews=telegram_dict.get("send_previews", True),
        preview_length=telegram_dict.get("preview_length", 350),
    )

    # Environment overrides for Filters
    categories_env = os.getenv("FILTER_CATEGORIES")
    if categories_env:
        categories = [int(x.strip()) for x in categories_env.split(",") if x.strip()]
    else:
        categories = filters_dict.get("categories", [])

    keywords_env = os.getenv("FILTER_KEYWORDS")
    if keywords_env:
        keywords = [x.strip() for x in keywords_env.split(",") if x.strip()]
    else:
        keywords = filters_dict.get("keywords", [])

    stop_words_env = os.getenv("FILTER_STOP_WORDS")
    if stop_words_env:
        stop_words = [x.strip() for x in stop_words_env.split(",") if x.strip()]
    else:
        stop_words = filters_dict.get("stop_words", [])

    min_price = float(os.getenv("FILTER_MIN_PRICE", filters_dict.get("min_price", 0.0)))
    
    max_price_val = os.getenv("FILTER_MAX_PRICE")
    max_price = float(max_price_val) if max_price_val else filters_dict.get("max_price")

    max_offers_val = os.getenv("FILTER_MAX_OFFERS")
    max_offers = int(max_offers_val) if max_offers_val else filters_dict.get("max_offers")

    filters_cfg = FilterConfig(
        categories=categories,
        keywords=keywords,
        stop_words=stop_words,
        min_price=min_price,
        max_price=max_price,
        max_offers=max_offers,
    )

    # Environment overrides for AI Assistant
    ai_enabled_env = os.getenv("AI_ENABLED")
    ai_enabled = ai_enabled_env.lower() in ("1", "true", "yes") if ai_enabled_env else ai_dict.get("enabled", True)
    ai_provider = os.getenv("AI_PROVIDER", ai_dict.get("provider", "openai"))
    ai_api_key = os.getenv("AI_API_KEY", ai_dict.get("api_key", ""))
    ai_base_url = os.getenv("AI_BASE_URL", ai_dict.get("base_url"))
    ai_model = os.getenv("AI_MODEL", ai_dict.get("model", "gpt-4o-mini"))
    ai_context_file = os.getenv("AI_CONTEXT_FILE", ai_dict.get("context_file", "config/portfolio.md"))
    ai_instructions_file = os.getenv("AI_INSTRUCTIONS_FILE", ai_dict.get("instructions_file", "config/instructions.md"))
    ai_prompts_dir = os.getenv("AI_PROMPTS_DIR", ai_dict.get("prompts_dir", "config/prompts"))
    ai_temperature = float(os.getenv("AI_TEMPERATURE", ai_dict.get("temperature", 0.7)))
    ai_max_tokens = int(os.getenv("AI_MAX_TOKENS", ai_dict.get("max_tokens", 1500)))
    ai_dumping_percent = int(os.getenv("AI_DUMPING_DISCOUNT_PERCENT", ai_dict.get("dumping_discount_percent", 35)))

    ai_cfg = AIConfig(
        enabled=ai_enabled,
        provider=ai_provider,
        api_key=ai_api_key,
        base_url=ai_base_url if ai_base_url else None,
        model=ai_model,
        context_file=ai_context_file,
        instructions_file=ai_instructions_file,
        prompts_dir=ai_prompts_dir,
        temperature=ai_temperature,
        max_tokens=ai_max_tokens,
        dumping_discount_percent=ai_dumping_percent,
    )

    # Profi.ru options
    profi_enabled_env = os.getenv("PROFI_ENABLED")
    profi_enabled = (
        profi_enabled_env.lower() in ("1", "true", "yes")
        if profi_enabled_env is not None
        else profi_dict.get("enabled", False)
    )
    profi_cookies = os.getenv("PROFI_COOKIES", profi_dict.get("cookies", ""))
    profi_url = os.getenv("PROFI_URL", profi_dict.get("url", "https://profi.ru/backoffice/n.php"))
    profi_poll_interval = (
        int(os.getenv("PROFI_POLL_INTERVAL_SECONDS"))
        if os.getenv("PROFI_POLL_INTERVAL_SECONDS")
        else profi_dict.get("poll_interval_seconds")
    )
    profi_browser_profile_dir = os.getenv(
        "PROFI_BROWSER_PROFILE_DIR",
        profi_dict.get("browser_profile_dir", "data/profi_browser_profile"),
    )

    profi_cfg = ProfiConfig(
        enabled=profi_enabled,
        cookies=profi_cookies,
        url=profi_url,
        poll_interval_seconds=profi_poll_interval,
        browser_profile_dir=profi_browser_profile_dir,
    )

    # General app options
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", raw_data.get("poll_interval_seconds", 60)))
    poll_jitter = int(os.getenv("POLL_JITTER_SECONDS", raw_data.get("poll_jitter_seconds", 15)))
    db_path = os.getenv("DATABASE_PATH", raw_data.get("database_path", "data/kwork_parser.db"))
    log_level = os.getenv("LOG_LEVEL", raw_data.get("log_level", "INFO"))
    log_file = os.getenv("LOG_FILE", raw_data.get("log_file", "logs/kwork_parser.log"))
    proxy = os.getenv("PROXY", raw_data.get("proxy"))

    # Ensure directories exist
    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)
    if log_file:
        log_dir = Path(log_file).parent
        log_dir.mkdir(parents=True, exist_ok=True)

    return AppConfig(
        poll_interval_seconds=poll_interval,
        poll_jitter_seconds=poll_jitter,
        database_path=db_path,
        log_level=log_level,
        log_file=log_file,
        proxy=proxy,
        telegram=telegram_cfg,
        filters=filters_cfg,
        ai=ai_cfg,
        profi=profi_cfg,
    )

