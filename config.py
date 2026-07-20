import os
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from email_validator import EmailNotValidError, validate_email

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def require_secret(name: str) -> str:
    value = env(name)
    weak_markers = ("change-me", "replace", "example", "dev-only", "dev-secret")
    looks_weak = any(marker in value.lower() for marker in weak_markers)
    if len(value) >= 32 and not looks_weak:
        return value
    production_like = globals().get("IS_PRODUCTION", False) or globals().get("VERCEL", False)
    if not production_like and env_bool("ALLOW_INSECURE_DEV_SECRETS", False):
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
     

VERCEL = bool(env("VERCEL"))
APP_ENV = env("APP_ENV", "production" if VERCEL else "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"
OWNER_EMAIL = env("OWNER_EMAIL").lower()
MAIL_PROVIDER = env("MAIL_PROVIDER", "mailjet").lower()
MAILJET_API_KEY = env("MAILJET_API_KEY")
MAILJET_SECRET_KEY = env("MAILJET_SECRET_KEY")
MAILJET_SENDER_EMAIL = env("MAILJET_SENDER_EMAIL").lower()
MAILJET_SENDER_NAME = env("MAILJET_SENDER_NAME", "Appointments")
COOKIE_SECURE = env_bool("COOKIE_SECURE", VERCEL)
DATABASE_URL = (env("DATABASE_URL") or env("POSTGRES_URL")).strip()
DB_PATH = Path(env("DB_PATH", str(default_db_path())))
UPLOAD_DIR = Path(env("UPLOAD_DIR", str(default_upload_dir())))
TZ_NAME = env("APP_TIMEZONE", "Asia/Jerusalem").lstrip(":") or "Asia/Jerusalem"
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL").rstrip("/")
ALLOWED_HOSTS = [item.strip() for item in env("ALLOWED_HOSTS").split(",") if item.strip()]
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["*.vercel.app"] if VERCEL else ["localhost", "127.0.0.1", "testserver"]
TRUST_PROXY_HEADERS = env_bool("TRUST_PROXY_HEADERS", VERCEL)
CRON_SECRET = env("CRON_SECRET")
BLOB_READ_WRITE_TOKEN = env("BLOB_READ_WRITE_TOKEN") or env("VERCEL_BLOB_READ_WRITE_TOKEN")
MAX_UPLOAD_BYTES = 3_000_000
GOOGLE_CALENDAR_ENABLED = env_bool("GOOGLE_CALENDAR_ENABLED", False)
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = env("GOOGLE_REFRESH_TOKEN")
GOOGLE_CALENDAR_ID = env("GOOGLE_CALENDAR_ID", "primary") or "primary"
GOOGLE_REDIRECT_URI = env("GOOGLE_REDIRECT_URI")
TOKEN_ENCRYPTION_KEY = (env("TOKEN_ENCRYPTION_KEY") or env("SESSION_SECRET"))


def _valid_https_url(value: str, *, allow_local_http: bool = False) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.netloc:
        return True
    return bool(
        allow_local_http
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1"}
    )


def validate_runtime_config() -> None:
    if not OWNER_EMAIL:
        raise RuntimeError("OWNER_EMAIL must be set")
    if not MAILJET_SENDER_EMAIL:
        raise RuntimeError("MAILJET_SENDER_EMAIL must be set")
    try:
        validate_email(OWNER_EMAIL, check_deliverability=False)
        validate_email(MAILJET_SENDER_EMAIL, check_deliverability=False)
    except EmailNotValidError:
        raise RuntimeError("OWNER_EMAIL and MAILJET_SENDER_EMAIL must be valid") from None
    if MAIL_PROVIDER != "mailjet":
        raise RuntimeError("MAIL_PROVIDER must be mailjet")
    if not MAILJET_API_KEY or not MAILJET_SECRET_KEY or not MAILJET_SENDER_EMAIL:
        raise RuntimeError("Mailjet credentials and sender email must be configured")
    require_secret("OTP_SECRET")
    require_secret("SESSION_SECRET")
    try:
        ZoneInfo(TZ_NAME)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError("APP_TIMEZONE is not a valid IANA timezone") from exc

    if IS_PRODUCTION:
        if not COOKIE_SECURE:
            raise RuntimeError("COOKIE_SECURE must be true in production")
        if len(CRON_SECRET) < 32:
            raise RuntimeError("CRON_SECRET must be at least 32 characters in production")
        if PUBLIC_BASE_URL and not _valid_https_url(PUBLIC_BASE_URL):
            raise RuntimeError("PUBLIC_BASE_URL must use HTTPS in production")
        if "*" in ALLOWED_HOSTS:
            raise RuntimeError("ALLOWED_HOSTS cannot contain a global wildcard")

    if GOOGLE_CALENDAR_ENABLED:
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            raise RuntimeError("Google Calendar is enabled but OAuth credentials are missing")
        if len(TOKEN_ENCRYPTION_KEY) < 32:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY or SESSION_SECRET must be at least 32 characters"
            )
        redirect = GOOGLE_REDIRECT_URI or (
            f"{PUBLIC_BASE_URL}/api/owner/google/callback" if PUBLIC_BASE_URL else ""
        )
        if IS_PRODUCTION and not _valid_https_url(redirect):
            raise RuntimeError("Google OAuth requires an HTTPS GOOGLE_REDIRECT_URI in production")

    if VERCEL:
        if not IS_PRODUCTION:
            raise RuntimeError("APP_ENV must be production on Vercel")
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is required on Vercel; /tmp SQLite is not durable")
        if urlparse(DATABASE_URL).scheme not in {"postgres", "postgresql"}:
            raise RuntimeError("DATABASE_URL must use PostgreSQL on Vercel")
        if not COOKIE_SECURE:
            raise RuntimeError("COOKIE_SECURE must be true on Vercel")
        if len(CRON_SECRET) < 32:
            raise RuntimeError("CRON_SECRET must be at least 32 characters on Vercel")
        if not BLOB_READ_WRITE_TOKEN:
            raise RuntimeError("BLOB_READ_WRITE_TOKEN is required for durable uploads on Vercel")
