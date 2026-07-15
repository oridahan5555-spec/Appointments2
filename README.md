# מערכת תורים יעל

מערכת תורים לעסק יחיד עם FastAPI, ממשק HTML/CSS/JavaScript, אימות במייל,
תזכורות Mailjet וסנכרון Google Calendar.

## הרצה מקומית

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

לפני ההרצה יש להחליף את ה-placeholders ב-`.env`. הקובץ הזה חסוי ומוחרג מ-Git.

- לקוחות: `http://localhost:8000/`
- ניהול: `http://localhost:8000/owner.html`

שרת הוא התוכנית שמקבלת בקשות מהדפדפן. ה-API ב-`app.py` בודק הרשאות וקלט,
קורא או כותב למסד הנתונים, ומחזיר תשובה לדפדפן.

## בדיקות

הבדיקות משתמשות במסד זמני ומדמות את Mailjet ואת Google. הן אינן שולחות מיילים
ואינן יוצרות אירועים אמיתיים.

```powershell
python -m pytest -q --cov=.
python -m ruff check . --exclude data --exclude test-results
python -m mypy app.py auth.py availability.py calendar_sync.py config.py db.py google_calendar.py mailer.py notifications.py secret_crypto.py storage.py api/index.py
python -m pip check
python -m pip_audit -r requirements.txt
python -m pip_audit -r requirements-dev.txt
node --check static/app.js
node --check static/owner.js
python -m playwright install chromium
# בחלון נוסף, כשהשרת פועל על http://127.0.0.1:8000:
python tests/browser_audit.py
```

## אחסון

בפיתוח מקומי אפשר להשתמש ב-SQLite ובתיקיית תמונות מקומית. החיבור מפעיל foreign
keys, WAL ו-busy timeout. לפני שינוי סכימה ישנה נוצר עותק
`data/db.pre-migration-*.sqlite` אוטומטי.

ב-Vercel אסור להסתמך על `/tmp`: המידע נמחק ואינו משותף בין פונקציות. לכן השרת
מסרב לעלות ב-Vercel בלי:

- PostgreSQL קבוע דרך `DATABASE_URL`, למשל Neon מתוך Vercel Marketplace.
- Vercel Blob דרך `BLOB_READ_WRITE_TOKEN` עבור תמונות העסק.
- `CRON_SECRET` עבור נתיב התזכורות.

אין לבצע העברה אוטומטית של מסד קיים. לפני פריסה עם לקוחות אמיתיים יש לגבות את
SQLite, ליצור PostgreSQL ריק, ולבצע העברת נתונים חד-פעמית ומבוקרת. יש להשוות
לפני ואחרי את מספר הלקוחות, התורים, השירותים והתזכורות.

## Mailjet

`MAILJET_SENDER_EMAIL` חייב להיות שולח מאומת ב-Mailjet. Mailjet שולח:

- קודי OTP בני 6 ספרות.
- הודעה לבעלת העסק וללקוחה על בקשת תור.
- אישור, דחייה, ביטול ושינוי מועד.
- תזכורות ללקוחה ולבעלת העסק 24 שעות ו-3 שעות לפני תור מאושר.
- קובץ ICS עם התראה 15 דקות לפני, כאשר הוא רלוונטי.

השליחה נשמרת כתור משימות idempotent עם ניסיונות חוזרים. תשובת HTTP של Mailjet
נחשבת הצלחה רק אם הודעת הספק עצמה מסומנת `success` וללא שגיאות.

## Google Calendar

1. ב-Google Cloud יש להפעיל את Google Calendar API.
2. יש ליצור OAuth Client מסוג Web application.
3. יש להוסיף Authorized redirect URI זהה בדיוק ל-`GOOGLE_REDIRECT_URI`.
4. לאחר הפריסה, בעלת העסק נכנסת לניהול ולוחצת על חיבור Google Calendar פעם אחת.

ה-refresh token נשמר מוצפן ב-PostgreSQL. אין להכניס אותו ל-JavaScript. אישור תור
יוצר אירוע עם התראה 15 דקות לפני; שינוי או ביטול מעדכנים או מוחקים אותו. כשל
Google אינו מבטל את פעולת התור, והוא נשמר לניסיון חוזר.

## תזכורות

`vercel.json` קורא ל-`/api/cron/reminders` כל 15 דקות. Vercel שולח את
`CRON_SECRET` כ-Bearer token והנתיב דוחה קריאה ללא התאמה מלאה.

תוכנית Hobby של Vercel אינה מספיקה ללוח זמנים של כל 15 דקות. לפני השקה צריך
לבחור אחת משתי אפשרויות:

1. לעבור לתוכנית Vercel שתומכת בתדירות הזו.
2. להשתמש בשירות scheduler חיצוני מאובטח ששולח את אותו Bearer token.

## משתני Vercel

יש להגדיר Production, Preview ו-Development בנפרד. בפרודקשן נדרשים השמות הבאים:

```text
APP_ENV
OWNER_EMAIL
OTP_SECRET
SESSION_SECRET
TOKEN_ENCRYPTION_KEY
MAIL_PROVIDER
MAILJET_API_KEY
MAILJET_SECRET_KEY
MAILJET_SENDER_EMAIL
MAILJET_SENDER_NAME
APP_TIMEZONE
PUBLIC_BASE_URL
ALLOWED_HOSTS
TRUST_PROXY_HEADERS
DATABASE_URL
BLOB_READ_WRITE_TOKEN
CRON_SECRET
COOKIE_SECURE
ALLOW_INSECURE_DEV_SECRETS
GOOGLE_CALENDAR_ENABLED
GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET
GOOGLE_CALENDAR_ID
GOOGLE_REDIRECT_URI
```

ערכי הפרודקשן העקרוניים:

```text
APP_ENV=production
APP_TIMEZONE=Asia/Jerusalem
MAIL_PROVIDER=mailjet
COOKIE_SECURE=true
ALLOW_INSECURE_DEV_SECRETS=false
TRUST_PROXY_HEADERS=true
GOOGLE_CALENDAR_ENABLED=true
```

`OTP_SECRET`, `SESSION_SECRET`, `TOKEN_ENCRYPTION_KEY` ו-`CRON_SECRET` צריכים
להיות ערכים אקראיים שונים באורך 32 בתים לפחות. אין להשתמש בערכי הדוגמה.

## פריסה ל-Vercel

1. ליצור PostgreSQL ו-Blob קבועים ולשמור גיבוי של SQLite המקומי.
2. להגדיר את כל משתני הסביבה לעיל בלי להעלות `.env` ל-GitHub.
3. להגדיר ב-Google את redirect URI של כתובת הפרודקשן המדויקת.
4. לפרוס commit בדוק מ-GitHub.
5. לפתוח `/api/business` ולוודא תשובת 200 וכותרות אבטחה.
6. להיכנס לניהול עם `OWNER_EMAIL` ולחבר Google פעם אחת.
7. לבצע תור בדיקה, לאשר, לשנות ולבטל אותו.
8. לוודא מיילים, אירוע Google ותזכורת Cron בלוגים המסוננים.
9. רק לאחר הבדיקות לפתוח את האתר ללקוחות אמיתיים.

## שחזור ו-Rollback

1. לעצור קביעת תורים חדשים או להחזיר את deployment הקודם ב-Vercel.
2. לא למחוק את מסד הנתונים הפעיל.
3. לשמור snapshot של PostgreSQL ושל Blob לפני שחזור.
4. לשחזר מגיבוי provider לנקודת הזמן הנדרשת.
5. לפרוס את commit היציב הקודם.
6. להריץ בדיקת עשן לקריאה, התחברות, קביעה וסנכרון לפני פתיחה מחדש.

סודות שנחשפו בעבר אינם משוחזרים. יש לבטל אותם אצל הספק ולהפיק חדשים.
