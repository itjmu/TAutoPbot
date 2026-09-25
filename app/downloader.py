"""downloader components."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import traceback
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from app import content as content
from app.i18n import language_context, tr
from services.media import VIDEO_EXTENSIONS, prepare_video, upload_metadata
from services.progress import current_progress, write_worker_progress

TELEGRAM_DOMAINS = {
    "t.me",
    "telegram.me",
    "telegram.dog",
    "telegram.org",
    "www.t.me",
    "www.telegram.me",
}


DOWNLOAD_LIMIT = 49_000_000


DOWNLOAD_TOTAL = int(os.getenv("DOWNLOAD_TOTAL_BYTES", "4000000000"))


DOWNLOAD_SOURCE_LIMIT = int(os.getenv("DOWNLOAD_SOURCE_BYTES", "1000000000"))


def telegram_url(url):
    if url.lower().startswith("tg:"):
        return True
    p = urlsplit(url if "://" in url else "https://" + url)
    host = (p.hostname or "").lower().rstrip(".")
    return host in TELEGRAM_DOMAINS or any(
        host.endswith("." + d)
        for d in ("telegram.org", "t.me", "telegram.me", "telegram.dog")
    )


def external_links(m):
    text = m.text or m.caption or ""
    found = []
    for match in content.LINK_RE.finditer(text):
        raw = match.group().rstrip(".,!?;:)]}")
        if raw.startswith("@"):
            continue
        if "://" not in raw:
            raw = "https://" + raw
        if content.valid_url(raw) and not telegram_url(raw) and raw not in found:
            found.append(raw)
    for e in m.entities or m.caption_entities or []:
        if (
            e.type == "text_link"
            and e.url
            and content.valid_url(e.url)
            and not telegram_url(e.url)
            and e.url not in found
        ):
            found.append(e.url)
    return found[:10]


def validate_download_url(url):
    if not content.valid_url(url):
        raise ValueError(tr("Нужна ссылка http:// или https://."))
    if telegram_url(url):
        raise ValueError(
            tr("Для Telegram перешлите сообщение боту и выберите «Создать пост».")
        )
    p = urlsplit(url)
    if p.port not in {None, 80, 443}:
        raise ValueError(tr("Ссылка должна использовать стандартный порт HTTP/HTTPS."))
    host = (p.hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError(tr("Локальные адреса не поддерживаются."))
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and not addr.is_global:
        raise ValueError(tr("Нужна публичная интернет-ссылка."))
    return url


def install_public_network_guard():
    """Worker-only guard: validate actual resolved addresses on every connection."""
    original = socket.getaddrinfo

    def safe_resolve(host, port, *args, **kwargs):
        entries = original(host, port, *args, **kwargs)
        if not entries or any(
            not ipaddress.ip_address(e[4][0].split("%")[0]).is_global for e in entries
        ):
            raise ValueError(tr("Доступ к локальному адресу запрещён."))
        return entries

    socket.getaddrinfo = safe_resolve
    old_connect = socket.socket.connect
    old_ex = socket.socket.connect_ex

    def check_address(address):
        if isinstance(address, tuple):
            try:
                ip = ipaddress.ip_address(str(address[0]).split("%")[0])
            except ValueError:
                return
            if not ip.is_global:
                raise ValueError(tr("Доступ к локальному адресу запрещён."))

    def connect(sock, address):
        check_address(address)
        return old_connect(sock, address)

    def connect_ex(sock, address):
        check_address(address)
        return old_ex(sock, address)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    for name in list(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            os.environ.pop(name, None)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_media_url(url, headers=None, cookiejar=None):
    validate_download_url(url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        SafeRedirect(),
        urllib.request.HTTPCookieProcessor(cookiejar),
    )
    clean = {"User-Agent": "Mozilla/5.0"}
    for k, v in (headers or {}).items():
        if (
            k.lower() in {"user-agent", "referer", "accept"}
            and "\n" not in str(v)
            and "\r" not in str(v)
        ):
            clean[k] = str(v)
    return opener.open(urllib.request.Request(url, headers=clean), timeout=20)


def ffmpeg_path():
    configured = os.getenv("FFMPEG_PATH", "").strip()
    if configured and Path(configured).is_file():
        return configured
    located = shutil.which("ffmpeg")
    if located:
        return located
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def deno_path():
    configured = os.getenv("DENO_PATH", "").strip()
    if configured and Path(configured).is_file():
        return configured
    name = "deno.exe" if os.name == "nt" else "deno"
    local = Path(sys.executable).parent / name
    return str(local) if local.is_file() else shutil.which("deno")


class QuietYDL:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def instagram_url(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == "instagram.com" or host.endswith(".instagram.com")


class DownloadConfigError(ValueError):
    """Administrator configuration errors must not be hidden by fallbacks."""


def cookies_file(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    sites = {
        "INSTAGRAM": ("instagram.com",),
        "TIKTOK": ("tiktok.com",),
        "YOUTUBE": ("youtube.com", "youtu.be"),
        "TWITTER": ("twitter.com", "x.com", "t.co"),
        "FACEBOOK": ("facebook.com", "fb.watch"),
        "VK": ("vk.com", "vkvideo.ru", "vk.ru"),
        "REDDIT": ("reddit.com", "redd.it"),
    }
    key = "DOWNLOAD_COOKIES_FILE"
    for site, domains in sites.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            specific = site + "_COOKIES_FILE"
            if os.getenv(specific, "").strip():
                key = specific
            break
    configured = os.getenv(key, "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    if not path.is_file():
        raise DownloadConfigError(
            tr(
                "Не найден файл cookies. Администратору нужно проверить {setting}.",
                setting=key,
            )
        )
    return str(path)


def resolve_download_url(url):
    """Resolve TikTok share links with GET; HEAD can return a different target."""
    validate_download_url(url)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"vm.tiktok.com", "vt.tiktok.com"} or (
        host in {"www.tiktok.com", "tiktok.com"} and parsed.path.startswith("/t/")
    ):
        with open_media_url(url) as response:
            resolved = response.url
        validate_download_url(resolved)
        target = urlsplit(resolved)
        if target.hostname not in {
            "www.tiktok.com",
            "tiktok.com",
            "m.tiktok.com",
        } or not re.search(r"/(?:video|photo)/\d+", target.path):
            raise ValueError(
                tr(
                    "Короткая ссылка TikTok не ведёт на публикацию. Откройте ролик и заново скопируйте ссылку через «Поделиться»."
                )
            )
        return resolved
    return url


def tiktok_embed_info(url):
    """Fallback using TikTok's own public embed page, without third-party APIs."""
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.tiktok.com", "tiktok.com", "m.tiktok.com"}:
        return None
    match = re.search(r"/(?:video|photo)/(\d+)", parsed.path)
    if not match:
        return None
    video_id = match[1]
    embed = "https://www.tiktok.com/embed/v2/" + video_id
    with open_media_url(embed) as response:
        page = response.read(2_000_001)
    if len(page) > 2_000_000:
        raise ValueError(tr("Сайт вернул слишком много данных."))
    state = re.search(
        r"<script\b[^>]*\bid=[\'\"]__FRONTITY_CONNECT_STATE__[\'\"][^>]*>(.*?)</script>",
        page.decode("utf-8"),
        re.S,
    )
    if not state:
        return None
    data = json.loads(state[1]).get("source", {}).get("data", {})
    item = (
        data.get("/embed/v2/" + video_id, {}).get("videoData", {}).get("itemInfos", {})
    )
    if str(item.get("id")) != video_id or item.get("secret") or item.get("forFriend"):
        return None
    urls = item.get("video", {}).get("urls") or []
    if not urls or not isinstance(urls[0], str):
        return None
    validate_download_url(urls[0])
    return {
        "backend": "tiktok_embed",
        "title": str(item.get("text") or "TikTok")[:180],
        "url": urls[0],
        "ext": "mp4",
        "headers": {"Referer": embed},
        "choices": [
            {"label": tr("🎬 Видео"), "kind": "video"},
            {"label": tr("🎵 Только звук (MP3)"), "kind": "audio"},
        ],
    }


def download_error(url, error):
    message = str(error)
    lower = message.lower()
    if any(
        x in lower for x in ("too many requests", "rate-limit", "rate limit")
    ) or re.search(r"\b(?:http(?: error)?|status(?: code)?)[: ]+429\b", lower):
        return tr("Сайт временно ограничил запросы. Подождите и попробуйте снова.")
    if instagram_url(url) and any(
        marker in message.lower()
        for marker in (
            "empty media response",
            "login required",
            "login_required",
            "log in",
            "rate-limit",
        )
    ):
        return tr(
            "Instagram не предоставил медиа. Проверьте доступность публикации в браузере. "
            "Администратору бота нужно настроить или обновить cookies Instagram "
            "(INSTAGRAM_COOKIES_FILE). Если доступ временно ограничен, попробуйте позже."
        )
    if any(
        x in lower
        for x in (
            "login required",
            "login_required",
            "log in",
            "sign in",
            "cookies",
            "private video",
            "authentication",
        )
    ) and not isinstance(error, ValueError):
        return tr(
            "Для этой публикации требуется вход. Администратору нужно настроить или обновить cookies соответствующего сайта."
        )
    if any(
        x in lower
        for x in (
            "geo-restrict",
            "not available in your country",
            "not available in your region",
        )
    ):
        return tr("Публикация недоступна в регионе сервера бота.")
    if any(
        x in lower
        for x in (
            "410 gone",
            "video has been removed",
            "video unavailable",
            "does not exist",
        )
    ) or re.search(r"\b(?:http(?: error)?|status(?: code)?)[: ]+404\b", lower):
        return tr("Публикация удалена или недоступна. Проверьте ссылку в браузере.")
    if any(
        x in lower
        for x in (
            "forbidden",
            "captcha",
            "challenge",
            "unexpected response from webpage",
            "empty media response",
        )
    ) or re.search(r"\b(?:http(?: error)?|status(?: code)?)[: ]+403\b", lower):
        return tr(
            "Сайт не предоставил медиа: возможна проверка браузера или ограничение доступа. Попробуйте позже; администратору стоит обновить загрузчики и cookies сайта."
        )
    if any(
        x in lower
        for x in (
            "timed out",
            "timeout",
            "connection reset",
            "name resolution",
            "unable to download webpage",
            "http error 5",
        )
    ):
        return tr(
            "Сайт не отвечает или произошёл сетевой сбой. Попробуйте ещё раз позже."
        )
    if "unsupported url" in lower:
        return tr(
            "Эта ссылка не поддерживается. Пришлите прямую ссылку на конкретную публикацию или медиафайл."
        )
    if "requested format is not available" in lower:
        return tr(
            "Выбранное качество больше недоступно. Отправьте ссылку заново и выберите другой формат."
        )
    if isinstance(error, ValueError) and not message.startswith("ERROR:"):
        return message
    return tr(
        "Загрузчики не смогли получить медиа с этой страницы. Проверьте публикацию в браузере; администратору нужно проверить версии yt-dlp и gallery-dl."
    )


@contextmanager
def youtube_session(opts):
    import yt_dlp

    with yt_dlp.YoutubeDL(opts) as ydl:
        if opts.get("cookiefile"):
            try:
                # Load once, but never rewrite the shared administrator cookie file.
                ydl.cookiejar
            except Exception as exc:
                raise DownloadConfigError(
                    tr(
                        "Не удалось прочитать cookies. Нужен действующий файл в формате Netscape."
                    )
                ) from exc
            finally:
                ydl.params["cookiefile"] = None
        yield ydl


def ydl_options(folder, url=""):
    def progress(d):
        write_worker_progress(
            folder,
            "download",
            d.get("downloaded_bytes", 0),
            d.get("total_bytes") or d.get("total_bytes_estimate"),
        )
        if d.get("downloaded_bytes", 0) > DOWNLOAD_SOURCE_LIMIT:
            raise ValueError(tr("Превышен лимит размера исходного файла."))

    opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": QuietYDL(),
        "noplaylist": True,
        "extractor_retries": 1,
        "retries": 1,
        "fragment_retries": 1,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 1,
        "max_filesize": DOWNLOAD_SOURCE_LIMIT,
        "outtmpl": str(folder / "media.%(ext)s"),
        "restrictfilenames": True,
        "cachedir": False,
        "enable_file_urls": False,
        "hls_prefer_native": True,
        "overwrites": True,
        "progress_hooks": [progress],
        "ffmpeg_location": ffmpeg_path(),
        "merge_output_format": "mp4",
        "postprocessor_args": {"ffmpeg_i": ["-protocol_whitelist", "file,pipe"]},
        "proxy": "",
        "remote_components": [],
    }
    cookies = cookies_file(url)
    if cookies:
        opts["cookiefile"] = cookies
    deno = deno_path()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    return opts


def audio_preference(fmt):
    # Extractors mark the creator's original track above defaults and dubs.
    preference = fmt.get("language_preference")
    return preference if preference is not None else -1


def media_choices(info, max_height=None):
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise ValueError(tr("Прямые эфиры не скачиваются. Пришлите завершённое видео."))
    formats = [
        dict(f)
        for f in info.get("formats", [])
        if not f.get("has_drm")
        and f.get("protocol") in {"http", "https", "m3u8_native", "http_dash_segments"}
        and re.fullmatch(r"[A-Za-z0-9_.-]+", str(f.get("format_id", "")))
    ]
    for f in formats:
        # Generic HTML players often provide a media URL without codec metadata.
        if f.get("vcodec") is None and f.get("ext") in {
            "mp4",
            "webm",
            "mkv",
            "mov",
            "m4v",
        }:
            f["vcodec"] = "unknown"
        if f.get("vcodec") is None and f.get("ext") in {
            "m4a",
            "mp3",
            "ogg",
            "opus",
            "wav",
        }:
            f["vcodec"] = "none"
        if f.get("acodec") is None and f.get("vcodec") is not None:
            f["acodec"] = "unknown"
    audio = [
        f
        for f in formats
        if f.get("acodec") not in {None, "none"} and f.get("vcodec") == "none"
    ]
    best_audio = max(
        audio,
        key=lambda f: (
            audio_preference(f),
            f.get("ext") == "m4a",
            f.get("abr") or f.get("tbr") or 0,
        ),
        default=None,
    )
    choices = []
    heights = {}
    for f in formats:
        if f.get("vcodec") in {None, "none"}:
            continue
        size = f.get("filesize") or f.get("filesize_approx")
        if size and size > DOWNLOAD_SOURCE_LIMIT:
            continue
        has_audio = f.get("acodec") not in {None, "none"}
        if not has_audio and not best_audio:
            continue
        height = int(f.get("height") or 0)
        if max_height is not None and (not height or height > max_height):
            continue
        score = (
            audio_preference(f if has_audio else best_audio),
            f.get("ext") == "mp4",
            has_audio,
            f.get("tbr") or 0,
        )
        if height not in heights or score > heights[height][0]:
            heights[height] = (score, f)
    for height, (_, f) in sorted(heights.items(), reverse=True)[:8]:
        selector = f["format_id"]
        if f.get("acodec") in {None, "none"}:
            selector += "+" + best_audio["format_id"]
        choices.append(
            {
                "label": f"🎬 {height}p" if height else tr("🎬 Видео"),
                "format": selector,
                "kind": "video",
                "height": height,
            }
        )
    source_audio = best_audio or max(
        (f for f in formats if f.get("acodec") not in {None, "none"}),
        key=audio_preference,
        default=None,
    )
    if source_audio:
        choices.append(
            {
                "label": tr("🎵 Только звук (MP3)"),
                "format": source_audio["format_id"],
                "kind": "audio",
            }
        )
    if not choices:
        raise ValueError(
            tr("Нет доступных форматов. Возможно, нужен вход или меньшее качество.")
        )
    return choices


def playlist_choices():
    return [
        {
            "label": tr("🎬 Весь плейлист · до {height}p", height=height),
            "kind": "video",
            "max_height": height,
        }
        for height in (2160, 1440, 1080, 720, 480, 360, 240, 144)
    ] + [{"label": tr("🎵 Весь плейлист MP3"), "kind": "audio", "max_height": None}]


def playlist_format(info, selected):
    # Resolve IDs separately for each entry; never exceed the chosen resolution.
    choices = media_choices(info, max_height=selected["max_height"])
    for choice in choices:
        if choice["kind"] == selected["kind"]:
            return choice["format"]
    raise ValueError(
        tr("Нет доступных форматов. Возможно, нужен вход или меньшее качество.")
    )


def gallery_items(url):
    from gallery_dl import config, extractor

    # Only explicitly configured cookie files; no browser sessions or user config.
    config.clear()
    config.set(("extractor",), "retries", 1)
    config.set(("extractor",), "timeout", 20)
    config.set(("extractor",), "proxy", None)
    config.set(("extractor",), "cookies", cookies_file(url))
    config.set(("extractor",), "cookies-update", False)
    config.set(("extractor",), "input", False)
    instance = extractor.find(url)
    if instance is None:
        raise ValueError(tr("Эта страница не поддерживается для фото."))
    items = []
    for n, item in enumerate(instance):
        if n > 100:
            break
        if item[0] == 3:
            address = item[1]
            meta = item[2]
            validate_download_url(address)
            ext = str(
                meta.get("extension") or urlsplit(address).path.rsplit(".", 1)[-1]
            ).lower()
            if ext not in {
                "jpg",
                "jpeg",
                "png",
                "webp",
                "gif",
                "mp4",
                "webm",
                "m4v",
                "mov",
                "mp3",
                "m4a",
                "ogg",
            }:
                continue
            headers = {"Referer": url}
            if instance.session:
                headers.update(
                    {
                        k: v
                        for k, v in instance.session.headers.items()
                        if k.lower() == "user-agent"
                    }
                )
            items.append(
                {
                    "url": address,
                    "ext": ext,
                    "headers": headers,
                    "cookiejar": instance.session.cookies if instance.session else None,
                }
            )
            if len(items) == 10:
                break
    if not items:
        raise ValueError(
            tr(
                "Нет доступных фото без авторизации. Используйте ссылку на конкретную публикацию или файл."
            )
        )
    return items


def sniff_direct(url):
    # Only probe plausible direct files. For social pages use their extractors.
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix not in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".mp4",
        ".webm",
        ".mov",
        ".m4v",
        ".mp3",
        ".m4a",
        ".ogg",
        ".wav",
    }:
        return None
    with open_media_url(url) as response:
        mime = response.headers.get_content_type()
        length = response.headers.get("Content-Length")
        if (
            not mime.startswith(("image/", "video/", "audio/"))
            and mime != "application/octet-stream"
        ):
            return None
        if length and int(length) > DOWNLOAD_SOURCE_LIMIT:
            raise ValueError(tr("Превышен лимит размера исходного файла."))
        return {
            "backend": "direct",
            "title": Path(urlsplit(url).path).name[:120],
            "ext": suffix.lstrip("."),
            "mime": mime,
        }


def inspect_download(url, folder):
    direct = sniff_direct(url)
    if direct:
        kind = (
            "image"
            if direct["ext"] in {"jpg", "jpeg", "png", "webp", "gif"}
            else ("audio" if direct["ext"] in {"mp3", "m4a", "ogg", "wav"} else "video")
        )
        direct["choices"] = [{"label": tr("📥 Скачать оригинал"), "kind": kind}]
        if kind == "video":
            direct["choices"].append(
                {"label": tr("🎵 Только звук (MP3)"), "kind": "audio"}
            )
        return direct
    try:
        opts = ydl_options(folder, url)
        opts.update(noplaylist=False, extract_flat="in_playlist")
        with youtube_session(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError(tr("Публикация недоступна."))
        if info.get("_type") in {"playlist", "multi_video"}:
            entries = []
            for entry in info.get("entries") or []:
                if not entry:
                    continue
                address = entry.get("webpage_url") or entry.get("url")
                if address and address.startswith(("https://", "http://")):
                    entries.append(
                        {
                            "url": address,
                            "title": str(entry.get("title") or tr("Видео"))[:180],
                        }
                    )
            if not entries:
                raise ValueError(tr("В плейлисте нет доступных видео."))
            return {
                "backend": "playlist",
                "title": str(info.get("title") or tr("Плейлист"))[:180],
                "entries": entries,
                "choices": playlist_choices(),
            }
        return {
            "backend": "yt",
            "title": str(info.get("title") or tr("Медиа"))[:180],
            "choices": media_choices(info),
        }
    except DownloadConfigError:
        raise
    except Exception as video_error:
        try:
            embedded = tiktok_embed_info(url)
            if embedded:
                # Refresh signed media URLs on download instead of storing them.
                return {
                    k: v for k, v in embedded.items() if k not in {"url", "headers"}
                }
        except Exception:
            pass
        try:
            items = gallery_items(url)
        except Exception as image_error:
            raise ValueError(download_error(url, video_error)) from image_error
        return {
            "backend": "gallery",
            "video_count": sum(
                item["ext"] in {"mp4", "webm", "m4v", "mov"} for item in items
            ),
            "title": tr("Изображения / медиа публикации"),
            "choices": [
                {
                    "label": tr("🖼 Скачать (до {v0} файлов)", v0=len(items)),
                    "kind": "gallery",
                }
            ],
        }


def download_direct_file(url, path, headers=None, cookiejar=None):
    size = 0
    with open_media_url(url, headers, cookiejar) as response, path.open("wb") as stream:
        length = response.headers.get("Content-Length")
        if length and int(length) > DOWNLOAD_SOURCE_LIMIT:
            raise ValueError(tr("Превышен лимит размера исходного файла."))
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > DOWNLOAD_SOURCE_LIMIT:
                raise ValueError(tr("Превышен лимит размера исходного файла."))
            stream.write(chunk)
            write_worker_progress(
                path.parent, "download", size, int(length) if length else None
            )
    if not size:
        raise ValueError(tr("Сайт вернул пустой файл."))
    return path


def download_files(request, folder):
    url = request["url"]
    info = request["info"]
    selected = info["choices"][int(request["choice"])]
    if info["backend"] == "tiktok_embed":
        fresh = tiktok_embed_info(url)
        if not fresh:
            raise ValueError(tr("Публикация недоступна."))
        fresh["backend"] = "direct"
        return download_files({**request, "url": fresh["url"], "info": fresh}, folder)
    if info["backend"] == "direct":
        path = download_direct_file(
            url, folder / ("media." + info["ext"]), info.get("headers")
        )
        if selected["kind"] == "audio" and info["ext"] not in {
            "mp3",
            "m4a",
            "ogg",
            "wav",
        }:
            target = folder / "audio.mp3"
            subprocess.run(
                [
                    ffmpeg_path(),
                    "-nostdin",
                    "-y",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    str(path),
                    "-vn",
                    "-b:a",
                    "128k",
                    str(target),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            path = target
        return [path]
    if info["backend"] == "gallery":
        files = []
        total = 0
        for i, item in enumerate(gallery_items(url)):
            path = download_direct_file(
                item["url"],
                folder / f"image_{i + 1}.{item['ext']}",
                item["headers"],
                item.get("cookiejar"),
            )
            total += path.stat().st_size
            if total > DOWNLOAD_TOTAL:
                raise ValueError(tr("Превышен настроенный лимит временных файлов."))
            files.append(path)
        return files
    if info["backend"] != "yt":
        raise ValueError(tr("Неизвестный источник."))
    opts = ydl_options(folder, url)
    if "max_height" in selected:

        def select_format(context):
            selector = playlist_format(context, selected)
            return ydl.build_format_selector(selector)(context)

        opts["format"] = select_format
    else:
        # Keep saved single-video choices and older playlist buttons compatible.
        opts["format"] = selected["format"]
    if selected["kind"] == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ]
    with youtube_session(opts) as ydl:
        try:
            fresh = ydl.extract_info(url, download=False)
        except Exception:
            embedded = tiktok_embed_info(url)
            if not embedded:
                raise
            embedded["backend"] = "direct"
            embedded["choices"] = [selected]
            return download_files(
                {**request, "url": embedded["url"], "info": embedded, "choice": 0},
                folder,
            )
        if (
            not fresh
            or fresh.get("is_live")
            or fresh.get("_type") in {"playlist", "multi_video"}
        ):
            raise ValueError(tr("Нужна завершённая одиночная публикация."))
        for f in fresh.get("requested_formats") or [fresh]:
            if f.get("protocol") not in {
                "http",
                "https",
                "m3u8_native",
                "http_dash_segments",
            }:
                raise ValueError(tr("Этот протокол скачивания не поддерживается."))
        ydl.process_info(fresh)
    allowed = {".mp4", ".webm", ".mkv", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".mov"}
    files = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in allowed
        and not re.search(r"\.f\d+\.", p.name)
    ]
    if not files:
        raise ValueError(tr("Скачивание не создало готовый медиафайл."))
    return [max(files, key=lambda p: p.stat().st_mtime)]


def download_worker_main():
    url = ""
    try:
        request = json.loads(sys.stdin.read(1_000_000))
        language_context.set(request.get("language", "ru"))
        url = validate_download_url(request["url"])
        folder = Path(request["folder"]).resolve()
        if not folder.is_dir():
            raise ValueError(tr("Временная папка не найдена."))
        install_public_network_guard()
        url = resolve_download_url(url)
        request["url"] = url
        if request["action"] == "inspect":
            result = inspect_download(url, folder)
        else:
            files = download_files(request, folder)
            prepared = []
            metadata = {}
            for path in files:
                if path.suffix.lower() in VIDEO_EXTENSIONS:
                    write_worker_progress(folder, "processing")
                    for part in prepare_video(path, ffmpeg_path(), DOWNLOAD_LIMIT):
                        prepared.append(part)
                        metadata[str(part.resolve())] = upload_metadata(
                            part, ffmpeg_path()
                        )
                else:
                    prepared.append(path)
            files = prepared
            for path in files:
                if (
                    not path.resolve().is_relative_to(folder)
                    or not 0 < path.stat().st_size <= DOWNLOAD_LIMIT
                ):
                    raise ValueError(tr("Размер файла превышает лимит Telegram 49 МБ."))
            result = {"files": [str(p.resolve()) for p in files], "metadata": metadata}
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except BaseException as exc:
        traceback.print_exc(file=sys.stderr)
        print(
            json.dumps(
                {"ok": False, "error": download_error(url, exc)[:700]},
                ensure_ascii=False,
            )
        )


async def stop_download_worker(proc):
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(proc.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await proc.wait()


async def run_download_worker(action, url, folder, info=None, selection=0):
    validate_download_url(url)
    kwargs = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    env = dict(
        os.environ,
        DB_FILE=":memory:",
        BOT_TOKEN="",
        ADMIN_ID="0",
        PYTHONIOENCODING="utf-8",
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.download_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=str(Path(__file__).resolve().parent.parent),
        **kwargs,
    )
    request = json.dumps(
        {
            "action": action,
            "url": url,
            "folder": str(folder),
            "info": info,
            "choice": selection,
            "language": language_context.get(),
        }
    ).encode()
    task = asyncio.create_task(proc.communicate(request))
    started = asyncio.get_running_loop().time()
    timeout = 180 if action == "inspect" else 3600
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.5)
            report = current_progress.get()
            if report and (folder / "progress.json").exists():
                try:
                    info_progress = json.loads(
                        (folder / "progress.json").read_text(encoding="utf-8")
                    )
                    await report.update(
                        info_progress["phase"],
                        info_progress.get("done", 0),
                        info_progress.get("total"),
                    )
                except (OSError, ValueError, KeyError):
                    pass
            if asyncio.get_running_loop().time() - started > timeout:
                raise ValueError(
                    tr(
                        "Скачивание заняло слишком долго. Попробуйте другую ссылку или меньшее качество."
                    )
                )
            if temporary_size(folder) > DOWNLOAD_TOTAL:
                raise ValueError(tr("Превышен настроенный лимит временных файлов."))
        stdout, stderr = task.result()
        if len(stdout) > 1_000_000:
            raise ValueError(tr("Сайт вернул слишком много данных."))
        try:
            output = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
        except Exception as exc:
            raise ValueError(
                tr("Не удалось обработать медиа. Проверьте зависимости загрузчика.")
            ) from exc
        if not output.get("ok"):
            logging.getLogger(__name__).error(
                "Download worker failed (%s): %s",
                action,
                stderr.decode("utf-8", errors="replace")[-4000:],
            )
            raise ValueError(output.get("error", tr("Скачивание не удалось.")))
        return output["result"]
    finally:
        await stop_download_worker(proc)
        if not task.done():
            await task


def temporary_size(folder):
    total = 0
    for path in folder.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            # yt-dlp renames completed parts while the parent scans the folder.
            continue
    return total
