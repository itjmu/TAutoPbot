# Upgrade an existing server without replacing its database

Use `dist/tautopbot-update.zip`, built with `python deploy/package_release.py`.
It contains an explicit allowlist of application code, locales, tests and deployment
instructions. It excludes local databases, `.env`, cookies, backups, logs, virtual
environments and systemd units. Do not upload the entire desktop folder.

The commands below assume the existing installation uses `/opt/tautopbot`,
service `tautopbot`, user `tautopbot`, and database `/var/lib/tautopbot/bot.db`.
If your installation differs, substitute its actual paths throughout. Keep its
existing service, environment file, BOT_TOKEN, ADMIN_ID, DB_FILE and cookie paths.
Do not print or paste secrets. Verify the database path in your existing service
configuration privately before proceeding. Never run `reset`, delete the database,
clear Telegram updates, or start a second polling process during an upgrade.

## 1. Upload and stage (bot can still run)

Upload **only** the ZIP to `/tmp/tautopbot-update.zip` using SFTP/SCP. On the server:

```sh
set -eu
STAGE=$(mktemp -d /tmp/tautopbot-release.XXXXXX)
python3 -m zipfile -e /tmp/tautopbot-update.zip "$STAGE"
python3 -m venv "$STAGE/.venv"
# Preserve installed dependency versions first; requirements then fill missing ones.
sudo /opt/tautopbot/.venv/bin/python -m pip freeze > "$STAGE/server-requirements.txt"
"$STAGE/.venv/bin/python" -m pip install -r "$STAGE/server-requirements.txt"
"$STAGE/.venv/bin/python" -m pip install -r "$STAGE/requirements.txt" -r "$STAGE/requirements-dev.txt"
(cd "$STAGE" && .venv/bin/python -m unittest discover -s tests -v)
```

Stop here if dependencies or tests fail. No production data has been changed.
Use the same server shell for the remaining commands so variables are retained.

## 2. Stop, back up and test migration on a copy

```sh
sudo systemctl stop tautopbot
if sudo systemctl is-active --quiet tautopbot; then exit 1; fi
sudo test -f /var/lib/tautopbot/bot.db
BACKUP=/var/backups/tautopbot-$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 700 "$BACKUP"
sudo tar -C /opt -czf "$BACKUP/code-and-venv.tgz" tautopbot
sudo chmod 600 "$BACKUP/code-and-venv.tgz"
SNAPSHOT=$(cd /opt/tautopbot && sudo -u tautopbot .venv/bin/python manage.py backup --database /var/lib/tautopbot/bot.db)
sudo cp -- "$SNAPSHOT" "$BACKUP/bot.db"
sudo chmod 600 "$BACKUP/bot.db"
CHECK_DB="$STAGE/migration-check.sqlite3"
sudo cp -- "$SNAPSHOT" "$CHECK_DB"
sudo chown "$(id -u):$(id -g)" "$CHECK_DB"
(cd "$STAGE" && DB_FILE="$CHECK_DB" .venv/bin/python -c 'from app import database; database.connect(); database.init_db(); database.db.close()')
"$STAGE/.venv/bin/python" "$STAGE/manage.py" check --database "$CHECK_DB"
```

The SQLite backup includes committed WAL data. Do not substitute a live `cp bot.db`
for that backup. If migration fails, keep the current code and restart the old
service; investigate the copy. The production DB has not been migrated yet.
Keep backups on the server and an encrypted off-server copy; never include them
in the release ZIP.

## 3. Apply code and restart once

```sh
# ZIP contains only allowlisted code; no .env or database, and no deletion step.
sudo python3 -m zipfile -e /tmp/tautopbot-update.zip /opt/tautopbot
sudo /opt/tautopbot/.venv/bin/python -m pip install -r /opt/tautopbot/requirements.txt
sudo systemctl start tautopbot
sudo systemctl status tautopbot --no-pager
sudo journalctl -u tautopbot -n 80 --no-pager
```

Startup applies the existing migrations to the **server's existing DB_FILE**.
Confirm one process, no migration/config errors, and the expected existing users,
channels and schedules. After startup settles, run:

```sh
sudo -u tautopbot /opt/tautopbot/.venv/bin/python /opt/tautopbot/manage.py health --database /var/lib/tautopbot/bot.db
```

Test a private draft, round-video preview, and scheduled list before publishing.
A real broadcast sends to users: do not use it as a smoke test. Forward mode applies
to newly submitted forwarded broadcasts; already queued legacy jobs keep copy mode.
Scheduled work resumes immediately on startup, including overdue jobs.

## If startup fails

Stop the service first. Preserve the failed code/database for diagnosis. Restore
the previous code and virtual environment from `code-and-venv.tgz` to their original
paths. Prefer a code-only rollback if the old version accepts the migrated schema.
Database rollback is a separate decision: restoring the snapshot loses changes
since the backup, and Telegram messages already sent are not undone.

If a DB restore is necessary, with every writer stopped, move the current database
and any `-wal` / `-shm` sidecars into a private recovery directory before restoring
`$BACKUP/bot.db` to the configured path (owner `tautopbot:tautopbot`, mode `600`).
Never overlay a snapshot while stale sidecars or a running writer remain. Run
`manage.py check` before starting once. Review uncertain deliveries and channel
messages before retrying to avoid duplicates.

These steps are a tested-code deployment procedure, not a guarantee for an unknown
server. Linux/systemd execution and live Telegram delivery must be verified there.
