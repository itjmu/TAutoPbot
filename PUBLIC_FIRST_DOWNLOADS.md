# Public-first downloads

The owner approved this flow after the final Start choice on 2026-10-12.

Both link inspection and media downloading begin without configured account cookies. The existing yt-dlp, gallery-dl, direct-file and embedded-source fallbacks remain available. When the public operation fails with a login requirement or bot-confirmation challenge, the worker can repeat the operation once with the cookie file configured for that site. It does not extract browser cookies, open accounts or write back to configured cookie files.

HTTP 429, rate limits and ordinary connection failures suppress that cookie attempt. Relevant yt-dlp warnings are retained in a bounded list, including a rate-limit warning followed by a final bot-confirmation error. Missing cookie configuration does not block a successful public link; its configuration error appears only when authenticated fallback is needed. Direct media requests use a domain-scoped, read-only cookie jar during that fallback. Final failures keep existing user-facing error handling.

Backend recovery can perform several extraction requests within one operation. The limit is one additional operation with configured cookies, not an assumption that every backend uses a single HTTP request. Playlist entries, quotas, bounded worker supervision, cancellation, upload behavior and existing split-layout versions remain unchanged.

Verification: `tests/test_download_public_first.py` covers anonymous defaults, public success, one fallback, exhausted/no configured fallback, missing files, both extractor causes, warning precedence, account context cleanup and worker startup. Existing Instagram/site routing and read-only-cookie checks remain. Full regression results: `public_first_download_tests.log`.

The updated worker successfully inspected the owner's public YouTube URL `https://www.youtube.com/watch?v=38oW0h49uIA` and returned the yt backend with seven choices. Evidence: `public_first_youtube_check.json`. This was metadata inspection only; no media download, Telegram send, polling restart or deployment was performed. It does not guarantee continued access or verify the Linux/production server connection.

MP3 lyrics were rechecked by existing real local FFmpeg/Mutagen tests: USLT text is embedded. The owner's Redmi Music display behavior and actual delivered file remain unverified. To distinguish missing file tags from a player-display limitation, inspect the prepared MP3 itself. No phone-player compatibility or lyrics-generation change was made.
