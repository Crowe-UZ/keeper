"""Чужой компьютер: папки «Загрузки» может не быть на привычном месте, а сохранённая папка может пропасть."""
import tempfile
import unittest
from pathlib import Path

from kachalka import config


class SaveFolder(unittest.TestCase):
    def test_downloads_folder_is_asked_from_windows(self):
        # «Загрузки» переносят на другой диск и в OneDrive: C:\Users\имя\Downloads там может не быть вовсе
        folder = config.downloads_folder()
        self.assertTrue(folder.is_absolute())
        self.assertEqual(config.DEFAULT_SAVE_DIR, folder / "Videos")

    def test_first_folder_that_can_be_created_is_taken(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "file.txt"
            blocker.write_text("x")
            gone = blocker / "Videos"  # создать нельзя: на пути файл (как отключённый диск E:)
            good = Path(d) / "new" / "Videos"
            self.assertEqual(config.first_usable([gone, good]), good)
            self.assertTrue(good.is_dir())

    def test_program_still_starts_when_nothing_fits(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "file.txt"
            blocker.write_text("x")
            self.assertEqual(config.first_usable([blocker / "a", blocker / "b"]), config.ROOT / "downloads")

    def test_saved_folder_on_a_missing_drive_does_not_stop_the_start(self):
        self.assertTrue(config.load_save_dir().is_dir())


if __name__ == "__main__":
    unittest.main()
