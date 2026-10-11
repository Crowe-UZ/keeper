"""Модели распознавания речи: какие бывают, где лежат, как скачиваются при первом использовании."""
import contextlib
from .errors import UserError
import hashlib
import shutil
import tarfile
import time
import urllib.error
import urllib.request

from .config import ROOT, log

MODELS_DIR = ROOT / "models"
_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
READY = ".ready"  # отметка внутри папки модели: распаковка дошла до конца. Без неё модель считается недокачанной

# ru    — GigaAM v3 (Сбер, лицензия MIT): русская речь, сам ставит пунктуацию.
# multi — Parakeet TDT 0.6B v3 (NVIDIA, лицензия CC-BY-4.0): английский и ещё 24 европейских языка.
# sha256 — контрольные суммы архивов из данных релиза на GitHub (сверено 2026-10-11).
MODELS = {
    "ru": {"title": "GigaAM v3 (русский)", "size_mb": 170,
           "dir": "sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16",
           "sha256": "f9620a0099019c6afcee26525ef9ed3297fa50dd5691c1902af0c948fc1a470b"},
    "multi": {"title": "Parakeet v3 (английский и европейские)", "size_mb": 487,
              "dir": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8",
              "sha256": "5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf"},
}
# Детектор речи Silero v6 (лицензия MIT): находит в записи куски с речью — по ним режем длинное аудио.
# Раньше брался v4 из набора sherpa-onnx; v6 заметно лучше отличает шум и музыку от голоса (данные авторов).
VAD_FILE = "silero_vad_v6.onnx"
VAD_URL = "https://github.com/snakers4/silero-vad/raw/v6.2.3/src/silero_vad/data/silero_vad.onnx"
VAD_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"  # сверено скачиванием 2026-10-11
TRIES = 3  # сколько раз продолжать загрузку после обрыва связи


class Cancelled(Exception):
    """Пользователь отменил задачу или качалка закрывается."""


def model_files(name):
    """Пути к частям модели, если она скачана и распакована целиком; иначе None."""
    d = MODELS_DIR / MODELS[name]["dir"]
    parts = {k: next(iter(sorted(d.glob(f"{k}*.onnx"))), None) for k in ("encoder", "decoder", "joiner")}
    tokens = d / "tokens.txt"
    if not (d / READY).exists() or not all(parts.values()) or not tokens.exists():
        return None
    return {**{k: str(v) for k, v in parts.items()}, "tokens": str(tokens)}


def checksum_ok(path, expected):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest() == expected


def resume_headers(have):
    """Заголовок «продолжить с этого места», если часть файла уже скачана."""
    return {"Range": f"bytes={have}-"} if have else {}


def _fetch_once(url, part, on_progress, should_stop):
    """Одна попытка: дописать в .part то, чего в нём ещё нет."""
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url, headers=resume_headers(have))  # адрес задан в коде, извне не приходит
    with urllib.request.urlopen(req, timeout=60) as r:
        if have and r.status != 206:  # сервер не умеет продолжать — начинаем файл заново
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        with open(part, "ab" if have else "wb") as f:
            while chunk := r.read(1 << 20):
                if should_stop():
                    raise Cancelled("загрузка модели прервана")
                f.write(chunk)
                have += len(chunk)
                on_progress(round(have * 100 / total, 1) if total else 0)


def _fetch(url, dest, on_progress, should_stop):
    """Скачать файл с прогрессом и возможностью прервать. Пишем в .part и после обрыва связи продолжаем
    с того же места: модель в сотни мегабайт не должна начинаться с нуля."""
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, TRIES + 1):
        try:
            _fetch_once(url, part, on_progress, should_stop)
            break
        except urllib.error.HTTPError as e:
            if e.code == 416:  # «дальше качать нечего» — файл уже целый
                break
            if attempt == TRIES:
                raise
        except OSError as e:  # обрыв связи, таймаут
            if attempt == TRIES:
                raise
            log.warning(f"загрузка {dest.name} оборвалась ({e!r}) — продолжаю, попытка {attempt + 1} из {TRIES}")
        time.sleep(2 * attempt)
    part.replace(dest)


def ensure_vad(on_progress=lambda p: None, should_stop=lambda: False):
    path = MODELS_DIR / VAD_FILE
    if not path.exists():
        MODELS_DIR.mkdir(exist_ok=True)
        _fetch(VAD_URL, path, on_progress, should_stop)
        if not checksum_ok(path, VAD_SHA256):
            path.unlink()
            raise UserError("Детектор речи скачался с ошибкой (не сошлась контрольная сумма). Нажми «Повторить».")
    return str(path)


def _unpack(archive, spec):
    """Распаковать архив модели. Сначала во временную папку рядом, потом переименовать: прерванная распаковка
    (выход из программы, выключение питания) не оставляет папку, похожую на готовую модель."""
    target, partial = MODELS_DIR / spec["dir"], MODELS_DIR / (spec["dir"] + ".partial")
    shutil.rmtree(partial, ignore_errors=True)
    shutil.rmtree(target, ignore_errors=True)
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(partial, filter="data")  # filter="data": архив не может писать мимо своей папки
    inner = partial / spec["dir"]  # в архиве модель лежит в папке со своим именем
    (inner / READY).write_text("ok", encoding="utf-8")
    inner.replace(target)
    shutil.rmtree(partial, ignore_errors=True)


def ensure_model(name, on_progress=lambda p: None, should_stop=lambda: False):
    """Вернуть пути к файлам модели; при первом обращении — скачать, сверить и распаковать её."""
    files = model_files(name)
    if files:
        return files
    spec = MODELS[name]
    MODELS_DIR.mkdir(exist_ok=True)
    archive = MODELS_DIR / (spec["dir"] + ".tar.bz2")
    log.info(f"скачиваю модель распознавания {spec['title']}, около {spec['size_mb']} МБ")
    _fetch(_BASE + archive.name, archive, on_progress, should_stop)
    try:
        if not checksum_ok(archive, spec["sha256"]):
            raise UserError(f"Модель {spec['title']} скачалась с ошибкой (не сошлась контрольная сумма). Нажми «Повторить».")
        on_progress(100)
        _unpack(archive, spec)
    finally:
        with contextlib.suppress(OSError):  # архив больше не нужен: целый распакован, битый надо качать заново
            archive.unlink()
    files = model_files(name)
    if not files:
        raise UserError(f"Модель {spec['title']} скачалась, но в архиве не оказалось нужных файлов.")
    log.info(f"модель {spec['title']} готова: {MODELS_DIR / spec['dir']}")
    return files
