"""Isolated worker entry point; never opens the application database."""

from app.downloader import download_worker_main
from services.worker_limits import apply_worker_limits

if __name__ == "__main__":
    apply_worker_limits()
    download_worker_main()
