"""Real yt-dlp HTTP Range resume and FFmpeg audio extraction, entirely offline."""
import hashlib
import http.server
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp
from downloader import engine, media


class RealMediaTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_audio_only_really_removes_video_and_keeps_audio_codec(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "video.mp4"
            result = subprocess.run([shutil.which("ffmpeg"), "-nostdin", "-loglevel", "error", "-y",
                                     "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=1",
                                     "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                                     "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(source)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            before = media.probe_media(source)
            self.assertEqual(before["video_stream_count"], 1)
            target = engine._drop_video_track(source)
            after = media.probe_media(target)
            self.assertEqual(after["video_stream_count"], 0)
            self.assertEqual(after["audio_streams"][0]["codec"], before["audio_streams"][0]["codec"])
            self.assertAlmostEqual(after["duration_seconds"], 1, delta=0.2)
            self.assertFalse(source.exists())

    def test_actual_download_resumes_with_range_after_saved_pause(self):
        content = bytes(range(256)) * 8192
        offsets = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                start = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                offsets.append(start)
                self.send_response(206 if start else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(content)-start))
                self.send_header("Accept-Ranges", "bytes")
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(content)-1}/{len(content)}")
                self.end_headers()
                try:
                    for index in range(start, len(content), 16384):
                        self.wfile.write(content[index:index+16384])
                        self.wfile.flush()
                        time.sleep(0.002)
                except (ConnectionError, OSError):
                    pass
            def log_message(self, *_args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                job_path = root / "url_download.json"
                with patch.object(engine.storage, "session_dir", return_value=root), patch.object(engine, "_job_path", return_value=job_path), patch.object(engine, "_spawn"):
                    engine._SHUTDOWN.clear()
                    engine.start_download("http-test", "https://rutube.ru/video/test/", "720", {"title":"sample"})
                    local = f"http://127.0.0.1:{server.server_port}/sample.mp4"
                    first_attempt = True
                    def extract(url, options, provider, *, download):
                        nonlocal first_attempt
                        hook = options["progress_hooks"][0]
                        if first_attempt:
                            def pause_at_bytes(payload):
                                hook(payload)
                                if payload.get("downloaded_bytes", 0) >= 65536:
                                    engine._STATE["http-test"]["_pause_requested"] = True
                                    raise engine.DownloadPaused
                            options = options | {"progress_hooks": [pause_at_bytes]}
                        with yt_dlp.YoutubeDL(options | {"logger": engine.youtube_access.QuietLogger()}) as ydl:
                            return ydl.extract_info(local, download=True)
                    with patch.object(engine, "_extract", side_effect=extract), patch.object(engine, "_COMPLETION_HANDLER", None), \
                            patch.object(engine.media, "prepare_video", side_effect=lambda path: (path, {
                                "audio_streams": [{"codec": "aac"}], "video_streams": [{"height": 720}]})):
                        engine._run("http-test")
                        self.assertEqual(engine.get_status("http-test")["status"], "paused")
                        partials = list((root / "media").glob("*.part"))
                        self.assertEqual(len(partials), 1)
                        partial_size = partials[0].stat().st_size
                        self.assertGreater(partial_size, 0)
                        self.assertLess(partial_size, len(content))
                        # Drop in-memory state to model a fresh process reading disk.
                        engine._STATE.pop("http-test")
                        first_attempt = False
                        self.assertTrue(engine.resume("http-test")[0])
                        engine._run("http-test")
                    state = engine.get_status("http-test")
                    self.assertEqual(state["status"], "completed", state.get("error"))
                    self.assertIn(partial_size, offsets, "yt-dlp must send a non-zero HTTP Range after resume")
                    final = Path(engine._load("http-test")["filename"])
                    self.assertEqual(hashlib.sha256(final.read_bytes()).digest(), hashlib.sha256(content).digest())
                    engine.forget("http-test")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
