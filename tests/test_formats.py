"""Выбор форматов из настроек: каким будет видео, звук и текст, и что получается, когда вышло не всё."""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kachalka import api, download, outputs, transcribe

PIECES = [(0.0, 4.0, "Добрый день, друзья. Меня зовут Илья."), (4.2, 6.0, "Сегодня первое занятие."),
          (65.0, 5.0, "Вторая тема — рынок.")]


class Settings(unittest.TestCase):
    def test_defaults_are_the_formats_that_play_everywhere(self):
        self.assertEqual(outputs.clean_formats(None), {"video": "mp4", "audio": "m4a", "text": "txt"})

    def test_unknown_values_fall_back_to_defaults(self):
        self.assertEqual(outputs.clean_formats({"video": "avi", "audio": "mp3", "text": 5, "extra": 1}),
                         {"video": "mp4", "audio": "mp3", "text": "txt"})

    def test_request_carries_the_chosen_formats(self):
        job = api.parse_job({"url": "https://e.com/v", "formats": {"audio": "mp3", "text": "srt"}})
        self.assertEqual(job["formats"], {"video": "mp4", "audio": "mp3", "text": "srt"})
        self.assertEqual(api.parse_job({"url": "https://e.com/v"})["formats"]["video"], "mp4")

    def test_file_extensions_follow_the_choice(self):
        self.assertEqual(outputs.extension("audio", {"audio": "mp3"}), ".mp3")
        self.assertEqual(outputs.extension("audio", {"audio": "m4a"}), ".m4a")
        self.assertEqual(outputs.extension("text", {"text": "srt"}), ".srt")
        self.assertEqual(outputs.extension("text", {"text": "timed"}), ".txt")


class DownloadFormat(unittest.TestCase):
    def test_best_quality_video_takes_any_codec_and_goes_to_mkv(self):
        got = download.pick_format("video", 0, has_ffmpeg=True, formats={"video": "best"})
        self.assertEqual(got["merge_output_format"], "mkv")
        self.assertEqual(got["format_sort"], ["res", "fps"])  # без предпочтения «совместимых» кодеков

    def test_mp3_is_made_by_reencoding(self):
        got = download.pick_format("audio", 0, has_ffmpeg=True, formats={"audio": "mp3"})
        self.assertEqual(got["postprocessors"][0]["preferredcodec"], "mp3")

    def test_default_formats_did_not_change(self):
        self.assertEqual(download.pick_format("video", 0, has_ffmpeg=True)["merge_output_format"], "mp4")
        self.assertEqual(download.pick_format("audio", 0, has_ffmpeg=True)["postprocessors"][0]["preferredcodec"], "m4a")


class TextForms(unittest.TestCase):
    def test_plain_text_is_paragraphs(self):
        self.assertEqual(transcribe.render(PIECES, "txt"),
                         "Добрый день, друзья. Меня зовут Илья. Сегодня первое занятие.\n\nВторая тема — рынок.\n")

    def test_timed_text_marks_where_each_paragraph_starts(self):
        self.assertEqual(transcribe.render(PIECES, "timed"),
                         "[00:00] Добрый день, друзья. Меня зовут Илья. Сегодня первое занятие.\n\n[01:05] Вторая тема — рынок.\n")

    def test_hours_appear_only_when_needed(self):
        self.assertTrue(transcribe.render([(3725.0, 3.0, "Поздний кусок.")], "timed").startswith("[1:02:05] "))

    def test_subtitles_are_numbered_cues_with_times(self):
        srt = transcribe.render(PIECES, "srt")
        cues = srt.strip().split("\n\n")
        self.assertEqual(cues[0].split("\n")[0], "1")
        self.assertRegex(cues[0].split("\n")[1], r"^00:00:00,000 --> 00:00:0\d,\d{3}$")
        self.assertEqual(cues[-1].split("\n")[1], "00:01:05,000 --> 00:01:10,000")
        self.assertEqual(cues[-1].split("\n")[2], "Вторая тема — рынок.")

    def test_a_long_piece_becomes_several_short_cues_in_order(self):
        long = (0.0, 20.0, "Первое предложение довольно длинное и подробное. Второе предложение тоже не короткое, скажем прямо. "
                           "Третье завершает мысль.")
        cues = transcribe.render([long], "srt").strip().split("\n\n")
        self.assertEqual(len(cues), 3)
        times = [c.split("\n")[1] for c in cues]
        self.assertEqual(times[0].split(" --> ")[1], times[1].split(" --> ")[0])  # идут встык
        self.assertEqual(times[-1].split(" --> ")[1], "00:00:20,000")

    def test_nothing_recognised_gives_empty_text(self):
        for form in ("txt", "timed", "srt"):
            self.assertEqual(transcribe.render([], form), "")


class Partial(unittest.TestCase):
    """Видео уже скачано, а текст не получился — задача не должна считаться провалом целиком."""

    def job(self, folder, want):
        return {"want": want, "mode": outputs.download_mode(want), "save_dir": folder, "language": "ru",
                "formats": outputs.clean_formats(None), "cancel": False}

    def test_failed_text_keeps_the_video_and_says_why(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "Урок.mp4"
            video.write_text("x")
            with mock.patch.object(outputs, "recognise", side_effect=RuntimeError("нет сети для модели")), \
                    mock.patch.object(outputs, "update"):
                files, note = outputs.make_outputs("j1", self.job(d, ["video", "text"]), video, None, logging.getLogger("t"))
            self.assertEqual(files, {"video": video})
            self.assertIn("текст не получился", note)
            self.assertIn("нет сети для модели", note)

    def test_text_only_job_still_fails_when_text_fails(self):
        with tempfile.TemporaryDirectory() as d:
            audio = Path(d) / "j1-1.m4a"
            audio.write_text("x")
            with mock.patch.object(outputs, "recognise", side_effect=RuntimeError("сбой")), mock.patch.object(outputs, "update"):
                with self.assertRaises(RuntimeError):
                    outputs.make_outputs("j1", self.job(d, ["text"]), audio, "Урок", logging.getLogger("t"))

    def test_cancel_is_never_turned_into_partial_success(self):
        from kachalka.models import Cancelled
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "Урок.mp4"
            video.write_text("x")
            with mock.patch.object(outputs, "recognise", side_effect=Cancelled("отмена")), mock.patch.object(outputs, "update"):
                with self.assertRaises(Cancelled):
                    outputs.make_outputs("j1", self.job(d, ["video", "text"]), video, None, logging.getLogger("t"))

    def test_chosen_text_form_decides_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            video = Path(d) / "Урок.mp4"
            video.write_text("x")
            job = {**self.job(d, ["video", "text"]), "formats": outputs.clean_formats({"text": "srt"})}
            with mock.patch.object(outputs, "recognise", return_value=(PIECES, "ru")), mock.patch.object(outputs, "update"):
                files, note = outputs.make_outputs("j1", job, video, None, logging.getLogger("t"))
            self.assertEqual(files["text"], Path(d) / "Урок.srt")
            self.assertIn("-->", files["text"].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
