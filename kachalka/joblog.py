"""Лог одной задачи: отдельный файл на каждую загрузку, вывод yt-dlp, cookies без значений."""
import logging
from datetime import datetime

from .config import (JOB_LOG_DIR, LOG_FORMAT)
from .state import jobs, lock


def job_logger(job_id):
    lg = logging.getLogger(f"job.{job_id}")
    if not lg.handlers:
        path = JOB_LOG_DIR / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{job_id}.log"
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(LOG_FORMAT)
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        with lock:
            jobs[job_id]["log_file"] = str(path)
    return lg


def close_job_logger(job_id):
    lg = logging.getLogger(f"job.{job_id}")
    for h in list(lg.handlers):
        h.close()
        lg.removeHandler(h)


class YtdlpLog:
    """Весь вывод yt-dlp (включая отладочный) — в лог задачи."""

    def __init__(self, lg):
        self.lg = lg

    def debug(self, msg):
        # yt-dlp шлёт сюда и обычный вывод; строки прогресса не нужны — их тысячи
        if not msg.startswith("[download]  ") or "Destination" in msg:
            self.lg.debug("yt-dlp: " + msg)

    def info(self, msg):
        self.lg.info("yt-dlp: " + msg)

    def warning(self, msg):
        self.lg.warning("yt-dlp: " + msg)

    def error(self, msg):
        self.lg.error("yt-dlp: " + msg)


def redact_cookies(cookies):
    """Для лога: только домен и имя cookie, значение — никогда."""
    return sorted({f"{c.get('domain')}:{c.get('name')}" for c in cookies})
