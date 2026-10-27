import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp
from downloader import engine, media


class SoundSelectionTests(unittest.TestCase):
    def setUp(self):
        engine._PROBE_CACHE.clear()

    def tearDown(self):
        engine._PROBE_CACHE.clear()

    def test_720_selection_prefers_h264_and_aac_over_av1_and_opus(self):
        formats = [
            {"format_id":"aac", "vcodec":"none", "acodec":"mp4a.40.2", "ext":"m4a"},
            {"format_id":"opus", "vcodec":"none", "acodec":"opus", "ext":"webm"},
            {"format_id":"h264", "vcodec":"avc1.64001f", "acodec":"none", "height":720, "ext":"mp4"},
            {"format_id":"av1", "vcodec":"av01.0.05M.08", "acodec":"none", "height":720, "ext":"mp4"},
        ]
        for item in formats:
            item["url"] = "https://example.test/" + item["format_id"]
        with yt_dlp.YoutubeDL({"quiet":True}) as ydl:
            select = ydl.build_format_selector(engine._format_selector("720"))
            result = list(select({"formats":formats,"has_merged_format":False,"incomplete_formats":False}))[0]
        self.assertEqual([f["format_id"] for f in result["requested_formats"]], ["h264", "aac"])

    def test_720_is_not_downgraded_to_480_for_a_preferred_codec(self):
        formats = [
            {"format_id":"aac", "vcodec":"none", "acodec":"mp4a.40.2", "ext":"m4a"},
            {"format_id":"h264-480", "vcodec":"avc1.64001f", "acodec":"none", "height":480, "ext":"mp4"},
            {"format_id":"av1-720", "vcodec":"av01.0.05M.08", "acodec":"none", "height":720, "ext":"mp4"},
        ]
        for item in formats:
            item["url"] = "https://example.test/" + item["format_id"]
        with yt_dlp.YoutubeDL({"quiet":True}) as ydl:
            select=ydl.build_format_selector(engine._format_selector("720"))
            result=list(select({"formats":formats,"has_merged_format":False,"incomplete_formats":False}))[0]
        self.assertEqual(result["requested_formats"][0]["height"],720)

    def test_probe_does_not_advertise_video_only_resolution_as_with_sound(self):
        info = {"title":"Silent", "formats":[
            {"vcodec":"avc1", "acodec":"none", "height":720, "ext":"mp4"}]}
        with patch.object(engine, "_extract", return_value=info), patch.object(engine, "preferred_quality", return_value="720"):
            result = engine.probe_metadata("https://youtu.be/test-silent")
        self.assertFalse(result["audio_available"])
        self.assertFalse(result["qualities"][0]["audio_available"])
        self.assertIn("аудиодорожки", result["qualities"][0]["unavailable_reason"])
        with self.assertRaisesRegex(ValueError, "без звука"):
            engine.start_download("not-created", "https://youtu.be/test-silent", "720", result)

    def test_probe_reports_audio_from_a_separate_track(self):
        info = {"title":"Sound", "formats":[
            {"vcodec":"avc1", "acodec":"none", "height":720, "ext":"mp4"},
            {"vcodec":"none", "acodec":"mp4a.40.2", "ext":"m4a"}]}
        with patch.object(engine, "_extract", return_value=info), patch.object(engine, "preferred_quality", return_value="720"):
            result = engine.probe_metadata("https://youtu.be/test-sound")
        self.assertTrue(result["qualities"][0]["audio_available"])
        self.assertEqual(result["qualities"][0]["audio_source"], "separate")

    def test_original_file_without_codec_metadata_is_not_falsely_rejected_as_silent(self):
        info = {"title":"original.mp4", "formats":[
            {"format_id":"source", "url":"https://files.example.test/video.mp4", "ext":"mp4"}]}
        with patch.object(engine, "_extract", return_value=info):
            result = engine.probe_metadata("https://disk.360.yandex.ru/i/unknown-codecs")
        self.assertIsNone(result["audio_available"])
        self.assertEqual(result["qualities"], [])

    def test_final_path_is_merged_file_even_if_video_fragment_is_larger(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            video=root/'movie.f136.mp4'
            merged=root/'movie.mp4'
            video.write_bytes(b'v'*1000)
            merged.write_bytes(b'muxed')
            result=engine._resolve_final_path({"out_template":str(root/'movie.%(ext)s'),"filename":str(video)},
                    {"requested_downloads":[{"filepath":str(video)}]})
            self.assertEqual(result,merged)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RealSoundTests(unittest.TestCase):
    def create_clip(self, path, audio_codec=None):
        args=[shutil.which("ffmpeg"), "-nostdin", "-loglevel", "error", "-y",
              "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=2"]
        if audio_codec:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio_codec:
            args += ["-c:a", audio_codec, "-strict", "-2"]
        args += ["-shortest", str(path)]
        result=subprocess.run(args,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_real_opus_in_mp4_is_converted_to_aac_preserving_video(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'film.mp4'
            self.create_clip(path,'libopus')
            before=media.inspect_media(path)
            self.assertEqual(before['audio_streams'][0]['codec'],'opus')
            output,after=media.prepare_video(path)
            self.assertEqual(output,path)
            self.assertEqual(after['audio_streams'][0]['codec'],'aac')
            self.assertEqual(after['video_streams'][0]['codec'],'h264')
            self.assertAlmostEqual(after['duration_seconds'],before['duration_seconds'],delta=.1)
            # Decode the sound; stream metadata alone is insufficient.
            decoded=subprocess.run([shutil.which('ffmpeg'),'-nostdin','-v','error','-i',str(output),
                 '-map','0:a:0','-f','s16le','-'],capture_output=True)
            self.assertEqual(decoded.returncode,0,decoded.stderr)
            self.assertGreater(len(decoded.stdout),10000)
            self.assertGreater(max(decoded.stdout),0)

    def test_silent_video_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'silent.mp4'
            self.create_clip(path)
            with self.assertRaises(media.MediaValidationError):
                media.prepare_video(path)


if __name__ == '__main__':
    unittest.main()
