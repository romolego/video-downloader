import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from downloader import engine, storage, youtube_access


class ProxyAccessTests(unittest.TestCase):
    def tearDown(self):
        youtube_access.forget()

    def test_generic_unavailability_is_retried_but_known_removal_is_not(self):
        self.assertTrue(youtube_access.worth_retrying(Exception("[youtube] abc: Video unavailable")))
        for reason in ("Private video", "Video unavailable. This video has been removed", "It was blocked due to the claimed content by SME."):
            self.assertFalse(youtube_access.worth_retrying(Exception(reason)), reason)

    def test_direct_success_uses_direct_connection(self):
        with patch.object(storage, "get_settings", return_value={"youtube_connection": "direct"}), \
                patch.object(engine, "_extract_clients", return_value={"title": "Available"}) as extract:
            self.assertEqual(engine._extract("url", {}, "youtube", download=False)["title"], "Available")
            self.assertNotIn('proxy', extract.call_args.args[1])

    def test_manual_proxy_is_used_for_both_probe_and_file(self):
        for download in (False, True):
            with self.subTest(download=download), \
                    patch.object(storage, "get_settings", return_value={"youtube_connection": "proxy", "youtube_proxy": "https://proxy.test:443"}), \
                    patch.object(engine, "_extract_clients", return_value={"title": "Film"}) as extract:
                result = engine._extract("url", {"format": "720"}, "youtube", download=download)
                self.assertEqual(result["title"], "Film")
                self.assertEqual(extract.call_args.args[1]["proxy"], "https://proxy.test:443")
                self.assertEqual(extract.call_args.kwargs["download"], download)

    def test_proxy_connection_error_does_not_expose_credentials_or_retry_clients(self):
        proxy = "https://private-user:private-password@proxy.test:443"
        with patch.object(storage, "get_settings", return_value={"youtube_connection": "proxy", "youtube_proxy": proxy}), \
                patch.object(engine, "_extract_clients", side_effect=Exception("Unable to connect to proxy " + proxy + ": Tunnel connection failed: 405 Not Allowed")):
            with self.assertRaises(engine.ProxyAccessError) as result:
                engine._extract("url", {}, "youtube", download=False)
        message = engine.humanize_error(result.exception)
        self.assertIn("HTTP 405", message)
        self.assertNotIn("private-user", message)
        self.assertNotIn("private-password", message)

    def test_copyright_reason_is_preserved_and_generic_error_does_not_assert_deletion(self):
        message = engine.humanize_error(Exception("ERROR: [youtube] abc: It was blocked due to the claimed content by SME."))
        self.assertIn("SME", message)
        self.assertIn("VPN", message)
        self.assertEqual(engine._classify_error(Exception("claimed content by SME")), "permanent")
        generic = engine.humanize_error(Exception("[youtube] abc: Video unavailable"))
        self.assertNotIn("удалено", generic)
        self.assertIn("не сообщил точную причину", generic)

    def test_manual_proxy_routes_real_metadata_and_media_requests(self):
        payload = b"proxy-download" * 200
        requested = []
        class Proxy(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def do_HEAD(self):
                requested.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
            def do_GET(self):
                self.do_HEAD()
                self.wfile.write(payload)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp, \
                    patch.object(storage, "get_settings", return_value={"youtube_connection": "proxy", "youtube_proxy": f"http://127.0.0.1:{server.server_port}", "youtube_cookies": "off"}), \
                    patch.object(youtube_access, "cookie_file", return_value=None):
                options = {"quiet": True, "no_warnings": True, "force_generic_extractor": True,
                           "outtmpl": str(Path(temp) / "clip.%(ext)s")}
                url = "http://download-test.invalid/clip.mp4"
                engine._extract(url, options, "youtube", download=False)
                engine._extract(url, options, "youtube", download=True)
                self.assertEqual((Path(temp) / "clip.mp4").read_bytes(), payload)
                self.assertGreaterEqual(requested.count(url), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ProxySettingsTests(unittest.TestCase):
    def test_proxy_setting_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(storage, "DATA_DIR", Path(temp)), \
                patch.object(storage, "COOKIES_DIR", Path(temp) / "cookies"), \
                patch.object(storage, "DB_PATH", Path(temp) / "library.sqlite3"):
            storage.init_db()
            for value in ("http://127.0.0.1:7890", "socks5://127.0.0.1:1080", "https://user:pass@proxy.test:443", ""):
                storage.update_settings({"youtube_proxy": value})
                self.assertEqual(storage.get_settings()["youtube_proxy"], value)
            for value in ("file:///C:/secret", "http://proxy.test", "https://proxy.test:99999", "https://proxy.test:443/path"):
                with self.assertRaises(ValueError):
                    storage.update_settings({"youtube_proxy": value})

    def test_retired_connection_settings_migrate_without_touching_projects(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(storage, "DATA_DIR", Path(temp)), \
                patch.object(storage, "COOKIES_DIR", Path(temp) / "cookies"), \
                patch.object(storage, "DB_PATH", Path(temp) / "library.sqlite3"):
            storage.init_db()
            storage.update_settings({'download_root': str(Path(temp)/'downloads')})
            project=storage.create_project('Keep this project')
            with storage.connect() as db:
                db.execute("INSERT INTO settings VALUES ('youtube_connection', 'retired_extension_mode')")
                db.execute("INSERT INTO settings VALUES ('removed_extension_path', 'private path')")
            storage.init_db()
            self.assertEqual(storage.get_settings()['youtube_connection'], 'direct')
            self.assertNotIn('removed_extension_path', storage.get_settings())
            self.assertEqual(storage.get_project(project['id']), project)


if __name__ == "__main__":
    unittest.main()
