"""Решения, которые принимает качалка до и после самого скачивания: формат, попытки, тексты ошибок."""
import logging
import tempfile
import unittest

from kachalka import download


class PickFormat(unittest.TestCase):
    def test_video_takes_best_video_and_audio_and_merges_into_mp4(self):
        got = download.pick_format("video", 0, has_ffmpeg=True)
        self.assertEqual(got["format"], "bv*+ba/b")
        self.assertEqual(got["merge_output_format"], "mp4")

    def test_resolution_comes_first_then_codecs_every_player_understands(self):
        # при равном разрешении — H.264 + AAC: такой mp4 играет везде, а не только в браузере
        self.assertEqual(download.pick_format("video", 0, has_ffmpeg=True)["format_sort"],
                         ["res", "fps", "hdr:12", "vcodec:h264", "acodec:m4a"])

    def test_quality_cap_is_a_preference_not_a_filter(self):
        # «не выше 720», а если такого нет — ближайшее сверху; считается по меньшей стороне, так что вертикальное видео не путает
        got = download.pick_format("video", 720, has_ffmpeg=True)
        self.assertEqual(got["format"], "bv*+ba/b")
        self.assertEqual(got["format_sort"][0], "res:720")

    def test_without_ffmpeg_takes_a_ready_file_with_sound(self):
        self.assertEqual(download.pick_format("video", 0, has_ffmpeg=False), {"format": "b", "format_sort": ["res"]})
        self.assertEqual(download.pick_format("video", 480, has_ffmpeg=False), {"format": "b", "format_sort": ["res:480"]})

    def test_page_video_gets_title_and_description_written_into_the_file(self):
        self.assertEqual(download.pick_format("video", 0, has_ffmpeg=True)["postprocessors"][0]["key"], "FFmpegMetadata")

    def test_caught_stream_gets_no_metadata(self):
        # у пойманного потока «описание» — это служебная ссылка плеера с номером пользователя и его адресом
        self.assertNotIn("postprocessors", download.pick_format("video", 0, has_ffmpeg=True, private=True))

    def test_audio_prefers_a_separate_track_and_never_the_heaviest_video(self):
        got = download.pick_format("audio", 720, has_ffmpeg=True)
        self.assertEqual(got["format"], "ba[acodec^=mp4a]/ba/b[height<=480]/b[height<=720]/w")
        self.assertEqual(got["postprocessors"][0], {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"})

    def test_audio_without_ffmpeg_is_saved_as_downloaded(self):
        self.assertNotIn("postprocessors", download.pick_format("audio", 0, has_ffmpeg=False))

    def test_text_only_takes_the_lightest_sound_source(self):
        # отдельная лёгкая звуковая дорожка, а если звук вшит в видео — самый лёгкий вариант видео, а не самый тяжёлый
        self.assertEqual(download.pick_format("text", 720, has_ffmpeg=True), {"format": "ba[abr<=80]/ba/w"})


class Connections(unittest.TestCase):
    def test_one_download_uses_all_sixteen_connections(self):
        self.assertEqual(download.fragments_for(1), 16)
        self.assertEqual(download.fragments_for(0), 16)

    def test_parallel_downloads_share_the_budget(self):
        # три загрузки по 16 соединений к одному серверу кончались обрывами — делим общий запас
        self.assertEqual(download.fragments_for(2), 8)
        self.assertEqual(download.fragments_for(3), 5)

    def test_never_fewer_than_four(self):
        self.assertEqual(download.fragments_for(10), 4)

    def test_pause_before_retry_grows_but_stays_short(self):
        self.assertEqual([download.retry_pause(n) for n in (1, 2, 3, 6)], [2, 4, 8, 10])

    def test_pause_is_called_the_way_yt_dlp_calls_it(self):
        # yt-dlp зовёт функцию паузы с именованным аргументом n — другое имя роняло загрузку при первом же обрыве куска
        self.assertEqual(download.retry_pause(n=2), 4)
        job = {"mode": "video", "max_height": 0, "sniffed": True, "url": "https://e.com/v.m3u8", "title": "t",
               "save_dir": tempfile.gettempdir(), "headers": {}, "want": ["video"]}
        pause = download.build_opts("x", job, None, logging.getLogger("t"))["retry_sleep_functions"]["fragment"]
        self.assertEqual(pause(n=1), 2)
        job["cancel"] = True
        self.assertEqual(pause(n=5), 0)  # после «Отмены» ждать незачем


class TextAhead(unittest.TestCase):
    def test_video_plus_text_recognises_in_parallel(self):
        self.assertTrue(download.text_ahead({"mode": "video", "want": ["video", "text"]}, "page"))
        self.assertTrue(download.text_ahead({"mode": "video", "want": ["video", "audio", "text"]}, "player"))

    def test_nothing_to_run_in_parallel_without_video_or_without_text(self):
        self.assertFalse(download.text_ahead({"mode": "video", "want": ["video"]}, "page"))
        self.assertFalse(download.text_ahead({"mode": "audio", "want": ["audio", "text"]}, "page"))
        self.assertFalse(download.text_ahead({"mode": "text", "want": ["text"]}, "page"))

    def test_lesson_page_with_several_players_stays_sequential(self):
        self.assertFalse(download.text_ahead({"mode": "video", "want": ["video", "text"]}, "lesson"))


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
        self.assertIn("защищено от копирования", download.friendly_error("This video is DRM protected"))
        self.assertIn("Не нашёл видео", download.friendly_error("ERROR: Unsupported URL: https://x"))
        self.assertIn("не отдал видео", download.friendly_error("HTTP Error 403: Forbidden"))
        self.assertIn("ffmpeg", download.friendly_error("ffmpeg exited with code 1"))

    def test_our_own_message_about_missing_ffmpeg_is_not_replaced(self):
        ours = "Для распознавания нужен ffmpeg — он достаёт звук из файла."
        self.assertEqual(download.friendly_error(ours), ours)

    def test_unknown_error_is_passed_through_but_cut(self):
        self.assertEqual(download.friendly_error("x" * 500), "x" * 300)

    def test_eta(self):
        self.assertEqual(download.fmt_eta(None), "?")
        self.assertEqual(download.fmt_eta(45.7), "45 с")
        self.assertEqual(download.fmt_eta(125), "2 мин 05 с")


if __name__ == "__main__":
    unittest.main()
