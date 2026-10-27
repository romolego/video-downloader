"""Only FFprobe inspection; no transcription or ML dependencies."""
import json
import shutil
import subprocess
from pathlib import Path


class MediaValidationError(RuntimeError):
    """A downloaded file cannot fulfill the requested video/audio contract."""


def inspect_media(path: Path) -> dict:
    tool = shutil.which("ffprobe")
    if not tool:
        raise RuntimeError("FFprobe не найден. Установите FFmpeg.")
    result = subprocess.run(
        [tool, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise MediaValidationError("Полученный файл не прошёл проверку: " + (result.stderr.strip() or "не удалось прочитать файл"))
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    def seconds(value):
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    audio = [{"index": s["index"], "codec": s.get("codec_name", ""),
              "duration_seconds": seconds(s.get("duration"))}
             for s in streams if s.get("codec_type") == "audio"]
    video = [{"index": s["index"], "codec": s.get("codec_name", ""),
              "width": s.get("width"), "height": s.get("height"),
              "duration_seconds": seconds(s.get("duration"))}
             for s in streams if s.get("codec_type") == "video"]
    return {"audio_streams": audio, "video_streams": video,
            "video_stream_count": len(video),
            "duration_seconds": seconds(payload.get("format", {}).get("duration")) or 0}


def probe_media(path: Path) -> dict:
    result = inspect_media(path)
    audio = result["audio_streams"]
    if not audio:
        raise MediaValidationError("В файле не найдена аудиодорожка. Загрузка не завершена со звуком.")
    return result


def prepare_video(path: Path) -> tuple[Path, dict]:
    """Require video+audio, and produce MP4/AAC compatible with common players."""
    info = probe_media(path)
    if not info["video_stream_count"]:
        raise MediaValidationError("Источник отдал только аудио вместо видео со звуком.")
    audio_duration = info["audio_streams"][0].get("duration_seconds")
    video_duration = info["video_streams"][0].get("duration_seconds") or info["duration_seconds"]
    if audio_duration and video_duration and audio_duration < video_duration - max(5, video_duration * .02):
        raise MediaValidationError("Аудиодорожка короче видео. Источник не отдал полный звук записи.")
    if path.suffix.lower() == ".mp4" and info["audio_streams"][0]["codec"] == "aac":
        return path, info
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaValidationError("FFmpeg не найден для подготовки совместимого звука AAC.")
    target = path.with_suffix(".mp4")
    partial = target.with_name(f".{target.stem}.compatible.mp4")
    audio_codec = info["audio_streams"][0]["codec"]
    result = subprocess.run([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a",
        "copy" if audio_codec == "aac" else "aac",
        *([] if audio_codec == "aac" else ["-b:a", "192k"]),
        "-movflags", "+faststart", str(partial)
    ], capture_output=True, text=True, encoding="utf-8", errors="replace",
       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not partial.is_file():
        partial.unlink(missing_ok=True)
        raise MediaValidationError("Не удалось подготовить MP4 со звуком AAC: " + result.stderr.strip())
    verified = probe_media(partial)
    if not verified["video_stream_count"] or verified["audio_streams"][0]["codec"] != "aac":
        partial.unlink(missing_ok=True)
        raise MediaValidationError("Сборка видео и звука не прошла проверку.")
    import os
    os.replace(partial, target)
    if path != target:
        path.unlink()
    return target, verified
