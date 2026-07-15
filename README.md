# מערכת תורים יעל

מערכת תורים לעסק יחיד: FastAPI, SQLite, HTML/CSS/JS ללא framework בפרונט.

## הרצה מקומית

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app:app --host 0.0.0.0 --port 8000
```

עמוד לקוחות: `http://localhost:8000/`

עמוד ניהול: `http://localhost:8000/owner.html`

## הגדרות פרודקשן

בפרודקשן חובה להגדיר ב-`.env`:

```env
OWNER_EMAIL=owner@example.com
OTP_SECRET=<random-64-hex>
SESSION_SECRET=<random-64-hex>
MAIL_PROVIDER=mailjet
MAILJET_API_KEY=<real-mailjet-api-key>
MAILJET_SECRET_KEY=<real-mailjet-secret-key>
MAILJET_SENDER_EMAIL=<verified-sender-in-mailjet>
MAILJET_SENDER_NAME=Appointments
GOOGLE_CALENDAR_ENABLED=true
GOOGLE_CLIENT_ID=<google-oauth-client-id>
GOOGLE_CLIENT_SECRET=<google-oauth-client-secret>
GOOGLE_REFRESH_TOKEN=<google-oauth-refresh-token>
GOOGLE_CALENDAR_ID=primary
COOKIE_SECURE=true
ALLOW_INSECURE_DEV_SECRETS=false
TZ=Asia/Jerusalem
DB_PATH=./data/db.sqlite
UPLOAD_DIR=./data/uploads
```

`MAILJET_SENDER_EMAIL` חייב להיות שולח מאומת בחשבון Mailjet. Mailjet משמש רק
לשליחת קוד האימות החד־פעמי בכניסה. עדכוני תורים ותזכורות אינם נשלחים במייל.

השרת צריך לרוץ מאחורי HTTPS. אם אין HTTPS, עוגיית ההתחברות לא צריכה להיות `Secure`, אבל בפרודקשן כן.

## גיבוי

SQLite הוא קובץ אחד, ולכן קל לגבות:

```powershell
sqlite3 data\db.sqlite ".backup 'backup\db-YYYY-MM-DD.sqlite'"
```

צריך לגבות גם את `data/uploads`.

## מושגים לאורי

שרת הוא תוכנה שמחכה לבקשות מהדפדפן. כאן `app.py` מקבל בקשות כמו "תן לי שעות פנויות", בודק במסד הנתונים, ומחזיר תשובה.

מסד נתונים הוא המקום שבו שומרים מידע קבוע. כאן זה קובץ SQLite בשם `data/db.sqlite`.

API הוא אוסף כתובות שהדפדפן יודע לקרוא להן. למשל `/api/slots` מחזיר שעות פנויות, ו-`/api/bookings` יוצר תור חדש.
