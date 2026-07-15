# ruff: noqa: E402, I001
import os
import secrets
from pathlib import Path

import pytest


TEST_ENV = {
    "OWNER_EMAIL": "owner@example.com",
    "OTP_SECRET": "a" * 64,  # pragma: allowlist secret
    "SESSION_SECRET": "b" * 64,  # pragma: allowlist secret
    "TOKEN_ENCRYPTION_KEY": "c" * 64,  # pragma: allowlist secret
    "MAIL_PROVIDER": "mailjet",
    "MAILJET_API_KEY": "test-mailjet-api-key",  # pragma: allowlist secret
    "MAILJET_SECRET_KEY": "test-mailjet-secret-key",  # pragma: allowlist secret
    "MAILJET_SENDER_EMAIL": "sender@example.com",
    "MAILJET_SENDER_NAME": "Appointments Test",
    "APP_ENV": "test",
    "APP_TIMEZONE": "Asia/Jerusalem",
    "PUBLIC_BASE_URL": "http://testserver",
    "ALLOWED_HOSTS": "testserver,localhost,127.0.0.1",
    "TRUST_PROXY_HEADERS": "false",
    "DATABASE_URL": "",
    "POSTGRES_URL": "",
    "DB_PATH": "test-results/bootstrap.sqlite",
    "UPLOAD_DIR": "test-results/uploads",
    "BLOB_READ_WRITE_TOKEN": "",
    "CRON_SECRET": "d" * 64,  # pragma: allowlist secret
    "COOKIE_SECURE": "false",
    "ALLOW_INSECURE_DEV_SECRETS": "false",  # pragma: allowlist secret
    "GOOGLE_CALENDAR_ENABLED": "false",
    "GOOGLE_CLIENT_ID": "test-google-client-id",
    "GOOGLE_CLIENT_SECRET": "test-google-client-secret",  # pragma: allowlist secret
    "GOOGLE_REFRESH_TOKEN": "",
    "GOOGLE_CALENDAR_ID": "primary",
    "GOOGLE_REDIRECT_URI": "http://testserver/api/owner/google/callback",
    "VERCEL": "",
}

os.environ.update(TEST_ENV)

import app as app_module  # noqa: E402
import auth  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import mailer  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    database_path = tmp_path / "db.sqlite"
    uploads_path = tmp_path / "uploads"
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "DB_PATH", database_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", uploads_path)
    monkeypatch.setattr(config, "VERCEL", False)
    monkeypatch.setattr(config, "IS_PRODUCTION", False)
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "GOOGLE_CALENDAR_ENABLED", False)
    monkeypatch.setattr(config, "BLOB_READ_WRITE_TOKEN", "")
    monkeypatch.setattr(db, "DB_PATH", database_path)
    monkeypatch.setattr(db, "IS_POSTGRES", False)
    db.init_db()
    yield database_path


@pytest.fixture
def sent_emails(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []

    def fake_send_email(
        recipient,
        template,
        title,
        message,
        details=None,
        code=None,
        ics_content=None,
    ):
        calls.append(
            {
                "recipient": recipient,
                "template": template,
                "title": title,
                "message": message,
                "details": details or [],
                "code": code,
                "ics_content": ics_content,
            }
        )
        return "mailjet:200"

    monkeypatch.setattr(mailer, "send_email", fake_send_email)
    return calls


@pytest.fixture
def client(sent_emails):
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture
def session_factory(client):
    def create(
        *,
        role: str = "customer",
        email: str | None = None,
        name: str = "Test Customer",
        existing_customer: bool = True,
        expires_at: int | None = None,
        target_client=None,
    ) -> dict:
        email = email or (
            config.OWNER_EMAIL if role == "owner" else f"user-{secrets.token_hex(4)}@example.com"
        )
        raw_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        customer_id = None
        with db.get_conn() as conn, db.transaction(conn, immediate=True):
            if role == "customer" and existing_customer:
                customer_id = db.upsert_customer(conn, email, name)
            conn.execute(
                "INSERT INTO sessions "
                "(token_hash,csrf_token,customer_id,email,role,expires_at,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    auth.token_hash(raw_token),
                    csrf_token,
                    customer_id,
                    email,
                    role,
                    expires_at if expires_at is not None else db.now() + 3600,
                    db.now(),
                ),
            )
        active_client = target_client or client
        active_client.cookies.set(auth.COOKIE, raw_token, domain="testserver.local", path="/")
        return {
            "raw_token": raw_token,
            "token_hash": auth.token_hash(raw_token),
            "csrf": csrf_token,
            "email": email,
            "customer_id": customer_id,
            "role": role,
        }

    return create
