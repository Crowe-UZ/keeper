"""Текст параллельно с видео.

Когда нужны и видео, и текст, ждать конца большой загрузки незачем: звук — малая часть видео.
Пока основная загрузка качает видео, отдельный поток берёт самый лёгкий источник звука и распознаёт его.
Текст готов за минуту-другую независимо от размера видео, а место в очереди загрузок освобождается раньше.
Если параллельный путь не удался, текст распознаётся из уже скачанного видео — как раньше.
"""
import contextlib
import itertools
import threading
from pathlib import Path

import yt_dlp

from .files import temp_dir
from .joblog import YtdlpLog
from .models import Cancelled
from .state import stopping, update
from .system import FFMPEG_DIR, JS_RUNTIMES
from .transcribe import transcribe

# Для распознавания звук всё равно сводится к 16 кГц моно, поэтому берём самое лёгкое: отдельную дорожку до 80 кбит/с,
# иначе любую отдельную; если звук вшит в видео — самый лёгкий вариант видео, а не самый тяжёлый.
# «Худший звук» (wa) не подходит: yt-dlp ставит язык выше качества и берёт дублированную дорожку.
TEXT_FORMAT = "ba[abr<=80]/ba/w"
_serial = itertools.count(1)


class TextAhead:
    """Один параллельный проход «скачать лёгкий звук -> распознать». Результат забирают через wait()."""

    def __init__(self, job_id, job, cookie_file, extract, lg):
        self.job_id, self.job, self.cookie_file, self.extract, self.lg = job_id, job, cookie_file, extract, lg
        self.stem = f"{job_id}-text{next(_serial)}"  # имя временного звука в .tmp; своё у каждой попытки
        self.pieces = self.model = self.error = None
        self.stage = ("распознавание", 0)
        self.shown = False  # показывать этапы в окне начинаем, когда видео уже скачано: до того там идёт его прогресс
        self.dropped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def should_stop(self):
        return bool(self.dropped.is_set() or self.job.get("cancel") or stopping.is_set())

    def drop(self):
        """Попытка скачать видео не удалась — этот проход больше не нужен."""
        self.dropped.set()

    def on_stage(self, name, percent):
        self.stage = (name, percent)
        if self.shown and not self.job.get("cancel"):
            update(self.job_id, status=name, percent=percent, speed="", eta=None)

    def hook(self, _progress):
        if self.should_stop():
            raise yt_dlp.utils.DownloadCancelled("параллельное распознавание остановлено")

    def download(self):
        """Скачать лёгкий источник звука во временную папку и вернуть путь к нему."""
        tmp = temp_dir(self.job["save_dir"])
        opts = {
            "outtmpl": f"{self.stem}.%(ext)s",
            "paths": {"home": str(tmp), "temp": str(tmp)},
            "format": TEXT_FORMAT,
            "logger": YtdlpLog(self.lg),
            "noprogress": True,
            "progress_hooks": [self.hook],
            "noplaylist": True,
            "retries": 10,
            "fragment_retries": 10,
            "concurrent_fragment_downloads": 4,  # рядом основная загрузка качает видео с того же сервера — не жадничаем
            "http_headers": self.job["headers"],
        }
        optional = {"js_runtimes": JS_RUNTIMES, "cookiefile": self.cookie_file, "ffmpeg_location": FFMPEG_DIR}
        opts.update({k: v for k, v in optional.items() if v})
        with yt_dlp.YoutubeDL(opts) as ydl:
            result = ydl.process_ie_result(self.extract(ydl), download=True)
        path = ((result.get("requested_downloads") or [{}])[0].get("filepath")) or result.get("filepath")
        if not path or not Path(path).exists():
            raise RuntimeError("лёгкий звук для распознавания не скачался")
        return path

    def run(self):
        try:
            self.lg.info(f"текст параллельно с видео: качаю лёгкий звук ({TEXT_FORMAT})")
            media = self.download()
            self.pieces, self.model = transcribe(media, self.job["language"], self.on_stage, self.should_stop)
            self.lg.info(f"текст параллельно с видео: распознано, модель {self.model}")
        except BaseException as e:  # noqa: BLE001 — поток не должен умирать молча: ошибку забирает wait() и решает, что с ней делать
            self.error = e
        finally:
            for part in temp_dir(self.job["save_dir"]).glob(self.stem + ".*"):
                with contextlib.suppress(OSError):  # не удалился — уберёт общая уборка .tmp
                    part.unlink()

    def wait(self):
        """Дождаться результата: (распознанные куски, модель). С этого момента этапы распознавания видны в окне.
        Бросает Cancelled при отмене и исходную ошибку, если параллельный проход не удался."""
        self.shown = True
        self.on_stage(*self.stage)
        self.thread.join()
        if self.error:
            raise Cancelled("распознавание прервано") if self.should_stop() else self.error
        return self.pieces, self.model
