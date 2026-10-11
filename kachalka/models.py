"""Модели распознавания речи: какие бывают, где лежат, как скачиваются при первом использовании."""
import contextlib
import shutil
import tarfile
import urllib.request

from .config import ROOT, log

MODELS_DIR = ROOT / "models"
_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

# ru    — GigaAM v3 (Сбер, лицензия MIT): русская речь, сам ставит пунктуацию.
# multi — Parakeet TDT 0.6B v3 (NVIDIA, лицензия CC-BY-4.0): английский и ещё 24 европейских языка.
MODELS = {
    "ru": {"title": "GigaAM v3 (русский)", "size_mb": 170,
           "dir": "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16"},
    "multi": {"title": "Parakeet v3 (английский и европейские)", "size_mb": 487,
              "dir": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"},
}
VAD_FILE = "silero_vad.onnx"  # находит в записи куски с речью — по ним режем длинное аудио


class Cancelled(Exception):
    """Пользователь отменил задачу или качалка закрывается."""


def model_files(name):
    """Пути к частям модели, если она уже скачана целиком; иначе None."""
    d = MODELS_DIR / MODELS[name]["dir"]
    parts = {k: next(iter(sorted(d.glob(f"{k}*.onnx"))), None) for k in ("encoder", "decoder", "joiner")}
    tokens = d / "tokens.txt"
    if not all(parts.values()) or not tokens.exists():
        return None
    return {**{k: str(v) for k, v in parts.items()}, "tokens": str(tokens)}


def _fetch(url, dest, on_progress, should_stop):
    """Скачать файл по частям — с прогрессом и возможностью прервать. Сначала в .part, чтобы обрыв не оставил «готовый» файл."""
    part = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:  # адрес задан в коде, извне не приходит
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                if should_stop():
                    raise Cancelled("загрузка модели прервана")
                f.write(chunk)
                done += len(chunk)
                on_progress(round(done * 100 / total, 1) if total else 0)
        part.replace(dest)
    finally:
        with contextlib.suppress(OSError):  # при успехе .part уже переименован — удалять нечего
            part.unlink()


def ensure_vad(on_progress=lambda p: None, should_stop=lambda: False):
    path = MODELS_DIR / VAD_FILE
    if not path.exists():
        MODELS_DIR.mkdir(exist_ok=True)
        _fetch(_BASE + VAD_FILE, path, on_progress, should_stop)
    return str(path)


def ensure_model(name, on_progress=lambda p: None, should_stop=lambda: False):
    """Вернуть пути к файлам модели; при первом обращении — скачать и распаковать её."""
    files = model_files(name)
    if files:
        return files
    spec = MODELS[name]
    MODELS_DIR.mkdir(exist_ok=True)
    archive = MODELS_DIR / (spec["dir"] + ".tar.bz2")
    log.info(f"скачиваю модель распознавания {spec['title']}, около {spec['size_mb']} МБ")
    try:
        _fetch(_BASE + archive.name, archive, on_progress, should_stop)
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(MODELS_DIR, filter="data")  # filter="data": архив не может писать мимо папки моделей
    except BaseException:
        shutil.rmtree(MODELS_DIR / spec["dir"], ignore_errors=True)  # недораспакованную модель нельзя оставлять «готовой»
        raise
    finally:
        with contextlib.suppress(OSError):
            archive.unlink()
    files = model_files(name)
    if not files:
        raise RuntimeError(f"Модель {spec['title']} скачалась, но в архиве не оказалось нужных файлов.")
    log.info(f"модель {spec['title']} готова: {MODELS_DIR / spec['dir']}")
    return files
