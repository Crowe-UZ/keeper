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


class Browsers(unittest.TestCase):
    def test_program_is_registered_for_every_supported_browser(self):
        # у каждого браузера своя запись в реестре, по которой он находит программу
        keys = launcher.host_keys()
        for part in (r"Google\Chrome", r"Microsoft\Edge", r"Yandex\YandexBrowser"):
            self.assertTrue(any(part in k for k in keys), part)
        self.assertTrue(all(k.endswith("NativeMessagingHosts\\" + launcher.HOST_NAME) for k in keys))

    def test_registration_writes_every_key(self):
        written = []

        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        fake = mock.Mock()
        fake.CreateKey.side_effect = lambda root, key: written.append(key) or FakeKey()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("sys.modules", {"winreg": fake}),                 mock.patch.object(launcher, "HERE", Path(d)), mock.patch.object(launcher, "extension_id", return_value="x" * 32):
            launcher.install()
            self.assertTrue((Path(d) / "native_host.json").is_file())
        self.assertEqual(written, launcher.host_keys())


class LastStepPage(unittest.TestCase):
    def test_page_shows_the_folder_the_user_actually_chose(self):
        # папку установки выбирает человек, в том числе с пробелами и кириллицей — на странице должна стоять она
        with tempfile.TemporaryDirectory() as d:
            here = Path(d) / "Мои программы" / "Keeper"
            here.mkdir(parents=True)
            (here / "next-steps.html").write_text("<code>{{EXTENSION_DIR}}</code>", encoding="utf-8")
            launcher.write_next_steps(here)
            self.assertEqual((here / "next-steps.html").read_text(encoding="utf-8"), f"<code>{here / 'extension'}</code>")

    def test_copy_from_the_repository_has_no_such_page(self):
        with tempfile.TemporaryDirectory() as d:
            launcher.write_next_steps(Path(d))  # страницы нет — ничего не делаем и не падаем
            self.assertEqual(list(Path(d).iterdir()), [])


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

    def test_installer_gets_plain_progress_lines(self):
        # установщик показывает проценты в своём окне: ему нужны короткие строки без русских букв и без повторов
        def model(name, on_progress):
            for percent in (0.4, 12.2, 12.9, 50.0, 100):
                on_progress(percent)

        out = io.StringIO()
        with mock.patch.object(fetch_models, "ensure_vad"), mock.patch.object(fetch_models, "ensure_model", model):
            code = fetch_models.main(["--lines", "ru"], out)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().split(), ["0", "12", "50", "100", "done"])
        self.assertTrue(out.getvalue().isascii())

    def test_plain_lines_report_a_failure(self):
        out = io.StringIO()
        with mock.patch.object(fetch_models, "ensure_vad"),                 mock.patch.object(fetch_models, "ensure_model", mock.Mock(side_effect=OSError("нет сети"))):
            code = fetch_models.main(["--lines", "ru"], out)
        self.assertEqual((code, out.getvalue().split()), (1, ["failed"]))


if __name__ == "__main__":
    unittest.main()
