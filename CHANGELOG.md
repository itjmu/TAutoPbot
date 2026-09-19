# Changelog

## 4.0.0

- Separate Contest and Raffle creation, dedicated templates, prize/contact fields,
  subscription link placement, channel-side participation and participant counts.
- Ranking or weighted random draws, participant/channel referrals, same-post
  result edits, creator notifications and durable recovery without rerolling.
- Configurable Free/Premium quotas, blue/green/red button styles, no trials and
  zero default referral Premium rewards.
- Linux systemd deployment guide and locked, backed-up database reset utility.

## 3.2.1

- Simplify contest creation to channel, post, deadline and a review card with sensible defaults.
- Make settings individually editable and move optional checks to Advanced.
- Add subscription checkboxes, post preview, saved drafts and resume.
- Preserve prize data when cancelling an edit and validate changed start/end times before launch.
- Translate the new flow into English and Kazakh and add quick-setup regressions.

## 3.2.0

- Add one-message download/upload progress and release upload streams on cancellation.
- Add per-user time zones, explicit schedule confirmation and readable auto-delete presets.
- Compare scheduler deadlines as UTC instants and report deletion permission failures.
- Add Russian, English and Kazakh UI catalogs, including background workers and notifications.
- Add a group administrator setup link next to the channel picker.
- Add persistent multipost batches with Free/Premium count and duration limits.
- Add contest setup, public directory, subscriptions, captcha, quiz and referral entries.
- Persist random/weighted/ranked draws before delivering results and prizes; add owner recovery controls.
- Expand offline tests for scheduling, locale coverage, cancellation, contest creation and prizes.

## 3.1.0

- Replace the 90-second stale heartbeat check with an automatically released OS lock.
- Add stop buttons to active downloads and `/stopdownload` without resetting editors.
- Add the channel picker/admin setup link and promotion-driven channel connection.
- Add persistent custom covers for standalone videos and individual album videos.

## 3.0.0

- Replace the monolithic entry point with explicit feature routers and application modules.
- Repair legacy source uniqueness constraints and destination inserts with an extra ID column.
- Preserve source destinations, seen messages and album intake when merging duplicates.
- Run media inspection, downloading and uploading in supervised background jobs.
- Keep menus and editing available during downloads; add status and cancellation.
- Confirm completed join conditions and retry failed approval notifications.
- Preserve existing FSM state names and the `python bot.py` launch command.
- Add offline dispatcher concurrency coverage, migration regressions and Ruff checks.

## 2.2.0

- Migrate legacy question answers, preserve inline links, and normalize source references.
- Add playlist delivery tracking and video splitting with playable MP4 metadata.
- Add admin payment settings and manual payment review.
