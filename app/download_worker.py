"""Isolated worker entry point; never opens the application database."""

from app.downloader import download_worker_main

if __name__ == "__main__":
    download_worker_main()
