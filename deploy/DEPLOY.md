# Deploying the portal — intercontinentalschools.com.ng

Ubuntu **24.04** VPS · Nginx · Gunicorn · PostgreSQL 16 · free HTTPS from Let's Encrypt.

**Use Ubuntu 24.04** (Python 3.12). Ubuntu 22.04 ships Python 3.10, which the pinned
pandas/numpy versions no longer support, so `pip install` would fail there.
Run the commands in order. Lines starting with `$` are typed on the server.

**Server size:** 1–2 vCPU and 2 GB RAM is plenty for one school.

## Layout on the server

```
/srv/intercontinental/
├── app/            the code, cloned from GitHub (the .env file lives here)
├── venv/           Python packages
├── data/           only used if you run on SQLite instead of PostgreSQL
├── media/          public uploads (school logo, branding)
├── private_media/  student documents, passports, reports — never public
├── static/         CSS/JS/images (collectstatic)
└── backups/        nightly database snapshots
```

---

## 1. Point the domain at the server

At your domain registrar, add two DNS records (use your server's public IP):

| Type | Name | Value |
|------|------|-------|
| A    | `@`  | `YOUR.SERVER.IP` |
| A    | `www`| `YOUR.SERVER.IP` |

DNS can take a few minutes to a few hours. Check with `ping intercontinentalschools.com.ng`.

## 2. Prepare the server

```bash
$ sudo apt update && sudo apt upgrade -y
$ sudo apt install -y python3 python3-venv python3-dev build-essential git nginx sqlite3 \
                      wkhtmltopdf fonts-dejavu ffmpeg certbot python3-certbot-nginx ufw \
                      postgresql postgresql-contrib
$ sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw --force enable
$ sudo timedatectl set-timezone Africa/Lagos
```

`wkhtmltopdf` makes the result-sheet and report PDFs. `postgresql` installs the database server
and the `pg_dump` / `pg_restore` tools the backups use. PostgreSQL only listens on the server itself
(localhost), so **do not** open port 5432 in the firewall.

### The database

Make up a long password for the database (keep it for `.env` in step 5):

```bash
$ python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Create the database and its user (paste the password where shown):

```bash
$ sudo -u postgres psql
postgres=# CREATE ROLE intercontinental LOGIN PASSWORD 'PASTE-THE-PASSWORD';
postgres=# CREATE DATABASE intercontinental OWNER intercontinental ENCODING 'UTF8' TEMPLATE template0;
postgres=# \q
```

Size PostgreSQL for a 2 GB server (adjust if yours is larger: about a quarter of the RAM for
`shared_buffers`, half for `effective_cache_size`):

```bash
$ sudo tee /etc/postgresql/16/main/conf.d/intercontinental.conf <<'CONF'
shared_buffers = 512MB
effective_cache_size = 1GB
work_mem = 8MB
maintenance_work_mem = 128MB
max_connections = 100
timezone = 'Africa/Lagos'
CONF
$ sudo systemctl restart postgresql
$ systemctl status postgresql          # "active"
```

## 3. Create the app user and folders

```bash
$ sudo adduser --system --group --home /srv/intercontinental --shell /bin/bash school
$ sudo usermod -aG www-data school
$ sudo mkdir -p /srv/intercontinental/{data,media,private_media,static,backups}
$ sudo chown -R school:www-data /srv/intercontinental
$ sudo chmod 750 /srv/intercontinental/{data,private_media,backups}
$ sudo chmod 755 /srv/intercontinental /srv/intercontinental/{media,static}
```

Allow the `school` user to restart the portal (used by `deploy/update.sh`):

```bash
$ echo 'school ALL=(root) NOPASSWD: /bin/systemctl restart intercontinental' | sudo tee /etc/sudoers.d/intercontinental
```

## 4. Get the code

```bash
$ sudo -iu school
$ git clone https://github.com/Dev-Samuel-Jeremiah/intercontinental.git /srv/intercontinental/app
$ python3 -m venv /srv/intercontinental/venv
$ /srv/intercontinental/venv/bin/pip install --upgrade pip
$ /srv/intercontinental/venv/bin/pip install -r /srv/intercontinental/app/requirements.txt
```

(If the repository is private, GitHub will ask for your username and a
[personal access token](https://github.com/settings/tokens) instead of a password.)

## 5. Settings (.env)

```bash
$ cd /srv/intercontinental/app
$ cp .env.example .env && chmod 600 .env
$ nano .env
```

Fill in at least:

| Setting | What to put |
|---------|-------------|
| `SECRET_KEY` | run `/srv/intercontinental/venv/bin/python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"` and paste the result |
| `FIELD_ENCRYPTION_KEY` | run `/srv/intercontinental/venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — **save a copy somewhere safe**; without it encrypted medical/welfare notes cannot be read |
| `DEBUG` | `False` |
| `DB_PASSWORD` | the database password you made in step 2 (`DB_NAME` / `DB_USER` are already `intercontinental`) |
| `ADMIN_URL` | a hard-to-guess path for Django's built-in admin, e.g. `manage-7f3k9/` |
| `GROQ_API_KEY` | the school's key from console.groq.com — runs every AI feature (exam solutions, report comments, tutor, safety checks on student posts) |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | the school's email account (Gmail: an *App password*) |
| `ADMIN_EMAIL` | where server error alerts go |
| `PAYSTACK_*`, `TERMII_*` / `AT_*` | the school's own keys, when you are ready |

The domain, the PostgreSQL connection and file locations are already set for this server in `.env.example`.

## 6. Create the database and the school

```bash
$ cd /srv/intercontinental/app
$ ../venv/bin/python manage.py migrate
$ ../venv/bin/python manage.py collectstatic --noinput
$ ../venv/bin/python manage.py setup_school \
      --email info@intercontinentalschools.com.ng --phone 080... --address "..., Kano" \
      --admin-username admin --admin-email ict@intercontinentalschools.com.ng
$ ../venv/bin/python manage.py check --deploy
$ exit
```

`setup_school` prints the administrator's temporary password — note it down.

## 7. Start the portal (Gunicorn + Nginx)

```bash
$ sudo cp /srv/intercontinental/app/deploy/intercontinental.service /etc/systemd/system/
$ sudo systemctl daemon-reload
$ sudo systemctl enable --now intercontinental
$ systemctl status intercontinental            # should say "active (running)"

$ sudo cp /srv/intercontinental/app/deploy/nginx.conf /etc/nginx/sites-available/intercontinental
$ sudo ln -sf /etc/nginx/sites-available/intercontinental /etc/nginx/sites-enabled/
$ sudo rm -f /etc/nginx/sites-enabled/default
$ sudo nginx -t && sudo systemctl reload nginx
```

## 8. Turn on HTTPS

```bash
$ sudo certbot --nginx -d intercontinentalschools.com.ng -d www.intercontinentalschools.com.ng \
      --redirect --agree-tos -m ict@intercontinentalschools.com.ng
```

Certificates renew automatically. Open **https://intercontinentalschools.com.ng** — the
landing page should load, and **/staff/login/** signs in with the admin password from step 6.

## 9. Backups (do not skip)

Install the daily backup timer (database every night at 01:30, plus uploaded files on Sundays):

```bash
$ sudo cp /srv/intercontinental/app/deploy/intercontinental-backup.* /etc/systemd/system/
$ sudo systemctl daemon-reload
$ sudo systemctl enable --now intercontinental-backup.timer
$ systemctl list-timers intercontinental-backup.timer      # shows the next run
```

And the school AI's nightly learning (02:30 — it remembers the day, learns from new results and
content, retrains its models and updates its predictions; if a night is missed the site catches up
by itself):

```bash
$ sudo cp /srv/intercontinental/app/deploy/intercontinental-ai.* /etc/systemd/system/
$ sudo systemctl daemon-reload
$ sudo systemctl enable --now intercontinental-ai.timer
```

And the school's own data export (shown under Administration → Data Export & Backups):

```bash
$ sudo crontab -u school -e
15 2 * * *  cd /srv/intercontinental/app && ../venv/bin/python manage.py nightly_backup
```

Every backup is integrity-checked and recorded under **Security → Platform & Backups**, where
administrators can see the last good backup, start one, download one, and see the system's
health. If the timer is ever missed, the site starts a backup itself once the last good one is
more than a day old, and administrators are notified if a backup fails.

Each night `pg_dump` writes a compressed dump, `db-YYYYMMDD-HHMMSS.pgdump`, which is checked by reading it
back before it counts as a good backup. Dumps go to `/srv/intercontinental/backups/` and are kept for
14 days (`BACKUP_KEEP_DAYS`).
**Copy them off the server** — a backup on the same disk is lost with the disk. Either mount a
second disk or network share and set, in `.env`:

```
BACKUP_OFFSITE_DIR=/mnt/offsite/school-backups
```

or install [rclone](https://rclone.org) (`sudo apt install rclone`, then `rclone config` to
connect Google Drive, Dropbox, S3…) and set:

```
BACKUP_OFFSITE_COMMAND=rclone copy {file} gdrive:intercontinental-backups
```

---

## 10. Watch the site (uptime monitor)

Create a free monitor at [UptimeRobot](https://uptimerobot.com) or
[Better Stack](https://betterstack.com) for:

```
https://intercontinentalschools.com.ng/healthz/
```

It answers `{"status": "ok"}` when the portal and its database are working, and `503` when not,
so you are emailed or texted within minutes if the site goes down. Server errors (500s) are also
emailed to `ADMIN_EMAIL` with the details.

## Go-live checklist

Tick every line before telling parents and staff the address.

- [ ] Ubuntu 24.04, `sudo apt upgrade` done, firewall on (`sudo ufw status` shows OpenSSH + Nginx Full only)
- [ ] `.env`: `DEBUG=False`, a fresh `SECRET_KEY`, `ALLOWED_HOSTS` = the domain(s), `chmod 600 .env`
- [ ] `FIELD_ENCRYPTION_KEY` set **and a copy stored off the server** (password manager)
- [ ] PostgreSQL running with its own long password (`DB_PASSWORD`), port 5432 **not** open in the firewall,
      `systemctl status postgresql` active, and *Security → Platform & Backups* shows "Database: PostgreSQL 16"
- [ ] `ADMIN_URL` changed from `admin/`
- [ ] Email works: `../venv/bin/python manage.py sendtestemail you@example.com`
- [ ] `../venv/bin/python manage.py check --deploy` shows no issues
- [ ] `../venv/bin/python manage.py security_check` shows no BAD lines (and you understand each WARN)
- [ ] HTTPS works and http:// redirects to https:// (certbot step 8)
- [ ] Backup timer enabled **and** off-site copy set (`BACKUP_OFFSITE_DIR` or `BACKUP_OFFSITE_COMMAND`);
      run one by hand: `../venv/bin/python manage.py backup_database` and confirm the copy arrived
- [ ] Restore tested once on a spare machine (see *Restoring a backup*)
- [ ] Uptime monitor on `/healthz/`
- [ ] Administrator signed in, changed the temporary password and set up two-step sign-in
- [ ] Paystack: live keys in `.env`, webhook URL `https://<domain>/finance/paystack/webhook/` added in the Paystack dashboard
- [ ] SMS/WhatsApp keys (if used); WhatsApp needs `WHATSAPP_APP_SECRET` or its webhook refuses messages
- [ ] Never run `seed_test_data` on the live site (it refuses unless forced) — test data belongs on a copy

## Releasing an update

On your computer: commit and `git push`. On the server:

```bash
$ sudo -iu school
$ cd /srv/intercontinental/app && ./deploy/update.sh
```

It backs up the database, pulls the code, installs packages, migrates, collects static
files, checks the settings and restarts the portal.

## Restoring a backup

The portal is stopped, the current database is kept under another name (so you can go back), and
the dump is loaded into a fresh, empty database:

```bash
$ sudo systemctl stop intercontinental
$ sudo -u postgres psql -c "ALTER DATABASE intercontinental RENAME TO intercontinental_before_restore"
$ sudo -u postgres psql -c "CREATE DATABASE intercontinental OWNER intercontinental ENCODING 'UTF8' TEMPLATE template0"
$ sudo -u postgres pg_restore --no-owner --role=intercontinental -d intercontinental \
      /srv/intercontinental/backups/db-YYYYMMDD-HHMMSS.pgdump
$ sudo systemctl start intercontinental
```

Sign in and check. When you are happy, drop the old copy:
`sudo -u postgres psql -c "DROP DATABASE intercontinental_before_restore"`.

(On SQLite instead: stop the portal, `gunzip -c db-….sqlite3.gz > /srv/intercontinental/data/db.sqlite3`,
delete the `-wal` / `-shm` files next to it, start the portal.)

## Moving an existing SQLite portal to PostgreSQL

If the school has already been using the portal on SQLite (or you want to bring the data from a
test machine), copy everything across — every row keeps its id, so sign-ins, uploaded files,
results and payments stay exactly as they were:

```bash
$ sudo systemctl stop intercontinental                     # nothing must change during the copy
$ sudo -iu school
$ cd /srv/intercontinental/app
$ ../venv/bin/python manage.py backup_database             # a last SQLite backup, just in case
$ nano .env                                                # DB_ENGINE=postgresql + DB_PASSWORD (step 5)
$ ../venv/bin/python manage.py migrate                     # creates the empty PostgreSQL tables
$ ../venv/bin/python manage.py copy_sqlite_to_postgres --sqlite /srv/intercontinental/data/db.sqlite3
$ exit
$ sudo systemctl start intercontinental
```

The copy checks first that both databases are at the same version and that no text is longer than
PostgreSQL allows, runs in one transaction (all or nothing), and finishes by comparing every
table's row count. Keep the same `SECRET_KEY` and `FIELD_ENCRYPTION_KEY` so encrypted notes stay
readable. To go back, set `DB_ENGINE=sqlite` again — the SQLite file is not changed.

## When something is wrong

| Look at | Command |
|---------|---------|
| Portal errors | `journalctl -u intercontinental -n 100 --no-pager` |
| Live log | `journalctl -u intercontinental -f` |
| Nginx errors | `sudo tail -n 50 /var/log/nginx/intercontinental.error.log` |
| 502 Bad Gateway | the portal is not running: `sudo systemctl restart intercontinental`, then check its log |
| Static files missing | rerun `collectstatic` (step 6) and check `/srv/intercontinental/static` exists |
| Database errors / "connection refused" | `systemctl status postgresql`, then `sudo tail -n 50 /var/log/postgresql/postgresql-16-main.log` |
| "password authentication failed" | `DB_PASSWORD` in `.env` differs from the one in step 2 — reset it: `sudo -u postgres psql -c "ALTER ROLE intercontinental PASSWORD '…'"` |

## Notes

- **PostgreSQL** is the production database: many people can save at once (results entry week,
  fee deadlines), and Gunicorn runs more workers on it (`deploy/gunicorn.conf.py`). Connections are
  kept open between requests and checked before reuse (`DB_CONN_MAX_AGE`).
- A database on another machine (managed PostgreSQL) works too: set `DB_HOST`, `DB_PORT` and
  `DB_SSLMODE=require`; install `postgresql-client-16` on the app server for backups (`pg_dump` must
  be the same major version as the server or newer).
- **SQLite** still works for a small install or local development (`DB_ENGINE=sqlite`).
- Never commit `.env` or the database. `.gitignore` already keeps them out.
