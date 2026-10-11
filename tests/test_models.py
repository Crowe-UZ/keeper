"""Модели распознавания: целая модель или нет, сверка скачанного, порядок действий при первой загрузке."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kachalka import models


def make_model(folder, complete=True):
    """Папка модели как после распаковки; complete=False — без отметки о завершении."""
    d = Path(folder) / models.MODELS["ru"]["dir"]
    d.mkdir(parents=True)
    for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.onnx", "tokens.txt"):
        (d / name).write_text("x")
    if complete:
        (d / models.READY).write_text("ok")
    return d


class Ready(unittest.TestCase):
    def test_complete_model_is_found(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(models, "MODELS_DIR", Path(tmp)):
            make_model(tmp)
            files = models.model_files("ru")
            self.assertTrue(files["encoder"].endswith("encoder.int8.onnx"))
            self.assertTrue(files["tokens"].endswith("tokens.txt"))

    def test_model_without_the_ready_mark_is_not_trusted(self):
        # так выглядит распаковка, прерванная посередине: файлы на месте, но могут быть обрезаны
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(models, "MODELS_DIR", Path(tmp)):
            make_model(tmp, complete=False)
            self.assertIsNone(models.model_files("ru"))

    def test_missing_part_means_no_model(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(models, "MODELS_DIR", Path(tmp)):
            (make_model(tmp) / "joiner.onnx").unlink()
            self.assertIsNone(models.model_files("ru"))


class Checksum(unittest.TestCase):
    def test_matching_file_passes_and_other_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "a.bin"
            f.write_bytes(b"hello")
            self.assertTrue(models.checksum_ok(f, hashlib.sha256(b"hello").hexdigest()))
            self.assertFalse(models.checksum_ok(f, hashlib.sha256(b"other").hexdigest()))

    def test_every_model_archive_has_a_checksum(self):
        for spec in models.MODELS.values():
            self.assertRegex(spec["sha256"], r"^[0-9a-f]{64}$")


class Resume(unittest.TestCase):
    def test_download_continues_from_what_is_already_on_disk(self):
        self.assertEqual(models.resume_headers(0), {})
        self.assertEqual(models.resume_headers(1500), {"Range": "bytes=1500-"})


if __name__ == "__main__":
    unittest.main()
