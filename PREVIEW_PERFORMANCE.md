# Preview performance — 30 September 2026

Workload: three fresh text-post previews in an in-memory test database, using the
real `TelegramRateLimit` and a simulated API with 10 ms latency. UI control
retirement/tracking is mocked to isolate preview delivery. No Telegram requests
are sent. Run `tests/benchmark_post_preview.py` with the project root on PYTHONPATH.

| Version | Requests per preview | Median elapsed time |
|---|---|---:|
| Before | sendMessage, editMessageReplyMarkup | 1.0826 s |
| After | sendMessage with its keyboard | 0.0163 s |

The improvement removes the extra request and its per-chat pacing wait. It does
not measure live Telegram latency, channel publishing throughput, or Linux load.
Album previews still attach their keyboard through an edit because media-group
sends do not accept it. Completed edits still send a fresh preview at the bottom
and retire the previous preview only after delivery.

Reaction updates now serialize by publication across both ordinary posts and the
published-post editor, avoiding out-of-order count edits. Edited-post reaction
counts use one grouped query rather than one query per reaction button. New
indexes support per-user selection updates; no separate database speedup is claimed.
