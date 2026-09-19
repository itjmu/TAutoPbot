# Telegram Automation Bot 4.0

Python 3.11+ / aiogram 3 / SQLite. Publishing, templates, Telegram sources,
join conditions, media downloads, multiposting, contests, and Premium **inside this bot**.

Linux deployment, backups, reset and rollback: [deployment guide](deploy/LINUX.md).

Default Free / Premium limits: video covers **3 / unlimited per day**, video
downloads **5 / 20 per day**, colored buttons **2 / 3** (blue/green; Premium adds
red). The normal Telegram button style is also available. There is **no Premium
trial**. Referral Premium rewards default to zero. Administrators can edit plan
limits under **Admin → Premium → Plan limits and features**, and prices under
**Prices and payments**. Changes apply immediately; paid/granted expiration dates
remain unchanged. Daily quotas reset at **00:00 UTC**.

Each downloaded playlist video counts separately; splitting a video into parts
does not multiply its quota cost. MP3 extraction from video also uses the download
quota. Failed/cancelled extraction refunds its reservation. A completed download
counts even if its later Telegram upload fails. Photos do not use the video quota.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
# For a new installation only; preserve an existing .env:
Copy-Item .env.example .env
# Set BOT_TOKEN and ADMIN_ID in .env.
.\.venv\Scripts\python bot.py
```

FFmpeg is bundled with `imageio-ffmpeg`; `FFMPEG_PATH` can override it.
For YouTube, install Deno and put it on PATH or set `DENO_PATH`.
Keep `yt-dlp[default]` updated when websites change. DRM videos are not supported.

### Instagram returns an empty media response

Instagram may require a logged-in session even for public posts. Check that the
post opens in your browser, then export Instagram cookies in Mozilla/Netscape
format and store them locally, for example as `cookies/instagram.txt`.
Set `INSTAGRAM_COOKIES_FILE=cookies/instagram.txt` in `.env` and restart the bot.
Relative paths are resolved from the project directory. This setting is used by
yt-dlp and the gallery-dl fallback for inspection and downloading.
Browser sessions are never imported automatically; configured cookie files are
read without rewriting them when a job ends.

Cookies grant account access: keep the file private and use a dedicated account
whose accessible media may be downloaded by bot users. Replace expired cookies
when necessary. Missing/deleted posts and Instagram rate limits can still prevent
downloads. See the [yt-dlp cookies FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to).

### TikTok and other social sites

TikTok share links (`vt.tiktok.com`, `vm.tiktok.com`, and `/t/`) are resolved
before extraction. A share link that redirects to the home page requires a new
link from the actual post. If yt-dlp cannot read a TikTok video page, the bot tries
TikTok's official embedded player, then gallery-dl. Embed videos offer video and
MP3; their temporary media URLs are refreshed when downloading. This fallback
does not promise every quality or bypass private-post access restrictions.

For authentication on other supported sites, set `DOWNLOAD_COOKIES_FILE` to a
Netscape cookie file. Optional site-specific settings take precedence:
`TIKTOK_COOKIES_FILE`, `YOUTUBE_COOKIES_FILE`, `TWITTER_COOKIES_FILE` (X/Twitter),
`FACEBOOK_COOKIES_FILE`, `VK_COOKIES_FILE`, `REDDIT_COOKIES_FILE`, and
`INSTAGRAM_COOKIES_FILE`. Both extraction backends use these settings; gallery
media requests retain domain-scoped session cookies. Keep these files private.
Bot users can access media available to the configured accounts.

If a website changes, update dependencies in the same Python environment used
to run the bot, then restart it:

```powershell
python -m pip install --upgrade "yt-dlp[default]" gallery-dl
```

Login requirements, expired links, regional restrictions, rate limits, DRM and
unsupported sites can still prevent downloads. The bot reports an actionable
message instead of a truncated upstream bug-report instruction.

## Upgrade an existing installation

Stop the previous bot process before starting this version. Keep `.env` and
`bot.db`. Install updated requirements, including `tzdata` for Windows time zones.
On startup, the legacy `request_answers.question_id` schema is
migrated to `item_id`. A timestamped `.bak` SQLite backup (including WAL data)
is created before this migration. New and partially migrated schemas are supported.
Legacy source tables also receive the missing unique indexes. Duplicate source
records are merged while preserving destination mappings, seen messages and
queued albums. Both the original two-column destination table and the legacy
table with an extra `id` column are supported.
Do not restore a database while a bot process is running.

Restart protection now uses an OS file lock (`bot.db.lock`) acquired before
migrations. A crash releases it immediately; there is no 90-second delay.
Launching a second instance exits normally and leaves the active bot running.
When upgrading from an older version, stop that old process once before launch.

## Behavior

- Subscription conditions include public or private join buttons. The bot
  needs administrator rights and invite permissions to create private links.
  Successful approval sends a confirmation with a channel button when available.
  Rechecking an approved request confirms its status. Failed notification delivery
  is retried by the scheduler; approval failures remain pending and are reported.
- Auto-request menus exclude public channels. Private channels and groups
  are available. Joining must use a link configured to request approval.
- Sources accept forwarded posts, usernames, public/post links, and numeric
  chat IDs. The bot must be an administrator of a source to receive new posts.
  For private sources the connecting user must also be an administrator.
  The Bot API cannot resolve arbitrary private invite links into chat IDs;
  forward a post or provide the ID instead. No historical scraping is performed.
- Authored posts, templates, and inline URL buttons preserve valid
  HTTP(S) links, including private Telegram invitations.
- **Add channel** includes Telegram's channel and group pickers with the requested admin
  rights. After promotion, the bot connects the selected channel automatically
  if setup was initiated in the last 15 minutes. Forwarding a post / entering a
  username remains available for channels where the bot is already an admin.
- **Edit → Video cover** accepts a photo for a video or a selected video in an
  album. Send `-` to remove the custom cover. Covers are stored with the draft,
  shown in previews, and included in scheduled publications. Already-published
  posts remain immutable; create a new draft to republish.
- Playlists are downloaded entry by entry (video or MP3). Failed entries do
  not stop the rest. Retrying the same selection skips recorded deliveries.
  A process crash between a Telegram upload and saving its message ID can
  still cause one repeated part; Telegram provides no upload idempotency key.
- Inspection, download, conversion and uploads run as supervised background
  jobs. The user's menu and editing remain available. Open **Скачать по ссылке**
  for progress or cancellation. One job per user and two concurrent media jobs
  are allowed. Shutdown cancels jobs and stops their child processes before
  closing Telegram and the database. Interrupted downloads can be retried.
  Every running-job message includes a stop button; `/stopdownload` (or
  `/stop_download`) also stops the user's job without clearing their editor state.
  `/cancel` continues to cancel only the current editor/input flow.
  One progress message is edited during downloading, processing and uploading.
  Download percentages appear when the source reports a total size; upload
  percentages measure bytes sent, followed by Telegram's delivery confirmation.
  Cancellation works during transfers and releases Windows file handles before cleanup.
- Video is encoded to H.264/AAC MP4 with original aspect ratio, streaming
  metadata and thumbnails. Large videos are split into independently playable
  parts, each checked to be at most 49,000,000 bytes. Encoding can take time.
  Default source limit is 1 GB/video and temporary storage limit is 4 GB/job;
  both are configurable. Non-video files over 49 MB are rejected.

## Language, time zones and scheduling

Open **Settings → Language / Time zone**. Russian is the default language;
English and Kazakh are available throughout the interface. User-authored posts,
channel names, questions and prize instructions are preserved as written.
Time zones support IANA names such as `Asia/Almaty` and fixed offsets such as
`UTC+05:00`. The initial time zone is UTC and is shown in scheduling prompts.

Choose **Time** on a draft, select a relative delay or a day and time, review the
exact local date, then confirm. Custom input accepts `25.12.2026 18:30`,
`2026-12-25 18:30`, or the next occurrence of `18:30`. Ambiguous/nonexistent
times during daylight-saving changes are rejected. Schedules are stored as UTC
instants; changing the time zone changes their display, not their delivery time.
Daily limits use UTC dates. Admin expiry inputs use the admin's zone.

**Auto-delete** is a delay after actual delivery, with presets and custom input
such as `30 min`, `6 h`, `1 d` (Russian and Kazakh units also work).
The maximum is 47 hours; `0` disables deletion. Publishing is checked every
10 seconds and deletion every 30 seconds while the bot is running. Permission
failures are recorded and reported to the owner. The bot must have delete rights.

## Multiposting

Choose one channel or group, send ready posts, then press **All posts added**.
An album counts as one post. Choose an interval in minutes, hours or days and
the start time, review every scheduled date, then confirm the whole batch.
Free permits 10 posts spanning up to 30 days; Premium permits 20 posts spanning
up to 60 days. The start must also be within the plan's 30/60-day window.
Batch limits apply separately from the ordinary single-post daily limit.

Draft collection and confirmed schedules survive restarts. Cancelling a batch
stops remaining scheduled posts; already delivered posts remain. Individual
delivery results are visible from the batch list. A confirmation is transactional:
validation failure cannot leave half a batch scheduled.

## Contest and Raffle

1. Choose **Create → Contest or Raffle → channel/group**.
2. Describe the prize, then send a custom text/photo/video post or choose
   **Template 1 (Contest)** / **Template 2 (Raffle)**. Set 1–20 winners, the end
   date and an optional prize-claim contact.
3. Review subscriptions (the publishing channel is selected initially), choose
   subscription links inside the text or as buttons, and optionally require
   captcha or invitations. Referral links can invite friends to enter the giveaway
   or join its publishing channel. Channel invitations require the bot's invite
   permission. Preview, then launch. Drafts can be saved and resumed.
4. **Participate · count** is the last channel-post button. It checks subscriptions
   in the channel and shows a popup: participating or the missing requirement.
   It does not redirect subscription-only entrants to the bot. The count includes
   entrants who have met the configured minimum invitation requirement.
5. Captcha/questions and personal referral links are available in the private bot
   chat: open the giveaway from **Active contests**, or send `/start contest_ID`.
   If a private step is required, the channel popup explains how to open it.
   Friends entering via a personal link must pass the base subscription/captcha
   checks. Self-referrals, duplicate entries and changing inviters are rejected.
   Channel referrals count actual new joins via the personal invite link; leaving
   removes that referral's credit. The captcha is a basic deterrent, not identity verification.
6. **Contest:** most qualifying invitations win; ties use earlier registration.
   **Raffle:** random selection, with one extra chance per qualifying invitation.
   Before the draw, subscriptions are rechecked. API failures postpone the draw
   rather than disqualifying entrants. Persisted winners are never rerolled.
7. The **same original post** is edited to show completion, winners and the claim
   contact, or “Please wait; you will be contacted soon to receive your prizes.”
   Its participation buttons are removed. The creator receives the winners by DM.

Private promo codes and single-use prize invitations remain supported and are
never placed in the public results. Winner DMs can fail if the user never started
the bot or blocked it; the creator is notified and can arrange delivery.
**My contests** shows delivery states. Failed result edits can be retried safely
on the same message. Ambiguous new-message sends need owner review before retry.
When recovering an uncertain initial publication, forward the original channel
post or provide its message ID so participation and result edits target it.

The bot must be running to publish and finish contests. If it was offline for
the entire scheduled contest, the contest expires without publishing. A contest
already published before downtime is finalized when the bot returns.
Membership checks require bot administrator access, as described in the
[Telegram Bot API](https://core.telegram.org/bots/api#getchatmember).

The simplified setup was informed by the public
[GiveShare overview](https://giveshare.ru/) and
[RandomGod creation guide](https://telegra.ph/Rukovodstvo-po-ispolzovaniyu-bota-RandomGodBot-20-06-06-2).

## Payments

Settings → Admin panel → **Оплата Premium** configures Stars prices and the
optional manual method's label, instructions, and duration. Manual payments
are disabled by default. Submitted references appear in **Заявки на проверку**;
only ADMIN_ID can approve or reject them. Approval grants Premium once.
Verify payment independently before approving a reference.

Stars invoices validate user, amount, currency, and payload. A repeated
successful-payment event does not grant Premium twice. `/paysupport` is available.
Existing pending invoices retain the price recorded when they were created.

Telegram requires Stars for digital goods sold inside Telegram. The configurable
manual reconciliation mechanism is not an exemption from that rule; keep it
disabled for payment arrangements not permitted by Telegram.
See https://core.telegram.org/bots/payments-stars.

## Development and verification

```powershell
python -m pip install -r requirements-dev.txt
python -m ruff check bot.py config.py manage.py app services tests
python -m ruff format --check bot.py config.py manage.py app services tests
python -m unittest discover -s tests -v
python -m compileall -q bot.py config.py manage.py app services tests
```

Tests use in-memory databases and local synthetic media; no bot token, live
payments or Telegram messages are needed. Live source delivery, membership
permissions and YouTube availability require an integration check with your bot.

## Project structure

```text
bot.py                    # Small CLI launcher; same startup command
config.py                 # Environment configuration and validation
app/
  application.py          # Startup, scheduler setup, shutdown
  routing.py              # Explicit router registration and priority
  database.py             # Schema, transactions, queries; no connection on import
  storage.py              # Durable aiogram FSM storage
  states.py               # FSM class names preserved across the upgrade
  accounts.py             # Users, Premium, limits and referral eligibility
  access.py               # Ownership and membership checks
  content.py              # Post payloads, entities, templates and rendering
  ui.py                   # Shared keyboard builders and message editing
  middleware.py           # Access and handler error boundary
  preferences.py          # Per-user time zones and local-time parsing
  i18n.py, locales/       # Russian source messages; English/Kazakh catalogs
  scheduler.py            # Publishing, deletion, rights, requests, notifications
  downloader.py           # Extraction and bounded child-process execution
  download_worker.py      # Independent media-worker entry point
  features/               # Routers for channels, sources, conditions, posts,
                          # editors, payments, referrals, admin, downloads,
                          # preferences, multiposting and contests
services/
  jobs.py                 # Task ownership, duplicate prevention, cancellation
  progress.py             # Shared transfer message and managed upload streams
  contests.py             # Eligibility, durable draws and prize/result outbox
  migrations.py           # Backup and legacy-schema repair
  media.py                # Video normalization, parts, thumbnails and metadata
  telegram_links.py       # Telegram reference parsing and invitation links
tests/                    # Regression tests and offline dispatcher integration
.github/workflows/        # Tests, formatting and lint on Python 3.11 / 3.13
```

SQLite access stays on the event-loop thread; transactions contain no awaits.
The connection is created during application startup, not when importing modules.
Feature routers use explicit module dependencies. The shared router middleware
runs once, commands precede state input, and fallback handlers are registered last.
The FSM state class names have been retained so existing conversations can resume.
Expensive extraction and encoding run in separate bounded processes. Background
jobs never retain an FSM event lock while waiting for media work or uploads.

The bot uses one polling process per SQLite database. This is a modular
single-process application, not a distributed worker cluster. A crashed process
does not automatically resume media jobs; persisted delivery records allow a
user-triggered retry to skip already delivered parts. Runtime source permissions
and third-party download availability still require live integration checks.

Telegram API references: [channel setup links](https://core.telegram.org/api/links#bot-links)
and [video covers](https://core.telegram.org/bots/api#sendvideo).
