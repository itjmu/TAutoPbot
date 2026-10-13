"""Two real public-download workers plus a rejected URL and loop heartbeat."""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloader import run_download_worker


async def main():
    finished = asyncio.Event()
    lag = []

    async def heartbeat():
        while not finished.is_set():
            started = time.monotonic()
            await asyncio.sleep(0.02)
            lag.append(max(0, time.monotonic() - started - 0.02))

    async def download(folder):
        url = "https://www.w3schools.com/html/mov_bbb.mp4"
        started = time.monotonic()
        info = await run_download_worker("inspect", url, folder)
        result = await run_download_worker(
            "download", url, folder, info=info, selection=0
        )
        files = [Path(path) for path in result["files"]]
        return {
            "success": bool(files)
            and all(path.is_file() and path.stat().st_size > 0 for path in files),
            "seconds": round(time.monotonic() - started, 3),
            "bytes": sum(path.stat().st_size for path in files),
        }

    with tempfile.TemporaryDirectory(prefix="audit-concurrent-") as folder:
        root = Path(folder)
        for name in ("first", "second", "invalid"):
            (root / name).mkdir()
        pulse = asyncio.create_task(heartbeat())
        started = time.monotonic()
        try:
            results = await asyncio.gather(
                download(root / "first"),
                download(root / "second"),
                run_download_worker("inspect", "file:///invalid", root / "invalid"),
                return_exceptions=True,
            )
        finally:
            finished.set()
            await pulse
        samples = sorted(lag)
        report = {
            "mode": "real public download workers; local loop heartbeat; no Telegram API",
            "downloads": [
                item if isinstance(item, dict) else {"error_type": type(item).__name__}
                for item in results[:2]
            ],
            "invalid_url_rejected": isinstance(results[2], Exception),
            "total_seconds": round(time.monotonic() - started, 3),
            "loop_p95_lag_ms": round(samples[int(len(samples) * 0.95)] * 1000, 2)
            if samples
            else None,
            "loop_max_lag_ms": round(max(samples, default=0) * 1000, 2),
            "heartbeat_samples": len(samples),
        }
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
