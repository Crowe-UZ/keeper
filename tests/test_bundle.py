"""Раскладка установщика: свой Python и свой ffmpeg лежат в папке программы; модели можно скачать заранее."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher
from kachalka import fetch_models, system
from kachalka.models import Cancelled


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


class OwnFfmpeg(unittest.TestCase):
    def test_bundled_ffmpeg_wins_over_the_system_one(self):
        # у человека может стоять старый или урезанный ffmpeg — берём тот, с которым программа проверена
        with tempfile.TemporaryDirectory() as d:
            exe = touch(Path(d) / "ffmpeg" / "bin" / "ffmpeg.exe")
            self.assertEqual(system.find_ffmpeg(Path(d)), str(exe.parent))

    def test_without_a_bundled_one_the_system_one_is_used(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(system.shutil, "which", return_value=r"C:\tools\ffmpeg.exe"):
            self.assertEqual(system.find_ffmpeg(Path(d)), r"C:\tools")


class OwnPython(unittest.TestCase):
    def test_installed_copy_runs_on_its_own_python(self):
        with tempfile.TemporaryDirectory() as d:
            exe = touch(Path(d) / "python" / "pythonw.exe")
            touch(Path(d) / ".venv" / "Scripts" / "pythonw.exe")
            self.assertEqual(launcher.runtime(Path(d), "pythonw.exe"), exe)

    def test_copy_from_the_repository_runs_on_its_environment(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(launcher.runtime(Path(d), "pythonw.exe"), Path(d) / ".venv" / "Scripts" / "pythonw.exe")


class ModelsAhead(unittest.TestCase):
    def run_cli(self, names, **patches):
        out = io.StringIO()
        with mock.patch.object(fetch_models, "ensure_vad", patches.get("vad", mock.Mock())), \
                mock.patch.object(fetch_models, "ensure_model", patches.get("model", mock.Mock())) as model:
            code = fetch_models.main(names, out)
        return code, out.getvalue(), model

    def test_chosen_models_are_downloaded(self):
        code, text, model = self.run_cli(["ru", "multi"])
        self.assertEqual(code, 0)
        self.assertEqual([c.args[0] for c in model.call_args_list], ["ru", "multi"])
        self.assertIn("GigaAM", text)

    def test_unknown_model_is_an_error_before_anything_is_downloaded(self):
        code, text, model = self.run_cli(["ru", "klingon"])
        self.assertEqual(code, 2)
        model.assert_not_called()

    def test_failed_download_is_reported_not_raised(self):
        # установка из-за этого не падает: модель скачается сама при первом запросе текста
        code, text, _ = self.run_cli(["ru"], model=mock.Mock(side_effect=OSError("нет сети")))
        self.assertEqual(code, 1)
        self.assertIn("нет сети", text)
        code, _, _ = self.run_cli(["ru"], model=mock.Mock(side_effect=Cancelled("стоп")))
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
