"""Имена файлов, описание готового файла, ключ «это то же самое видео» и адреса, которые зовёт расширение."""
import os
import tempfile
import unittest
from pathlib import Path

from kachalka import api, files, history


class Names(unittest.TestCase):
    def test_forbidden_characters_are_replaced(self):
        self.assertEqual(files.safe_name('Урок 1: "Введение" / часть?'), "Урок 1_ _Введение_ _ часть_")

    def test_name_is_never_empty_and_never_too_long(self):
        self.assertEqual(files.safe_name(" . "), "video")
        self.assertEqual(len(files.safe_name("я" * 400)), 150)

    def test_glob_special_characters_are_escaped(self):
        self.assertEqual(files.glob_escape("Видео [1]*"), "Видео [[]1[]][*]")


class TempFolder(unittest.TestCase):
    def test_each_running_copy_keeps_its_pieces_in_its_own_folder(self):
        # иначе вторая запущенная копия при старте стирала недокачанное у первой
        with tempfile.TemporaryDirectory() as d:
            mine = files.temp_dir(d)
            self.assertEqual(mine, Path(d) / ".tmp" / str(os.getpid()))
            self.assertTrue(mine.is_dir())

    def test_cleanup_removes_only_folders_of_copies_that_are_gone(self):
        with tempfile.TemporaryDirectory() as d:
            mine = files.temp_dir(d)
            (mine / "a.part").write_text("x")
            dead = Path(d) / ".tmp" / "999999999"  # такого процесса нет
            dead.mkdir()
            (dead / "b.part").write_text("x")
            (Path(d) / ".tmp" / "old.part").write_text("x")  # кусок от версии, где папка была общей
            files.clean_stale_temp([d])
            self.assertTrue((mine / "a.part").exists())
            self.assertFalse(dead.exists())
            self.assertFalse((Path(d) / ".tmp" / "old.part").exists())


class Probe(unittest.TestCase):
    def test_describes_resolution_codecs_and_bitrate(self):
        info = {"streams": [{"codec_type": "video", "codec_name": "h264", "height": 720, "r_frame_rate": "30000/1001"},
                            {"codec_type": "audio", "codec_name": "aac"}],
                "format": {"bit_rate": "2676000"}}
        self.assertEqual(files.describe_probe(info), "720p30 h264 · aac · 2676 кбит/с")

    def test_no_probe_gives_empty_text(self):
        self.assertEqual(files.describe_probe(None), "")
        self.assertEqual(files.describe_probe({"error": "x"}), "")


class SameVideo(unittest.TestCase):
    def test_page_link_is_identified_by_url_and_mode(self):
        job = {"sniffed": False, "want": ["video"], "url": "https://youtu.be/x", "page_url": "p", "title": "t"}
        self.assertEqual(history.job_key(job), "video|https://youtu.be/x")
        self.assertNotEqual(history.job_key(job), history.job_key({**job, "want": ["audio"]}))
        self.assertNotEqual(history.job_key(job), history.job_key({**job, "want": ["video", "text"]}))

    def test_caught_stream_ignores_its_ever_changing_link(self):
        job = {"sniffed": True, "want": ["video"], "url": "https://cdn/master?token=1", "page_url": "https://s/lesson", "title": "Урок — видео 2"}
        self.assertEqual(history.job_key(job), history.job_key({**job, "url": "https://cdn/master?token=2"}))
        self.assertNotEqual(history.job_key(job), history.job_key({**job, "title": "Урок — видео 1"}))


class Routes(unittest.TestCase):
    def test_every_address_the_extension_calls_is_served(self):
        called = {"/status", "/open-folder", "/choose-folder", "/open-file", "/show-file", "/open-logs", "/open-log",
                  "/clear", "/cancel", "/retry"}
        self.assertEqual(called - set(api.ROUTES), set())


if __name__ == "__main__":
    unittest.main()
