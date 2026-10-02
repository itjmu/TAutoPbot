# Linux launch (Python 3.11+)

For an existing server, follow [UPGRADE.md](UPGRADE.md) instead of fresh-install
steps. Use the code-only ZIP and preserve the server database and environment.

Use a single polling process per bot token/database. The service runs as an
unprivileged user. No public HTTP port is required.

1. Install Python, its venv package and FFmpeg using your distribution's package
   manager. On Ubuntu 24.04:

   ```sh
   sudo apt-get update
   sudo apt-get install python3 python3-venv ffmpeg
   sudo useradd --system --home /var/lib/tautopbot --shell /usr/sbin/nologin tautopbot
   sudo install -d -o tautopbot -g tautopbot -m 700 /var/lib/tautopbot
   sudo install -d /opt/tautopbot
   ```

2. Copy project code to `/opt/tautopbot`. Exclude `.env`, databases, `backups/`,
   cookies, caches, and Windows environments. Install dependencies:

   ```sh
   cd /opt/tautopbot
   sudo python3 -m venv .venv
   sudo .venv/bin/python -m pip install -r requirements.txt
   ```

   For YouTube, install Deno from its official distribution in `/usr/local/bin`
   and set `DENO_PATH` in the service environment. See
   [Deno installation](https://docs.deno.com/runtime/getting_started/installation/).
   yt-dlp's packaged JavaScript components are installed by `yt-dlp[default]`.

3. Copy `.env.example` to `/etc/tautopbot.env` and fill in `BOT_TOKEN` and
   `ADMIN_ID`. Set absolute cookies paths under `/var/lib/tautopbot/cookies/`
   only if needed. Protect the environment file (`root:tautopbot`, mode `640`)
   and cookie files (`tautopbot:tautopbot`, mode `600`). Do not copy the old token
   into documentation or source control.

4. Verify before starting:

   ```sh
   sudo .venv/bin/python -m pip install -r requirements-dev.txt
   .venv/bin/python -m ruff check bot.py config.py manage.py app services tests
   .venv/bin/python -m unittest discover -s tests -v
   sudo systemd-analyze verify deploy/tautopbot.service
   sudo install -m 644 deploy/tautopbot.service /etc/systemd/system/tautopbot.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now tautopbot
   sudo journalctl -u tautopbot -n 100 --no-pager
   ```

   With no database in `/var/lib/tautopbot`, the first launch creates a fresh one.
   Daily quotas reset at 00:00 UTC. Displayed giveaway deadlines use the creator's
   selected time zone. Check that the Linux host's time synchronization is enabled.

5. In Telegram, open `/start`, add a test channel, and give the bot permission to
   post and edit messages. It needs administrator access to every required
   subscription chat; channel referrals also need permission to invite users.
   Test one Contest and one Raffle with a second account: subscribe, tap
   Participate, verify captcha/referrals if enabled, and check the original post
   and the creator's DM after completion. Group/channel membership updates are
   explicitly subscribed to by the bot. No Telegram messages are sent by offline tests.

## Backups, upgrades and reset

Stop the service before maintenance. Maintenance refuses to run while the bot
holds the database lock. Backups use SQLite's backup API and include committed WAL
data. Store an encrypted off-host copy and periodically verify restoration.

```sh
sudo systemctl stop tautopbot
cd /opt/tautopbot
sudo -u tautopbot .venv/bin/python manage.py backup --database /var/lib/tautopbot/bot.db
sudo -u tautopbot .venv/bin/python manage.py check --database /var/lib/tautopbot/bot.db
```

For an intentional clean slate only:

```sh
sudo -u tautopbot .venv/bin/python manage.py reset --database /var/lib/tautopbot/bot.db --confirm-reset
```

Reset backs up the previous database before replacing it. It clears all users,
channels, posts, schedules, giveaways, payments, Premium grants and custom plan
limits; it does not delete messages already posted to Telegram or change the bot
token. Previously published buttons and scheduled tasks will no longer be managed.
The old database backup is sensitive and should not be distributed with the code.

For upgrades, stop the service, back up the database, replace the code, install
requirements and run tests, then start the service. Do not reset on routine upgrades.
After a restart, review any deliveries marked uncertain before retrying. Result
edits can safely retry on the same message; uncertain new messages need review.

Rollback: stop the service, preserve the failed database for investigation, restore
the validated backup as `bot.db` with owner `tautopbot:tautopbot` and mode `600`,
restore the matching previous code, then start the service. Never restore a DB over
an active process. Review channel posts before retrying deliveries after rollback.

The unit caps memory at 2 GB and permits at most 128 tasks. Adjust resources for
your workload. Download limits default to 1 GB per source and 4 GB temporary data
per job; allow space for two concurrent jobs and backups. Site availability and
Telegram permissions require the live smoke test above before public launch.
# Backup timer and heartbeat monitoring

The optional `tautopbot-backup.service` and `tautopbot-backup.timer` create a
verified SQLite snapshot daily, including committed WAL data while the bot runs.
After installing the bot at the paths used by the supplied service:

```sh
sudo cp deploy/tautopbot-backup.service deploy/tautopbot-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tautopbot-backup.timer
sudo systemctl start tautopbot-backup.service
sudo systemctl status tautopbot-backup.service
```

Snapshots accumulate in `/var/lib/tautopbot/backups`. Configure disk monitoring,
retention and an off-server copy appropriate to your deployment. This timer does
not implement retention or off-server storage.

An external monitor can run this read-only command; a missing heartbeat or one
older than 90 seconds returns a nonzero exit code:

```sh
sudo -u tautopbot /opt/tautopbot/.venv/bin/python /opt/tautopbot/manage.py health --database /var/lib/tautopbot/bot.db
```

## Optional worker isolation and health details

`DOWNLOAD_SANDBOX=off` preserves the existing subprocess deployment. On Linux, `DOWNLOAD_SANDBOX=required` requires bubblewrap and supported user namespaces; it fails closed if unavailable. It exposes a copied worker bundle, job directory, system runtime and explicitly selected runtime/cookie files, rather than the host project database or environment file. Networking remains available for downloads; this is not an independent egress firewall. Verify kernel and service namespace compatibility before enabling this option. No server verification was performed during the Windows checks.

`DOWNLOAD_WORKER_CPU_SECONDS=1800` and `DOWNLOAD_WORKER_MEMORY_BYTES=2147483648` configure worker ceilings. Linux uses resource limits, while Windows uses Job Objects for the process family. A zero value disables the corresponding CPU/memory ceiling; negative values are rejected. Size Linux virtual-address limits for the installed runtime, including Deno. These resource limits alone do not isolate files.

Add `--details` to `manage.py health` to display the saved runtime queue, latency and scheduler snapshot. Existing heartbeat exit-code behavior is preserved. The full local verification report is `SCALE_FIXES_2026-10-02.md`.
