import asyncio
import io
import json
import re
import sqlite3
import os
from pathlib import Path

import pytest
from PIL import Image

import availability
import config
import db
import storage
from tests.helpers import csrf_headers


def png_bytes(size=(32, 32), color=(198, 61, 112, 255)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


def test_owner_upload_decodes_image_and_ignores_malicious_filename(client, session_factory):
    owner = session_factory(role="owner")

    response = client.post(
        "/api/owner/upload",
        headers=csrf_headers(owner),
        files={"file": ("../../attack.php.png", png_bytes(), "image/png")},
    )

    assert response.status_code == 200
    url = response.json()["url"]
    assert re.fullmatch(r"/uploads/[a-f0-9]{32}\.png", url)
    saved = config.UPLOAD_DIR / Path(url).name
    assert saved.is_file()
    with Image.open(saved) as image:
        assert image.format == "PNG"
        assert image.size == (32, 32)


def test_upload_rejects_anonymous_active_content_and_oversized_data(client, session_factory):
    anonymous = client.post(
        "/api/owner/upload",
        files={"file": ("image.png", png_bytes(), "image/png")},
    )
    owner = session_factory(role="owner")
    active_content = client.post(
        "/api/owner/upload",
        headers=csrf_headers(owner),
        files={
            "file": (
                "payload.svg",
                b'<svg onload="alert(1)"></svg>',
                "image/svg+xml",
            )
        },
    )
    oversized = client.post(
        "/api/owner/upload",
        headers=csrf_headers(owner),
        files={
            "file": (
                "large.jpg",
                b"x" * (config.MAX_UPLOAD_BYTES + 1),
                "image/jpeg",
            )
        },
    )

    assert anonymous.status_code == 403
    assert active_content.status_code == 400
    assert oversized.status_code == 400


def test_vercel_blob_upload_uses_blob_token_and_returns_url(monkeypatch):
    uploaded = {}

    class FakeResult:
        def __init__(self, url):
            self.url = url

    class FakeClient:
        def __init__(self, token=None):
            assert token == "blob-token"
            self.token = token
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.closed = True

        async def put(
            self,
            path,
            body,
            *,
            access,
            content_type,
            add_random_suffix=False,
            overwrite=False,
            cache_control_max_age=None,
            multipart=False,
            on_upload_progress=None,
            token=None,
        ):
            assert path.startswith("business/")
            assert len(body) > 0
            assert access == "private"
            assert content_type in {"image/png", "image/jpeg", "image/webp"}
            assert add_random_suffix is False
            assert overwrite is False
            assert cache_control_max_age == 31536000
            assert multipart is False
            assert on_upload_progress is None
            assert token is None
            return FakeResult("https://blob.vercel.com/business/test.png")

    monkeypatch.setattr(config, "BLOB_READ_WRITE_TOKEN", "blob-token")
    monkeypatch.setattr(config, "VERCEL", True)
    monkeypatch.setattr("storage.AsyncBlobClient", FakeClient)

    result = asyncio.run(storage.save_public_image(png_bytes()))
    assert result == "https://blob.vercel.com/business/test.png"


@pytest.mark.skipif(
    not (os.getenv("BLOB_READ_WRITE_TOKEN") or os.getenv("VERCEL_BLOB_READ_WRITE_TOKEN")),
    reason="Vercel blob upload token is not configured",
)
def test_vercel_blob_upload_integration_works_with_real_store():
    result = asyncio.run(storage.save_public_image(png_bytes()))
    assert isinstance(result, str)
    assert result.startswith("https://")
    assert ".blob.vercel-storage.com/" in result or "vercel.com/api/blob" not in result


def test_upload_path_traversal_and_unknown_names_return_404(client):
    assert client.get("/uploads/not-a-random-name.png").status_code == 404
    assert client.get("/uploads/%2e%2e%2f.env").status_code in {404, 405}


def test_normalize_image_rejects_fake_image_and_preserves_allowed_type():
    with pytest.raises(storage.ImageValidationError):
        storage.normalize_image(b"\x89PNG\r\n\x1a\nnot-really-an-image")

    normalized, extension, content_type = storage.normalize_image(png_bytes())

    assert extension == ".png"
    assert content_type == "image/png"
    assert len(normalized) <= config.MAX_UPLOAD_BYTES


def test_israel_dst_nonexistent_and_ambiguous_times_are_rejected():
    with pytest.raises(ValueError, match="nonexistent"):
        availability.to_unix("2026-03-27", "02:30", 30)
    with pytest.raises(ValueError, match="ambiguous"):
        availability.to_unix("2026-10-25", "01:30", 30)

    before_jump = availability.to_unix("2026-03-27", "01:30", 30)
    after_jump = availability.to_unix("2026-03-27", "03:30", 30)
    assert before_jump[1] - before_jump[0] == 1800
    assert after_jump[1] - after_jump[0] == 1800


def test_availability_skips_ambiguous_dst_slots_instead_of_failing_range():
    with db.get_conn() as conn:
        conn.execute("UPDATE settings SET max_days_ahead=365 WHERE id=1")
        conn.execute(
            "UPDATE working_hours SET is_closed=0,open_time='01:00',close_time='03:00',"
            "slot_interval_minutes=30 WHERE day_of_week=0"
        )
        days = availability.available_slots(conn, "2026-10-25", "2026-10-25", 30)

    assert len(days) == 1
    assert "01:00" not in days[0]["times"]
    assert "01:30" not in days[0]["times"]


def test_sqlite_initialization_is_idempotent_and_preserves_data():
    with db.get_conn() as conn:
        customer_id = db.upsert_customer(conn, "preserve@example.com", "Preserve")
        before = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]

    db.init_db()
    db.init_db()

    with db.get_conn() as conn:
        after = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
        preserved = db.customer_by_id(conn, customer_id)
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert before == after
    assert preserved["email"] == "preserve@example.com"
    assert foreign_keys == 1
    assert busy_timeout == 20000


def test_current_schema_skips_full_ddl_on_repeated_cold_start(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("schema DDL should not run again")

    monkeypatch.setattr(db.Connection, "executescript", unexpected)

    db.init_db()


def test_sqlite_pre_migration_backup_is_a_readable_copy(isolated_database):
    with db.get_conn() as conn:
        db.upsert_customer(conn, "backup@example.com", "Backup")
        db._backup_sqlite(conn)

    backups = list(isolated_database.parent.glob("db.pre-migration-*.sqlite"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        count = backup.execute(
            "SELECT COUNT(*) FROM customers WHERE email='backup@example.com'"
        ).fetchone()[0]
    assert count == 1


def test_vercel_runtime_guard_blocks_temporary_sqlite_and_insecure_settings(
    monkeypatch,
):
    monkeypatch.setattr(config, "VERCEL", True)
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://appointments.example.com")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    monkeypatch.setattr(config, "BLOB_READ_WRITE_TOKEN", "blob-token")

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        config.validate_runtime_config()

    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///tmp/booking.sqlite")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        config.validate_runtime_config()


def test_vercel_runtime_guard_requires_production_cookie_cron_and_blob(
    monkeypatch,
):
    monkeypatch.setattr(config, "VERCEL", True)
    monkeypatch.setattr(config, "IS_PRODUCTION", False)
    with pytest.raises(RuntimeError, match="APP_ENV"):
        config.validate_runtime_config()

    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://appointments.example.com")
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://db.example/test")
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        config.validate_runtime_config()

    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    monkeypatch.setattr(config, "CRON_SECRET", "short")
    with pytest.raises(RuntimeError, match="CRON_SECRET"):
        config.validate_runtime_config()

    monkeypatch.setattr(config, "CRON_SECRET", "d" * 64)
    monkeypatch.setattr(config, "BLOB_READ_WRITE_TOKEN", "")
    with pytest.raises(RuntimeError, match="BLOB_READ_WRITE_TOKEN"):
        config.validate_runtime_config()

    monkeypatch.setattr(config, "BLOB_READ_WRITE_TOKEN", "blob-token")
    config.validate_runtime_config()


def test_production_rejects_known_development_secret(monkeypatch):
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setattr(config, "VERCEL", False)
    monkeypatch.setenv("OTP_SECRET", "dev-only-otp-secret-change-before-production-123456789")

    with pytest.raises(RuntimeError, match="OTP_SECRET"):
        config.validate_runtime_config()


def test_vercel_default_paths_are_temporary_and_not_described_as_durable(
    monkeypatch,
):
    monkeypatch.setenv("VERCEL", "1")

    assert config.default_db_path().as_posix() == "/tmp/booking-db.sqlite"  # noqa: S108
    assert config.default_upload_dir().as_posix() == "/tmp/booking-uploads"  # noqa: S108


def test_static_pages_and_entrypoint_are_available(client):
    from api.index import app as vercel_app
    from app import app as main_app

    home = client.get("/")
    owner = client.get("/owner.html")

    assert home.status_code == 200
    assert owner.status_code == 200
    assert "text/html" in home.headers["content-type"]
    assert "text/html" in owner.headers["content-type"]
    assert vercel_app is main_app


def test_vercel_and_postgres_production_contracts_are_present():
    vercel = json.loads((config.BASE_DIR / "vercel.json").read_text(encoding="utf-8"))
    postgres_schema = (config.BASE_DIR / "schema_postgres.sql").read_text(encoding="utf-8")

    assert vercel["crons"] == [{"path": "/api/cron/reminders", "schedule": "*/15 * * * *"}]
    assert "schema_postgres.sql" in vercel["builds"][0]["config"]["includeFiles"]
    assert (config.BASE_DIR / ".python-version").read_text().strip() == "3.12"
    assert "EXCLUDE USING gist" in postgres_schema
    assert "bookings_no_time_overlap" in postgres_schema
    assert "validate_booking_status_transition" in postgres_schema
    assert "notification_jobs" in postgres_schema
    assert "rate_limit_events" in postgres_schema


def test_frontend_has_no_dangerous_html_sink_or_embedded_credentials():
    static_dir = config.BASE_DIR / "static"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (static_dir / "app.js", static_dir / "owner.js")
    )

    assert "innerHTML" not in source
    assert "insertAdjacentHTML" not in source
    assert "eval(" not in source
    assert "xkeysib-" not in source
    assert "xsmtpsib-" not in source
    assert "GOOGLE_CLIENT_SECRET" not in source
    assert "MAILJET_SECRET_KEY" not in source
    assert "לאמת את הטלפון" not in source


def test_malformed_json_and_long_input_return_sanitized_validation_error(client):
    malformed = client.post(
        "/api/auth/request-code",
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    too_long = client.post("/api/auth/request-code", json={"email": "a" * 300 + "@example.com"})

    assert malformed.status_code == 422
    assert too_long.status_code == 422
    assert "traceback" not in malformed.text.lower()
    assert str(config.BASE_DIR) not in malformed.text
