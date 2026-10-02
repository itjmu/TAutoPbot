"""Public sample download through the real worker; no Telegram or saved data."""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloader import run_download_worker


async def main():
    url = "https://www.w3schools.com/html/mov_bbb.mp4"
    with tempfile.TemporaryDirectory(prefix="tautopbot-download-smoke-") as folder:
        path = Path(folder)
        started = time.monotonic()
        info = await run_download_worker("inspect", url, path)
        inspected = time.monotonic() - started
        result = await run_download_worker(
            "download", url, path, info=info, selection=0
        )
        files = [Path(value) for value in result["files"]]
        print(
            json.dumps(
                {
                    "inspection_passed": bool(info["choices"]),
                    "download_passed": bool(files)
                    and all(
                        file.is_file() and file.stat().st_size > 0 for file in files
                    ),
                    "files": len(files),
                    "bytes": sum(file.stat().st_size for file in files),
                    "inspection_seconds": round(inspected, 3),
                    "total_seconds": round(time.monotonic() - started, 3),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
