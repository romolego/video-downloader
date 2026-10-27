from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from downloader import engine as downloader
from downloader import youtube_access


def setUpModule():
    # Unit tests use fixed settings and never touch the actual user's database.
    global _settings_patch
    _settings_patch = patch.object(downloader.storage, "get_settings", return_value={
        "download_quality": "audio", "download_mode": "audio", "youtube_cookies": "off"
    })
    _settings_patch.start()


def tearDownModule():
    _settings_patch.stop()


class ProviderTests(unittest.TestCase):
    def test_supported_hosts_are_explicit(self) -> None:
        cases = {
            "https://youtu.be/abc": "youtube",
            "https://vk.com/video-1_2": "vk",
            "https://rutube.ru/video/abc/": "rutube",
            "https://disk.yandex.ru/d/abc": "yandexdisk",
            "https://disk.360.yandex.ru/i/abc": "yandexdisk",
            "https://drive.google.com/file/d/abc/view": "gdrive",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(downloader.detect_provider(url), expected)
        self.assertIsNone(downloader.detect_provider("file:///etc/passwd"))
        self.assertIsNone(
            downloader.detect_provider("https://youtube.com.evil.test/video")
        )

    def test_vk_live_url_is_normalized(self) -> None:
        self.assertEqual(
            downloader.normalize_url(
                "https://vkvideo.ru/live-211211142_456239216"
            ),
            "https://vkvideo.ru/video-211211142_456239216",
        )

    def test_vk_audio_prefers_complete_combined_hls(self) -> None:
        self.assertEqual(
            downloader._format_selector("audio", "vk"),
            (
                "worst[height>0][protocol=m3u8_native][format_id^=hls-]/"
                "worst[height>0]"
            ),
        )
        # /best в конце — запасной путь для источников без отдельной
        # аудиодорожки (записи Zoom). Для YouTube он не срабатывает: там
        # bestaudio есть всегда.
        self.assertEqual(
            downloader._format_selector("audio", "youtube"),
            "bestaudio[ext=m4a][format_note*=original]/"
            "bestaudio[format_note*=original]/bestaudio[ext=m4a]/bestaudio/best",
        )

    @patch.object(downloader.shutil, "which", return_value="ffmpeg.exe")
    @patch.object(downloader.subprocess, "run")
    def test_vk_combined_hls_is_reduced_to_audio_without_reencoding(
        self,
        run: MagicMock,
        _which: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "recording.mp4"
            source.write_bytes(b"combined stream")

            def create_output(command: list[str], **_kwargs: object) -> MagicMock:
                Path(command[-1]).write_bytes(b"audio only")
                return MagicMock(returncode=0, stderr="")

            run.side_effect = create_output

            result = downloader._keep_audio_only(source)

            self.assertEqual(result, Path(directory) / "recording.m4a")
            self.assertEqual(result.read_bytes(), b"audio only")
            self.assertFalse(source.exists())
            command = run.call_args.args[0]
            self.assertIn("-vn", command)
            self.assertEqual(command[command.index("-c:a") + 1], "copy")
            self.assertEqual(command[command.index("-map") + 1], "0:a:0")


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        downloader._PROBE_CACHE.clear()

    def tearDown(self) -> None:
        downloader._PROBE_CACHE.clear()

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_probe_builds_quality_choices(self, youtube_dl: MagicMock) -> None:
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.return_value = {
            "title": "Тестовое видео",
            "uploader": "Автор",
            "duration": 125,
            "webpage_url": "https://youtu.be/abc",
            "formats": [
                {
                    "vcodec": "avc1",
                    "acodec": "none",
                    "height": 1080,
                    "ext": "mp4",
                    "filesize": 10_000,
                },
                {
                    "vcodec": "none",
                    "acodec": "aac",
                    "ext": "m4a",
                    "filesize": 2_000,
                },
            ],
        }

        # Настройка качества патчится явно: тест не должен зависеть от того,
        # что пользователь выбрал в живой базе настроек.
        with patch.object(downloader, "preferred_quality", return_value="audio"):
            result = downloader.probe_metadata("https://youtu.be/abc")

        self.assertEqual(result["provider_label"], "YouTube")
        self.assertEqual(result["duration_sec"], 125)
        self.assertEqual(result["qualities"][0]["quality"], "1080")
        self.assertEqual(result["qualities"][-1]["quality"], "audio")
        # По умолчанию «Авто» — только звук, поэтому оценка размера равна
        # размеру аудиодорожки, а не видео.
        self.assertEqual(result["auto_filesize_bytes"], 2_000)

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_probe_estimates_size_from_bitrate(
        self, youtube_dl: MagicMock
    ) -> None:
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.return_value = {
            "title": "Видео без Content-Length",
            "duration": 100,
            "formats": [
                {
                    "vcodec": "avc1",
                    "acodec": "none",
                    "height": 720,
                    "ext": "mp4",
                    "tbr": 800,
                },
                {
                    "vcodec": "none",
                    "acodec": "aac",
                    "ext": "m4a",
                    "abr": 128,
                },
            ],
        }

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            result = downloader.probe_metadata("https://youtu.be/abc")

        # 128 кбит/с * 100 с — размер аудиодорожки под «Авто» = только звук.
        self.assertEqual(result["auto_filesize_bytes"], 1_600_000)

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_probe_reuses_recent_result(self, youtube_dl: MagicMock) -> None:
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.return_value = {
            "title": "VK эфир",
            "duration": 60,
            "formats": [],
        }

        first = downloader.probe_metadata(
            "https://vkvideo.ru/live-211211142_456239216"
        )
        first["title"] = "Изменено клиентом"
        second = downloader.probe_metadata(
            "https://vkvideo.ru/video-211211142_456239216"
        )

        self.assertEqual(second["title"], "VK эфир")
        instance.extract_info.assert_called_once_with(
            "https://vkvideo.ru/video-211211142_456239216",
            download=False,
        )

    @patch.object(downloader.time, "sleep")
    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_probe_retries_transient_ssl_failure(
        self,
        youtube_dl: MagicMock,
        sleep: MagicMock,
    ) -> None:
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.side_effect = [
            RuntimeError("SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC"),
            {"title": "VK эфир", "formats": []},
        ]

        result = downloader.probe_metadata("https://vkvideo.ru/video-1_2")

        self.assertEqual(result["title"], "VK эфир")
        self.assertEqual(instance.extract_info.call_count, 2)
        sleep.assert_called_once_with(1)


class DurableControlTests(unittest.TestCase):
    def setUp(self) -> None:
        downloader._STATE.clear()
        downloader._THREADS.clear()
        downloader._LAST_SAVE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_id = "project-test"
        self.project_dir = self.root / self.project_id
        self.project_dir.mkdir()
        self.job_path = self.project_dir / "url_download.json"

    def tearDown(self) -> None:
        downloader._STATE.clear()
        downloader._THREADS.clear()
        downloader._LAST_SAVE.clear()
        self.temp.cleanup()

    @patch.object(downloader, "_spawn")
    def test_start_persists_and_pauses(self, spawn: MagicMock) -> None:
        with (
            patch.object(
                downloader.storage,
                "session_dir",
                return_value=self.project_dir,
            ),
            patch.object(
                downloader,
                "_job_path",
                return_value=self.job_path,
            ),
        ):
            state = downloader.start_download(
                self.project_id,
                "https://rutube.ru/video/abc/",
                "720",
                {"title": "Лекция", "duration_sec": 3600},
                {
                    "language": "ru",
                    "quality": "fast",
                    "diarization": True,
                },
            )
            self.assertEqual(state["status"], "queued")
            self.assertTrue(self.job_path.is_file())
            self.assertEqual(
                json.loads(self.job_path.read_text(encoding="utf-8"))[
                    "quality"
                ],
                "720",
            )
            self.assertEqual(
                state["transcription_options"]["quality"],
                "fast",
            )
            self.assertTrue(downloader.pause(self.project_id))
            self.assertEqual(
                downloader.get_status(self.project_id)["status"],
                "paused",
            )
            spawn.assert_called_once_with(self.project_id)

    def test_cancel_of_saved_pause_removes_partial_file(self) -> None:
        media_dir = self.project_dir / "media"
        media_dir.mkdir()
        partial = media_dir / "downloaded_test.mp4.part"
        partial.write_bytes(b"partial")
        state = {
            "project_id": self.project_id,
            "status": "paused",
            "url": "https://youtu.be/abc",
            "provider": "youtube",
            "quality": "auto",
            "title": "Видео",
            "out_template": str(media_dir / "downloaded_test.%(ext)s"),
            "started_at": 1,
            "resumable": True,
        }
        with patch.object(
            downloader,
            "_job_path",
            return_value=self.job_path,
        ):
            downloader._save(self.project_id, state)
            downloader._STATE.clear()
            self.assertTrue(downloader.cancel(self.project_id))
            self.assertFalse(partial.exists())
            self.assertEqual(
                downloader.get_status(self.project_id)["status"],
                "cancelled",
            )


if __name__ == "__main__":
    unittest.main()


class AutoQualityTests(unittest.TestCase):
    """Подбор качества под «Авто»: настройка — пожелание, а не требование.

    У каждого видео свой набор высот, точного совпадения обычно нет. Проверяется
    именно поведение на несовпадении: сервис не должен ни отказываться, ни молча
    скачивать максимум.
    """

    FULL = [
        {"quality": "1080", "height": 1080},
        {"quality": "720", "height": 720},
        {"quality": "360", "height": 360},
        {"quality": "audio", "height": None},
    ]

    def _resolve(self, preference: str, qualities=None) -> str:
        with patch.object(
            downloader, "preferred_quality", return_value=preference
        ):
            chosen = downloader.resolve_auto_quality(qualities or self.FULL)
        return chosen["quality"]

    def test_exact_match(self) -> None:
        for preference in ("360", "720", "1080"):
            with self.subTest(preference=preference):
                self.assertEqual(self._resolve(preference), preference)

    def test_audio_preferred(self) -> None:
        self.assertEqual(self._resolve("audio"), "audio")

    def test_max_takes_the_highest(self) -> None:
        self.assertEqual(self._resolve("max"), "1080")

    def test_no_exact_match_falls_down(self) -> None:
        """Хотели 480p, есть 360 и 720 — берём 360, ближайшее снизу."""
        self.assertEqual(self._resolve("480"), "360")

    def test_all_higher_falls_up(self) -> None:
        """Хотели 480p, а есть только 720 и 1080 — берём 720, не 1080."""
        only_high = [
            {"quality": "1080", "height": 1080},
            {"quality": "720", "height": 720},
        ]
        self.assertEqual(self._resolve("480", only_high), "720")

    def test_audio_missing_falls_to_lightest_video(self) -> None:
        no_audio = [
            {"quality": "1080", "height": 1080},
            {"quality": "720", "height": 720},
        ]
        self.assertEqual(self._resolve("audio", no_audio), "720")

    def test_only_audio_available(self) -> None:
        audio_only = [{"quality": "audio", "height": None}]
        self.assertEqual(self._resolve("720", audio_only), "audio")

    def test_empty_list_is_not_a_crash(self) -> None:
        with patch.object(downloader, "preferred_quality", return_value="720"):
            self.assertIsNone(downloader.resolve_auto_quality([]))


class ZoomProviderTests(unittest.TestCase):
    """Zoom как источник и код доступа к записи.

    Поддомен у каждого аккаунта свой (us02web, us05web, имя компании), поэтому
    точный список хостов не годится — распознавание идёт по суффиксу домена. И
    суффикс обязан быть настоящим: notzoom.us.evil.com не Zoom.
    """

    REAL = (
        "https://us02web.zoom.us/rec/share/"
        "cjn3T0a-Uvz-3-l0VkvHzXLAxD4dk_DolbnHB6wTwnE2ERUvIEIQgWYUjuwkEdLx"
        ".0IzOX-ChACoUdy39?startTime=1786687309000"
    )

    def test_real_share_link_is_recognised(self) -> None:
        self.assertEqual(downloader.detect_provider(self.REAL), "zoom")
        self.assertTrue(downloader.needs_passcode(self.REAL))

    def test_any_account_subdomain_works(self) -> None:
        for host in ("us02web", "us05web", "company", "eu01web"):
            url = f"https://{host}.zoom.us/rec/play/abc"
            with self.subTest(host=host):
                self.assertEqual(downloader.detect_provider(url), "zoom")

    def test_bare_domain_works(self) -> None:
        self.assertEqual(
            downloader.detect_provider("https://zoom.us/rec/share/x"), "zoom"
        )

    def test_lookalike_domain_is_rejected(self) -> None:
        """Суффикс проверяется по границе точки, а не подстрокой."""
        for url in (
            "https://notzoom.us.evil.com/rec/share/x",
            "https://zoom.us.evil.com/rec/share/x",
            "https://fakezoom.us/rec/share/x",
        ):
            with self.subTest(url=url):
                self.assertIsNone(downloader.detect_provider(url))

    def test_other_providers_need_no_passcode(self) -> None:
        for url in (
            "https://youtu.be/abc",
            "https://vk.com/video-1_2",
            "https://disk.yandex.ru/i/abc",
        ):
            with self.subTest(url=url):
                self.assertFalse(downloader.needs_passcode(url))

    def test_zoom_is_listed_as_supported(self) -> None:
        self.assertIn("Zoom", downloader.SUPPORTED_PROVIDERS_TEXT)
        self.assertEqual(downloader.PROVIDER_LABELS["zoom"], "Zoom")

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_passcode_reaches_yt_dlp(self, youtube_dl: MagicMock) -> None:
        """Код уходит в videopassword — именно так его называет yt-dlp."""
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.return_value = {"title": "Запись", "formats": []}

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            downloader.probe_metadata(self.REAL, "G*0f!27Y")

        options = youtube_dl.call_args[0][0]
        self.assertEqual(options["videopassword"], "G*0f!27Y")

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_no_passcode_means_no_option(self, youtube_dl: MagicMock) -> None:
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.return_value = {"title": "Видео", "formats": []}

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            downloader.probe_metadata("https://youtu.be/abc")

        self.assertNotIn("videopassword", youtube_dl.call_args[0][0])

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_probe_cache_separates_passcodes(self, youtube_dl: MagicMock) -> None:
        """С неверным кодом ответа нет, с верным есть — один ключ кеша врал бы."""
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.return_value = {"title": "Запись", "formats": []}

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            downloader.probe_metadata(self.REAL, "первый")
            downloader.probe_metadata(self.REAL, "второй")

        self.assertEqual(instance.extract_info.call_count, 2)

    def test_passcode_never_leaves_in_the_snapshot(self) -> None:
        """Код нужен только yt-dlp и не должен уезжать в браузер."""
        snapshot = downloader._snapshot(
            {"project_id": "p", "status": "queued", "passcode": "СЕКРЕТ"}
        )
        self.assertNotIn("passcode", snapshot)
        self.assertNotIn("СЕКРЕТ", str(snapshot))

    def test_zoom_api_change_gets_an_honest_message(self) -> None:
        """Вместо трассы yt-dlp — объяснение и что делать."""
        message = downloader.humanize_error(
            Exception("ERROR: [zoom] abc: Unable to extract data; please report")
        )
        self.assertIn("Zoom изменил проверку кода доступа", message)
        self.assertIn("вручную", message)

    def test_missing_passcode_message(self) -> None:
        message = downloader.humanize_error(
            Exception("This video is protected by a passcode")
        )
        self.assertIn("кодом доступа", message.lower())
        self.assertIn("введите", message.lower())

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_zoom_formats_become_quality_choices(
        self, youtube_dl: MagicMock
    ) -> None:
        """Zoom отдаёт кодеки как None — это «неизвестно», а не «дорожки нет».

        Форма данных снята с настоящей записи: два потока по 1440p, оба с
        vcodec=None и acodec=None. При склейке None в "none" такие форматы не
        попадали ни в видео, ни в звук, и список качеств выходил пустым.
        """
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.return_value = {
            "title": "Показ релиза",
            "duration": 3236,
            "formats": [
                {
                    "format_id": "view",
                    "ext": "mp4",
                    "vcodec": None,
                    "acodec": None,
                    "height": 1440,
                    "tbr": 700,
                },
                {
                    "format_id": "view_with_share",
                    "ext": "mp4",
                    "vcodec": None,
                    "acodec": None,
                    "height": 1440,
                    "tbr": 900,
                },
            ],
        }

        with patch.object(downloader, "preferred_quality", return_value="audio"), \
                patch.object(downloader, "_zoom_cookies", return_value=None):
            result = downloader.probe_metadata(self.REAL, "код")

        self.assertTrue(result["qualities"], "список качеств не должен быть пустым")
        self.assertEqual(result["qualities"][0]["height"], 1440)
        # Отдельной аудиодорожки у Zoom нет, поэтому «только звук» обязан
        # честно откатиться на самое лёгкое видео, а не остаться ни с чем.
        chosen = downloader.resolve_auto_quality(result["qualities"])
        self.assertIsNotNone(chosen)

    def test_progressive_download_keeps_only_sound(self) -> None:
        """Просили звук, а источник отдал видео со звуком — видео уходит.

        У YouTube отдельной аудиодорожки может не быть вовсе: клиент, чьи
        ссылки сервер соглашается отдавать, предлагает один совмещённый 360p.
        Без этого шага «Только звук» молча занимал бы вдвое больше места.
        """
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "концерт.mp4"
            source.write_bytes(b"video with sound")
            probe = {
                "video_stream_count": 1,
                "audio_streams": [{"index": 1, "codec": "aac"}],
            }

            def create_output(command: list[str], **_kwargs: object) -> MagicMock:
                Path(command[-1]).write_bytes(b"sound only")
                return MagicMock(returncode=0, stderr="")

            with patch.object(downloader.media, "probe_media", return_value=probe),                     patch.object(downloader.shutil, "which", return_value="ffmpeg.exe"),                     patch.object(
                        downloader.subprocess, "run", side_effect=create_output
                    ):
                result = downloader._drop_video_track(source)

            self.assertEqual(result.suffix, ".m4a")
            self.assertFalse(source.exists())

    def test_sound_only_file_is_left_alone(self) -> None:
        """Лишний прогон FFmpeg по готовому звуку — потерянные минуты."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "запись.m4a"
            source.write_bytes(b"sound only")
            probe = {
                "video_stream_count": 0,
                "audio_streams": [{"index": 0, "codec": "aac"}],
            }

            with patch.object(downloader.media, "probe_media", return_value=probe),                     patch.object(downloader.subprocess, "run") as run:
                result = downloader._drop_video_track(source)

            self.assertEqual(result, source)
            run.assert_not_called()

    def test_opus_is_not_forced_into_an_mp4_box(self) -> None:
        """Дорожка переносится как есть, поэтому контейнер выбирается по кодеку."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "эфир.webm"
            source.write_bytes(b"video with opus")
            probe = {
                "video_stream_count": 1,
                "audio_streams": [{"index": 1, "codec": "opus"}],
            }

            def create_output(command: list[str], **_kwargs: object) -> MagicMock:
                Path(command[-1]).write_bytes(b"opus only")
                return MagicMock(returncode=0, stderr="")

            with patch.object(downloader.media, "probe_media", return_value=probe),                     patch.object(downloader.shutil, "which", return_value="ffmpeg.exe"),                     patch.object(
                        downloader.subprocess, "run", side_effect=create_output
                    ):
                result = downloader._drop_video_track(source)

            self.assertEqual(result.suffix, ".opus")

    def test_auto_quality_counts_as_sound_when_settings_say_so(self) -> None:
        """Под «Авто» стоит настройка, и по умолчанию она — только звук."""
        with patch.object(downloader, "preferred_quality", return_value="audio"):
            self.assertTrue(downloader._audio_only_requested({"quality": "auto"}))
            self.assertTrue(downloader._audio_only_requested({"quality": "audio"}))
            self.assertFalse(downloader._audio_only_requested({"quality": "720"}))
        with patch.object(downloader, "preferred_quality", return_value="1080"):
            self.assertFalse(downloader._audio_only_requested({"quality": "auto"}))

    def test_audio_selector_falls_back_to_best(self) -> None:
        """У Zoom звук только внутри общего MP4 — запрос «только звук» не должен падать."""
        selector = downloader._format_selector("audio")
        self.assertTrue(selector.endswith("/best"), selector)


BOT_CHECK = (
    "ERROR: [youtube] fIlp-ONY_KI: Sign in to confirm you're not a bot. "
    "Use --cookies-from-browser or --cookies for the authentication."
)


class YoutubeBotCheckTests(unittest.TestCase):
    """Бот-проверка YouTube лечится перебором клиентов, а не отказом.

    Ошибка приходит на запрос, а не на видео: тот же ролик отдаётся, если
    спросить его от имени другого клиента API. Пользователь об этом знать не
    должен — до него доходит либо видео, либо объяснение по-русски.
    """

    URL = "https://www.youtube.com/live/fIlp-ONY_KI"

    def setUp(self) -> None:
        downloader._PROBE_CACHE.clear()
        youtube_access.forget()
        # Личный файл cookies и браузер в тестах не участвуют: результат не
        # должен зависеть от того, что лежит в data/cookies на этой машине.
        self.no_cookies = patch.object(
            youtube_access, "cookie_file", return_value=None
        )
        self.no_browser = patch.object(
            youtube_access, "browser_cookie_source", return_value=None
        )
        self.no_cookies.start()
        self.no_browser.start()
        self.addCleanup(self.no_cookies.stop)
        self.addCleanup(self.no_browser.stop)
        self.addCleanup(downloader._PROBE_CACHE.clear)
        self.addCleanup(youtube_access.forget)

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_bot_check_is_retried_with_another_client(
        self, youtube_dl: MagicMock
    ) -> None:
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.side_effect = [
            Exception(BOT_CHECK),
            {"title": "Запись", "formats": []},
        ]

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            result = downloader.probe_metadata(self.URL)

        self.assertEqual(result["title"], "Запись")
        self.assertEqual(instance.extract_info.call_count, 2)
        # Вторая попытка обязана отличаться от первой, иначе это просто повтор
        # того же запроса — а он снова упрётся в ту же проверку.
        first, second = (call[0][0] for call in youtube_dl.call_args_list)
        self.assertNotIn("extractor_args", first)
        self.assertEqual(
            second["extractor_args"]["youtube"]["player_client"],
            list(youtube_access.FALLBACK_CLIENT_SETS[0]),
        )

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_hidden_formats_are_asked_for_explicitly(
        self, youtube_dl: MagicMock
    ) -> None:
        """«Формат недоступен» на живом видео — это спрятанные форматы.

        YouTube требует за них одноразовый токен, а yt-dlp без токена просто
        убирает их из списка. Запасная попытка просит оставить их на месте.
        """
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.side_effect = [
            Exception("ERROR: Requested format is not available"),
            {"title": "Видео", "formats": []},
        ]

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            downloader.probe_metadata(self.URL)

        second = youtube_dl.call_args_list[1][0][0]
        self.assertEqual(
            second["extractor_args"]["youtube"]["formats"], ["missing_pot"]
        )

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_removed_video_is_not_retried(self, youtube_dl: MagicMock) -> None:
        """Перебор лечит запрос, а не видео: удалённое повторять незачем."""
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.side_effect = Exception(
            "ERROR: [youtube] abc: Video unavailable. This video has been removed"
        )

        with self.assertRaises(Exception):
            downloader.probe_metadata(self.URL)

        self.assertEqual(instance.extract_info.call_count, 1)

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_working_way_is_remembered(self, youtube_dl: MagicMock) -> None:
        """Найденный обход идёт первым: иначе каждая ссылка снова ждала бы отказ."""
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.side_effect = [
            Exception(BOT_CHECK),
            {"title": "Первое", "formats": []},
            {"title": "Второе", "formats": []},
        ]

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            downloader.probe_metadata(self.URL)
            downloader.probe_metadata("https://youtu.be/second")

        self.assertEqual(instance.extract_info.call_count, 3)
        third = youtube_dl.call_args_list[2][0][0]
        self.assertIn("extractor_args", third)

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_other_providers_are_asked_once(self, youtube_dl: MagicMock) -> None:
        """Перебор — только для YouTube: остальным лишние запросы ни к чему."""
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.side_effect = Exception(BOT_CHECK)

        with self.assertRaises(Exception):
            downloader.probe_metadata("https://rutube.ru/video/abc/")

        self.assertEqual(instance.extract_info.call_count, 1)

    def test_message_explains_what_to_do(self) -> None:
        """До пользователя доходит объяснение, а не трасса со ссылками на GitHub."""
        message = downloader.humanize_error(Exception(BOT_CHECK))

        self.assertNotIn("--cookies", message)
        self.assertNotIn("github", message.lower())
        self.assertIn("cookies.txt", message)
        self.assertIn(str(youtube_access.COOKIES_DIR), message)

    def test_typographic_apostrophe_in_bot_check_is_understood(self) -> None:
        message = downloader.humanize_error(Exception(BOT_CHECK.replace("you're", "you’re")))
        self.assertIn("не от робота", message)

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_forbidden_file_is_retried_with_another_client(
        self, youtube_dl: MagicMock
    ) -> None:
        """403 на файле — свойство клиента, а не видео.

        Клиент по умолчанию (android_vr) ссылку отдаёт, а сервер по ней
        отвечает 403. Раньше загрузка на этом и вставала, хотя у другого
        клиента ссылка своя и рабочая.
        """
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.side_effect = [
            Exception(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            ),
            {"title": "Концерт", "formats": []},
        ]

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            result = downloader.probe_metadata(self.URL)

        self.assertEqual(result["title"], "Концерт")
        self.assertEqual(instance.extract_info.call_count, 2)

    @patch.object(downloader.yt_dlp, "YoutubeDL")
    def test_probe_does_not_teach_the_download_a_broken_way(
        self, youtube_dl: MagicMock
    ) -> None:
        """У проверки и у загрузки свой удачный способ.

        Клиент по умолчанию отдаёт данные о видео и при этом отвечает 403 на
        сам файл. С общей памятью удачная проверка возвращала бы загрузку к
        нему же — и та упиралась бы в 403 на каждой ссылке.
        """
        instance = youtube_dl.return_value.__enter__.return_value
        instance.extract_info.side_effect = [
            {"title": "Концерт", "formats": []},
            Exception("ERROR: unable to download video data: HTTP Error 403"),
            {"title": "Концерт", "formats": []},
            {"title": "Второй", "formats": []},
        ]

        with patch.object(downloader, "preferred_quality", return_value="audio"):
            downloader.probe_metadata(self.URL)
        downloader._extract(self.URL, {}, "youtube", download=True)
        downloader._extract("https://youtu.be/second", {}, "youtube", download=True)

        # Вторая загрузка обязана начать с того способа, который сработал у
        # первой, а не с клиента по умолчанию.
        fourth = youtube_dl.call_args_list[3][0][0]
        self.assertEqual(
            fourth["extractor_args"]["youtube"]["player_client"],
            list(youtube_access.FALLBACK_CLIENT_SETS[0]),
        )

    def test_each_attempt_pins_its_own_clients(self) -> None:
        """Наборы перебираются поодиночке, и android_vr среди них нет.

        В общем списке yt-dlp выбрал бы формат сломанного клиента, и попытки
        не отличались бы друг от друга ничем, кроме потраченного времени.
        """
        names = [plan.name for plan in youtube_access.plans("youtube")]
        sets = [name for name in names if name.startswith("clients:")]

        self.assertEqual(len(sets), len(set(sets)))
        self.assertEqual(
            len(sets), len(youtube_access.FALLBACK_CLIENT_SETS)
        )
        self.assertNotIn(
            "android_vr",
            [
                client
                for clients in youtube_access.FALLBACK_CLIENT_SETS
                for client in clients
            ],
        )

    def test_age_limit_is_explained_as_itself(self) -> None:
        """Возрастное ограничение — не бот-проверка, и путать их нельзя."""
        message = downloader.humanize_error(
            Exception(
                "ERROR: [youtube] abc: Sign in to confirm your age. "
                "This video may be inappropriate for some users."
            )
        )

        self.assertIn("возрастным ограничением", message)
        self.assertNotIn("не от робота", message)
        self.assertIn("cookies.txt", message)

    def test_own_cookie_file_goes_first(self) -> None:
        """Раз файл положили, значит обычные попытки уже не проходили."""
        with tempfile.TemporaryDirectory() as directory:
            cookies = Path(directory) / "cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File", encoding="utf-8")
            self.no_cookies.stop()
            self.addCleanup(self.no_cookies.start)
            with patch.object(
                youtube_access, "cookie_file", return_value=cookies
            ):
                names = [plan.name for plan in youtube_access.plans("youtube")]

        self.assertEqual(names[0], "cookies_file")

    def test_zoom_session_survives_a_youtube_style_overlay(self) -> None:
        """Добавка способа не должна затирать чужие настройки экстрактора."""
        base = {
            "cookiefile": "zoom.txt",
            "extractor_args": {"zoom": {"foo": ["bar"]}},
        }
        plan = next(
            plan
            for plan in youtube_access.plans("youtube")
            if plan.name.startswith("clients:")
        )

        merged = youtube_access.merge(base, plan)

        self.assertEqual(merged["cookiefile"], "zoom.txt")
        self.assertEqual(merged["extractor_args"]["zoom"], {"foo": ["bar"]})
        self.assertIn("youtube", merged["extractor_args"])
        # Исходный набор остаётся нетронутым: его переиспользуют следующие
        # попытки, и подмешанное однажды осталось бы в них навсегда.
        self.assertNotIn("youtube", base["extractor_args"])
