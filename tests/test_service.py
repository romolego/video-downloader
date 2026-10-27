import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from downloader import engine, storage, youtube_access
from downloader.app import app

HEADERS = {"X-Downloader-Client": "local"}
METADATA = {"title": "Тестовая запись", "provider": "youtube", "duration_sec": 19,
            "qualities": [{"quality": "720", "height": 720}, {"quality": "1080", "height": 1080}]}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(storage, "DATA_DIR", self.root),
                        patch.object(storage, "DB_PATH", self.root / "test.sqlite3"),
                        patch.object(storage, "COOKIES_DIR", self.root / "cookies"),
                        patch.object(youtube_access, "COOKIES_DIR", self.root / "cookies"),
                        patch.object(engine, "probe_metadata", return_value=METADATA)]
        for p in self.patches:
            p.start()
        storage.init_db()
        storage.update_settings({"download_root": str(self.root / "files"), "project_name_format": "title_only"})
        engine._STATE.clear()
        engine._THREADS.clear()
        engine._PENDING.clear()
        engine._SHUTDOWN.clear()
        engine._LAST_SAVE.clear()
        self.client = TestClient(app)  # requests without lifespan; scheduler is explicitly tested below

    def tearDown(self):
        engine.shutdown()
        self.client.close()
        engine._STATE.clear()
        engine._THREADS.clear()
        engine._PENDING.clear()
        engine._SHUTDOWN.clear()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def post(self, path, body=None):
        return self.client.post(path, json=body, headers=HEADERS)

    def make_job(self, project_id=None, quality="auto", passcode=None):
        response = self.post("/api/downloads", {"url": "https://youtu.be/jNQXAC9IVRw", "project_id": project_id,
                             "quality": quality, "passcode": passcode})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_many_downloads_in_project_survive_rename_and_folder_move(self):
        first = self.make_job()
        second = self.make_job(first["project_id"], "audio")
        initial = storage.session_dir(first["id"])
        folder = self.post("/api/folders", {"name": "Учёба"}).json()
        response = self.client.patch("/api/projects/" + first["project_id"], headers=HEADERS,
                                     json={"name": "Переименованный", "folder_id": folder["id"]})
        self.assertEqual(response.status_code, 200)
        library = self.client.get("/api/library").json()
        self.assertEqual(len(library["projects"]), 1)
        self.assertEqual(len(library["downloads"]), 2)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(initial, storage.session_dir(first["id"]))
        self.client.delete("/api/folders/" + folder["id"], headers=HEADERS)
        self.assertIsNone(storage.get_project(first["project_id"])["folder_id"])

    def test_quality_is_frozen_for_queued_and_resumed_jobs(self):
        storage.update_settings({"download_quality": "480", "download_mode": "video"})
        job = self.make_job()
        self.assertEqual(engine.get_status(job["id"])["quality"], "720")
        self.post(f"/api/downloads/{job['id']}/pause")
        storage.update_settings({"download_mode": "audio", "download_quality": "2160"})
        self.post(f"/api/downloads/{job['id']}/resume")
        self.assertEqual(engine.get_status(job["id"])["quality"], "720")
        self.assertEqual(self.make_job()["id"] in engine._STATE, True)
        newest = next(reversed(engine._STATE.values()))
        self.assertEqual(newest["quality"], "audio")

    def test_real_source_low_resolution_choice_is_accepted(self):
        with patch.object(engine, "probe_metadata", return_value=METADATA | {
                "qualities": [{"quality": "240", "height": 240}]}):
            job = self.make_job(quality="240")
            self.assertEqual(engine.get_status(job["id"])["quality"], "240")

    def test_cancel_requested_during_finalization_does_not_complete(self):
        job = self.make_job(quality="audio")
        def extract(url, options, provider, *, download):
            path = Path(options["outtmpl"].replace("%(ext)s", "mp4"))
            path.write_bytes(b"video")
            return {"requested_downloads": [{"filepath": str(path)}]}
        def drop_video(path):
            engine._STATE[job["id"]]["_cancel_requested"] = True
            return path
        with patch.object(engine, "_extract", side_effect=extract), patch.object(engine, "_drop_video_track", side_effect=drop_video):
            engine._run(job["id"])
        self.assertEqual(engine.get_status(job["id"])["status"], "cancelled")

    def test_pause_restart_recover_resume_cancel_preserves_partials(self):
        job = self.make_job(quality="720")
        partial = storage.session_dir(job["id"]) / "media" / "Тестовая запись.mp4.part"
        partial.write_bytes(b"already downloaded")
        self.post(f"/api/downloads/{job['id']}/pause")
        engine._STATE.clear()
        engine._PENDING.clear()
        self.assertEqual(engine.recover_interrupted_downloads(), 0)
        self.assertEqual(engine.get_status(job["id"])["status"], "paused")
        self.assertEqual(partial.read_bytes(), b"already downloaded")
        self.post(f"/api/downloads/{job['id']}/resume")
        engine._STATE.clear()
        engine._PENDING.clear()
        self.assertEqual(engine.recover_interrupted_downloads(), 1)
        self.assertIn(job["id"], engine._PENDING)
        self.post(f"/api/downloads/{job['id']}/cancel")
        self.assertFalse(partial.exists())
        self.assertEqual(engine.get_status(job["id"])["status"], "cancelled")

    def test_completion_requires_no_whisper_or_transcription_handler(self):
        job = self.make_job(quality="720")
        def extract(url, options, provider, *, download):
            self.assertTrue(options["continuedl"])
            path = Path(options["outtmpl"].replace("%(ext)s", "mp4"))
            path.write_bytes(b"finished video")
            options["progress_hooks"][0]({"status": "finished", "filename": str(path)})
            return {"requested_downloads": [{"filepath": str(path)}]}
        with patch.object(engine, "_extract", side_effect=extract), patch.object(engine, "_COMPLETION_HANDLER", None), \
                patch.object(engine.media, "prepare_video", side_effect=lambda path: (path, {
                    "audio_streams": [{"codec": "aac"}], "video_streams": [{"height": 720}]})):
            engine._run(job["id"])
        self.assertEqual(engine.get_status(job["id"])["status"], "completed")
        response = self.client.get(f"/api/downloads/{job['id']}/file")
        self.assertEqual(response.content, b"finished video")
        self.assertIn("attachment", response.headers["content-disposition"])

    def test_secrets_not_in_api_and_cross_origin_mutations_are_blocked(self):
        job = self.make_job(passcode="secret-passcode")
        self.assertNotIn("secret-passcode", self.client.get("/api/library").text)
        self.assertIn("secret-passcode", (storage.session_dir(job["id"]) / "url_download.json").read_text(encoding="utf-8"))
        self.assertEqual(self.client.post("/api/projects", json={"name":"x"}).status_code, 403)
        self.assertEqual(self.client.post("/api/projects", json={"name":"x"}, headers=HEADERS | {"Origin":"https://evil.test"}).status_code, 403)

    def test_deletion_blocks_active_jobs_and_can_keep_downloaded_files(self):
        job = self.make_job()
        response = self.client.delete("/api/projects/" + job["project_id"], headers=HEADERS)
        self.assertEqual(response.status_code, 409)
        self.post(f"/api/downloads/{job['id']}/pause")
        path = storage.session_dir(job["id"])
        file = path / "media" / "finished.mp4"
        file.write_bytes(b"saved")
        self.assertEqual(self.client.delete("/api/projects/" + job["project_id"], headers=HEADERS).status_code, 200)
        self.assertTrue(file.exists())
        self.assertEqual(len(storage.list_sessions()), 0)

    def test_file_endpoint_cannot_serve_an_unrelated_file(self):
        job = self.make_job()
        other = self.root / "private.txt"
        other.write_text("private")
        state = engine._STATE[job["id"]]
        state.update(status="completed", filename=str(other))
        engine._save(job["id"], state)
        self.assertEqual(self.client.get(f"/api/downloads/{job['id']}/file").status_code, 404)

    def test_queue_limit_fifo_and_queued_pause(self):
        storage.update_settings({"max_parallel": "1"})
        jobs = [self.make_job()["id"] for _ in range(3)]
        self.post(f"/api/downloads/{jobs[1]}/pause")
        entered = threading.Event()
        release = threading.Event()
        order = []
        def worker(job_id):
            order.append(job_id)
            entered.set()
            release.wait(3)
            engine._STATE[job_id]["status"] = "completed"
        with patch.object(engine, "_run", side_effect=worker):
            engine.start_scheduler()
            self.assertTrue(entered.wait(2))
            self.assertEqual(order, [jobs[0]])
            release.set()
            deadline = time.monotonic() + 3
            while len(order) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            engine.shutdown()
        self.assertEqual(order, [jobs[0], jobs[2]])
        self.assertEqual(engine.get_status(jobs[1])["status"], "paused")

    def test_settings_validation_and_new_root_do_not_move_existing_files(self):
        job = self.make_job()
        original = storage.session_dir(job["id"])
        response = self.client.put("/api/settings", headers=HEADERS, json={"max_parallel":"100"})
        self.assertEqual(response.status_code, 400)
        new_root = self.root / "new"
        self.client.put("/api/settings", headers=HEADERS, json={"download_root":str(new_root)})
        self.assertEqual(original, storage.session_dir(job["id"]))
        new_job = self.make_job()
        self.assertTrue(storage.session_dir(new_job["id"]).resolve().is_relative_to(new_root.resolve()))


if __name__ == "__main__":
    unittest.main()
