from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yt_dlp

from . import media, storage, youtube_access, zoom_access


logger = logging.getLogger(__name__)

SUPPORTED_HOSTS: dict[str, str] = {
    "youtube.com": "youtube",
    "www.youtube.com": "youtube",
    "m.youtube.com": "youtube",
    "music.youtube.com": "youtube",
    "youtu.be": "youtube",
    "vk.com": "vk",
    "m.vk.com": "vk",
    "vkvideo.ru": "vk",
    "www.vkvideo.ru": "vk",
    "m.vkvideo.ru": "vk",
    "vk.ru": "vk",
    "rutube.ru": "rutube",
    "www.rutube.ru": "rutube",
    "disk.yandex.ru": "yandexdisk",
    "disk.yandex.com": "yandexdisk",
    "disk.yandex.by": "yandexdisk",
    "disk.yandex.kz": "yandexdisk",
    "disk.360.yandex.ru": "yandexdisk",
    "disk.360.yandex.com": "yandexdisk",
    "disk.360.yandex.by": "yandexdisk",
    "disk.360.yandex.kz": "yandexdisk",
    "yadi.sk": "yandexdisk",
    "drive.google.com": "gdrive",
    "docs.google.com": "gdrive",
}
PROVIDER_LABELS = {
    "youtube": "YouTube",
    "vk": "VK",
    "rutube": "RuTube",
    "yandexdisk": "Яндекс.Диск",
    "gdrive": "Google Drive",
    "zoom": "Zoom",
}
SUPPORTED_PROVIDERS_TEXT = (
    "YouTube, VK, RuTube, Яндекс.Диск, Google Drive и Zoom"
)

ACTIVE_STATUSES = {
    "queued",
    "extracting",
    "downloading",
    "reconnecting",
    "resuming",
    "finalizing",
    "pausing",
    "stopping",
}
RECOVERABLE_STATUSES = {
    "queued",
    "extracting",
    "downloading",
    "reconnecting",
    "resuming",
    "finalizing",
}
TERMINAL_STATUSES = {"completed", "cancelled"}
_STATE: dict[str, dict[str, Any]] = {}
_THREADS: dict[str, threading.Thread] = {}
_LOCK = threading.RLock()
_LAST_SAVE: dict[str, float] = {}
_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_COMPLETION_HANDLER: (
    Callable[[str, Path, dict[str, Any]], str] | None
) = None

_RECONNECT_BASE_SECONDS = 5
_RECONNECT_MAX_SECONDS = 60
_NO_PROGRESS_LIMIT_SECONDS = 30 * 60
_MAX_ZERO_PROGRESS_ATTEMPTS = 6
_PROBE_CACHE_TTL_SECONDS = 5 * 60
_PROBE_ATTEMPTS = 3
_PENDING: list[str] = []
_DISPATCHER: threading.Thread | None = None
_SHUTDOWN = threading.Event()
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_VKVIDEO_LIVE_PATH = re.compile(r"^/live(-?\d+_\d+)/?$", re.IGNORECASE)
# Контейнер под кодек звука. Дорожка переносится как есть, без перекодирования,
# а положить её не в тот контейнер FFmpeg просто не даст: opus и vorbis живут в
# webm и ogg, AAC — в m4a.
_AUDIO_CONTAINERS = {
    "aac": ".m4a",
    "alac": ".m4a",
    "mp3": ".mp3",
    "opus": ".opus",
    "vorbis": ".ogg",
    "flac": ".flac",
    "ac3": ".ac3",
}


class DownloadPaused(Exception):
    pass


class DownloadCancelled(Exception):
    pass


class DownloadInterrupted(Exception):
    pass


def configure_completion_handler(
    handler: Callable[[str, Path, dict[str, Any]], str],
) -> None:
    global _COMPLETION_HANDLER
    _COMPLETION_HANDLER = handler


# Провайдеры, у которых поддомен свой у каждого аккаунта, поэтому точный список
# хостов не годится: us02web.zoom.us, us05web.zoom.us, company.zoom.us.
SUPPORTED_HOST_SUFFIXES = {
    "zoom.us": "zoom",
}

# Провайдеры, которым нужен код доступа к записи.
PASSCODE_PROVIDERS = {"zoom"}


def detect_provider(url: str) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    exact = SUPPORTED_HOSTS.get(host)
    if exact:
        return exact
    for suffix, provider in SUPPORTED_HOST_SUFFIXES.items():
        if host == suffix or host.endswith("." + suffix):
            return provider
    return None


def _zoom_cookies(url: str, passcode: str | None) -> Path | None:
    """Пройти код доступа Zoom и вернуть файл cookies для yt-dlp.

    Свой экстрактор Zoom у yt-dlp сломан: сервис перешёл на страницу-компонент,
    а старая ручка проверки пароля удалена. Проверку проходим сами и передаём
    yt-dlp уже открытую сессию — дальше он извлекает запись обычным путём.
    """
    if detect_provider(url) != "zoom":
        return None
    try:
        return zoom_access.authorize(url, passcode)
    except zoom_access.ZoomAccessError:
        raise
    except Exception as exc:
        logger.warning("Не удалось пройти код доступа Zoom: %s", exc)
        return None


def needs_passcode(url: str) -> bool:
    """Нужен ли этой ссылке код доступа к записи."""
    return detect_provider(url) in PASSCODE_PROVIDERS


def normalize_url(url: str) -> str:
    cleaned = (url or "").strip()
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return cleaned
    if (parsed.hostname or "").lower() not in {
        "vkvideo.ru",
        "www.vkvideo.ru",
        "m.vkvideo.ru",
    }:
        return cleaned
    match = _VKVIDEO_LIVE_PATH.fullmatch(parsed.path or "")
    if not match:
        return cleaned
    return parsed._replace(
        path=f"/video{match.group(1)}",
        fragment="",
    ).geturl()


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _js_runtime_options() -> dict[str, Any]:
    """JavaScript-движок для yt-dlp.

    С 2026 года YouTube отдаёт ссылки на дорожки только после расшифровки
    подписи его JavaScript-плеером. Без движка yt-dlp видит неполный список
    форматов, звуковые дорожки отвечают 403, а совмещённый файл качается
    первые сотни килобайт и замолкает — загрузка падала с «Соединение слишком
    долго не передаёт данные». По умолчанию yt-dlp ищет только deno; на этой
    машине стоит Node, поэтому движок указывается явно, когда он найден.
    """
    node = shutil.which("node")
    if not node:
        return {}
    return {"js_runtimes": {"node": {"path": node}}}


class ProxyAccessError(RuntimeError):
    pass


def _extract(url, options, provider, *, download):
    if provider != "youtube":
        return _extract_clients(url, options, provider, download=download)
    settings = storage.get_settings()
    mode = settings.get("youtube_connection", "direct")
    if mode != "proxy":
        return _extract_clients(url, options, provider, download=download)
    proxy = settings.get("youtube_proxy", "")
    if not proxy:
        raise ProxyAccessError("В настройках выбрано подключение через прокси, но его адрес не указан.")
    # The same yt-dlp connection handles metadata, both media tracks and resume.
    # Do not log upstream proxy credentials.
    try:
        return _extract_clients(url, options | {"proxy": proxy, "logger": youtube_access.QuietLogger()}, provider, download=download)
    except (DownloadPaused, DownloadCancelled, DownloadInterrupted):
        raise
    except Exception as exc:
        text = str(exc).lower()
        if any(marker in text for marker in ("proxy", "tunnel connection", "certificate verify", "connect tunnel")):
            status = re.search(r"(?:failed:|response|error)\s*(\d{3})", text)
            detail = f" (HTTP {status[1]})" if status else ""
            raise ProxyAccessError(f"Прокси не установил соединение с YouTube{detail}. Проверьте подключение или выберите другой прокси в настройках.") from None
        raise


def _extract_clients(
    url: str,
    options: dict[str, Any],
    provider: str | None,
    *,
    download: bool,
) -> Any:
    """Спросить у yt-dlp данные ссылки, перебирая обходные пути YouTube.

    YouTube отвечает «подтвердите, что вы не робот» на запрос, а не на видео:
    тот же ролик отдаётся, если спросить его от имени другого клиента API.
    Раньше такая ошибка была окончательной и приезжала пользователю английской
    трассой со ссылками на GitHub. Теперь способы перебираются молча, и первый
    сработавший запоминается, чтобы следующая ссылка пошла сразу через него.
    Остальные источники этого перебора не знают: у них ровно одна попытка.
    """
    last: Exception | None = None
    options = {**_js_runtime_options(), **options}
    for plan in youtube_access.plans(provider, download=download):
        try:
            with yt_dlp.YoutubeDL(youtube_access.merge(options, plan)) as ydl:
                info = ydl.extract_info(url, download=download)
        except (DownloadPaused, DownloadCancelled, DownloadInterrupted):
            # Пауза и отмена приходят из хука прогресса. Это решение
            # пользователя, а не отказ источника: перебирать после них нечего.
            raise
        except Exception as exc:
            if options.get("proxy") and any(marker in str(exc).lower() for marker in ("unable to connect to proxy", "tunnel connection failed", "certificate verify")):
                raise
            if not youtube_access.worth_retrying(exc):
                raise
            logger.info(
                "YouTube: способ «%s» не прошёл (%s), пробуем следующий",
                plan.name,
                type(exc).__name__,
            )
            last = exc
            continue
        youtube_access.remember(plan.name, download=download)
        return info
    # Способы кончились. Наружу уходит последняя ошибка: она свежее прочих и
    # описывает то состояние источника, с которым пользователь столкнётся,
    # если попробует ещё раз.
    if last is not None:
        raise last
    return None


def probe_metadata(url: str, passcode: str | None = None) -> dict[str, Any]:
    provider = detect_provider(url)
    if not provider:
        raise ValueError(
            f"Поддерживаются только ссылки: {SUPPORTED_PROVIDERS_TEXT}."
        )
    normalized_url = normalize_url(url)
    # Ключ кеша включает код доступа: с неверным кодом ответа нет, а с верным
    # он появляется, и один ключ на оба случая отдавал бы устаревший результат.
    cache_key = f"{normalized_url}|{passcode or ''}"
    now = time.monotonic()
    with _LOCK:
        cached = _PROBE_CACHE.get(cache_key)
        if cached and now - cached[0] <= _PROBE_CACHE_TTL_SECONDS:
            return deepcopy(cached[1])
        if cached:
            _PROBE_CACHE.pop(cache_key, None)
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
    }
    if passcode:
        # Так yt-dlp называет код доступа к записи. У Zoom он вводится в форму
        # на странице записи, а не передаётся в ссылке.
        options["videopassword"] = passcode
    cookie_file = _zoom_cookies(normalized_url, passcode)
    if cookie_file:
        options["cookiefile"] = str(cookie_file)
    info: Any = None
    for attempt in range(_PROBE_ATTEMPTS):
        try:
            info = _extract(normalized_url, options, provider, download=False)
            break
        except Exception as exc:
            if (
                _classify_error(exc) == "permanent"
                or attempt + 1 >= _PROBE_ATTEMPTS
            ):
                raise
            time.sleep(2**attempt)
    if isinstance(info, dict) and info.get("_type") == "playlist":
        entries = info.get("entries") or []
        info = entries[0] if entries else {}
    if not isinstance(info, dict):
        info = {}

    formats = info.get("formats") or []
    duration_sec = _safe_int(info.get("duration"))
    video_by_height: dict[int, dict[str, Any]] = {}
    combined_by_height: dict[int, dict[str, Any]] = {}
    best_audio: dict[str, Any] | None = None

    def format_size(item: dict[str, Any]) -> int | None:
        direct = _safe_int(
            item.get("filesize") or item.get("filesize_approx")
        )
        if direct:
            return direct
        # Многие сайты не отдают Content-Length до начала загрузки. В таком
        # случае yt-dlp всё равно знает средний битрейт, которого достаточно
        # для понятной пользователю ориентировочной оценки.
        bitrate_kbps = item.get("tbr")
        if not bitrate_kbps and str(item.get("vcodec") or "none") == "none":
            bitrate_kbps = item.get("abr")
        try:
            if duration_sec and bitrate_kbps:
                return int(
                    float(bitrate_kbps) * 1000 / 8 * duration_sec
                )
        except (TypeError, ValueError):
            pass
        return None

    for item in formats:
        if not isinstance(item, dict):
            continue
        # None означает «кодек неизвестен», а не «дорожки нет»: строку "none"
        # yt-dlp ставит явно. Zoom возвращает оба кодека как None, и при
        # склейке в "none" его записи не попадали ни в видео, ни в звук —
        # список качеств выходил пустым.
        video_codec = str(item.get("vcodec") or "").lower()
        audio_codec = str(item.get("acodec") or "").lower()
        height = _safe_int(item.get("height"))
        size = format_size(item) or 0
        if video_codec == "none" and audio_codec != "none":
            if best_audio is None or size > (format_size(best_audio) or 0):
                best_audio = item
        elif video_codec != "none" and height:
            previous = video_by_height.get(height)
            if previous is None or size > (format_size(previous) or 0):
                video_by_height[height] = item
            if audio_codec != "none":
                previous_combined = combined_by_height.get(height)
                if (
                    previous_combined is None
                    or size > (format_size(previous_combined) or 0)
                ):
                    combined_by_height[height] = item

    qualities: list[dict[str, Any]] = []
    used_heights: set[int] = set()
    audio_size = format_size(best_audio) if best_audio else None
    for target in (2160, 1440, 1080, 720, 480, 360):
        available = [
            height
            for height in video_by_height
            if height <= target and height not in used_heights
        ]
        if not available:
            continue
        chosen_height = max(available)
        used_heights.add(chosen_height)
        video_format = video_by_height[chosen_height]
        video_size = format_size(video_format)
        combined = combined_by_height.get(chosen_height) is video_format
        has_combined = chosen_height in combined_by_height
        audio_available = best_audio is not None or has_combined
        total_size = (
            video_size
            if combined
            else (
                video_size + audio_size
                if video_size and audio_size
                else video_size or audio_size
            )
        )
        qualities.append(
            {
                "quality": str(chosen_height),
                "label": f"{chosen_height}p",
                "height": chosen_height,
                "ext": video_format.get("ext") or "mp4",
                "filesize_bytes": total_size,
                "audio_available": audio_available,
                "audio_source": "combined" if combined else "separate" if best_audio else "combined" if has_combined else None,
                "audio_codec": (best_audio or combined_by_height.get(chosen_height) or {}).get("acodec"),
                "unavailable_reason": None if audio_available else "У этого качества нет доступной аудиодорожки",
            }
        )
    if best_audio:
        qualities.append(
            {
                "quality": "audio",
                "label": "Только звук",
                "height": None,
                "ext": best_audio.get("ext") or "m4a",
                "filesize_bytes": audio_size,
            }
        )

    # Оценка размера под «Авто» должна совпадать с тем, что реально скачается,
    # поэтому здесь та же функция подбора, что и в _format_selector.
    automatic_quality = resolve_auto_quality(qualities)
    main_filesize = _safe_int(
        info.get("filesize") or info.get("filesize_approx")
    )
    result = {
        "provider": provider,
        "provider_label": PROVIDER_LABELS[provider],
        "title": info.get("title") or "Видео",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration_sec": duration_sec,
        "filesize_bytes": main_filesize,
        "auto_filesize_bytes": (
            automatic_quality.get("filesize_bytes")
            if automatic_quality
            else main_filesize
        ),
        "webpage_url": info.get("webpage_url") or url.strip(),
        "qualities": qualities,
        "audio_available": (
            True if best_audio or combined_by_height or any(
                item.get("acodec") and item["acodec"] != "none" for item in formats if isinstance(item, dict)
            ) else None if not formats or any(
                item.get("acodec") is None for item in formats if isinstance(item, dict)
            ) else False
        ),
    }
    with _LOCK:
        _PROBE_CACHE[cache_key] = (time.monotonic(), deepcopy(result))
    return result


def _job_path(project_id: str) -> Path:
    return storage.session_dir(project_id) / "url_download.json"


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Состояние загрузки для интерфейса.

    Поля перечислены явно, и код доступа к записи сюда намеренно не попадает:
    он нужен только yt-dlp и не должен уезжать в браузер.
    """
    return {
        "project_id": state.get("project_id"),
        "status": state.get("status"),
        "url": state.get("url"),
        "provider": state.get("provider"),
        "provider_label": PROVIDER_LABELS.get(
            str(state.get("provider") or ""),
            str(state.get("provider") or ""),
        ),
        "quality": state.get("quality"),
        "title": state.get("title"),
        "uploader": state.get("uploader"),
        "duration_sec": state.get("duration_sec"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "percent": state.get("percent"),
        "downloaded_bytes": state.get("downloaded_bytes") or 0,
        "total_bytes": state.get("total_bytes"),
        "speed_bps": state.get("speed_bps"),
        "eta_sec": state.get("eta_sec"),
        "filename": (
            Path(state["filename"]).name if state.get("filename") else None
        ),
        "error": state.get("error"),
        "attempts": int(state.get("attempts") or 0),
        "resumable": bool(state.get("resumable")),
        "audio_verified": bool(state.get("audio_verified")),
        "audio_codec": state.get("audio_codec"),
        "video_height": state.get("video_height"),
        "transcription_job_id": state.get("transcription_job_id"),
        "transcription_options": state.get("transcription_options") or {},
        # Загрузки, начатые до появления флага, лежат на диске без него, и
        # старое поведение для них — расшифровывать.
        "transcribe": bool(state.get("transcribe", True)),
    }


def _save(project_id: str, state: dict[str, Any]) -> None:
    with _LOCK:
        _save_locked(project_id, state)


def _save_locked(project_id: str, state: dict[str, Any]) -> None:
    path = _job_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    persisted = {
        key: value
        for key, value in state.items()
        if not key.startswith("_")
    }
    persisted["updated_at"] = time.time()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(persisted, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _load(project_id: str) -> dict[str, Any] | None:
    path = _job_path(project_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("project_id", project_id)
    payload["_pause_requested"] = False
    payload["_cancel_requested"] = False
    return payload


def get_status(project_id: str) -> dict[str, Any] | None:
    with _LOCK:
        state = _STATE.get(project_id)
        if state:
            return _snapshot(state)
    loaded = _load(project_id)
    return _snapshot(loaded) if loaded else None


def list_downloads() -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    try:
        projects = storage.list_sessions()
    except Exception:
        projects = []
    for project in projects:
        project_id = str(project["id"])
        loaded = _load(project_id)
        if loaded:
            result[project_id] = loaded
    with _LOCK:
        result.update(_STATE)
    return sorted(
        (_snapshot(item) for item in result.values()),
        key=lambda item: float(item.get("started_at") or 0),
        reverse=True,
    )


def active_project_ids() -> set[str]:
    return {
        item["project_id"]
        for item in list_downloads()
        if item["status"] in ACTIVE_STATUSES
    }


def forget(project_id: str) -> None:
    with _LOCK:
        _STATE.pop(project_id, None)
        _THREADS.pop(project_id, None)
    _LAST_SAVE.pop(project_id, None)


def _siblings_named(folder: Path, prefix: str) -> list[Path]:
    """Файлы папки с этим именем — вместе с кусками вида `Имя.f137.mp4`.

    Раньше здесь стоял `glob(f"{prefix}*")`. Название видео попадает в шаблон
    как есть, а квадратная скобка в нём — это уже класс символов, и такой
    поиск молча не находил ничего. Сравнение имён обходится без экранирования.
    """
    try:
        return [
            path
            for path in folder.iterdir()
            if path.is_file()
            and (path.stem == prefix or path.stem.startswith(f"{prefix}."))
        ]
    except OSError:
        return []


def _free_base(folder: Path, name: str) -> str:
    """Имя, которое в этой папке ещё никем не занято."""
    taken = {path.stem for path in _all_files(folder)}
    candidate = name
    index = 2
    while candidate in taken:
        candidate = f"{name} ({index})"
        index += 1
    return candidate


def _all_files(folder: Path) -> list[Path]:
    try:
        return [path for path in folder.iterdir() if path.is_file()]
    except OSError:
        return []


def _sanitize_filename(value: str) -> str:
    clean = _INVALID_FILENAME.sub("_", (value or "").strip())
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    return (clean or "video")[:100]


def preferred_quality() -> str:
    """Качество по умолчанию из настроек: audio, число или max."""
    settings = storage.get_settings()
    return "audio" if settings.get("download_mode") == "audio" else settings["download_quality"]


def resolve_auto_quality(qualities: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Какой из доступных вариантов подставить под «Авто».

    Настройка задаёт пожелание, а не жёсткое требование: у каждого видео свой
    набор высот, и точного совпадения обычно нет. Правила подбора:

    * `audio` — только звук, если он есть; иначе самое лёгкое видео.
    * `max` — самое высокое доступное.
    * число N — самое высокое видео не выше N. Если все доступные выше N
      (например хотели 480p, а есть только 720p и 1080p), берётся самое низкое:
      ближайшее сверху лучше, чем отказ или неожиданные 1080p.
    """
    audio = next(
        (item for item in qualities if item.get("quality") == "audio"), None
    )
    videos = sorted(
        (item for item in qualities if item.get("height") and item.get("audio_available") is not False),
        key=lambda item: int(item["height"]),
    )
    preference = preferred_quality()

    if preference == "audio":
        return audio or (videos[0] if videos else None)
    if not videos:
        return audio
    if preference == "max":
        return videos[-1]
    try:
        target = int(preference)
    except (TypeError, ValueError):
        target = 1080
    fitting = [item for item in videos if int(item["height"]) <= target]
    # Ближайшее снизу, а если такого нет — ближайшее сверху.
    return fitting[-1] if fitting else videos[0]


def _format_selector(quality: str, provider: str | None = None) -> str:
    if quality == "max":
        return _video_selector("")
    if quality == "audio":
        # У VK отдельный DASH audio иногда короче исходника, а audio-only HLS
        # может собираться с повреждёнными AAC-пакетами. Берём самый лёгкий
        # полный совмещённый HLS, а после загрузки оставляем только его AAC.
        if provider == "vk":
            return (
                "worst[height>0][protocol=m3u8_native][format_id^=hls-]/"
                "worst[height>0]"
            )
        # /best в конце — запасной путь для источников без отдельной
        # аудиодорожки: у записей Zoom звук только внутри общего MP4, и без
        # этого запроса «только звук» падал бы с «формат недоступен».
        # YouTube всё чаще кладёт рядом с оригиналом автоматический дубляж
        # на другом языке, и у дорожек одинаковые номера форматов. Сортировка
        # yt-dlp обычно ставит оригинал выше, но на это нельзя полагаться:
        # чужой синтетический голос вместо живого не заметен ни по имени
        # файла, ни по длительности. Поэтому оригинал запрашивается явно.
        return (
            "bestaudio[ext=m4a][format_note*=original]/"
            "bestaudio[format_note*=original]/"
            "bestaudio[ext=m4a]/bestaudio/best"
        )
    if quality.isdigit() and 100 <= int(quality) <= 4320:
        return _video_selector(f"[height<={quality}]")
    # «Авто» — это настройка пользователя, а не жёсткие 1080p, как было раньше.
    # Именно из-за них качество приходилось понижать руками при каждой загрузке.
    preference = preferred_quality()
    if preference == "audio":
        return _format_selector("audio", provider)
    if preference.isdigit():
        return _format_selector(preference, provider)
    return _video_selector("")


def _video_selector(height_filter: str) -> str:
    # Prefer original AAC/m4a and H.264, with validated conversion if AAC is unavailable.
    audio = "(bestaudio[ext=m4a][format_note*=original]/bestaudio[ext=m4a]/bestaudio[format_note*=original]/bestaudio)"
    exact = height_filter.replace("<=", "=")
    preferred = (f"bestvideo{exact}[vcodec^=avc1]+{audio}/bestvideo{exact}+{audio}/"
                 if height_filter else "")
    return (preferred + f"bestvideo{height_filter}+{audio}/"
            f"best{height_filter}[acodec!=?none]/best[acodec!=?none]")


def start_download(
    project_id: str,
    url: str,
    quality: str,
    metadata: dict[str, Any],
    transcription_options: dict[str, Any] | None = None,
    passcode: str | None = None,
    transcribe: bool = False,
) -> dict[str, Any]:
    provider = detect_provider(url)
    if not provider:
        raise ValueError(
            f"Поддерживаются только ссылки: {SUPPORTED_PROVIDERS_TEXT}."
        )
    selected_quality = (
        quality
        if quality in {"auto", "audio", "max"}
        or (quality.isdigit() and 100 <= int(quality) <= 4320)
        else "auto"
    )
    # Freeze the actual choice at enqueue time: settings may change while queued/paused.
    if metadata.get("audio_available") is False:
        raise ValueError("Источник не отдал доступную аудиодорожку. Видео без звука скачиваться не будет.")
    if selected_quality == "auto":
        preference = preferred_quality()
        chosen = resolve_auto_quality(metadata.get("qualities") or [])
        selected_quality = (
            "audio" if preference == "audio"
            else str(chosen["quality"]) if chosen else preference
        )
    elif selected_quality.isdigit():
        heights = sorted(int(q["height"]) for q in metadata.get("qualities", [])
                         if q.get("height") and q.get("audio_available") is not False)
        if heights:
            fitting = [h for h in heights if h <= int(selected_quality)]
            selected_quality = str(fitting[-1] if fitting else heights[0])
    media_dir = storage.session_dir(project_id) / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    # Скачанный файл называется как сама запись. Раньше к названию спереди
    # приклеивались слово `downloaded` и отметка времени — они брали на себя
    # уникальность, но в проводнике это выглядело служебной строкой. Теперь
    # уникальность обеспечивает номер в конце, как у любого повторного файла.
    safe_title = _free_base(
        media_dir,
        _sanitize_filename(str(metadata.get("title") or "Видео")),
    )
    out_template = str(media_dir / f"{safe_title}.%(ext)s")
    state = {
        "project_id": project_id,
        "status": "queued",
        "url": url.strip(),
        "provider": provider,
        # Код доступа к записи. Хранится в состоянии на диске, иначе докачка
        # после перезапуска сервиса упиралась бы в форму пароля. В ответах API
        # он не отдаётся — за это отвечает _snapshot.
        "passcode": (passcode or "").strip() or None,
        "quality": selected_quality,
        "title": metadata.get("title") or "Видео",
        "uploader": metadata.get("uploader"),
        "duration_sec": metadata.get("duration_sec"),
        "out_template": out_template,
        "started_at": time.time(),
        "finished_at": None,
        "percent": 0.0,
        "downloaded_bytes": 0,
        "total_bytes": None,
        "speed_bps": None,
        "eta_sec": None,
        "filename": None,
        "error": None,
        "attempts": 0,
        "resumable": False,
        "transcription_job_id": None,
        "transcription_options": transcription_options or {},
        # Ставить ли расшифровку в очередь, когда файл скачается. Выключенный
        # флаг не отменяет её насовсем: файл ложится в проект готовым к работе,
        # и запустить распознавание можно потом, когда дойдут руки.
        "transcribe": bool(transcribe),
        "_pause_requested": False,
        "_cancel_requested": False,
    }
    with _LOCK:
        existing = _STATE.get(project_id)
        if existing and existing.get("status") in ACTIVE_STATUSES:
            raise ValueError("Эта загрузка уже выполняется.")
        _STATE[project_id] = state
    _save(project_id, state)
    _spawn(project_id)
    return _snapshot(state)


def pause(project_id: str) -> bool:
    with _LOCK:
        state = _STATE.get(project_id)
        if not state or state.get("status") not in ACTIVE_STATUSES:
            return False
        thread = _THREADS.get(project_id)
        if not thread or not thread.is_alive():
            if project_id in _PENDING:
                _PENDING.remove(project_id)
            _finish(project_id, state, "paused", resumable=True)
            return True
        state["_pause_requested"] = True
        state["status"] = "pausing"
        _save(project_id, state)
    return True


def cancel(project_id: str) -> bool:
    with _LOCK:
        state = _STATE.get(project_id) or _load(project_id)
        if not state or state.get("status") in TERMINAL_STATUSES:
            return False
        thread = _THREADS.get(project_id)
        if not thread or not thread.is_alive():
            if project_id in _PENDING:
                _PENDING.remove(project_id)
            _STATE[project_id] = state
            _cleanup_partial(state)
            _finish(project_id, state, "cancelled")
            return True
        state["_cancel_requested"] = True
        state["_pause_requested"] = False
        state["status"] = "stopping"
        _save(project_id, state)
    return True


def resume(project_id: str) -> tuple[bool, str]:
    with _LOCK:
        thread = _THREADS.get(project_id)
        if thread and thread.is_alive():
            return False, "Загрузка ещё не остановилась."
        state = _STATE.get(project_id) or _load(project_id)
        if not state:
            return False, "Сохранённая загрузка не найдена."
        if state.get("status") not in {"paused", "error", "interrupted"}:
            return False, "Эту загрузку сейчас нельзя продолжить."
        state["_pause_requested"] = False
        state["_cancel_requested"] = False
        state["status"] = "resuming"
        state["error"] = None
        state["resumable"] = False
        _STATE[project_id] = state
        _save(project_id, state)
    _spawn(project_id)
    return True, ""


def recover_interrupted_downloads() -> int:
    recovered = 0
    for project in storage.list_sessions():
        project_id = str(project["id"])
        state = _load(project_id)
        if not state:
            continue
        status = state.get("status")
        if status == "pausing":
            state["status"] = "paused"
            state["resumable"] = True
            _save(project_id, state)
        elif status == "stopping":
            state["status"] = "cancelled"
            state["resumable"] = False
            _cleanup_partial(state)
            _save(project_id, state)
        elif status in RECOVERABLE_STATUSES:
            state["status"] = "resuming"
            state["resumable"] = False
            with _LOCK:
                _STATE[project_id] = state
            _save(project_id, state)
            _spawn(project_id)
            recovered += 1
    return recovered


def _spawn(project_id: str) -> None:
    with _LOCK:
        if project_id not in _PENDING:
            _PENDING.append(project_id)


def start_scheduler() -> None:
    global _DISPATCHER
    with _LOCK:
        if _DISPATCHER and _DISPATCHER.is_alive():
            return
        _SHUTDOWN.clear()
        _DISPATCHER = threading.Thread(target=_dispatch, name="download-queue", daemon=True)
        _DISPATCHER.start()


def _dispatch() -> None:
    while not _SHUTDOWN.is_set():
        limit = int(storage.get_settings().get("max_parallel", "2"))
        with _LOCK:
            alive = sum(t.is_alive() for t in _THREADS.values())
            while _PENDING and alive < limit and not _SHUTDOWN.is_set():
                project_id = _PENDING.pop(0)
                state = _STATE.get(project_id)
                if not state or state["status"] not in {"queued", "resuming"}:
                    continue
                thread = threading.Thread(target=_run, args=(project_id,),
                                          name=f"download-{project_id[:8]}", daemon=True)
                _THREADS[project_id] = thread
                thread.start()
                alive += 1
        _SHUTDOWN.wait(0.2)


def shutdown() -> None:
    _SHUTDOWN.set()
    if _DISPATCHER:
        _DISPATCHER.join(timeout=2)
    # Daemon workers retain their durable states if a remote server does not respond.
    deadline = time.monotonic() + 5
    for thread in list(_THREADS.values()):
        thread.join(timeout=max(0, deadline - time.monotonic()))


def _run(project_id: str) -> None:
    with _LOCK:
        state = _STATE.get(project_id) or _load(project_id)
        if not state:
            return
        _STATE[project_id] = state
    last_progress_at = time.time()
    last_bytes = int(state.get("downloaded_bytes") or 0)

    def progress_hook(payload: dict[str, Any]) -> None:
        nonlocal last_progress_at, last_bytes
        if _SHUTDOWN.is_set():
            raise DownloadInterrupted
        if state.get("_cancel_requested"):
            raise DownloadCancelled
        if state.get("_pause_requested"):
            raise DownloadPaused
        if payload.get("status") == "downloading":
            downloaded = int(payload.get("downloaded_bytes") or 0)
            total = payload.get("total_bytes") or payload.get(
                "total_bytes_estimate"
            )
            state["status"] = "downloading"
            state["downloaded_bytes"] = downloaded
            state["total_bytes"] = _safe_int(total)
            state["speed_bps"] = _safe_int(payload.get("speed"))
            state["eta_sec"] = _safe_int(payload.get("eta"))
            state["filename"] = payload.get("filename")
            state["percent"] = (
                min(99.9, downloaded / total * 100)
                if total and total > 0
                else None
            )
            if downloaded > last_bytes:
                last_bytes = downloaded
                last_progress_at = time.time()
            _save_throttled(project_id, state)
        elif payload.get("status") == "finished":
            state["status"] = "finalizing"
            state["filename"] = payload.get("filename")
            _save(project_id, state)

    options = {
        "outtmpl": state["out_template"],
        "format": _format_selector(
            str(state.get("quality") or "auto"),
            str(state.get("provider") or ""),
        ),
        "merge_output_format": "mp4",
        "format_sort": ["res", "vcodec:h264", "acodec:aac"],
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "continuedl": True,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "writeinfojson": False,
        "writedescription": False,
        "writethumbnail": False,
    }
    if state.get("passcode"):
        options["videopassword"] = str(state["passcode"])
    while True:
        if _SHUTDOWN.is_set():
            state["status"] = "resuming"
            _save(project_id, state)
            return
        if state.get("_cancel_requested"):
            _cleanup_partial(state)
            _finish(project_id, state, "cancelled")
            return
        if state.get("_pause_requested"):
            _finish(project_id, state, "paused", resumable=True)
            return
        try:
            zoom_cookies = _zoom_cookies(str(state.get("url") or ""), state.get("passcode"))
            if zoom_cookies:
                options["cookiefile"] = str(zoom_cookies)
            state["status"] = (
                "resuming" if state.get("downloaded_bytes") else "extracting"
            )
            _save(project_id, state)
            info = _extract(
                normalize_url(str(state["url"])),
                options,
                str(state.get("provider") or ""),
                download=True,
            )
            if state.get("_cancel_requested"):
                raise DownloadCancelled
            if state.get("_pause_requested"):
                raise DownloadPaused
            final_path = _resolve_final_path(state, info)
            if not final_path:
                raise RuntimeError("Файл не найден после загрузки")
            if _audio_only_requested(state):
                final_path = _drop_video_track(final_path)
                media_info = media.probe_media(final_path)
            else:
                final_path, media_info = media.prepare_video(final_path)
            state["audio_verified"] = True
            state["audio_codec"] = media_info["audio_streams"][0]["codec"]
            state["video_height"] = (media_info.get("video_streams") or [{}])[0].get("height")
            if state.get("_cancel_requested"):
                raise DownloadCancelled
            if state.get("_pause_requested"):
                raise DownloadPaused
            size = final_path.stat().st_size
            state.update(
                {
                    "status": "finalizing",
                    "filename": str(final_path),
                    "downloaded_bytes": size,
                    "total_bytes": size,
                    "percent": 100.0,
                    "speed_bps": None,
                    "eta_sec": None,
                }
            )
            _save(project_id, state)
            transcription_job_id = _COMPLETION_HANDLER(
                project_id,
                final_path,
                _snapshot(state),
            ) if _COMPLETION_HANDLER else None
            state["transcription_job_id"] = transcription_job_id or None
            _finish(project_id, state, "completed")
            return
        except DownloadInterrupted:
            state["status"] = "resuming"
            _save(project_id, state)
            return
        except DownloadPaused:
            _finish(project_id, state, "paused", resumable=True)
            return
        except DownloadCancelled:
            _cleanup_partial(state)
            _finish(project_id, state, "cancelled")
            return
        except Exception as exc:
            if state.get("_cancel_requested"):
                _cleanup_partial(state)
                _finish(project_id, state, "cancelled")
                return
            if state.get("_pause_requested"):
                _finish(project_id, state, "paused", resumable=True)
                return
            human_error = humanize_error(exc)
            if _classify_error(exc) == "permanent":
                _finish(
                    project_id,
                    state,
                    "error",
                    error=human_error,
                    resumable=True,
                )
                return
            attempts = int(state.get("attempts") or 0) + 1
            state.update(
                {
                    "status": "reconnecting",
                    "attempts": attempts,
                    "error": None,
                    "speed_bps": None,
                    "eta_sec": None,
                }
            )
            _save(project_id, state)
            if (
                not state.get("downloaded_bytes")
                and attempts >= _MAX_ZERO_PROGRESS_ATTEMPTS
            ):
                _finish(
                    project_id,
                    state,
                    "error",
                    error=(
                        f"Не удалось начать загрузку после {attempts} попыток: "
                        f"{human_error}"
                    ),
                    resumable=True,
                )
                return
            if time.time() - last_progress_at > _NO_PROGRESS_LIMIT_SECONDS:
                _finish(
                    project_id,
                    state,
                    "error",
                    error="Соединение слишком долго не передаёт данные.",
                    resumable=True,
                )
                return
            delay = min(
                _RECONNECT_MAX_SECONDS,
                _RECONNECT_BASE_SECONDS
                * (2 ** min(max(attempts - 1, 0), 6)),
            )
            deadline = time.time() + delay
            while time.time() < deadline:
                if (
                    state.get("_cancel_requested")
                    or state.get("_pause_requested")
                    or _SHUTDOWN.is_set()
                ):
                    break
                time.sleep(0.25)


def _audio_only_requested(state: dict[str, Any]) -> bool:
    """Просили ли только звук.

    «Авто» тоже может им оказаться: под ним стоит настройка пользователя, и по
    умолчанию она как раз «Только звук».
    """
    quality = str(state.get("quality") or "auto")
    if quality == "audio":
        return True
    return quality == "auto" and preferred_quality() == "audio"


def _drop_video_track(source: Path) -> Path:
    """Убрать видеоряд, если он есть, оставив звук нетронутым.

    Отдельной аудиодорожки у источника может не оказаться вовсе. У VK так
    ведёт себя полный HLS, а у YouTube — клиент, ссылки которого сервер
    сегодня соглашается отдавать: он предлагает один совмещённый формат 360p.
    Скачать его приходится целиком, но держать на диске видео, когда просили
    звук, незачем — это вдвое больше места и не то, что обещает настройка.
    """
    try:
        info = media.probe_media(source)
    except RuntimeError as exc:
        raise RuntimeError(f"Не удалось извлечь только звук: {exc}") from exc
    if not info.get("video_stream_count"):
        return source
    codec = str(info["audio_streams"][0].get("codec") or "").lower()
    return _keep_audio_only(source, _AUDIO_CONTAINERS.get(codec, ".m4a"))


def _keep_audio_only(source: Path, suffix: str = ".m4a") -> Path:
    """Удалить видеоряд из совмещённого файла без перекодирования дорожки."""
    target = source.with_suffix(suffix)
    if target == source:
        return source
    partial = target.with_name(f".{target.stem}.partial{target.suffix}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg не найден для извлечения аудиодорожки")
    partial.unlink(missing_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "copy",
            str(partial),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not partial.is_file():
        partial.unlink(missing_ok=True)
        detail = result.stderr.strip() or "Не удалось оставить только звук"
        raise RuntimeError(detail)
    os.replace(partial, target)
    source.unlink()
    return target


def _resolve_final_path(
    state: dict[str, Any],
    info: Any,
) -> Path | None:
    # Post-merge filepath is authoritative. Track fragments can be larger than
    # a remuxed result and must never be advertised as the completed video.
    if isinstance(info, dict) and info.get("filepath"):
        merged = Path(info["filepath"])
        if merged.is_file():
            return merged
    template = Path(state["out_template"])
    prefix = template.name.replace(".%(ext)s", "")
    for suffix in (".mp4", ".mkv", ".webm", ".m4a", ".opus", ".mp3", ".ogg"):
        merged = template.parent / (prefix + suffix)
        if merged.is_file():
            return merged
    candidates: list[Path] = []
    if isinstance(info, dict):
        for item in info.get("requested_downloads") or []:
            if isinstance(item, dict) and item.get("filepath"):
                candidates.append(Path(item["filepath"]))
        if info.get("_filename"):
            candidates.append(Path(info["_filename"]))
    if state.get("filename"):
        candidates.append(Path(state["filename"]))
    template = Path(state["out_template"])
    prefix = template.name.replace(".%(ext)s", "")
    candidates.extend(
        path
        for path in _siblings_named(template.parent, prefix)
        if not path.name.endswith((".part", ".ytdl", ".json"))
    )
    existing = [path for path in candidates if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_size) if existing else None


def _finish(
    project_id: str,
    state: dict[str, Any],
    status: str,
    *,
    error: str | None = None,
    resumable: bool = False,
) -> None:
    state.update(
        {
            "status": status,
            "finished_at": time.time(),
            "speed_bps": None,
            "eta_sec": None,
            "error": error,
            "resumable": resumable,
            "_pause_requested": False,
            "_cancel_requested": status == "cancelled",
        }
    )
    _save(project_id, state)


def _save_throttled(
    project_id: str,
    state: dict[str, Any],
    interval_seconds: float = 2.0,
) -> None:
    now = time.time()
    if now - _LAST_SAVE.get(project_id, 0) >= interval_seconds:
        _LAST_SAVE[project_id] = now
        _save(project_id, state)


def _cleanup_partial(state: dict[str, Any]) -> None:
    template = Path(str(state.get("out_template") or ""))
    directory = template.parent
    prefix = template.name.replace(".%(ext)s", "")
    if not directory.is_dir() or not prefix:
        return
    for path in directory.glob(f"{prefix}*"):
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, (media.MediaValidationError, ProxyAccessError)):
        return "permanent"
    text = str(exc).lower()
    transient = (
        "timed out",
        "timeout",
        "connection",
        "network",
        "temporarily",
        "temporary failure",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "http error 5",
        "http error 429",
        "too many requests",
        "broken pipe",
        "incomplete read",
        "service unavailable",
        "503",
        "502",
        "504",
    )
    if any(marker in text for marker in transient):
        return "transient"
    permanent = (
        "private",
        "unavailable",
        "unsupported url",
        "not a valid url",
        "http error 401",
        "http error 403",
        "http error 404",
        "http error 410",
        "sign in",
        "log in to",
        "members-only",
        "requested format is not available",
        "no video formats",
        "copyright",
        "claimed content",
        "blocked in your country",
        "has been removed",
        "does not exist",
        "geo restrict",
    )
    return "permanent" if any(marker in text for marker in permanent) else "transient"


def humanize_error(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, ProxyAccessError):
        return text
    if any(marker in lowered for marker in ("claimed content", "copyright", "blocked in your country", "not available in your country", "geo restrict")):
        owner = re.search(r"claimed content by\s+(.+?)(?:\.\s|\.$|$)", text, re.I)
        reason = f" по требованию правообладателя {owner[1]}" if owner else " из-за прав на контент или региона подключения"
        return ("YouTube заблокировал доступ к видео" + reason + ". Если видео открывается через VPN, включите VPN для всего компьютера или укажите свой прокси в настройках загрузчика.")
    if "protected by a passcode" in lowered:
        return "Запись закрыта кодом доступа — введите его и повторите проверку"
    if "wrong passcode" in lowered or "invalid passcode" in lowered:
        return "Код доступа не подошёл"
    if "[zoom]" in lowered and "unable to extract data" in lowered:
        # Zoom в 2026 году убрал старый эндпоинт проверки пароля
        # (validate_meet_passwd отвечает «This API has been deprecated») и перешёл
        # на страницу-компонент. yt-dlp за этим ещё не поспел: открытая issue
        # yt-dlp#16377, воспроизводится и на самой свежей версии. Сообщение
        # должно объяснять это, а не сваливать на пользователя трассу.
        return (
            "Zoom изменил проверку кода доступа, и загрузчик пока не умеет "
            "её проходить. Скачайте запись из Zoom вручную и добавьте файл в "
            "проект — распознавание сработает как обычно"
        )
    if youtube_access.is_age_check(lowered):
        return youtube_access.age_check_message()
    if youtube_access.is_bot_check(lowered):
        # Сюда попадаем, только когда не прошёл ни один обходной путь из
        # _extract. Показывать при этом исходный текст yt-dlp бессмысленно:
        # он на английском и советует ключи командной строки, которых у
        # пользователя нет.
        return youtube_access.bot_check_message()
    if "private" in lowered or "unavailable" in lowered:
        if "private" in lowered:
            return "Видео приватное: нужен аккаунт, которому владелец разрешил просмотр."
        if "has been removed" in lowered or "does not exist" in lowered:
            return "YouTube сообщает, что видео удалено или не существует."
        return "YouTube не отдал видео и не сообщил точную причину. Если оно открывается через VPN, включите VPN для всего компьютера или укажите свой прокси в настройках загрузчика."
    if "not a valid url" in lowered:
        return "Неверная ссылка"
    if "unsupported url" in lowered:
        return "Этот источник не поддерживается"
    if (
        "requested format is not available" in lowered
        or "no video formats found" in lowered
    ):
        # У YouTube это чаще всего не «нет такого качества», а спрятанные
        # форматы: сервис требует за них одноразовый токен. Все обходные пути
        # к этому моменту уже перебраны в _extract.
        return (
            "Источник не отдал ни одного пригодного качества. Попробуйте "
            "позже или выберите другое качество"
        )
    if "http error 4" in lowered:
        return "Источник не разрешил доступ к файлу"
    if any(
        marker in lowered
        for marker in (
            "timed out",
            "timeout",
            "connection",
            "network",
            "ssl",
            "bad record mac",
        )
    ):
        return "Проблема с сетью или соединением"
    clean = text.split(";")[0].strip()
    if clean.startswith("ERROR:"):
        clean = clean[6:].strip()
    return clean or "Ошибка загрузки видео"
