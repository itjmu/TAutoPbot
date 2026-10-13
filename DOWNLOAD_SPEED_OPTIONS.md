# Download speed changes and remaining options

The owner approved original delivery, fast compatible splitting and automatic fallback on 2026-10-12 after the earlier cancellation. The following is implemented in `services/media.py` and wired through the existing download worker for all supported source backends.

- Compatible H.264/yuv420p MP4 with AAC or no audio, within 49 MiB: reuse the original bytes without remuxing or encoding. Existing video metadata/thumbnail preparation and Telegram document fallback remain.
- Larger compatible H.264 videos: try stream-copy splitting on existing keyframes, with AAC or no audio. All parts must be nonempty, fit the upload limit and retain total duration. Up to three shorter segment attempts handle variable bitrate. Unsupported or oversized results use the established encoding fallback automatically.
- New inspected downloads persist `media_prepare_version=2` in existing JSON. Playlist entries inherit it. Older stored downloads retain the previous preparation layout, preserving partial-delivery resume indexes across this update. No database schema migration is needed.
- Existing sources, quality choices, playlists, quotas, worker supervision, cancellation and part delivery tracking remain available. Network fragment concurrency is unchanged.

Local benchmark: generated 16-second 480x270 H.264/AAC video, 3038245 bytes; median of three before/after preparation runs. Original delivery: **0.8266 -> 0.0775 seconds**. Splitting with a 650000-byte test limit: **2.2786 -> 0.8171 seconds**. Every output fit the test limit; split duration was 16.02 seconds versus the 16-second original. These are synthetic local preparation measurements, not source-download, thumbnail or Telegram-upload timings; they do not establish a guaranteed speedup on real sites. Evidence: `audio_download_speed_benchmark.json`.

Real local tests compare concatenated encoded video and audio packet bytes across split parts with the source, check duration/size/portrait dimensions, and exercise automatic encoding fallback. Worker-version routing and playlist retry checks are offline. Live sources, Telegram delivery and Linux/server execution remain unverified.

Remaining discussion-only options:

| Option | Benefit | Decision needed |
| --- | --- | --- |
| Reuse previous Telegram file IDs for repeated source links | Avoid repeat source download and upload | Freshness and private/source ownership policy |
| Download several HLS/DASH fragments concurrently | May improve throughput on compatible sources | Source benchmarks and bounded concurrency |
| Fetch the next playlist item while sending the current one | Overlap transfers | Disk/network limits and cancellation |
| Choose playlist item numbers/ranges | Download only wanted items | Simple selection UI |
| Queue links from different sources | Collect multiple files in one request | Batch limit and failure handling |

FFmpeg describes [stream copy](https://ffmpeg.org/ffmpeg.html#Streamcopy); yt-dlp describes [fragment concurrency and playlist selection](https://github.com/yt-dlp/yt-dlp#download-options). Future material changes require plain Russian questions and the final start/postpone/discuss/change-details choice recorded in AGENTS.md.

Public-first site access is now implemented; see [PUBLIC_FIRST_DOWNLOADS.md](PUBLIC_FIRST_DOWNLOADS.md).
