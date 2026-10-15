# Intercontinental School Kano — School Management System

A complete school management system for one school: academics, CBT, results,
admissions, attendance, timetable, classroom, finance and fees, payroll, HR,
hostel, transport, library, welfare, facilities, inventory, communications,
the parent and student portals, and more.

This is a dedicated installation. The whole site is the school's portal: the
home page is the school's landing page (`templates/landing.html`) with sign-in
at `/staff/login/`, there are no subscriptions or plans, and
every module is switched on.

## Sections

The school has three sections, created by `setup_school`:

| Section            | Code |
|--------------------|------|
| Nursery & Primary  | NP   |
| Secondary          | SEC  |
| Darun              | DAR  |

Administrators put classes and staff into sections under
**Administration & Help → Sections**. A class belongs to one section; a
staff member can work in several (useful for the combined lessons Darun pupils
join after 12pm).

## First-time setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill it in
python manage.py migrate
python manage.py setup_school \
    --email info@<school-domain> --phone 080... --address "..., Kano" \
    --admin-username admin --admin-email ict@<school-domain>
python manage.py collectstatic --noinput
```

`setup_school` prints the administrator's temporary password. They must change it
the first time they sign in at `/staff/login/`. Running it again is safe;
anything that already exists is left as it is.

Then, signed in as the administrator:

1. **Settings**: upload the school logo, motto, address and phone number. These
   appear on result sheets, receipts, invoices and payslips.
2. **Administration & Help → Branding**: set the colours, the browser icon and
   the sign-in page message.
3. **Administration & Help → Sections**: put classes and staff into their sections.

For the Django admin site (`/admin/`), create a separate server account with
`python manage.py createsuperuser`.

## Scheduled jobs (cron)

```cron
30 1 * * *  cd /srv/intercontinental/app && ../venv/bin/python manage.py backup_database
15 2 * * *  cd /srv/intercontinental/app && ../venv/bin/python manage.py nightly_backup
```

`backup_database` takes a consistent snapshot of the SQLite database into
`BACKUP_DIR` (add `--media` to include uploaded files). `nightly_backup` is the
school's own data export, listed under **Administration & Help → Data Export &
Backups**. Both keep 14 days. Copy a recent one off the server regularly.

Other apps provide their own management commands (reminders, retention and so
on). Run `python manage.py help` to see them.

## Production notes

**Full step-by-step guide: [deploy/DEPLOY.md](deploy/DEPLOY.md)** — Ubuntu VPS,
Nginx, Gunicorn, SQLite and HTTPS for intercontinentalschools.com.ng. Config files
are in `deploy/`; `deploy/update.sh` releases a new version.

- The database is SQLite in WAL mode (`SQLITE_PATH` in `.env`, outside the code
  folder). Switching to PostgreSQL later is a `.env` change (`DB_ENGINE=postgresql`).
- Point the portal's domain (`ROOT_DOMAIN`) at the server, with Nginx in front of
  Gunicorn. TLS is terminated at Nginx; `SECURE_PROXY_SSL_HEADER` is already set.
- Paystack, SMS (Termii / Africa's Talking), email and AI keys all belong to the
  school's own accounts. Fee payments go straight to the school's Paystack account.
- Set `FIELD_ENCRYPTION_KEY` once and keep a safe copy. Without it, encrypted
  medical, counselling and safeguarding notes cannot be read.
- `SMS_METERING` stays off, because the school pays its SMS provider directly.

## Running the tests

```bash
python manage.py test tests --parallel 4
```
