# Scale fixes and verification — 2026-10-02

This report supersedes the outstanding findings in `PROJECT_AUDIT_2026-10-02.md`. Existing posting, topic selection, editing, contest, payment, download and draft workflows are preserved. No production database migration, polling restart or deployment was performed.

## Implemented

- Contest scheduling selects due work instead of repeatedly examining an arbitrary first page. Dirty participant counts use rotating bounded refreshes and revision checks so concurrent changes are not lost.
- Participant counting uses set-based queries; weighted draws use a prefix tree with the same ticket distribution. Membership checks are bounded and paginated. Draw results, winners and publication jobs retain transactional persistence and retry behavior.
- Channel registration repeats ownership and quota checks after network awaits. Database triggers prevent new conflicting active owners without deleting historical rows.
- SQLite workers have bounded queues, smaller batches, read-only transactions and coordinated internal writers. Busy retries roll back complete batches before retrying. Expensive counting and closing work runs outside the event loop.
- Rights checks and join approvals use bounded due queues and retry timestamps. Successfully checked chats are not repeatedly scanned on every tick.
- Telegram dispatch has a bounded queue, interactive priority with background aging, conservative group pacing and bounded chat metadata. Telegram rate limits still constrain aggregate delivery.
- Completed user and contest locks are reclaimable. Empty FSM records are removed only when they contain no draft/state.
- Home navigation snapshots obsolete messages before asynchronous cleanup. Cleanup failures remain durable; new panels and active workflows are preserved. Missing messages do not cause unnecessary fallback edits.
- Download workers receive an environment allowlist and only the selected cookie file. Output, playlist size, admission frequency and FFmpeg threads are bounded. Worker resource limits and process-family termination protect Windows jobs and provide Linux controls.
- Optional Linux `DOWNLOAD_SANDBOX=required` builds an isolated job filesystem using bubblewrap and fails closed when unavailable. Default `off` preserves existing installations. Resource limits alone do not isolate the filesystem.
- Runtime health records bounded queue, handler, scheduler and loop-lag metrics. `python manage.py health --details` exposes the saved snapshot. CI includes a scheduled Python dependency advisory check.

## Verification completed

| Check | Result |
|---|---|
| Complete offline regression suite | 234 passed, 35.808 seconds |
| Application, services, tests and tools | Ruff lint/format and compilation passed |
| Existing database read-only integrity | Passed |
| Migration of temporary database copy | Passed; nine protected table row counts preserved |
| Repeated migration of that copy | Passed; row counts unchanged |
| Existing conflicting active chat owners | Zero |
| Real Telegram topic text and round-video delivery | Passed |
| Real Telegram button edit and clear | Passed |
| Private menus/home and contest questionnaire | Passed |
| Durable topic publication and published editor | Passed |
| Contest membership, draw and results | Passed |
| Live test message cleanup | Zero failures |
| Public sample video inspection/download | Passed; one 546,026-byte output |
| Windows worker descendant termination | Passed in a real subprocess test |
| OSV Python dependency advisory query | 32 packages checked; no listed findings |

Telegram workflow tests used synthetic incoming updates with actual outgoing Telegram API calls, an isolated temporary database and the authorized administrator/topic. They did not start polling, consume pending updates or run production schedules. Temporary test messages were deleted. The download check used a public W3Schools sample; it does not establish that every supported site or cookie works.

## Measured synthetic workloads

Same-machine measurements; these are component workloads, not whole-bot throughput or concurrent-user capacity guarantees.

| Workload | Before | After | Verification |
|---|---:|---:|---|
| 50,000 entries, ten participant counts | 0.240416 s | 0.088677 s | About 2.7× faster |
| 50,000 entries, ten referral-qualified counts | 0.311164 s | 0.277674 s | Same 16,667 eligible entries |
| 50,000 entries, 100 weighted winners | 0.369107 s | 0.027825 s | About 13.3× faster; identical controlled tickets |
| 50,000 source rows, 300 isolated lookups | 0.510289 s | 0.000566 s | Index lookup replaces table scan |
| Locks retained after 10,000 completed users | 10,000 | 0 | No completed-user lock accumulation |
| Cleanup of 100 already missing messages | — | 0 fallback edits | No redundant fallback traffic |

Detailed evidence: `scale_full_tests.log`, `scale_after_results.json`, `migration_snapshot_results.json`, `live_scale_results.json`, `live_workflow_results.json`, `live_download_results.json`, `dependency_audit_results.json`.

Historical standalone root audit scripts retain pre-existing lint/format issues; the maintained application, test and tool directories pass the checks used above.

## Practical boundaries

The bot remains a single-process SQLite application. Legacy synchronous database calls remain; an uncoordinated external writer can still cause `database is locked`. Internal mixed sync/async writers passed the coordination regression. No claim is made that all database work is asynchronous.

No Linux/server execution, remote CI execution, sustained multi-user load, real Stars purchase, all-site download verification or independent penetration test was performed. Tens of thousands of registered users and tens of thousands of simultaneous active users are different workloads; the latter is not established by these tests.

Linux resource controls and bubblewrap filesystem isolation are implemented but not activated or verified on a server. Windows Job Objects bound resources and descendants; they are not a filesystem security boundary. Bubblewrap retains networking for downloads and is not an independent egress firewall. Python advisory results do not audit operating-system packages, FFmpeg/Deno binaries or undisclosed vulnerabilities.
