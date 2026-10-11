"""Что остаётся после отмены и после грубого выхода: временная папка, cookies, дочерние процессы, недописанный звук."""
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yt_dlp

from kachalka import api, download, files, outputs
from kachalka.models import Cancelled
from kachalka.system import FFMPEG_DIR

ROOT = Path(__file__).resolve().parent.parent
DEAD = "999999999"  # процесса с таким номером нет


class OwnTempFolder(unittest.TestCase):
    def test_pieces_live_in_a_folder_with_our_own_name(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(files.temp_dir(d), Path(d) / ".keeper-tmp" / str(os.getpid()))

    def test_someone_elses_tmp_folder_is_left_alone(self):
        # .tmp в папке загрузок может быть чужой: раньше мы стирали в ней всё подряд
        with tempfile.TemporaryDirectory() as d:
            foreign = Path(d) / ".tmp"
            (foreign / "project").mkdir(parents=True)
            (foreign / "notes.txt").write_text("x")
            (foreign / DEAD).mkdir()  # а это наше, от прошлой версии: папка с номером процесса, которого нет
            files.clean_stale_temp([d])
            self.assertTrue((foreign / "project").is_dir())
            self.assertTrue((foreign / "notes.txt").exists())
            self.assertFalse((foreign / DEAD).exists())

    def test_our_old_empty_tmp_folder_disappears(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".tmp" / DEAD).mkdir(parents=True)
            files.clean_stale_temp([d])
            self.assertFalse((Path(d) / ".tmp").exists())

    def test_own_folder_is_cleaned_except_running_copies(self):
        with tempfile.TemporaryDirectory() as d:
            mine = files.temp_dir(d)
            (mine / "a.part").write_text("x")
            dead = mine.parent / DEAD
            dead.mkdir()
            (mine.parent / "loose.part").write_text("x")
            files.clean_stale_temp([d])
            self.assertTrue((mine / "a.part").exists())
            self.assertFalse(dead.exists())
            self.assertFalse((mine.parent / "loose.part").exists())


class Cookies(unittest.TestCase):
    COOKIE = [{"domain": ".example.com", "name": "sid", "value": "secret"}]

    def test_cookies_of_a_copy_that_died_are_removed_at_start(self):
        # после грубого выхода файл с cookies сайта оставался во временной папке Windows навсегда
        with tempfile.TemporaryDirectory() as d, mock.patch.object(files, "COOKIE_HOME", Path(d)):
            mine = Path(files.write_cookies(self.COOKIE))
            self.assertEqual(mine.parent, Path(d) / str(os.getpid()))
            left = Path(d) / DEAD
            left.mkdir()
            (left / "x.txt").write_text("secret")
            files.clean_stale_temp([])
            self.assertFalse(left.exists())
            self.assertTrue(mine.exists())
            files.clean_temp([])  # обычный выход: за собой убираем сами
            self.assertFalse(mine.exists())


class Folder(unittest.TestCase):
    def test_folder_we_can_write_to(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(files.writable(d))
            self.assertEqual(list(Path(d).iterdir()), [])  # проверка ничего после себя не оставляет

    def test_folder_that_does_not_exist(self):
        self.assertFalse(files.writable(Path(tempfile.gettempdir()) / "keeper-no-such-folder" / "x"))


class ChooseFolder(unittest.TestCase):
    def test_second_window_is_not_opened_while_the_first_is_on_screen(self):
        with api.choosing, mock.patch.object(api, "ask_folder") as ask:
            code, answer = api.choose_folder()
        self.assertEqual(code, 409)
        self.assertIn("уже открыто", answer["error"])
        ask.assert_not_called()

    def test_folder_we_cannot_write_to_is_refused(self):
        missing = str(Path(tempfile.gettempdir()) / "keeper-no-such-folder")
        with mock.patch.object(api, "ask_folder", return_value=missing), mock.patch.object(api, "save_settings") as save:
            before = api.state.save_dir
            code, answer = api.choose_folder()
        self.assertEqual((code, api.state.save_dir), (400, before))
        save.assert_not_called()

    def test_closing_the_window_changes_nothing(self):
        with mock.patch.object(api, "ask_folder", return_value=""), mock.patch.object(api, "save_settings") as save:
            code, answer = api.choose_folder()
        self.assertEqual((code, answer["changed"]), (200, False))
        save.assert_not_called()

    def test_chosen_folder_is_remembered(self):
        before = api.state.save_dir
        try:
            with tempfile.TemporaryDirectory() as d, mock.patch.object(api, "ask_folder", return_value=d), \
                    mock.patch.object(api, "save_settings") as save:
                code, answer = api.choose_folder()
                self.assertEqual((code, answer["changed"], api.state.save_dir), (200, True, Path(d)))
                save.assert_called_once_with(save_dir=d)
        finally:
            api.state.save_dir = before


@unittest.skipUnless(sys.platform == "win32", "только Windows")
class Children(unittest.TestCase):
    def test_child_processes_die_together_with_the_program(self):
        # ffmpeg, запущенный для склейки, переживал аварию программы и держал недописанный файл
        code = ("import os, subprocess\n"
                "from kachalka.children import tie_children\n"
                "assert tie_children()\n"
                "p = subprocess.Popen(['ping', '-n', '60', '127.0.0.1'], stdout=subprocess.DEVNULL)\n"
                "print(p.pid, flush=True)\n"
                "os._exit(1)\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=30)
        pid = int(out.stdout.strip())
        for _ in range(30):
            if not files.process_alive(pid):
                break
            time.sleep(0.1)
        alive = files.process_alive(pid)
        if alive:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        self.assertFalse(alive)


class CancelAfterDownload(unittest.TestCase):
    def job(self, folder, **more):
        return {"want": ["video", "audio"], "save_dir": folder, "formats": outputs.clean_formats(None), "language": "ru", **more}

    def test_cancel_before_audio_extraction_stops_the_job(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(outputs, "extract_audio") as extract:
            video = Path(d) / "Урок.mp4"
            video.write_bytes(b"x")
            with self.assertRaises(Cancelled):
                outputs.make_outputs("j1", self.job(d, cancel=True), video, None, logging.getLogger("t"))
            extract.assert_not_called()

    def test_cancel_between_processing_steps_of_ytdlp(self):
        # склейка и вынимание звука внутри yt-dlp идут уже после скачивания — обработчик прогресса там молчит
        hook = download.cancel_hook({"cancel": True})
        with self.assertRaises(yt_dlp.utils.DownloadCancelled):
            hook({"status": "started", "postprocessor": "Merger"})
        download.cancel_hook({"cancel": False})({"status": "started", "postprocessor": "Merger"})

    @unittest.skipUnless(FFMPEG_DIR, "нужен ffmpeg")
    def test_failed_extraction_leaves_no_half_written_file(self):
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "Урок.mp4"
            broken.write_bytes(b"not a video")
            with self.assertRaises(outputs.UserError):
                outputs.extract_audio(broken, Path(d) / "Урок.m4a")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir() if p.is_file()), ["Урок.mp4"])
            self.assertEqual(list(files.temp_dir(d).iterdir()), [])

    @unittest.skipUnless(FFMPEG_DIR, "нужен ffmpeg")
    def test_cancel_during_extraction_kills_ffmpeg_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d) / "Урок.mp4"
            media.write_bytes(b"not a video")
            started = time.time()
            with self.assertRaises(Cancelled):
                outputs.extract_audio(media, Path(d) / "Урок.mp3", "mp3", cancelled=lambda: True)
            self.assertLess(time.time() - started, 5)
            self.assertFalse((Path(d) / "Урок.mp3").exists())


if __name__ == "__main__":
    unittest.main()
