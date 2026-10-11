"""Что получить из одной загрузки: видео, звук, текст — в любом сочетании, по одному файлу на каждое.

Скачивание всегда одно: звук вынимается из уже скачанного видео, текст распознаётся с того же файла.
Если нужен только текст — качается одна звуковая дорожка во временную папку, после распознавания она удаляется.
"""
import subprocess
from .errors import UserError
from pathlib import Path

from .config import NO_WINDOW
from .state import stopping, update
from .models import Cancelled
from .system import FFMPEG_DIR
from .transcribe import render, transcribe

ORDER = ("video", "audio", "text")
LABELS = {"video": "видео", "audio": "звук", "text": "текст"}
# Форматы из настроек окна. Первый в каждой строке — по умолчанию.
#   видео: mp4 — играет везде (при равном разрешении H.264 + AAC); best — максимальное качество в любом кодеке, файл .mkv
#   звук:  m4a — без пережатия; mp3 — привычный, но это второе сжатие
#   текст: txt — абзацы; timed — абзацы со временем начала; srt — субтитры
FORMATS = {"video": ("mp4", "best"), "audio": ("m4a", "mp3"), "text": ("txt", "timed", "srt")}
EXTENSIONS = {"m4a": ".m4a", "mp3": ".mp3", "txt": ".txt", "timed": ".txt", "srt": ".srt"}


def clean_want(want):
    """Что просили — в постоянном порядке, без повторов и неизвестного; ничего не просили — значит видео."""
    asked = [w for w in ORDER if w in (want or [])]
    return asked or ["video"]


def download_mode(want):
    """Что качать, чтобы получить всё нужное одной загрузкой."""
    return next(w for w in ORDER if w in want)


def want_label(want):
    return " + ".join(LABELS[w] for w in ORDER if w in want)


def clean_formats(formats):
    """Выбор форматов из запроса: неизвестное и пропущенное заменяется форматом по умолчанию."""
    asked = formats if isinstance(formats, dict) else {}
    return {kind: asked.get(kind) if asked.get(kind) in allowed else allowed[0] for kind, allowed in FORMATS.items()}


def extension(kind, formats):
    """Расширение файла для звука или текста в выбранном формате."""
    return EXTENSIONS[clean_formats(formats)[kind]]


def free_path(folder, stem, ext):
    """Свободное имя файла с таким расширением: «Урок.txt», а если занято — «Урок (2).txt».
    Файлы с другим расширением не мешают: текст и должен лежать рядом со своим видео."""
    path, n = Path(folder) / f"{stem}{ext}", 1
    while path.exists():
        n += 1
        path = Path(folder) / f"{stem} ({n}){ext}"
    return path


def extract_audio(media, dest, form="m4a"):
    """Звук из скачанного видео — отдельным файлом. «m4a» — сначала без пережатия (мгновенно и без потери качества);
    если дорожка не AAC и в .m4a как есть не ложится — пережимаем в AAC. «mp3» — всегда пережатие."""
    ffmpeg = [str(Path(FFMPEG_DIR) / "ffmpeg"), "-v", "error", "-y", "-i", str(media), "-vn"]
    tries = [["-c:a", "libmp3lame", "-q:a", "2"]] if form == "mp3" else [["-c:a", "copy", "-movflags", "+faststart"],
                                                                         ["-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]]
    for codec in tries:
        r = subprocess.run([*ffmpeg, *codec, str(dest)], capture_output=True, text=True, timeout=3600, creationflags=NO_WINDOW)
        if not r.returncode and dest.exists() and dest.stat().st_size:
            return
    raise UserError("Не получилось вынуть звук из видео (нужен ffmpeg): " + r.stderr.strip()[-300:])


def recognise(job_id, job, media, ahead, lg):
    """Текст записи: из параллельного прохода, если он был и удался, иначе — распознаём скачанный файл."""
    if ahead:
        try:
            return ahead.wait()
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 — параллельный путь необязателен: любая его неудача записана и заменяется обычным путём
            lg.warning(f"параллельное распознавание не удалось ({e!r}) — распознаю из скачанного файла")

    def stage(name, percent):
        if not job.get("cancel"):  # после «Отмены» статус уже «отмена…» — не затираем его
            update(job_id, status=name, percent=percent, speed="", eta=None)

    return transcribe(media, job["language"], stage, lambda: job.get("cancel") or stopping.is_set())


def make_text(job_id, job, media, stem, lg, ahead=None):
    """Распознать речь и сохранить текст рядом с остальными файлами. Возвращает (путь или None, пояснение)."""
    pieces, model = recognise(job_id, job, media, ahead, lg)
    words = sum(len(text.split()) for _, _, text in pieces)
    if not words:
        lg.info(f"распознавание ({model}): речь не найдена")
        return None, "речи в видео не нашлось — текстового файла нет"
    path = free_path(job["save_dir"], stem, extension("text", job["formats"]))
    path.write_text(render(pieces, job["formats"]["text"]), encoding="utf-8")
    lg.info(f"текст сохранён: {path} ({words} слов, модель {model})")
    return path, f"текст готов, слов: {words}"


def make_outputs(job_id, job, media, stem, lg, ahead=None):
    """Из скачанного файла сделать всё, что просили. media — что скачалось; stem — имя для текста,
    когда скачивалась только временная звуковая дорожка. Возвращает ({вид: путь}, пояснение про текст)."""
    want, mode = job["want"], download_mode(job["want"])
    files, note = {}, ""
    if mode != "text":
        files[mode] = media
    if mode == "video" and "audio" in want:
        update(job_id, status="звук из видео", percent=0, speed="")
        files["audio"] = free_path(job["save_dir"], media.stem, extension("audio", job["formats"]))
        extract_audio(media, files["audio"], job["formats"]["audio"])
        lg.info(f"звук из видео: {files['audio']}")
    if "text" in want:
        try:
            path, note = make_text(job_id, job, media, stem or media.stem, lg, ahead)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001 — причина записана и показана; уже скачанное видео из-за текста не теряем
            if not files:  # просили только текст — тогда это провал всей задачи
                raise
            path, note = None, f"текст не получился: {str(e)[:200]}"
            lg.warning(f"видео и звук готовы, а текст не получился: {e!r}")
        if path:
            files["text"] = path
    return files, note
