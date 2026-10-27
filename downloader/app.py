from __future__ import annotations

import importlib.metadata
import os
import shutil
import sqlite3
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from . import engine, storage, youtube_access


@asynccontextmanager
async def lifespan(app):
    storage.init_db()
    youtube_access.ensure_cookies_folder()
    engine.recover_interrupted_downloads()
    engine.start_scheduler()
    yield
    engine.shutdown()


app = FastAPI(title="Локальный загрузчик", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])


@app.middleware("http")
async def local_writes(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        # A cross-site form cannot set this custom header; no CORS permission is granted.
        if request.headers.get("X-Downloader-Client") != "local":
            return JSONResponse({"detail": "Откройте интерфейс локального загрузчика"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Запрос с другой страницы запрещён"}, status_code=403)
    return await call_next(request)


@app.exception_handler(ValueError)
async def bad_value(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(KeyError)
async def missing(request, exc):
    return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)


@app.exception_handler(sqlite3.IntegrityError)
async def bad_reference(request, exc):
    return JSONResponse({"detail": "Папка или проект больше не существуют. Обновите страницу."}, status_code=409)


@app.exception_handler(OSError)
async def filesystem_error(request, exc):
    return JSONResponse({"detail": f"Не удалось обратиться к файлам: {exc}"}, status_code=400)


class Named(BaseModel):
    name: str = Field(min_length=1, max_length=180)


class ProjectCreate(Named):
    folder_id: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=180)
    folder_id: str | None = None


class Probe(BaseModel):
    url: str = Field(min_length=1, max_length=4096)
    passcode: str | None = Field(default=None, max_length=500)


class DownloadCreate(Probe):
    project_id: str | None = None
    project_name: str | None = Field(default=None, max_length=180)
    folder_id: str | None = None
    quality: str = "auto"


def public_downloads():
    records = {row["id"]: row for row in storage.list_sessions()}
    result = []
    for snapshot in engine.list_downloads():
        download_id = snapshot["project_id"]
        if download_id not in records:
            continue
        record = records[download_id]
        result.append({k: v for k, v in snapshot.items() if not k.startswith("transcri") and k != "project_id"} |
                      {"id": download_id, "project_id": record["project_id"], "path": record["path"]})
    return result


@app.get("/api/library")
def library():
    return {"projects": storage.list_projects(), "folders": storage.list_folders(),
            "downloads": public_downloads(), "settings": storage.get_settings()}


@app.get("/api/health")
def health():
    def version(package):
        try:
            return importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            return None
    return {"service": "video-downloader", "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"), "node": shutil.which("node"),
            "deno": shutil.which("deno"), "yt_dlp": version("yt-dlp"),
            "ejs": version("yt-dlp-ejs"), "cookies_dir": str(storage.COOKIES_DIR),
            "data_dir": str(storage.DATA_DIR)}


@app.put("/api/settings")
def update_settings(values: dict[str, str]):
    result = storage.update_settings(values)
    youtube_access.forget()
    engine._PROBE_CACHE.clear()
    return result


@app.post("/api/probe")
def probe(body: Probe):
    try:
        return engine.probe_metadata(body.url, body.passcode)
    except Exception as exc:
        raise HTTPException(400, engine.humanize_error(exc)) from exc


@app.post("/api/projects")
def create_project(body: ProjectCreate):
    return storage.create_project(body.name, body.folder_id)


@app.patch("/api/projects/{project_id}")
def update_project(project_id: str, body: ProjectUpdate):
    values = body.model_dump(exclude_unset=True)
    return storage.update_project(project_id, **values)


@app.post("/api/folders")
def create_folder(body: Named):
    return storage.create_folder(body.name)


@app.patch("/api/folders/{folder_id}")
def rename_folder(folder_id: str, body: Named):
    storage.update_folder(folder_id, body.name)
    return {"ok": True}


@app.delete("/api/folders/{folder_id}")
def delete_folder(folder_id: str):
    storage.delete_folder(folder_id)
    return {"ok": True}


@app.post("/api/downloads")
def create_download(body: DownloadCreate):
    if body.quality not in {"auto", "audio", "max"} and not (body.quality.isdigit() and 100 <= int(body.quality) <= 4320):
        raise ValueError("Выберите качество из списка")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("Для загрузки и извлечения звука нужен FFmpeg. Запустите установщик загрузчика.")
    if body.project_id:
        project = storage.get_project(body.project_id)
    try:
        metadata = engine.probe_metadata(body.url, body.passcode)
    except Exception as exc:
        raise HTTPException(400, engine.humanize_error(exc)) from exc
    if not body.project_id:
        if metadata.get("audio_available") is False:
            raise ValueError("Источник не отдал доступную аудиодорожку. Видео без звука скачиваться не будет.")
        project = storage.create_project(body.project_name or metadata["title"], body.folder_id)
    download_id = storage.create_download(project["id"])
    try:
        engine.start_download(download_id, body.url, body.quality, metadata,
                              passcode=body.passcode, transcribe=False)
    except Exception:
        storage.delete_download_record(download_id)
        raise
    return {"id": download_id, "project_id": project["id"]}


@app.post("/api/downloads/{download_id}/{action}")
def control_download(download_id: str, action: str):
    storage.session_dir(download_id)
    if action == "pause":
        ok, message = engine.pause(download_id), "Загрузку сейчас нельзя поставить на паузу"
    elif action == "resume":
        ok, message = engine.resume(download_id)
    elif action == "cancel":
        ok, message = engine.cancel(download_id), "Загрузку сейчас нельзя отменить"
    else:
        raise HTTPException(404, "Действие не найдено")
    if not ok:
        raise HTTPException(409, message)
    return {"ok": True}


def ensure_idle(ids):
    with engine._LOCK:
        for download_id in ids:
            state = engine.get_status(download_id)
            thread = engine._THREADS.get(download_id)
            if state and state["status"] in engine.ACTIVE_STATUSES or thread and thread.is_alive():
                raise HTTPException(409, "Сначала поставьте загрузки на паузу или отмените их")


@app.delete("/api/downloads/{download_id}")
def delete_download(download_id: str, delete_files: bool = False):
    with engine._LOCK:
        path = storage.session_dir(download_id).resolve()
        ensure_idle([download_id])
        if delete_files and path.exists():
            # Use exactly the registered per-download directory, never a supplied filesystem path.
            shutil.rmtree(path)
        storage.delete_download_record(download_id)
        engine.forget(download_id)
    return {"ok": True}


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str, delete_files: bool = False):
    with engine._LOCK:
        project = storage.get_project(project_id)
        ids = [d["id"] for d in storage.list_sessions() if d["project_id"] == project_id]
        ensure_idle(ids)
        if delete_files:
            path = Path(project["path"]).resolve()
            if path.exists():
                shutil.rmtree(path)
        storage.delete_project_record(project_id)
        for download_id in ids:
            engine.forget(download_id)
    return {"ok": True}


def final_file(download_id):
    storage.session_dir(download_id)
    state = engine._load(download_id)
    if not state or state.get("status") != "completed":
        raise HTTPException(409, "Файл ещё не готов")
    path = Path(state.get("filename") or "").resolve()
    expected = (storage.session_dir(download_id) / "media").resolve()
    if not path.is_relative_to(expected) or not path.is_file():
        raise HTTPException(404, "Файл не найден: возможно, его перенесли вручную")
    return path


@app.get("/api/downloads/{download_id}/file")
def save_file(download_id: str):
    path = final_file(download_id)
    return FileResponse(path, filename=path.name)


@app.get("/api/downloads/{download_id}/play")
def play_file(download_id: str):
    return FileResponse(final_file(download_id))


class OpenFolder(BaseModel):
    kind: str
    id: str | None = None


@app.post("/api/open-folder")
def open_folder(body: OpenFolder):
    if body.kind == "project":
        path = Path(storage.get_project(body.id)["path"])
    elif body.kind == "download":
        path = storage.session_dir(body.id) / "media"
    elif body.kind == "cookies":
        path = youtube_access.ensure_cookies_folder()
    elif body.kind == "root":
        path = Path(storage.get_settings()["download_root"])
    else:
        raise ValueError("Папка не найдена")
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if os.uname().sysname == "Darwin" else "xdg-open", str(path)])
    return {"ok": True}


@app.post("/api/pick-folder")
def pick_folder():
    if os.name != "nt":
        raise ValueError("Введите путь папки вручную")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File",
         str(storage.ROOT / "scripts" / "pick_folder.ps1"),
         "-InitialPath", storage.get_settings()["download_root"]],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ValueError("Не удалось открыть выбор папки. Введите путь вручную.")
    return {"path": result.stdout.strip() or None}


@app.post("/api/shutdown")
def shutdown():
    stop = getattr(app.state, "stop", None)
    if not stop:
        raise HTTPException(503, "Остановите сервис через команду запуска")
    threading.Timer(0.5, stop).start()
    return {"ok": True}


app.mount("/assets", StaticFiles(directory=storage.ROOT / "web"), name="assets")


@app.get("/")
def index():
    return FileResponse(storage.ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache"})
