# Project audit — 2026-10-02

Implementation update: fixes and completed verification are recorded in [SCALE_FIXES_2026-10-02.md](SCALE_FIXES_2026-10-02.md). Findings and measurements below describe the original audit baseline.

## Conclusion and scope

The product has useful safeguards and a passing offline regression suite, but its ability to serve tens of thousands of **active** users is not established. Registered account count alone is not a capacity target: simultaneous updates, source traffic, downloads, contest entries and Telegram requests determine load.

This review covered application startup, routing/FSM, database/storage, posting/editing, channels/topics, sources, contests, join requests, payments/access, downloads, scheduling, rate limits and deployment configuration. It combined source inspection, existing tests and synthetic probes. It is not an exhaustive proof of every branch or a penetration test.

No application source was changed for this audit. Existing user changes were preserved. No production database, Telegram posts, polling process or deployment was modified. The probes use synthetic databases and mocked Telegram. Windows measurements do not establish Linux/server performance.

## Verified correctness problems — fix first

### 1. Due contests can starve indefinitely — high

`services/contests.py:595` selects the first 200 scheduled/active contests ordered by start time, without selecting specifically due starts or endings. Old active contests can occupy every slot on every tick.

Reproduction: create 200 older active contests and one due scheduled contest; the tick selects 200 and never selects the due contest. This is a correctness issue, independent of server speed.

Proposed change: separate indexed queues for due starts, due endings and count refreshes. Use fair pagination for refresh work. Preserve retry handling and existing contest state transitions. Test more than 200 active contests, due starts/endings beyond the first page, and failing jobs.

### 2. Concurrent channel registration bypasses policy — high

`app/features/channels.py:167` checks ownership and account limits around awaited Telegram operations, then inserts without a final atomic policy check. The database uniqueness of owner/chat pairs does not enforce one active controlling owner per chat.

Mocked concurrent reproductions produced two active owners for one chat, and three active channels for an account whose configured limit was one. The tests mock valid admin rights; this is not evidence that an arbitrary outsider can take over a chat.

Proposed change: perform the final ownership/quota check and insertion in one short transaction after Telegram validation. Add a database constraint for the intended active ownership invariant, after safely resolving existing conflicting data. Lock quota updates by owner as needed; preserve inactive history and idempotent reconnects.

### 3. Undeliverable approval notices can block later notices — high

`app/scheduler.py:170` always selects the first 100 approved requests without `approval_notified_at`. `app/features/conditions.py:268` leaves that field unset when all notification targets return forbidden/bad-request errors. The same permanently unreachable rows can repeatedly consume all slots.

This finding follows directly from the query and error paths; it has not been tested against live Telegram.

Proposed change: explicit delivery state, attempts and `next_attempt_at`; distinguish permanent target failures from retryable network/rate errors. Continue processing later rows and keep approval itself independent of notification success.

## Database and responsiveness

### 4. Synchronous SQLite work can freeze handlers and fail under contention — high

`app/database.py:22` creates a synchronous connection with a 0.1-second busy timeout. Handlers execute queries/commits on the event loop. `services/sqlite_worker.py:24` also starts `BEGIN IMMEDIATE`, including for worker callbacks that only read, reserving the writer unnecessarily.

Synthetic independent connections to one temporary database reproduced a synchronous write blocking for **0.158 seconds**, then raising `database is locked`. This demonstrates the contention mechanism, not its production frequency. SQLite WAL still permits only one writer: [SQLite WAL documentation](https://sqlite.org/wal.html).

Proposed change: centralize writes through a bounded async worker, keep transactions short, and give read-only worker operations a read path. Remove expensive synchronous scans from handlers. Measure mixed handler/scheduler/worker load before choosing whether PostgreSQL is necessary; account count alone does not justify a migration.

### 5. Source dispatch performs a full table scan — medium/high

`app/features/sources.py:68` looks up active Telegram sources by source chat. The existing owner-first unique index does not serve that lookup.

Synthetic 50,000-row database, 300 lookups:

| Query | Elapsed | Query plan |
| --- | ---: | --- |
| Existing indices | 0.520 s | `SCAN post_sources` |
| Added experimental `(source_chat_id, kind, active)` index | 0.000601 s | Indexed search |

This is an isolated in-memory query benchmark, not an end-to-end bot speedup. The experimental index exists only in the probe database.

Proposed change: add the source lookup index through an additive migration. Review message lookup `(channel_id,message_id)` and other measured hot queries similarly. Date queries such as `julianday(publish_at)` in `app/scheduler.py:24` inhibit ordinary date range index use; normalize legacy timestamps before changing comparison semantics.

### 6. Contest counts repeatedly scan participants — medium/high

`services/contests.py:93` calculates eligibility using correlated referral counts, including a zero-referral requirement. Active contest refreshes repeat this work, and synchronous counting runs on the event loop.

Synthetic 50,000 entries: ten count calls took **0.244 seconds** in the latest run. Earlier runs were slower, so this is not a stable latency guarantee. Repeated counts across many active contests amplify the work.

Proposed change: a simpler zero-referral query, indexed/set-based referral aggregation, dirty flags and coalesced count refreshes. Cached counters must preserve eligibility and referral correctness. Benchmark referral-heavy data too; this probe does not model it.

### 7. Home cleanup multiplies failed API calls — medium

`app/ui.py:91` processes historical transient message IDs inline. A failed delete falls back to editing controls even when the message no longer exists. Mocking 100 already deleted messages resulted in **100 deletes plus 100 fallback edits**. This also applies to the recently added cleanup feature.

Proposed change: untrack IDs after successful deletion elsewhere, skip fallback for known not-found errors, and bound/defer cleanup while keeping the menu responsive. Preserve active join/download controls and delivered media. No live Telegram duration was measured.

## Telegram throughput and fairness

### 8. Background requests compete with all interactive requests — high at scale

`services/telegram_rate.py` spaces most method starts by 0.05 seconds: nominally 20 starts/second globally. Membership/rights checks share this budget with posting and UI edits. A retry-after currently pauses the global limiter, even when the triggering limit may be local. Waiting callers repeatedly wake and contend without explicit priorities. Clearing the entire chat-type cache at 2,000 entries also loses known group pacing information.

`app/scheduler.py:118` checks all active channels every six hours. The current access helper makes approximately four Telegram calls per channel. At 50,000 channels, 200,000 calls consume at least **2 hours 47 minutes** of this request budget before latency, retries or competing work. This is arithmetic from current pacing, not a measured run.

Proposed change: a bounded fair request queue with interactive priority and starvation protection; cache bot identity; spread channel checks by due timestamps; use an LRU/TTL chat cache. Scope retry-after pauses carefully. Do not simply raise the limit: Telegram documents separate chat/group/broadcast limits in its [Bot FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).

### 9. Join recheck batches and large draws have long backlogs — medium/high

The periodic join rechecker selects at most 100 candidates every 300 seconds, a nominal maximum of 1,200 selections/hour. A 10,000-row due backlog takes at least about 8 hours 20 minutes to visit without competing work. Initial incoming join requests have their own immediate handling; this bound concerns periodic rechecks.

Contest finishing verifies participants/subscription channels serially. A cold 50,000-entry contest with three required subscriptions can require roughly 150,000 membership checks: at 20 request starts/second, at least **2 hours 5 minutes** before other traffic. Existing resumability/caches help, but do not eliminate cold verification cost.

Proposed change: adaptive bounded recheck batches, fair job scheduling, explicit backlog/closing progress, and carefully defined membership cache freshness. Do not remove eligibility checks or alter drawing rules merely to improve throughput.

## Security and resource controls

### 10. Downloader subprocess isolation is incomplete — hardening priority

The worker has useful URL/IP restrictions, timeouts and process separation. However, it runs with the bot's operating-system identity and inherits a broadly copied environment (`app/downloader.py:1000`). Clearing selected variables is not a filesystem boundary: a compromised worker can potentially read files permitted to the bot user, including credentials. Python socket guards do not constitute a firewall for every external subprocess.

No working SSRF/RCE exploit was demonstrated. This is a defense-in-depth gap around parsers and external tools processing untrusted inputs.

Proposed change: separate unprivileged worker identity/container, minimal environment allowlist, explicitly mounted cookie files only where required, restricted filesystem/network access, and CPU/RAM/process/disk limits. Check compatibility with existing media features before enabling restrictions.

### 11. Worker output is bounded too late — medium/high

`app/downloader.py:1027` uses `proc.communicate()` to collect output. The one-megabyte stdout check at line 1055 occurs after collection. Stderr is also accumulated. Large extraction metadata or error output can consume parent memory before rejection; execution timeouts are not byte limits.

Proposed change: enforce byte caps while reading stdout/stderr, terminate the process tree on overflow, and bound playlist metadata/results. Keep diagnostic output truncated and free of secrets. Add a synthetic oversized-output worker test.

### 12. Global download slots need per-user fairness — medium/high

Existing global concurrency/queue caps protect some resources, but expensive inspection and image work need independent per-owner admission limits. Charging a video quota after inspection does not protect inspection resources from repeated requests. A small number of accounts can consume shared capacity.

Proposed change: per-owner queue caps/cooldowns, fair scheduling, inspection budgets and bounded retries. Keep subscriptions and current legitimate workflows compatible.

## Memory, retention and operational gaps

- Installed aiogram `SimpleEventIsolation` retained **10,000 locks** after 10,000 completed synthetic users. Use safe bounded/weak lock lifecycle management that cannot create two locks for the same active key. Contest locks have some completion cleanup already; review unfinished/error paths rather than assuming none are removed.
- Define retention for seen-source records, completed jobs, transient UI state, logs and temporary files. Persistent button sessions and publication snapshots are functional data; do not purge them indiscriminately.
- Avoid repeated topic metadata writes for unchanged observations in busy groups.
- Heartbeats alone do not establish delivery health. Add event-loop lag, queue depth/oldest age, scheduler lag, SQLite busy/errors, API retry-after rates, memory/disk and download resource metrics. Alert on stale jobs and overdue publications.
- One polling process is appropriate for the current architecture. Starting more instances against one token/database is not a scaling plan; process-local locks and Telegram update ownership need redesign first.
- Keep compatibility callbacks and migrations unless usage/data evidence proves they are obsolete. Historical handoffs are context, not justification to remove features or repeat maintenance operations.

## Safeguards found and dependency limits

The inspected paths use parameterized user values and allowlisted dynamic SQL identifiers; no obvious user-controlled `shell=True`, eval or pickle path was found. Payment amount/currency checks and duplicate-payment handling, ownership checks, publication-bound callbacks, private admin restrictions, URL/IP guards and deployment restrictions are useful existing protections. This does not certify the absence of vulnerabilities.

Installed versions inspected: aiogram 3.31.0, aiohttp 3.14.3, APScheduler 3.11.3, yt-dlp 2026.8.19, gallery-dl 1.32.12, requests 2.34.2, urllib3 2.8.0. `pip-audit` was unavailable; no complete transitive advisory scan was performed. For one checked advisory, installed aiohttp 3.14.3 is the patched version: [official aiohttp advisory](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-cq5v-8q36-5273). This is not a clean bill of health for every dependency. Add periodic advisory scanning and review upgrades individually.

## Verification and recommended sequence

1. Fix contest starvation, registration atomicity and approval notification starvation. Add targeted regression tests for each.
2. Add the measured source index, reduce count/cleanup work, and remove synchronous database contention from interactive paths.
3. Introduce fair API/background/download scheduling and worker isolation/output limits.
4. Run Linux staging load and failure tests before stating capacity. Use multiple load levels, realistic source/contest/download mixes, and report p50/p95/p99 menu latency, overdue jobs, errors, memory and queue age. Include 429s, blocked recipients, worker crashes, database contention and restart recovery. Define acceptable latency/backlog targets with the owner.

Existing offline suite: **215 tests passed in 34.658 seconds**. Repository lint passed. Additional probe: `tests/audit_scale_probe.py`, results: `audit_scale_results.json`. The source fanout quota hypothesis in that probe was **not reproduced**: both owners received processing; it is not reported as a bug.

The measured values above come from the latest synthetic run and will vary with hardware/cache/load. No real Telegram load test, Linux deployment verification or production soak test was performed.
