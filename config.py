import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require_secret(name: str) -> str:
    value = env(name)
    if len(value) >= 32:
        return value
    if env_bool("ALLOW_INSECURE_DEV_SECRETS", False):
        return f"dev-only-{name}-change-before-production"
    raise RuntimeError(f"{name} must be set to at least 32 characters")


def default_db_path() -> Path:
    if env("VERCEL"):
        return Path("/tmp/booking-db.sqlite")
    return BASE_DIR / "data" / "db.sqlite"


def default_upload_dir() -> Path:
    if env("VERCEL"):
        return Path("/tmp/booking-uploads")
    return BASE_DIR / "data" / "uploads"


OWNER_EMAIL = env("OWNER_EMAIL").strip().lower()
MAIL_PROVIDER = env("MAIL_PROVIDER", "mailjet").strip().lower()
MAILJET_API_KEY = env("MAILJET_API_KEY").strip()
MAILJET_SECRET_KEY = env("MAILJET_SECRET_KEY").strip()
MAILJET_SENDER_EMAIL = env("MAILJET_SENDER_EMAIL").strip().lower()
MAILJET_SENDER_NAME = env("MAILJET_SENDER_NAME", "Appointments").strip()
COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
DB_PATH = Path(env("DB_PATH", str(default_db_path())))
UPLOAD_DIR = Path(env("UPLOAD_DIR", str(default_upload_dir())))
TZ_NAME = env("APP_TIMEZONE", "Asia/Jerusalem").strip().lstrip(":") or "Asia/Jerusalem"
GOOGLE_CALENDAR_ENABLED = env_bool("GOOGLE_CALENDAR_ENABLED", False)
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID").strip()
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET").strip()
GOOGLE_REFRESH_TOKEN = env("GOOGLE_REFRESH_TOKEN").strip()
GOOGLE_CALENDAR_ID = env("GOOGLE_CALENDAR_ID", "primary").strip() or "primary"
GOOGLE_REDIRECT_URI = env("GOOGLE_REDIRECT_URI").strip()

