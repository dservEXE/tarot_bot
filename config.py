"""Configuration only. Importing the project never starts a bot or makes requests."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # Keep an existing minimal server venv runnable during migration.
    def load_dotenv(path, override=False):
        path = Path(path)
        if not path.exists():
            return False
        for raw_line in path.read_text(encoding='utf-8-sig').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if key and (override or key not in os.environ):
                os.environ[key] = value
        return True

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env', override=False)


def flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {'1', 'true', 'yes', 'on'}


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    admin_ids: frozenset[int]
    brand_name: str
    support_contact: str
    seller_name: str
    seller_inn: str
    terms_url: str
    privacy_url: str
    payments_enabled: bool
    payment_provider: str
    yookassa_shop_id: str
    yookassa_secret_key: str = field(repr=False)
    yookassa_test: bool
    yookassa_method: str
    payment_return_url: str
    ai_enabled: bool
    openai_api_key: str = field(repr=False)
    openai_model: str
    payments_admin_only: bool = False
    payment_documents_in_bot: bool = False

    def payment_problems(self, *, require_documents: bool = True) -> list[str]:
        problems = []
        if not self.support_contact:
            problems.append('SUPPORT_CONTACT')
        for name, url in [('TERMS_URL', self.terms_url), ('PRIVACY_URL', self.privacy_url)]:
            if (require_documents or url) and (urlparse(url).scheme != 'https' or not urlparse(url).netloc):
                problems.append(name)
        if self.payment_provider == 'yookassa':
            if not self.yookassa_shop_id or not self.yookassa_secret_key:
                problems.append('YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY')
        elif self.payment_provider != 'stars':
            problems.append('PAYMENT_PROVIDER: yookassa или stars')
        return problems

    def checkout_problems(self) -> list[str]:
        return self.payment_problems(require_documents=not (
            self.payments_admin_only or self.payment_documents_in_bot))

    def payment_allowed(self, user_id: int) -> bool:
        return (self.payments_enabled and not self.checkout_problems()
                and (not self.payments_admin_only or user_id in self.admin_ids))

    def validate_startup(self) -> None:
        if not self.bot_token or ':' not in self.bot_token:
            raise ValueError('Укажите BOT_TOKEN в .env; см. START_HERE.md')
        if not self.admin_ids:
            raise ValueError('Укажите ADMIN_IDS в .env')
        if self.payments_enabled and self.checkout_problems():
            raise ValueError('Оплата не настроена: ' + ', '.join(self.checkout_problems()))
        if self.ai_enabled and (not self.openai_api_key or not self.openai_model):
            raise ValueError('Для AI_ENABLED нужны OPENAI_API_KEY и OPENAI_MODEL')


def get_settings() -> Settings:
    raw_ids = os.getenv('ADMIN_IDS', '982023162').replace(' ', '').split(',')
    return Settings(
        bot_token=os.getenv('BOT_TOKEN', ''),
        admin_ids=frozenset(int(value) for value in raw_ids if value),
        brand_name=os.getenv('BRAND_NAME', 'Твой астролог'),
        support_contact=os.getenv('SUPPORT_CONTACT', ''),
        seller_name=os.getenv('SELLER_NAME', ''), seller_inn=os.getenv('SELLER_INN', ''),
        terms_url=os.getenv('TERMS_URL', ''), privacy_url=os.getenv('PRIVACY_URL', ''),
        payments_enabled=flag('PAYMENTS_ENABLED'),
        payment_provider=os.getenv('PAYMENT_PROVIDER', 'yookassa').lower(),
        yookassa_shop_id=os.getenv('YOOKASSA_SHOP_ID', ''),
        yookassa_secret_key=os.getenv('YOOKASSA_SECRET_KEY', ''),
        yookassa_test=flag('YOOKASSA_TEST', True),
        yookassa_method=os.getenv('YOOKASSA_METHOD', 'auto').lower(),
        payment_return_url=os.getenv('PAYMENT_RETURN_URL', ''),
        ai_enabled=flag('AI_ENABLED'), openai_api_key=os.getenv('OPENAI_API_KEY', ''),
        openai_model=os.getenv('OPENAI_MODEL', ''),
        payments_admin_only=flag('PAYMENTS_ADMIN_ONLY'),
        payment_documents_in_bot=flag('PAYMENT_DOCUMENTS_IN_BOT'),
    )
