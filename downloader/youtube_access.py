"""Обход бот-проверки YouTube.

YouTube отвечает «Sign in to confirm you're not a bot» не на видео, а на
запрос: адрес попал под подозрение, и страница требует вход. Тот же ролик
обычно спокойно отдаётся, если спросить его от имени другого клиента API —
у мобильных и телевизионных приложений своя ручка, и на ней проверки нет.
Поэтому вместо одной попытки здесь перебирается несколько способов, и первый
сработавший запоминается для следующих ссылок.

Второй источник тех же ошибок — одноразовый токен (PO token), которого у
загрузчика нет. Форматы, требующие его, yt-dlp по умолчанию выбрасывает из
списка, и вместо понятной причины пользователь видит «Requested format is not
available» на живом видео. В запасных попытках этот фильтр снимается.

Третий случай — 403 уже на самом файле: ссылка извлеклась, а сервер её не
отдал. Это тоже свойство клиента, а не видео. Поэтому наборы клиентов
перебираются по одному: в общем списке yt-dlp всё равно выбрал бы формат
сломанного клиента, и каждая попытка упиралась бы в ту же ошибку.

Когда не помогает ничего, остаются cookies: файл, положенный пользователем в
`data/cookies`, или сессия установленного браузера. Автоматически из браузера
они не берутся — это привязывает загрузки к личному аккаунту Google, и такое
решение принимает владелец аккаунта, а не сервис.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

from . import storage


logger = logging.getLogger(__name__)

COOKIES_DIR = storage.COOKIES_DIR
# Первое найденное имя и используется. `cookies.txt` — то, как файл называет
# любое расширение-экспортёр по умолчанию, поэтому переименовывать не нужно.
COOKIE_FILE_NAMES = ("youtube.txt", "youtube_cookies.txt", "cookies.txt")
INSTRUCTIONS_NAME = "Как добавить cookies"

# Наборы клиентов, которые перебираются, когда стандартные не прошли. Каждый
# набор — отдельная попытка, а не один общий список, и это принципиально:
# клиент может успешно отдать ссылку на файл, а сервер потом ответит на неё
# 403. Так сегодня ведёт себя android_vr, который стоит у yt-dlp по умолчанию.
# В объединённом списке он всё равно побеждал бы при выборе формата, и перебор
# каждый раз упирался бы в ту же ошибку.
FALLBACK_CLIENT_SETS = (
    ("android",),
    ("ios",),
    ("tv_simply", "tv"),
    ("mweb", "web_safari", "web"),
    # Широкая сеть напоследок: если поодиночке форматов не нашлось,
    # объединённый список хотя бы соберёт их со всех клиентов сразу.
    ("android", "ios", "tv_simply", "mweb", "web_safari", "web"),
)
# Браузеры в порядке проверки. Первым Firefox: на Windows у него cookies лежат
# в обычной базе, а Chrome, Edge и Opera с 2024 года шифруют их привязкой к
# приложению, и прочитать их со стороны уже нельзя.
BROWSERS = (
    "firefox",
    "chrome",
    "edge",
    "opera",
    "brave",
    "vivaldi",
    "chromium",
)

# Ошибки, после которых имеет смысл взять следующий способ. Здесь только те,
# что говорят о запросе, а не о видео: «удалено», «приватное» и «недоступно в
# вашей стране» перебором не лечатся, и повторять их — впустую тратить время
# пользователя.
RETRYABLE_MARKERS = (
    "confirm you're not a bot",
    "confirm you are not a bot",
    "sign in to confirm",
    "please sign in",
    "requested format is not available",
    "no video formats found",
    "the page needs to be reloaded",
    "failed to extract any player response",
    "unable to extract yt initial data",
    "unable to extract player response",
    # 403 на самом файле: ссылка извлеклась, но сервер её не отдаёт. Это
    # свойство клиента, а не видео — у другого клиента ссылка своя, и она
    # обычно работает. Без этой строки загрузка вставала на первом же таком
    # ответе, хотя видео было доступно.
    "unable to download video data",
    "http error 403",
    # Сюда же — неудача при чтении cookies из браузера. Она приходит от
    # yt-dlp такой же ошибкой загрузки, а означает лишь, что этот способ
    # неприменим: на Windows у Chrome, Edge и Opera база зашифрована.
    "cookies database",
    "cookie database",
    "failed to decrypt",
)
BOT_CHECK_MARKERS = (
    "confirm you're not a bot",
    "confirm you are not a bot",
)
# Возрастное ограничение приходит такой же просьбой войти, но причина другая,
# и объяснять её надо своими словами: «подтвердите, что вы не робот» на видео
# 18+ только сбивает с толку.
AGE_CHECK_MARKERS = (
    "confirm your age",
    "age-restricted",
    "age restricted",
    "inappropriate for some users",
)

# Сколько удачный способ считается актуальным. Блокировка снимается сама, и
# держать обходной путь вечно не нужно: после паузы первой снова идёт обычная
# попытка, самая быстрая и с полным набором качеств.
_MEMO_TTL_SECONDS = 30 * 60
_LOCK = threading.RLock()
# Память раздельная для проверки ссылки и для загрузки, и это не мелочь:
# клиент по умолчанию отлично отдаёт данные о видео и при этом отвечает 403 на
# сам файл. С общей памятью удачная проверка каждый раз возвращала бы загрузку
# к заведомо неработающему способу.
_MEMO: dict[str, tuple[float, str]] = {}
_BROWSER_MEMO: tuple[float, str | None] | None = None
_BROWSER_MEMO_TTL_SECONDS = 10 * 60


class Strategy(NamedTuple):
    """Способ достучаться до источника: имя для журнала и добавка к настройкам."""

    name: str
    options: dict[str, Any]


# Для всех источников, кроме YouTube, способ ровно один — обычный запрос.
DIRECT = Strategy("default", {})


class QuietLogger:
    """Заглушка вместо журнала yt-dlp при чтении cookies из браузера.

    Своего журнала у `extract_cookies_from_browser` нет, а стандартный пишет
    прямо в консоль сервиса: «could not find brave cookies database» на каждый
    непоставленный браузер выглядит как поломка, хотя это обычный перебор.
    """

    def debug(self, *_args: object, **_kwargs: object) -> None: ...

    def info(self, *_args: object, **_kwargs: object) -> None: ...

    def warning(self, *_args: object, **_kwargs: object) -> None: ...

    def error(self, *_args: object, **_kwargs: object) -> None: ...


def cookie_file() -> Path | None:
    """Файл cookies, положенный пользователем, если он есть."""
    for name in COOKIE_FILE_NAMES:
        candidate = COOKIES_DIR / name
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def ensure_cookies_folder() -> Path:
    """Создать папку для cookies и положить туда инструкцию.

    Инструкция пишется рядом с файлом, а не только в тексте ошибки: между
    «увидел ошибку» и «дошёл до папки» пользователь успевает забыть половину
    шагов, а здесь они под рукой.
    """
    try:
        COOKIES_DIR.mkdir(parents=True, exist_ok=True)
        hint = COOKIES_DIR / f"{INSTRUCTIONS_NAME}.txt"
        if not hint.exists():
            hint.write_text(INSTRUCTIONS, encoding="utf-8")
    except OSError as exc:
        logger.warning("Не удалось подготовить папку cookies: %s", exc)
    return COOKIES_DIR


def browser_cookie_source() -> str | None:
    """Браузер, из которого разрешено и получается взять cookies YouTube.

    Настройка «off» — не брать вовсе (так по умолчанию). Имя браузера — брать
    из него. «auto» — перебрать установленные и выбрать первый, в котором
    действительно есть cookies YouTube: браузер без входа в аккаунт бот-проверку
    не снимет, а времени на попытку уйдёт столько же.
    """
    global _BROWSER_MEMO
    try:
        choice = storage.get_settings().get("youtube_cookies", "off")
    except Exception:
        return None
    if choice == "off":
        return None
    if choice != "auto":
        return choice
    now = time.monotonic()
    with _LOCK:
        memo = _BROWSER_MEMO
        if memo and now - memo[0] <= _BROWSER_MEMO_TTL_SECONDS:
            return memo[1]
    found = next(
        (name for name in BROWSERS if _youtube_cookie_count(name) > 0), None
    )
    with _LOCK:
        _BROWSER_MEMO = (time.monotonic(), found)
    return found


def _youtube_cookie_count(browser: str) -> int:
    try:
        from yt_dlp.cookies import extract_cookies_from_browser

        jar = extract_cookies_from_browser(browser, logger=QuietLogger())
    except Exception as exc:
        logger.debug("Cookies из %s недоступны: %s", browser, exc)
        return 0
    return sum(1 for cookie in jar if "youtube.com" in (cookie.domain or ""))


def plans(provider: str | None, *, download: bool = False) -> Iterator[Strategy]:
    """Способы обращения к источнику в порядке перебора.

    Генератор, а не список: cookies браузера обходятся дороже остального —
    базу нужно открыть и проверить, есть ли в ней вход в YouTube. Пока хватает
    обычных попыток, делать это незачем, поэтому последний способ вычисляется,
    только когда до него дошла очередь. У всех источников, кроме YouTube,
    способ ровно один — обычный запрос.
    """
    if provider != "youtube":
        yield DIRECT
        return
    preferred = _preferred_name(download)
    served: set[str] = set()
    # Если в прошлый раз выручили cookies браузера, начинаем с них: иначе
    # запомненный способ каждый раз стоял бы в конце очереди и три попытки
    # перед ним уходили бы впустую.
    if preferred and preferred.startswith("browser_cookies:"):
        served.add(preferred)
        yield Strategy(preferred, _browser_options(preferred.split(":", 1)[1]))
    for plan in _cheap_plans(preferred):
        if plan.name not in served:
            served.add(plan.name)
            yield plan
    browser = browser_cookie_source()
    if browser and f"browser_cookies:{browser}" not in served:
        yield Strategy(
            f"browser_cookies:{browser}", _browser_options(browser)
        )


def client_plan_name(clients: tuple[str, ...]) -> str:
    return "clients:" + ",".join(clients)


def _client_options(clients: tuple[str, ...]) -> dict[str, Any]:
    return {
        "extractor_args": {
            "youtube": {
                "player_client": list(clients),
                # Форматы без одноразового токена yt-dlp прячет, и на живом
                # видео остаётся пустой список. Токена у нас нет, но ссылки
                # обычно рабочие — просим оставить их на месте.
                "formats": ["missing_pot"],
            }
        }
    }


def _browser_options(browser: str) -> dict[str, Any]:
    return {"cookiesfrombrowser": (browser, None, None, None)}


def _cheap_plans(preferred: str | None) -> list[Strategy]:
    """Способы, которые ничего не стоят до самого запроса."""
    found: list[Strategy] = []
    cookies = cookie_file()
    if cookies:
        # Свой файл идёт первым: раз пользователь его положил, значит обычные
        # попытки уже не проходили, и начинать с них — терять время на каждой
        # ссылке.
        found.append(Strategy("cookies_file", {"cookiefile": str(cookies)}))
    found.append(DIRECT)
    found.extend(
        Strategy(client_plan_name(clients), _client_options(clients))
        for clients in FALLBACK_CLIENT_SETS
    )
    known = next(
        (plan for plan in found if plan.name == preferred), None
    )
    if not known:
        return found
    return [known] + [plan for plan in found if plan.name != preferred]


def _memo_key(download: bool) -> str:
    return "download" if download else "probe"


def _preferred_name(download: bool) -> str | None:
    with _LOCK:
        memo = _MEMO.get(_memo_key(download))
    if not memo or time.monotonic() - memo[0] > _MEMO_TTL_SECONDS:
        return None
    return memo[1]


def remember(name: str, *, download: bool = False) -> None:
    """Запомнить сработавший способ, чтобы не перебирать заново каждый раз."""
    key = _memo_key(download)
    with _LOCK:
        if name == DIRECT.name:
            _MEMO.pop(key, None)
        else:
            _MEMO[key] = (time.monotonic(), name)


def forget() -> None:
    global _BROWSER_MEMO
    with _LOCK:
        _MEMO.clear()
        _BROWSER_MEMO = None


def worth_retrying(exc: BaseException) -> bool:
    """Стоит ли пробовать следующий способ после этой ошибки."""
    text = str(exc).lower().replace("’", "'")
    if any(marker in text for marker in ("private video", "has been removed", "does not exist", "copyright", "claimed content", "blocked in your country")):
        return False
    if "video unavailable" in text:
        # Some clients hide the actual restriction; another may return formats
        # or a precise copyright/region reason. A vague refusal is inconclusive.
        return True
    return any(marker in text for marker in RETRYABLE_MARKERS)


def is_bot_check(text: str) -> bool:
    lowered = text.lower().replace("’", "'")
    return any(marker in lowered for marker in BOT_CHECK_MARKERS)


def is_age_check(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in AGE_CHECK_MARKERS)


def merge(base: dict[str, Any], plan: Strategy) -> dict[str, Any]:
    """Настройки yt-dlp с добавкой выбранного способа.

    `extractor_args` сливается по ключам, а не заменяется целиком: у Zoom и
    других источников там могут лежать свои значения, и затирать их нельзя.
    """
    options = dict(base)
    for key, value in plan.options.items():
        if key == "extractor_args" and isinstance(base.get(key), dict):
            merged = dict(base[key])
            merged.update(value)
            options[key] = merged
        else:
            options[key] = value
    return options


def _cookies_advice() -> str:
    """Что делать с cookies. Папка готовится здесь же, вместе с инструкцией."""
    folder = ensure_cookies_folder()
    return (
        "Сохраните cookies YouTube в файл cookies.txt и положите его в папку "
        f"«{folder}» (инструкция лежит там же) либо включите «Cookies YouTube "
        "из браузера» в настройках."
    )


def bot_check_message() -> str:
    """Что показать, когда не прошёл ни один способ."""
    return (
        "YouTube потребовал подтвердить, что запрос не от робота, и не отдал "
        "видео доступными способами. Обновите загрузчик через «Обновить "
        "загрузчик.bat» и попробуйте позже. Для доступа через аккаунт: "
        + _cookies_advice()
    )


def age_check_message() -> str:
    """Возрастное ограничение снимается только входом в аккаунт."""
    return (
        "Видео с возрастным ограничением: YouTube отдаёт его только тем, кто "
        "вошёл в аккаунт. " + _cookies_advice()
    )


INSTRUCTIONS = f"""Как добавить cookies YouTube

Эта папка нужна, только если YouTube просит подтвердить, что вы не робот.
Обычно загрузка обходится без неё.

1. Установите в браузер расширение для экспорта cookies — например
   «Get cookies.txt LOCALLY» (Chrome, Opera, Edge) или «cookies.txt»
   (Firefox). Подойдёт любое, которое сохраняет формат Netscape.
2. Откройте youtube.com и войдите в аккаунт.
3. Экспортируйте cookies этой вкладки в файл.
4. Положите файл сюда под именем cookies.txt (подойдёт и youtube.txt).

Файл начинается со строки «# Netscape HTTP Cookie File». Если внутри JSON,
экспорт сделан в другом формате — выберите в расширении Netscape.

Что важно знать:

* Файл — это вход в ваш аккаунт Google. Он лежит только на этом компьютере и
  никуда не отправляется, кроме самого YouTube, но и передавать его никому
  не стоит.
* Cookies живут несколько недель. Когда перестанут работать, экспортируйте
  заново.
* Лучше заводить для этого отдельный аккаунт: YouTube не любит выгрузки и
  может ограничить тот, чьи cookies используются.
* Вместо файла можно включить «Cookies YouTube из браузера» в настройках
  сервиса — тогда они берутся из установленного браузера. На Windows это
  работает только с Firefox: Chrome, Edge и Opera с 2024 года шифруют свою
  базу так, что прочитать её со стороны нельзя.

Имена файлов, которые сервис ищет здесь: {", ".join(COOKIE_FILE_NAMES)}.
"""
