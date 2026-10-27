"""Проход кода доступа к записи Zoom.

Zoom в 2026 году сменил механику. Раньше на странице записи была HTML-форма
`password_form`, и yt-dlp отправлял код на `/rec/validate_meet_passwd`. Сейчас
эта ручка отвечает `{"status":false,"errorMessage":"This API has been
deprecated"}`, а страница редиректит на `/rec/component-page` с
`componentName=need-password`. yt-dlp попадает на React-страницу без данных и
падает с «Unable to extract data» (открытая issue yt-dlp#16377, воспроизводится
и на 2026.07.04).

Новый адрес проверки найден в лениво подгружаемом чанке страницы
(`fe-recording-base/js/chunk-54fe3682.js`):

    path = "/nws/recording/1.0" + ("/guest" if nwsDomain else "")
           + ("/validate-meeting-passwd" if useWhichPasswd == "meeting"
              else "/validate-passwd")
    body = {id, passwd, action, recaptcha}   — форма, не JSON

Здесь этот шаг проходится самостоятельно, а полученные cookies отдаются yt-dlp
файлом: дальше он извлекает запись обычным путём, потому что страница уже
открыта. Когда yt-dlp починит свой экстрактор, этот модуль можно будет убрать —
всё остальное останется на месте.
"""
from __future__ import annotations

import json
import re
import tempfile
import time
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from urllib.parse import urlparse

import requests


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_TIMEOUT = 30
_PAGE_DATA = re.compile(r"window\.__data__\s*=\s*(\{.*?\})\s*;", re.S)


class ZoomAccessError(RuntimeError):
    """Не удалось открыть запись: неверный код или изменившийся Zoom."""


def _base_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _page_data(html: str) -> dict:
    """Разобрать window.__data__ — это JS-объект, а не JSON."""
    found = _PAGE_DATA.search(html)
    if not found:
        return {}
    raw = found.group(1)
    # Ключи без кавычек и строки в одинарных — приводим к JSON.
    raw = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', raw)
    raw = raw.replace("'", '"')
    raw = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _cookie_file(session: requests.Session, host: str) -> Path:
    """Сложить cookies сессии в файл формата Netscape для yt-dlp."""
    jar = MozillaCookieJar()
    for item in session.cookies:
        jar.set_cookie(
            Cookie(
                version=0,
                name=item.name,
                value=item.value or "",
                port=None,
                port_specified=False,
                domain=item.domain or host,
                domain_specified=True,
                domain_initial_dot=str(item.domain or "").startswith("."),
                path=item.path or "/",
                path_specified=True,
                secure=bool(item.secure),
                # yt-dlp не любит сессионные cookies без срока: даём час, за это
                # время загрузка успевает начаться и получить свои ссылки.
                expires=int(item.expires or time.time() + 3600),
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    handle = tempfile.NamedTemporaryFile(
        prefix="zoom-cookies-", suffix=".txt", delete=False
    )
    handle.close()
    path = Path(handle.name)
    jar.save(str(path), ignore_discard=True, ignore_expires=True)
    return path


def authorize(url: str, passcode: str | None) -> Path | None:
    """Открыть запись кодом доступа и вернуть файл cookies для yt-dlp.

    None означает «код не понадобился»: запись открыта, и обычного пути
    достаточно. Исключение — код нужен, но не подошёл или его не дали.
    """
    base = _base_url(url)
    host = urlparse(url).hostname or ""
    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA, "Referer": base})

    try:
        page = session.get(url, timeout=_TIMEOUT)
        page.raise_for_status()
        data = _page_data(page.text)
        meeting_id = data.get("meetingId")
        if not meeting_id:
            return None

        info = session.get(
            f"{base}nws/recording/1.0/play/share-info/{meeting_id}",
            timeout=_TIMEOUT,
        ).json()
        result = (info or {}).get("result") or {}
        if result.get("componentName") != "need-password":
            # Код не нужен — пусть yt-dlp работает как обычно.
            return None

        if not passcode:
            raise ZoomAccessError(
                "Запись закрыта кодом доступа — введите его и повторите проверку"
            )

        which = result.get("useWhichPasswd") or "meeting"
        guest = "/guest" if result.get("nwsDomain") else ""
        endpoint = (
            f"{base}nws/recording/1.0{guest}/validate-meeting-passwd"
            if which == "meeting"
            else f"{base}nws/recording/1.0{guest}/validate-passwd"
        )
        target_id = (
            result.get("meetingId") if which == "meeting" else result.get("fileId")
        )
        checked = session.post(
            endpoint,
            # Именно форма: на JSON Zoom отвечает «Missing Request Parameters».
            data={
                "id": target_id or meeting_id,
                "passwd": passcode.strip(),
                "action": result.get("action") or "",
                "recaptcha": "",
            },
            timeout=_TIMEOUT,
        ).json()
        if not (checked or {}).get("status"):
            message = str((checked or {}).get("errorMessage") or "").strip()
            raise ZoomAccessError(
                f"Код доступа не подошёл{': ' + message if message else ''}"
            )
    except ZoomAccessError:
        raise
    except requests.RequestException as exc:
        raise ZoomAccessError(f"Zoom не ответил: {exc}") from exc
    except (ValueError, KeyError) as exc:
        raise ZoomAccessError(
            "Zoom изменил страницу записи, проход кода доступа не удался"
        ) from exc

    return _cookie_file(session, host)
