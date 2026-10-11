"""Распознавание речи в аудио и видео — локально, без интернета (кроме первой загрузки модели).

Как устроено: ffmpeg отдаёт звук потоком -> детектор речи (VAD) режет его на куски по паузам ->
каждый кусок распознаёт модель -> куски собираются в текст с абзацами.
Русскую речь распознаёт GigaAM v3, английскую и другие европейские — Parakeet v3 (см. models.py).
"""
import re
import subprocess
import tempfile
import threading
from array import array
from pathlib import Path

from .config import NO_WINDOW, log
from .models import Cancelled, ensure_model, ensure_vad
from .system import FFMPEG_DIR

RATE = 16000  # обе модели и детектор речи ждут 16 кГц, один канал
VAD_WINDOW = 512  # детектор речи принимает звук порциями ровно такого размера
MAX_PIECE = 20  # секунд: длиннее модели распознают хуже, поэтому речь без пауз режем принудительно
SAMPLE_SPEECH = 20  # секунд речи, по которым определяем язык
PARAGRAPH_PAUSE = 1.5  # секунд тишины между кусками — и начинается новый абзац
PARAGRAPH_CHARS = 700  # абзац длиннее этого закрываем на ближайшем конце предложения

one_at_a_time = threading.Lock()  # распознавание занимает все ядра: два сразу только мешали бы друг другу
_loaded = {}  # имя модели -> распознаватель; держим один, чтобы не занимать память двумя


def guess_language(text):
    """«ru» — русская речь, «multi» — любая другая; None — слов нет, судить не по чему.
    Считаем слова, а не буквы: в русской лекции бывает много английских терминов, и они длинные.
    В английской речи кириллических слов нет вовсе, поэтому трети кириллицы достаточно, чтобы счесть речь русской.
    Украинскую (буквы і, ї, є, ґ) русской не считаем — её умеет многоязычная модель."""
    low = text.lower()
    cyr = len(re.findall(r"[а-яёіїєґ]+", low))
    lat = len(re.findall(r"[a-z]+", low))
    if not cyr and not lat:
        return None
    ukrainian = len(re.findall(r"[іїєґ]", low)) > 2
    return "ru" if cyr * 3 >= cyr + lat and not ukrainian else "multi"


def pick_model(language, sample_text):
    """Какой моделью распознавать: явный выбор пользователя либо язык пробного куска (не поняли — русская)."""
    if language in ("ru", "multi"):
        return language
    return guess_language(sample_text) or "ru"


def to_text(pieces):
    """Куски (начало, длительность, текст) -> текст абзацами: абзац кончается на долгой паузе
    или, если вырос слишком большим, на ближайшем конце предложения."""
    paragraphs, current, last_end = [], [], None
    for start, duration, text in pieces:
        text = text.strip()
        if not text:
            continue
        size = sum(len(t) + 1 for t in current)
        paused = last_end is not None and start - last_end > PARAGRAPH_PAUSE
        too_long = size > PARAGRAPH_CHARS and current[-1].endswith((".", "!", "?", "…"))
        if current and (paused or too_long):
            paragraphs.append(" ".join(current))
            current = []
        current.append(text)
        last_end = start + duration
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs) + "\n" if paragraphs else ""


def duration_of(media):
    """Длительность записи в секундах — для процентов; 0, если узнать не удалось."""
    try:
        out = subprocess.run([str(Path(FFMPEG_DIR) / "ffprobe"), "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(media)], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        return float(out.stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0


def read_audio(media, limit=None):
    """Звук из любого аудио или видео — порциями по 10 секунд, 16 кГц моно. limit — взять только первые секунды."""
    cmd = [str(Path(FFMPEG_DIR) / "ffmpeg"), "-nostdin", "-v", "error", "-i", str(media)]
    cmd += ["-t", str(limit)] if limit else []
    # Сообщения ffmpeg — во временный файл, а не в трубу: на битой записи их тысячи, труба переполняется,
    # ffmpeg встаёт на записи, мы — на чтении звука, и распознавание висит вечно.
    with tempfile.TemporaryFile() as errors:
        ff = subprocess.Popen(cmd + ["-vn", "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=errors, creationflags=NO_WINDOW)
        try:
            while raw := ff.stdout.read(RATE * 4 * 10):
                chunk = array("f")
                chunk.frombytes(raw[:len(raw) - len(raw) % 4])
                yield chunk
            ff.wait(timeout=30)
            if ff.returncode:
                errors.seek(0)
                raise RuntimeError("Не получилось прочитать звук из файла: " + errors.read().decode("utf-8", "replace")[-300:])
        finally:
            if ff.poll() is None:  # нас прервали посреди файла — ffmpeg дальше не нужен
                ff.kill()
            ff.stdout.close()


def speech_pieces(media, vad_path, limit=None, should_stop=lambda: False):
    """Куски с речью: (начало в секундах, отсчёты). Тишина и музыка без голоса в них не попадают."""
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = vad_path
    cfg.silero_vad.min_silence_duration = 0.4
    cfg.silero_vad.max_speech_duration = MAX_PIECE
    cfg.sample_rate = RATE
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=120)

    def ready():
        while not vad.empty():
            yield vad.front.start / RATE, vad.front.samples
            vad.pop()

    rest = array("f")
    for chunk in read_audio(media, limit):
        if should_stop():
            raise Cancelled("распознавание прервано")
        rest.extend(chunk)
        pos = 0
        while len(rest) - pos >= VAD_WINDOW:
            vad.accept_waveform(rest[pos:pos + VAD_WINDOW])
            pos += VAD_WINDOW
        rest = rest[pos:]
        yield from ready()
    vad.flush()
    yield from ready()


def recognizer(name, on_progress, should_stop):
    """Распознаватель для модели; модель при первом обращении скачивается. В памяти держим один."""
    if name not in _loaded:
        import sherpa_onnx
        files = ensure_model(name, on_progress, should_stop)
        _loaded.clear()
        _loaded[name] = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=files["encoder"], decoder=files["decoder"], joiner=files["joiner"], tokens=files["tokens"],
            num_threads=4, model_type="nemo_transducer", decoding_method="greedy_search")
    return _loaded[name]


def recognize(rec, samples):
    stream = rec.create_stream()
    stream.accept_waveform(RATE, samples)
    rec.decode_stream(stream)
    return stream.result.text.strip()


def detect_model(media, language, vad_path, on_progress, should_stop):
    """Какой моделью распознавать запись. При «авто» слушаем начало многоязычной моделью:
    русскую речь она пишет кириллицей — тогда берём русскую модель, она на русском точнее."""
    if language in ("ru", "multi"):
        return language
    rec = recognizer("multi", on_progress, should_stop)
    heard, sample = 0.0, []
    for _start, samples in speech_pieces(media, vad_path, limit=180, should_stop=should_stop):
        sample.append(recognize(rec, samples))
        heard += len(samples) / RATE
        if heard >= SAMPLE_SPEECH:
            break
    return pick_model("auto", " ".join(sample))


def wait_turn(on_stage, should_stop):
    """Дождаться своей очереди на распознавание. Ждём короткими шагами, чтобы «Отмена» действовала и в ожидании."""
    if one_at_a_time.acquire(blocking=False):
        return
    on_stage("ждёт распознавания", 0)
    while not one_at_a_time.acquire(timeout=0.5):
        if should_stop():
            raise Cancelled("распознавание прервано")


def transcribe(media, language="auto", on_stage=lambda stage, percent: None, should_stop=lambda: False):
    """Распознать речь в файле. Возвращает (текст абзацами, имя модели).
    on_stage(этап, процент) — «загрузка модели» или «распознавание»; should_stop() — пора прерваться."""
    try:
        import sherpa_onnx  # noqa: F401 — проверяем, что движок распознавания установлен, до долгой работы
    except ImportError as e:
        raise RuntimeError("Распознавание речи не установлено. Запусти install.bat в папке программы.") from e
    if not FFMPEG_DIR:
        raise RuntimeError("Для распознавания нужен ffmpeg — он достаёт звук из файла.")

    def loading(percent):
        on_stage("загрузка модели", percent)

    wait_turn(on_stage, should_stop)
    try:
        vad_path = ensure_vad(loading, should_stop)
        name = detect_model(media, language, vad_path, loading, should_stop)
        rec = recognizer(name, loading, should_stop)
        total = duration_of(media)
        on_stage("распознавание", 0)
        pieces = []
        for start, samples in speech_pieces(media, vad_path, should_stop=should_stop):
            length = len(samples) / RATE
            pieces.append((start, length, recognize(rec, samples)))
            on_stage("распознавание", round(min(99.0, (start + length) * 100 / total), 1) if total else 0)
    finally:
        one_at_a_time.release()
    log.debug(f"распознано кусков: {len(pieces)}, модель {name}, файл {Path(media).name}")
    return to_text(pieces), name
