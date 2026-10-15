# Deploying the portal — intercontinentalschools.com.ng

Ubuntu 22.04/24.04 VPS · Nginx · Gunicorn · SQLite · free HTTPS from Let's Encrypt.
Run the commands in order. Lines starting with `$` are typed on the server.

**Server size:** 1–2 vCPU and 2 GB RAM is plenty for one school.

## Layout on the server

```
/srv/intercontinental/
├── app/            the code, cloned from GitHub (the .env file lives here)
├── venv/           Python packages
├── data/           db.sqlite3  ← the database, never inside app/
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
                      wkhtmltopdf fonts-dejavu certbot python3-certbot-nginx ufw
$ sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw --force enable
$ sudo timedatectl set-timezone Africa/Lagos
```

`wkhtmltopdf` makes the result-sheet and report PDFs.

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
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | the school's email account (Gmail: an *App password*) |
| `ADMIN_EMAIL` | where server error alerts go |
| `PAYSTACK_*`, `TERMII_*` / `AT_*` | the school's own keys, when you are ready |

The domain, SQLite and file locations are already set for this server in `.env.example`.

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

```bash
$ sudo crontab -u school -e
```

Add:

```cron
# Database snapshot every night at 01:30, plus uploaded files every Sunday
30 1 * * *  cd /srv/intercontinental/app && ../venv/bin/python manage.py backup_database
45 1 * * 0  cd /srv/intercontinental/app && ../venv/bin/python manage.py backup_database --media
# The school's own data export (shown under Administration → Data Export & Backups)
15 2 * * *  cd /srv/intercontinental/app && ../venv/bin/python manage.py nightly_backup
```

Snapshots go to `/srv/intercontinental/backups/` and are kept for 14 days. **Copy them off
the server regularly** (download them, or use your VPS provider's backup feature) — a
backup on the same disk is lost with the disk.

---

## Releasing an update

On your computer: commit and `git push`. On the server:

```bash
$ sudo -iu school
$ cd /srv/intercontinental/app && ./deploy/update.sh
```

It backs up the database, pulls the code, installs packages, migrates, collects static
files, checks the settings and restarts the portal.

## Restoring a backup

```bash
$ sudo systemctl stop intercontinental
$ sudo -iu school
$ cp /srv/intercontinental/data/db.sqlite3 /srv/intercontinental/data/db.sqlite3.before-restore
$ rm -f /srv/intercontinental/data/db.sqlite3-wal /srv/intercontinental/data/db.sqlite3-shm
$ gunzip -c /srv/intercontinental/backups/db-YYYYMMDD-HHMMSS.sqlite3.gz > /srv/intercontinental/data/db.sqlite3
$ exit
$ sudo systemctl start intercontinental
```

## When something is wrong

| Look at | Command |
|---------|---------|
| Portal errors | `journalctl -u intercontinental -n 100 --no-pager` |
| Live log | `journalctl -u intercontinental -f` |
| Nginx errors | `sudo tail -n 50 /var/log/nginx/intercontinental.error.log` |
| 502 Bad Gateway | the portal is not running: `sudo systemctl restart intercontinental`, then check its log |
| Static files missing | rerun `collectstatic` (step 6) and check `/srv/intercontinental/static` exists |

## Notes

- **SQLite** is fine for one school: WAL mode and write queuing are switched on in
  `lms_project/settings.py`. If the school grows to many thousands of daily users, move to
  PostgreSQL (`DB_ENGINE=postgresql` in `.env`) — nothing else in the code changes.
- Never commit `.env` or the database. `.gitignore` already keeps them out.
