"""Распознавание речи в аудио и видео — локально, без интернета (кроме первой загрузки модели).

Как устроено: ffmpeg отдаёт звук потоком -> детектор речи (VAD) режет его на куски по паузам ->
соседние куски склеиваются в отрезки до 22 секунд -> отрезок распознаёт модель -> текст собирается абзацами.
Русскую речь распознаёт GigaAM v3, английскую и другие европейские — Parakeet v3 (см. models.py).
"""
import os
from .errors import UserError
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
PARAGRAPH_LIMIT = 1500  # а длиннее этого — на любом стыке: сплошная речь без точек не должна быть одним абзацем
MAX_CHUNK = 22  # секунд: соседние куски речи склеиваем до такой длины (так советуют авторы GigaAM)
TOO_LONG = 28  # секунд: кусок длиннее делим на части — модели на такую длину не рассчитаны
PAD = 0.3  # секунд тишины оставляем между склеенными кусками
FORCED_CUT = 0.25  # секунд: стык без паузы — значит фразу разрезали принудительно, а не на её конце
TOO_SHORT = 0.6  # секунд: одинокий звук короче этого не распознаём — слов в нём нет
UNLOAD_AFTER = 300  # секунд без распознавания — и модель выгружается из памяти (это 0,3–0,9 ГБ)
SENTENCE_END = (".", "!", "?", "…")
FILLERS = {"м", "мм", "ммм", "мммм", "угу", "ага", "а", "э", "эм", "хм", "hm", "hmm", "mm", "mmm", "mmmm", "mhm",
           "yeah", "uh", "um", "oh", "ah"}

one_at_a_time = threading.Lock()  # распознавание занимает все ядра: два сразу только мешали бы друг другу
_loaded = {}  # имя модели -> распознаватель; держим один, чтобы не занимать память двумя
_unload_timer = None  # таймер выгрузки модели после простоя


def guess_language(text):
    """«ru» — русская речь, «multi» — любая другая; None — слов нет, судить не по чему.
    Считаем слова, а не буквы: в русской лекции бывает много английских терминов, и они длинные.
    В английской речи кириллических слов нет вовсе, поэтому трети кириллицы достаточно, чтобы счесть речь русской.
    Другую кириллицу русской не считаем — её умеет многоязычная модель: украинскую узнаём по буквам і, ї, є, ґ,
    болгарскую — по тому, что в длинном куске нет ни одной из букв ы, э, ё."""
    low = text.lower()
    cyr = len(re.findall(r"[а-яёіїєґ]+", low))
    lat = len(re.findall(r"[a-z]+", low))
    if not cyr and not lat:
        return None
    ukrainian = len(re.findall(r"[іїєґ]", low)) > 2
    bulgarian = cyr >= 15 and not re.search(r"[ыэё]", low)
    return "ru" if cyr * 3 >= cyr + lat and not ukrainian and not bulgarian else "multi"


def pick_model(language, sample_text):
    """Какой моделью распознавать: явный выбор пользователя либо язык пробного куска (не поняли — русская)."""
    if language in ("ru", "multi"):
        return language
    return guess_language(sample_text) or "ru"


def threads_for(cpus):
    """Сколько потоков дать модели: половина логических ядер, но не больше четырёх. На 12 потоках замер дал
    13× реального времени против 21× на четырёх; на двухъядерном ноутбуке четыре потока заняли бы всё."""
    return max(1, min(4, (cpus or 4) // 2))


def merge_pieces(pieces):
    """Склеить соседние куски речи в отрезки до MAX_CHUNK секунд. Детектор режет по каждой паузе, и в лекции
    выходят куски по 2–8 секунд; модели же готовили на отрезках по 15–22 секунды. На каждой лишней границе
    появлялись оборванное слово, лишняя точка и заглавная буква. Паузу между склеенными кусками сжимаем до PAD."""
    start, buffer, end = None, None, None
    for piece_start, samples in pieces:
        length = len(samples) / RATE
        if buffer is not None:
            gap = max(0.0, piece_start - end)
            pad = min(gap, PAD)
            if gap <= PARAGRAPH_PAUSE and len(buffer) / RATE + pad + length <= MAX_CHUNK:
                buffer.extend([0.0] * int(pad * RATE))
                buffer.extend(samples)
                end = piece_start + length
                continue
            yield start, buffer
        start, buffer, end = piece_start, list(samples), piece_start + length
    if buffer is not None:
        yield start, buffer


def split_long(samples):
    """Слишком длинный кусок (пение, речь поверх музыки: детектор не нашёл паузы) поделить на равные части
    не длиннее MAX_PIECE секунд. Обычный кусок возвращается как есть."""
    if len(samples) <= TOO_LONG * RATE:
        return [samples]
    parts = -(-len(samples) // (MAX_PIECE * RATE))  # деление с округлением вверх
    size = -(-len(samples) // parts)
    return [samples[i:i + size] for i in range(0, len(samples), size)]


def is_filler(text, seconds):
    """Одинокий короткий звук, принятый за слово: «Mmm.», «Угу.», «Yeah.». Так модели отвечают на вздох, щелчок
    или ноту. Внутри длинной речи такие слова не трогаем — там они настоящие."""
    return seconds < 1.5 and re.sub(r"[^a-zа-яё]", "", text.lower()) in FILLERS


def paragraphs(pieces):
    """Куски (начало, длительность, текст) -> абзацы (начало, текст). Абзац кончается на долгой паузе; если вырос
    слишком большим — на ближайшем конце предложения, а если концов предложений нет вовсе — на любом стыке.
    Кусок, который идёт вплотную за оборванной фразой, не начинает новое предложение с заглавной буквы."""
    done, current, began, last_end = [], [], 0.0, None
    for start, duration, text in pieces:
        text = text.strip()
        if not text:
            continue
        size = sum(len(t) + 1 for t in current)
        gap = None if last_end is None else start - last_end
        ended = bool(current) and current[-1].endswith(SENTENCE_END)
        paused = gap is not None and gap > PARAGRAPH_PAUSE
        too_long = (size > PARAGRAPH_CHARS and ended) or size > PARAGRAPH_LIMIT
        if current and (paused or too_long):
            done.append((began, " ".join(current)))
            current = []
        elif current and not ended and gap is not None and gap < FORCED_CUT:
            first = text.split()[0]
            if not (len(first.strip(".,!?…:;")) > 1 and first.isupper()):  # сокращение заглавными (JTBD) оставляем как есть
                text = text[0].lower() + text[1:]
        if not current:
            began = start
        current.append(text)
        last_end = start + duration
    if current:
        done.append((began, " ".join(current)))
    return done


def to_text(pieces):
    """Обычный текст абзацами."""
    parts = [text for _, text in paragraphs(pieces)]
    return "\n\n".join(parts) + "\n" if parts else ""


def stamp(seconds):
    """Время для человека: 01:05, а с часа записи — 1:02:05."""
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


def to_timed(pieces):
    """Текст абзацами, у каждого абзаца — время его начала: удобно искать место в записи."""
    parts = [f"[{stamp(start)}] {text}" for start, text in paragraphs(pieces)]
    return "\n\n".join(parts) + "\n" if parts else ""


def srt_time(seconds):
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def to_srt(pieces):
    """Субтитры .srt. Кусок речи длится до 22 секунд — для субтитра это много, поэтому он делится по предложениям,
    а время между ними распределяется по длине текста: точного времени каждого слова у нас нет."""
    cues = []
    for start, duration, text in pieces:
        sentences = [x for x in re.split(r"(?<=[.!?…])\s+", text.strip()) if x]
        total = sum(len(x) for x in sentences) or 1
        at = start
        for i, sentence in enumerate(sentences):
            end = start + duration if i == len(sentences) - 1 else at + duration * len(sentence) / total
            cues.append((at, end, sentence))
            at = end
    return "".join(f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n\n" for n, (a, b, text) in enumerate(cues, 1))


RENDER = {"txt": to_text, "timed": to_timed, "srt": to_srt}


def render(pieces, form):
    """Распознанные куски -> текст в выбранном виде: «txt» — абзацы, «timed» — абзацы со временем, «srt» — субтитры."""
    return RENDER.get(form, to_text)(pieces)


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
                raise UserError("Не получилось прочитать звук из файла: " + errors.read().decode("utf-8", "replace")[-300:])
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
    cfg.silero_vad.window_size = VAD_WINDOW  # для детектора v5/v6 обязателен именно такой размер
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
            num_threads=threads_for(os.cpu_count()), model_type="nemo_transducer", decoding_method="greedy_search")
    return _loaded[name]


def recognize(rec, samples):
    """Текст одного отрезка речи. Слишком длинный отрезок распознаётся по частям."""
    texts = []
    for part in split_long(samples):
        stream = rec.create_stream()
        stream.accept_waveform(RATE, part)
        rec.decode_stream(stream)
        texts.append(stream.result.text.strip())
    return " ".join(t for t in texts if t)


def unload_later():
    """Выгрузить модель из памяти, если распознавания не было UNLOAD_AFTER секунд. Программа висит в трее часами,
    и держать всё это время 0,3–0,9 ГБ незачем: загрузка модели заново занимает секунду-две."""
    global _unload_timer
    if _unload_timer:
        _unload_timer.cancel()

    def unload():
        if one_at_a_time.acquire(blocking=False):  # идёт распознавание — не трогаем, таймер взведут заново
            try:
                _loaded.clear()
            finally:
                one_at_a_time.release()

    _unload_timer = threading.Timer(UNLOAD_AFTER, unload)
    _unload_timer.daemon = True
    _unload_timer.start()


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
    """Распознать речь в файле. Возвращает (куски (начало, длительность, текст), имя модели);
    в текст нужного вида их превращает render().
    on_stage(этап, процент) — «загрузка модели» или «распознавание»; should_stop() — пора прерваться."""
    try:
        import sherpa_onnx  # noqa: F401 — проверяем, что движок распознавания установлен, до долгой работы
    except ImportError as e:
        raise UserError("Распознавание речи не установлено. Запусти install.bat в папке программы.") from e
    if not FFMPEG_DIR:
        raise UserError("Для распознавания нужен ffmpeg — он достаёт звук из файла.")

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
        for start, samples in merge_pieces(speech_pieces(media, vad_path, should_stop=should_stop)):
            length = len(samples) / RATE
            text = recognize(rec, samples) if length >= TOO_SHORT else ""
            if text and not is_filler(text, length):
                pieces.append((start, length, text))
            on_stage("распознавание", round(min(99.0, (start + length) * 100 / total), 1) if total else 0)
    finally:
        one_at_a_time.release()
        unload_later()
    if total > 30 and sum(len(text.split()) for _, _, text in pieces) < 3:
        pieces = []  # одно-два слова на всю длинную запись — это не речь, а случайный звук
    log.debug(f"распознано кусков: {len(pieces)}, модель {name}, файл {Path(media).name}")
    return pieces, name
