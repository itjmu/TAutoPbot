# Functional and performance audit — 2026-10-13

## Result

308 automated tests passed in 53.553 seconds after one confirmed priority fix. Ruff passed for application, services, tests, entry points and the new audit tools. Real Telegram checks passed, and all recorded temporary messages were removed with zero cleanup failures. This is verified coverage of the workflows below, not proof that every branch, external website or future load will succeed.

## Fix

`services/jobs.py`: supervised audio/download jobs inherited interactive Telegram request priority. A backlog of their requests could therefore hold menu traffic behind background work. Jobs now set the existing background context for their lifetime and reset it in `finally`. The existing limiter still ages background requests into FIFO after two seconds; its pacing, quotas, cancellation and admission bounds are unchanged. Two regression tests cover task context isolation and cancellation releasing the user slot.

Controlled comparison using the actual BackgroundJobs and TelegramRateLimit classes, 12 queued background requests and a mocked API transport:

| Measurement | Before | After |
| --- | ---: | ---: |
| Interactive request wait | 715.88 ms | 17.85 ms |

The before case explicitly reproduces the former inherited interactive context. This measures queue wait, not real Telegram network latency or total production throughput. Script: `tools/audit_load_2026_10_13.py`; evidence: `audit_2026_10_13_load.json`.

## Coverage and real results

| Area | Verification |
| --- | --- |
| Menus, Back/Home, questionnaire navigation | Real Telegram API and synthetic dispatcher updates in a separate database; channels, posts, requests, conditions, downloads, contests and settings opened and returned home. Audio controls additionally covered by offline integration tests. |
| Channel/group/topic publication and editing | Real topic publication, durable sent state, published button removal/save; separate channel text publication, protection, edit and cleanup. |
| Photo and albums | Real channel photo and two-item album delivered; formatting, album previews, edited item identity, companions and retry cases covered offline. |
| Scheduling and deletion | Real scheduled post stayed scheduled before its deadline, then published exactly once across repeated ticks. Protection appeared in Telegram's returned message. Automatic deletion updated durable state successfully. Silent-send flags additionally verified offline; handset sound behavior was not observed. |
| Audio | Real Telegram upload, source download, preparation and delivery of MP3, M4A, OGG voice, FLAC and WAV. A generated two-second sample took respectively 2.007, 0.665, 1.543, 2.072 and 1.744 seconds. These timings do not predict large-file transfer time. |
| Lyrics | Downloading the prepared files back from real Telegram preserved MP3 USLT and M4A ©lyr text. This confirms embedded text survives transport; the owner's Redmi Music display compatibility remains unverified. |
| Contests | Real membership check, isolated draw and delivery of synthetic results; no actual prize. Eligibility, referrals, timed starts/endings, fairness, outbox recovery, layout and failures covered offline. |
| Templates and logos | Offline integration plus real local FFmpeg: automatic/manual templates, removing/switching templates, logo sizing/placement, photo overlays, video cover-only processing and preserving original video. |
| Audio limits and recovery | Offline integration and real local media processing: lazy format selection, five outputs, ten-minute boundary, 20 MB bounds, oversize WAV/FLAC, retry classification, upload no-retry, cancellation/temp cleanup, timeout preserving source and background completion after Home. |
| Conditions and join requests | Offline integrated filters, ownership, card creation, quiz/forms, subscriptions, approval idempotency, notification failures and non-starvation. Real unsolicited member joins were not created. |
| Payments, Premium, administration and broadcasts | Offline access, amount validation, payment replay/idempotency, admin routing, bounded broadcast/retry/resume and ownership checks. No real payment, account entitlement or recipient broadcast was performed. |
| Sources and playlists | Offline forwarding/filter/album logic, source migrations, playlist continuation after failure and retry skipping already-sent entries. No long external playlist was downloaded in this audit. |
| Download access | Offline anonymous-first, selected-cookie fallback, 429/network classification, read-only cookies and missing-cookie public success. No personal browser/account access. |
| Storage and restart | Real temporary SQLite migrations/durability/rollback, FSM storage, integrity, contention, runtime lock and Windows descendant termination in the automated suite. Production database integrity was checked read-only and returned `ok`. |

Real API workflow tests use synthetic incoming updates rather than physical taps in a Telegram phone application. Actual sends, edits, media transfers, membership calls and deletes reached Telegram. These tests never poll. Production data was not migrated, reset or edited. Only test-created messages were deleted.

## Concurrent work and download measurements

- One real public download through the worker: 546,026 bytes; inspection 7.628 seconds; total 17.793 seconds (`audit_2026_10_13_download.log`).
- Two concurrent real workers downloading the public W3Schools sample both succeeded in 19.408 and 19.381 seconds, total 19.428 seconds. An invalid local URL was rejected independently. A 20 ms local heartbeat collected 605 samples: p95 additional delay 14.69 ms, maximum 26.94 ms (`audit_2026_10_13_concurrency.json`). This checks local event-loop responsiveness, not real menu delivery during those transfers.
- 1,000 concurrent submitted writes to a real temporary SQLite database completed in 0.129 seconds. All 1,000 rows persisted, integrity was `ok`, and maximum measured loop delay was 2.26 ms (`audit_2026_10_13_load.json`).

The existing two-download/two-audio processing bounds, worker isolation and Telegram rate limits remain. Raising those arbitrarily would risk resource exhaustion or rate-limit failures. This audit does not establish capacity for thousands of simultaneously active users. Network speed and source restrictions remain external constraints.

## Evidence and execution limits

- Baseline: `audit_2026_10_13_tests.log` — 306 tests, 50.851 seconds, OK.
- After fix: `audit_2026_10_13_final_tests.log` — 308 tests, 53.553 seconds, OK.
- Real workflow/media: `audit_2026_10_13_live_extended.log` — all reported checks true, cleanup failures 0.
- Real channel/photo/album: `audit_2026_10_13_channel_media.log` — all reported checks true, cleanup failures 0.
- Runnable audit helpers: `tools/audit_live_2026_10_13.py`, `tools/audit_load_2026_10_13.py`, `tools/audit_download_concurrency.py`.

Expected injected network errors appear in the unittest logs; the suite's final status is OK. No polling restart or deployment was performed. The source fix takes effect when the running bot next loads the updated code. Linux/server execution, handset display/sounds, all third-party sites, sustained production-scale load and every possible button/state combination were not verified. Existing working-tree changes and product features were retained.
