#!/usr/bin/env bash
# Deploy the latest code from GitHub. Run on the server as the `school` user:
#     cd /srv/intercontinental/app && ./deploy/update.sh
# A database snapshot is taken first, so a bad migration can be rolled back.
set -euo pipefail

APP=/srv/intercontinental/app
PY=/srv/intercontinental/venv/bin/python
PIP=/srv/intercontinental/venv/bin/pip
cd "$APP"

echo "→ Backing up the database"
$PY manage.py backup_database --trigger UPDATE

echo "→ Fetching the latest code"
git pull --ff-only

echo "→ Installing packages"
$PIP install -q -r requirements.txt

echo "→ Updating the database"
$PY manage.py migrate --noinput

echo "→ Collecting static files"
$PY manage.py collectstatic --noinput -v 0

echo "→ Checking settings"
$PY manage.py check --deploy

echo "→ Restarting the portal"
sudo systemctl restart intercontinental
sleep 2
systemctl is-active --quiet intercontinental && echo "✓ Portal is running" || {
    echo "✗ The portal did not start — see: journalctl -u intercontinental -n 50"; exit 1; }
