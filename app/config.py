import re

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM
    openrouter_api_key: str = ""
    model_cheap: str = "google/gemini-2.5-flash"
    model_strong: str = "anthropic/claude-sonnet-4.5"
    model_orchestrator: str = ""

    # Владелец
    owner_phone: str = ""

    # Green API (WhatsApp-шлюз). Ассистент — обязательный инстанс,
    # рассыльный — опциональный второй (пока нет — шлём с ассистента).
    green_api_url: str = "https://api.green-api.com"
    green_api_id_instance: str = ""
    green_api_token: str = ""
    green_api_outreach_id_instance: str = ""
    green_api_outreach_token: str = ""
    green_api_webhook_token: str = ""

    # Postgres на поде InstaPods (instapods services creds <pod> -s postgresql)
    database_url: str = "postgresql://localhost:5432/agenthub"

    # Поиск
    serper_api_key: str = ""
    tavily_api_key: str = ""
    twogis_api_key: str = ""

    # Google Sheets
    google_service_account_file: str = "secrets/google-service-account.json"
    google_share_email: str = ""
    sheets_public_link: bool = True

    # Рассылка
    outreach_daily_limit: int = 20
    outreach_batch_per_tick: int = 2
    outreach_min_delay_s: int = 45
    outreach_max_delay_s: int = 180

    # Встроенный планировщик (замена n8n)
    scheduler_enabled: bool = True
    outreach_tick_minutes: int = 15
    monitoring_tick_hours: int = 6

    data_dir: str = "data"

    @property
    def orchestrator_model_name(self) -> str:
        return self.model_orchestrator or self.model_cheap

    @property
    def owner_chat_id(self) -> str:
        return phone_to_chat_id(self.owner_phone)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits
    return digits


def phone_to_chat_id(phone: str) -> str:
    return f"{normalize_phone(phone)}@c.us"


settings = Settings()
