import base64
import io
import json
import urllib.error

import db
import mailer


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


def test_mailjet_full_success_escapes_html_and_preserves_ics(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse({"Messages": [{"Status": "success"}]})

    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    ics = "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

    status = mailer.send_email(
        "USER@example.com",
        "booking_approved",
        "<Approval>",
        "Hello <script>alert(1)</script>",
        details=[("Name", "<img src=x onerror=alert(1)>")],
        ics_content=ics,
    )

    assert status == "mailjet:200"
    request, timeout = requests[0]
    payload = json.loads(request.data.decode("utf-8"))
    message = payload["Messages"][0]
    assert timeout == 15
    assert "<script>" not in message["HTMLPart"]
    assert "&lt;script&gt;" in message["HTMLPart"]
    assert "&lt;img" in message["HTMLPart"]
    assert base64.b64decode(message["Attachments"][0]["Base64Content"]).decode() == ics
    assert request.get_header("Authorization").startswith("Basic ")
    with db.get_conn() as conn:
        log = conn.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 1").fetchone()
    assert log["recipient"] == "user@example.com"
    assert log["provider_status"] == "mailjet:200"


def test_mailjet_partial_message_failure_is_not_marked_success(monkeypatch):
    monkeypatch.setattr(
        mailer.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(
            {"Messages": [{"Status": "error", "Errors": [{"ErrorCode": "x"}]}]}
        ),
    )

    status = mailer.send_email("customer@example.com", "booking_received", "Title", "Message")

    assert status == "mailjet-response-error:200"


def test_mailjet_rejects_unexpected_message_count(monkeypatch):
    monkeypatch.setattr(
        mailer.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(
            {"Messages": [{"Status": "success"}, {"Status": "success"}]}
        ),
    )

    status = mailer.send_email("customer@example.com", "booking_received", "Title", "Message")

    assert status == "mailjet-response-error:200"


def test_mailjet_http_failure_is_sanitized(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            mailer.MAILJET_URL,
            401,
            "Unauthorized secret details",
            hdrs=None,
            fp=io.BytesIO(b'{"private":"provider body"}'),
        )

    monkeypatch.setattr(mailer.urllib.request, "urlopen", fail)

    status = mailer.send_email("customer@example.com", "booking_received", "Title", "Message")

    assert status == "mailjet:401"
    assert "private" not in status
    assert "secret" not in status


def test_mailjet_timeout_is_reported_without_raising(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise TimeoutError("network details")

    monkeypatch.setattr(mailer.urllib.request, "urlopen", timeout)

    status = mailer.send_email("customer@example.com", "booking_received", "Title", "Message")

    assert status == "network:TimeoutError"


def test_mailjet_invalid_recipient_and_template_never_reach_network(monkeypatch):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("network must not be called")

    monkeypatch.setattr(mailer.urllib.request, "urlopen", unexpected)

    invalid_recipient = mailer.send_email(
        "bad\r\nBcc: victim@example.com", "otp", "Title", "Message"
    )
    invalid_template = mailer.send_email(
        "customer@example.com", "arbitrary-template", "Title", "Message"
    )

    assert invalid_recipient == "invalid-recipient"
    assert invalid_template == "template-not-allowed"


def test_mailjet_invalid_json_is_not_marked_success(monkeypatch):
    monkeypatch.setattr(
        mailer.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(b"not-json"),
    )

    status = mailer.send_email("customer@example.com", "booking_received", "Title", "Message")

    assert status == "mailjet-response-error:invalid-json"
