"""Решения, которые принимает качалка до и после самого скачивания: формат, попытки, тексты ошибок."""
import unittest

from kachalka import download


class PickFormat(unittest.TestCase):
    def test_video_best_quality_merges_best_video_and_audio(self):
        self.assertEqual(download.pick_format("video", 0, has_ffmpeg=True)["format"], "bv*+ba/b")

    def test_video_is_merged_into_mp4(self):
        self.assertEqual(download.pick_format("video", 0, has_ffmpeg=True)["merge_output_format"], "mp4")

    def test_quality_cap_falls_back_to_best_when_nothing_fits(self):
        self.assertEqual(download.pick_format("video", 720, has_ffmpeg=True)["format"],
                         "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b")

    def test_without_ffmpeg_takes_a_ready_file_with_sound(self):
        self.assertEqual(download.pick_format("video", 0, has_ffmpeg=False), {"format": "b"})
        self.assertEqual(download.pick_format("video", 480, has_ffmpeg=False), {"format": "b[height<=480]/b"})

    def test_audio_is_converted_to_mp3_only_with_ffmpeg(self):
        with_ffmpeg = download.pick_format("audio", 720, has_ffmpeg=True)
        self.assertEqual(with_ffmpeg["format"], "bestaudio/best")
        self.assertEqual(with_ffmpeg["postprocessors"][0]["preferredcodec"], "mp3")
        self.assertEqual(download.pick_format("audio", 0, has_ffmpeg=False), {"format": "bestaudio/best"})


    def test_text_only_takes_the_sound_track_as_is(self):
        self.assertEqual(download.pick_format("text", 720, has_ffmpeg=True), {"format": "bestaudio/best"})


class JobKind(unittest.TestCase):
    def test_getcourse_player_link(self):
        self.assertEqual(download.job_kind("https://vh.example.com/sign-player/?json=abc"), "player")

    def test_getcourse_lesson_page(self):
        self.assertEqual(download.job_kind("https://school.example.com/pl/teach/control/lesson/view?id=1"), "lesson")

    def test_any_other_page(self):
        self.assertEqual(download.job_kind("https://www.youtube.com/watch?v=x"), "page")


class PlanAttempts(unittest.TestCase):
    def test_public_site_tries_without_cookies_first(self):
        self.assertEqual(download.plan_attempts({"sniffed": False}, "page", "c.txt"), [None, "c.txt"])

    def test_public_site_without_cookies_has_one_attempt(self):
        self.assertEqual(download.plan_attempts({"sniffed": False}, "page", None), [None])

    def test_caught_stream_goes_straight_with_cookies(self):
        self.assertEqual(download.plan_attempts({"sniffed": True}, "page", "c.txt"), ["c.txt"])

    def test_getcourse_goes_straight_with_cookies(self):
        self.assertEqual(download.plan_attempts({"sniffed": False}, "lesson", "c.txt"), ["c.txt"])
        self.assertEqual(download.plan_attempts({"sniffed": False}, "player", "c.txt"), ["c.txt"])


class Texts(unittest.TestCase):
    def test_friendly_error_names_the_cause(self):
        self.assertIn("DRM", download.friendly_error("This video is DRM protected"))
        self.assertIn("не нашлось видео", download.friendly_error("ERROR: Unsupported URL: https://x"))
        self.assertIn("не пустил", download.friendly_error("HTTP Error 403: Forbidden"))
        self.assertIn("ffmpeg", download.friendly_error("ffmpeg exited with code 1"))

    def test_unknown_error_is_passed_through_but_cut(self):
        self.assertEqual(download.friendly_error("x" * 500), "x" * 300)

    def test_eta(self):
        self.assertEqual(download.fmt_eta(None), "?")
        self.assertEqual(download.fmt_eta(45.7), "45 с")
        self.assertEqual(download.fmt_eta(125), "2 мин 05 с")


if __name__ == "__main__":
    unittest.main()
