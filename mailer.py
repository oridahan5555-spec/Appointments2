import base64
import html
import json
import urllib.error
import urllib.request

import config
import db

MAILJET_URL = "https://api.mailjet.com/v3.1/send"

SUBJECTS = {
    "otp": "קוד האימות שלך",
}


def _details_html(details: list[tuple[str, str]]) -> str:
    if not details:
        return ""

    rows = "".join(
        f'<tr><td style="padding:8px 0;color:#7b6670;width:34%;">'
        f'{html.escape(str(label))}</td>'
        f'<td style="padding:8px 0;color:#321824;font-weight:700;">'
        f'{html.escape(str(value))}</td></tr>'
        for label, value in details
    )

    return (
        '<table role="presentation" '
        'style="width:100%;margin:22px 0;border-collapse:collapse;">'
        f"{rows}</table>"
    )


def _html_email(
    title: str,
    message: str,
    details: list[tuple[str, str]],
    code: str | None,
) -> str:
    code_html = ""

    if code:
        code_html = (
            '<div style="margin:24px 0;padding:18px;text-align:center;'
            'background:#fff3f7;border:1px solid #f0cbd8;'
            'border-radius:8px;font-size:32px;font-weight:800;'
            'color:#8f2750;direction:ltr;">'
            f"{html.escape(str(code))}</div>"
        )

    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
  <meta charset="utf-8">
</head>
<body style="margin:0;background:#f8f5f6;font-family:Arial,sans-serif;color:#321824;">
  <table role="presentation" style="width:100%;border-collapse:collapse;">
    <tr>
      <td style="padding:28px 14px;">
        <table role="presentation"
          style="width:100%;max-width:560px;margin:auto;background:#ffffff;
          border:1px solid #eadfe3;border-collapse:separate;
          border-radius:8px;overflow:hidden;">
          <tr>
            <td style="height:8px;background:#c63d70;"></td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <div style="font-size:14px;font-weight:700;color:#a42f5c;
                margin-bottom:10px;">Appointments</div>

              <h1 style="font-size:26px;line-height:1.3;margin:0 0 14px;
                color:#321824;">{html.escape(str(title))}</h1>

              <p style="font-size:16px;line-height:1.7;margin:0;
                color:#604b54;">{html.escape(str(message))}</p>

              {code_html}
              {_details_html(details)}

              <p style="font-size:13px;line-height:1.6;margin:24px 0 0;
                color:#8c7881;">
                הודעה זו נשלחה אוטומטית ממערכת התורים.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_email(
    recipient: str,
    template: str,
    title: str,
    message: str,
    details: list[tuple[str, str]] | None = None,
    code: str | None = None,
    ics_content: str | None = None,
) -> str:
    details = details or []

    if template != "otp":
        status = "template-not-allowed"
        with db.get_conn() as conn:
            db.log_email(conn, recipient, template, status)
        return status

    api_key = config.MAILJET_API_KEY
    secret_key = config.MAILJET_SECRET_KEY
    sender_email = config.MAILJET_SENDER_EMAIL
    sender_name = config.MAILJET_SENDER_NAME or "Appointments"

    if not api_key or not secret_key or not sender_email:
        status = "configuration-missing"

        with db.get_conn() as conn:
            db.log_email(conn, recipient, template, status)

        return status

    text_lines = [title, "", message]

    if code:
        text_lines.extend(["", f"קוד האימות: {code}"])

    text_lines.extend(
        f"{label}: {value}"
        for label, value in details
    )

    mailjet_message = {
        "From": {
            "Email": sender_email,
            "Name": sender_name,
        },
        "To": [
            {
                "Email": recipient,
            }
        ],
        "Subject": SUBJECTS.get(template, title),
        "TextPart": "\n".join(text_lines),
        "HTMLPart": _html_email(title, message, details, code),
        "CustomID": template.replace("_", "-")[:100],
    }

    if ics_content:
        mailjet_message["Attachments"] = [
            {
                "ContentType": "text/calendar; charset=utf-8; method=PUBLISH",
                "Filename": "appointment.ics",
                "Base64Content": base64.b64encode(
                    ics_content.encode("utf-8")
                ).decode("ascii"),
            }
        ]

    payload = {
        "Messages": [mailjet_message]
    }

    credentials = base64.b64encode(
        f"{api_key}:{secret_key}".encode("utf-8")
    ).decode("ascii")

    request = urllib.request.Request(
        MAILJET_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Basic {credentials}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response_body = json.loads(response.read().decode("utf-8"))
            messages = response_body.get("Messages", [])
            all_sent = bool(messages) and all(
                str(item.get("Status", "")).lower() == "success"
                for item in messages
            )
            status = f"mailjet:{response.status}" if all_sent else f"mailjet-response-error:{response.status}"

    except urllib.error.HTTPError as exc:
        status = f"mailjet:{exc.code}"

    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        status = "mailjet-response-error:invalid-json"

    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        status = f"network:{type(exc).__name__}"

    with db.get_conn() as conn:
        db.log_email(conn, recipient, template, status)

    return status
