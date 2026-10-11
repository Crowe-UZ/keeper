"""Не качать лишнего: «Повторить» продолжает с места сбоя; текст и звук к уже скачанному видео берутся из файла."""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kachalka import download, files, history, outputs


class FakeYdl:
    def __init__(self):
        self.params = {"outtmpl": {"default": ""}}


class Resume(unittest.TestCase):
    def job(self, folder):
        return {"title": "Урок 5", "save_dir": folder, "mode": "video", "sniffed": True}

    def test_retry_keeps_the_file_name_so_ytdlp_continues_it(self):
        with tempfile.TemporaryDirectory() as d:
            job, ydl, names = self.job(d), FakeYdl(), []
            download.name_download(ydl, "j1", job, {}, 1, 1, True, names)
            first = ydl.params["outtmpl"]["default"]
            (files.temp_dir(d) / "Урок 5.mp4.part").write_bytes(b"half")  # загрузка оборвалась на половине
            download.name_download(ydl, "j1", job, {}, 1, 1, True, [])
            self.assertEqual(ydl.params["outtmpl"]["default"], first)  # а не «Урок 5 (2)»

    def test_another_job_with_the_same_title_gets_its_own_name(self):
        with tempfile.TemporaryDirectory() as d:
            ydl = FakeYdl()
            download.name_download(ydl, "j1", self.job(d), {}, 1, 1, True, [])
            (files.temp_dir(d) / "Урок 5.mp4.part").write_bytes(b"half")
            download.name_download(ydl, "j2", self.job(d), {}, 1, 1, True, [])
            self.assertEqual(ydl.params["outtmpl"]["default"], "Урок 5 (2).%(ext)s")

    def test_pieces_are_kept_after_a_failure_and_removed_after_cancel(self):
        failed = RuntimeError("обрыв связи")
        self.assertFalse(download.clean_after({"cancel": False}, failed))  # «Повторить» продолжит с этого места
        self.assertTrue(download.clean_after({"cancel": True}, failed))    # отменил — докачивать не будут
        self.assertTrue(download.clean_after({"cancel": False}, ([], None)))  # готово: временный звук больше не нужен


class LocalFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.folder = Path(self.dir.name)
        patch = mock.patch.object(history, "HISTORY_FILE", self.folder / "history.json")
        patch.start()
        self.addCleanup(patch.stop)

    def remember(self, want, file, url="https://site/lesson"):
        file.write_bytes(b"x")
        job = {"sniffed": False, "want": want, "url": url}
        history.remember({**job, "key": history.job_key(job)}, str(file))

    def job(self, want, **more):
        job = {"sniffed": False, "want": want, "url": "https://site/lesson", "save_dir": str(self.folder / "other"),
               "formats": outputs.clean_formats(None), "language": "ru", **more}
        return {**job, "key": history.job_key(job)}

    def test_downloaded_video_is_found_when_text_is_asked_later(self):
        video = self.folder / "Урок.mp4"
        self.remember(["video"], video)
        self.assertEqual(download.local_source(self.job(["text"])), video)
        self.assertEqual(download.local_source(self.job(["video", "text"])), video)
        self.assertEqual(download.local_source(self.job(["audio"])), video)

    def test_other_video_or_deleted_file_is_not_used(self):
        video = self.folder / "Урок.mp4"
        self.remember(["video"], video, url="https://site/another")
        self.assertIsNone(download.local_source(self.job(["text"])))
        self.remember(["video"], video)
        video.unlink()
        self.assertIsNone(download.local_source(self.job(["text"])))

    def test_audio_is_enough_for_text_but_not_for_video(self):
        audio = self.folder / "Урок.m4a"
        self.remember(["audio"], audio)
        self.assertEqual(download.local_source(self.job(["text"])), audio)
        self.assertIsNone(download.local_source(self.job(["video", "text"])))

    def test_text_file_is_not_a_source(self):
        self.remember(["text"], self.folder / "Урок.txt")
        self.assertIsNone(download.local_source(self.job(["text"], force=True)))
        self.assertIsNone(download.local_source(self.job(["audio"])))

    def test_download_again_ignores_local_files(self):
        self.remember(["video"], self.folder / "Урок.mp4")
        self.assertIsNone(download.local_source(self.job(["text"], force=True)))

    def test_text_is_written_next_to_the_video(self):
        video = self.folder / "Урок.mp4"
        video.write_bytes(b"x")
        with mock.patch.object(outputs, "transcribe", return_value=([(0.0, 2.0, "Привет всем.")], "ru")) as heard:
            made, note = outputs.from_local("j1", self.job(["video", "text"]), video, logging.getLogger("t"))
        self.assertEqual(heard.call_args.args[0], video)
        self.assertEqual(made, {"video": video, "text": self.folder / "Урок.txt"})
        self.assertEqual((self.folder / "Урок.txt").read_text(encoding="utf-8"), "Привет всем.\n")

    def test_audio_is_taken_out_of_the_video_without_downloading(self):
        video = self.folder / "Урок.mp4"
        video.write_bytes(b"x")
        with mock.patch.object(outputs, "extract_audio") as extract, mock.patch.object(outputs, "update"):
            made, _ = outputs.from_local("j1", self.job(["audio"]), video, logging.getLogger("t"))
        self.assertEqual(made, {"audio": self.folder / "Урок.m4a"})
        self.assertEqual(extract.call_args.args[:3], (video, self.folder / "Урок.m4a", "m4a"))

    def test_ready_audio_in_the_right_format_is_not_made_again(self):
        audio = self.folder / "Урок.m4a"
        audio.write_bytes(b"x")
        with mock.patch.object(outputs, "extract_audio") as extract, mock.patch.object(outputs, "update"):
            made, _ = outputs.from_local("j1", self.job(["audio"]), audio, logging.getLogger("t"))
        self.assertEqual(made, {"audio": audio})
        extract.assert_not_called()

    def test_history_survives_as_valid_json(self):
        self.remember(["video"], self.folder / "Урок.mp4")
        self.assertEqual(len(json.loads((self.folder / "history.json").read_text(encoding="utf-8"))), 1)


if __name__ == "__main__":
    unittest.main()
