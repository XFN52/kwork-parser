from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field


class KworkAttachment(BaseModel):
    id: int
    name: str
    url: str
    size: int = 0
    mime_type: Optional[str] = None


class KworkClientProfile(BaseModel):
    username: str = ""
    full_name: str = ""
    profession: str = ""
    description: str = ""
    payer_level: str = ""
    payer_description: str = ""
    registered_at: str = ""
    skills: List[str] = Field(default_factory=list)


class KworkProject(BaseModel):
    id: Union[int, str]
    title: str
    description: str
    price_limit: float
    possible_price_limit: float
    offers_count: int = 0
    category_id: int = 0
    category_name: Optional[str] = None
    time_left: str = ""
    date_create: str = ""
    client_name: str = ""
    client_url: str = ""
    client_hired_percent: Optional[str] = None
    client_profile: Optional[KworkClientProfile] = None
    url: str = ""
    files: List[KworkAttachment] = Field(default_factory=list)
    attachments_text: Optional[str] = None
    source: str = "kwork"
    response_price: Optional[str] = None
    is_fresh: Optional[bool] = None
    is_viewed: Optional[bool] = None


    @property
    def display_price(self) -> str:
        """Formatted price string (e.g. '1 000 - 3 000 ₽' or '1 000 ₽')."""
        min_p = int(self.price_limit)
        max_p = int(self.possible_price_limit)
        if max_p > min_p and max_p > 0:
            return f"{min_p:,} – {max_p:,} ₽".replace(",", " ")
        elif min_p > 0:
            return f"{min_p:,} ₽".replace(",", " ")
        return "Договорная"


class FilterConfig(BaseModel):
    categories: List[int] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    stop_words: List[str] = Field(default_factory=list)
    min_price: float = 0.0
    max_price: Optional[float] = None
    max_offers: Optional[int] = None


class TelegramConfig(BaseModel):
    bot_token: str = ""
    chat_ids: List[int] = Field(default_factory=list)
    send_previews: bool = True
    preview_length: int = 350


class AIConfig(BaseModel):
    enabled: bool = True
    provider: str = "openai"  # openai, openrouter, deepseek, groq, ollama, etc.
    api_key: str = ""
    base_url: Optional[str] = None  # e.g. https://openrouter.ai/api/v1 or https://api.deepseek.com/v1
    model: str = "gpt-4o-mini"
    context_file: str = "config/portfolio.md"
    instructions_file: str = "config/instructions.md"
    prompts_dir: str = "config/prompts"
    temperature: float = 0.7
    max_tokens: int = 1500
    dumping_discount_percent: int = 35


class ProfiConfig(BaseModel):
    enabled: bool = False
    cookies: str = ""
    url: str = "https://profi.ru/backoffice/n.php"
    poll_interval_seconds: Optional[int] = None
    browser_profile_dir: Optional[str] = "data/profi_browser_profile"


class AppConfig(BaseModel):
    poll_interval_seconds: int = 60
    poll_jitter_seconds: int = 15
    database_path: str = "data/kwork_parser.db"
    log_level: str = "INFO"
    log_file: Optional[str] = "logs/kwork_parser.log"
    proxy: Optional[str] = None
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    profi: ProfiConfig = Field(default_factory=ProfiConfig)

