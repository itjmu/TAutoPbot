"""Run the Telegram bot with: python bot.py."""

import asyncio
import sys

if __name__ == "__main__":
    if "--download-worker" in sys.argv:
        from app.downloader import download_worker_main

        download_worker_main()
    else:
        from app.application import main

        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass
