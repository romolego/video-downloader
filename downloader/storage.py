from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("VIDEO_DOWNLOADER_DATA", str(ROOT / "data"))).resolve()
COOKIES_DIR = DATA_DIR / "cookies"
DB_PATH = DATA_DIR / "library.sqlite3"
LOCK = threading.RLock()
DEFAULT_SETTINGS = {
    "download_quality": "1080", "download_mode": "video", "max_parallel": "2",
    "youtube_cookies": "off", "download_root": str(ROOT / "downloads"),
    "project_name_format": "date_title", "project_view": "folders", "theme": "light",
    "youtube_connection": "direct", "youtube_proxy": "",
}
SETTING_VALUES = {
    "download_quality": {"audio", "360", "480", "720", "1080", "1440", "2160", "max"},
    "download_mode": {"audio", "video"}, "max_parallel": {"1", "2", "3", "4"},
    "youtube_cookies": {"off", "auto", "firefox", "chrome", "edge", "opera", "brave", "vivaldi", "chromium"},
    "project_name_format": {"date_title", "date_compact", "date_dotted", "date_time_title", "title_date", "title_only"}, "project_view": {"folders", "flat"},
    "theme": {"light", "dark"},
    "youtube_connection": {"direct", "proxy"},
}


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS folders (id TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, folder_id TEXT REFERENCES folders(id) ON DELETE SET NULL,
            path TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS downloads (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL, created_at REAL NOT NULL);
        """)
        # Retired connection modes must not keep existing installations on a broken route.
        db.execute("UPDATE settings SET value='direct' WHERE key='youtube_connection' AND value NOT IN ('direct', 'proxy')")
        db.execute("DELETE FROM settings WHERE key NOT IN (%s)" % ','.join('?' for _ in DEFAULT_SETTINGS), tuple(DEFAULT_SETTINGS))


def get_settings():
    result = dict(DEFAULT_SETTINGS)
    with connect() as db:
        result.update({r["key"]: r["value"] for r in db.execute("SELECT * FROM settings")})
    return result


def update_settings(values):
    for key, value in values.items():
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"Неизвестная настройка: {key}")
        if not isinstance(value, str):
            raise ValueError("Значения настроек должны быть строками")
        if key in SETTING_VALUES and value not in SETTING_VALUES[key]:
            raise ValueError(f"Недопустимое значение настройки {key}")
        if key == "youtube_proxy":
            value = value.strip()
            if value:
                try:
                    parsed = urlsplit(value)
                    valid = parsed.scheme in {"http", "https", "socks4", "socks4a", "socks5", "socks5h"} and parsed.hostname and parsed.port and not parsed.path.strip("/") and not parsed.query and not parsed.fragment and not any(c.isspace() for c in value)
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError("Укажите адрес прокси: http://127.0.0.1:7890 или socks5://127.0.0.1:1080")
            values[key] = value
        if key == "download_root":
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError("Укажите абсолютный путь папки загрузок")
            path.mkdir(parents=True, exist_ok=True)
            # Check write access before accepting a path; existing projects retain their paths.
            check = path / f".write-check-{uuid.uuid4().hex}"
            check.write_bytes(b"")
            check.unlink()
            values[key] = str(path.resolve())
    with LOCK, connect() as db:
        db.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?)", values.items())
    return get_settings()


def rows(query, args=()):
    with connect() as db:
        return [dict(row) for row in db.execute(query, args)]


def get_project(project_id):
    found = rows("SELECT * FROM projects WHERE id=?", (project_id,))
    if not found:
        raise KeyError("Проект не найден")
    return found[0]


def list_projects():
    return rows("SELECT * FROM projects ORDER BY created_at DESC")


def list_folders():
    return rows("SELECT * FROM folders ORDER BY name COLLATE NOCASE")


def clean_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")[:100]
    if not name:
        raise ValueError("Введите название")
    if name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        name = "_" + name
    return name


def create_folder(name):
    folder = {"id": uuid.uuid4().hex, "name": clean_name(name)}
    with LOCK, connect() as db:
        db.execute("INSERT INTO folders VALUES (:id, :name)", folder)
    return folder


def update_folder(folder_id, name):
    with LOCK, connect() as db:
        if not db.execute("UPDATE folders SET name=? WHERE id=?", (clean_name(name), folder_id)).rowcount:
            raise KeyError("Папка не найдена")


def delete_folder(folder_id):
    with LOCK, connect() as db:
        db.execute("DELETE FROM folders WHERE id=?", (folder_id,))


def create_project(name, folder_id=None):
    settings = get_settings()
    name = clean_name(name)
    now = datetime.now()
    formats = {
        "date_title": f"{now:%Y} - {now:%m} - {now:%d} - {name}",
        "date_compact": f"{now:%Y-%m-%d} — {name}",
        "date_dotted": f"{now:%d.%m.%Y} — {name}",
        "date_time_title": f"{now:%Y-%m-%d} {now:%H-%M} — {name}",
        "title_date": f"{name} — {now:%Y-%m-%d}", "title_only": name,
    }
    name = formats[settings["project_name_format"]]
    project_id = uuid.uuid4().hex
    path = Path(settings["download_root"]) / f"{name} [{project_id[:8]}]"
    project = {"id": project_id, "name": name, "folder_id": folder_id or None,
               "path": str(path), "created_at": time.time()}
    path.mkdir(parents=True, exist_ok=True)
    with LOCK, connect() as db:
        db.execute("INSERT INTO projects VALUES (:id, :name, :folder_id, :path, :created_at)", project)
    return project


def update_project(project_id, name=None, folder_id=...):
    project = get_project(project_id)
    if name is not None:
        project["name"] = clean_name(name)
    if folder_id is not ...:
        project["folder_id"] = folder_id or None
    # Stable physical paths keep ongoing downloads and saved partials valid after rename/move.
    with LOCK, connect() as db:
        db.execute("UPDATE projects SET name=:name, folder_id=:folder_id WHERE id=:id", project)
    return get_project(project_id)


def create_download(project_id):
    project = get_project(project_id)
    download_id = uuid.uuid4().hex
    path = Path(project["path"]) / "files" / download_id
    path.mkdir(parents=True, exist_ok=True)
    with LOCK, connect() as db:
        db.execute("INSERT INTO downloads VALUES (?, ?, ?, ?)",
                   (download_id, project_id, str(path), time.time()))
    return download_id


def session_dir(download_id):
    found = rows("SELECT path FROM downloads WHERE id=?", (download_id,))
    if not found:
        raise KeyError("Загрузка не найдена")
    return Path(found[0]["path"])


def list_sessions():
    """Engine adapter: one durable session per download, many per project."""
    return rows("SELECT * FROM downloads ORDER BY created_at")


def delete_download_record(download_id):
    with LOCK, connect() as db:
        db.execute("DELETE FROM downloads WHERE id=?", (download_id,))


def delete_project_record(project_id):
    with LOCK, connect() as db:
        db.execute("DELETE FROM projects WHERE id=?", (project_id,))
