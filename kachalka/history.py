"""Что уже скачано — чтобы не качать второй раз."""
import json
import os
from datetime import datetime

from .config import (HISTORY_FILE)
from .state import lock


def job_key(job):
    """По чему узнаём то же самое видео. У пойманного потока ссылка каждый раз новая (токены),
    поэтому для него это страница + название. Что именно просили (видео, звук, текст) — тоже часть ключа:
    скачанное видео не мешает потом отдельно попросить текст."""
    what = "+".join(job["want"])
    if job["sniffed"]:
        return f"{what}|{job['page_url']}|{job['title']}"
    return f"{what}|{job['url']}"


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # истории ещё нет или файл испорчен
        return []


def remember(job, path):
    with lock:
        items = [h for h in load_history() if h.get("key") != job["key"]]
        items.append({"key": job["key"], "file": path, "time": datetime.now().isoformat(timespec="seconds")})
        HISTORY_FILE.write_text(json.dumps(items[-1000:], ensure_ascii=False, indent=1), encoding="utf-8")


def already_downloaded(key):
    """Файл, в который это видео уже скачано, — если он ещё лежит на месте."""
    for h in load_history():
        if h.get("key") == key and os.path.exists(h.get("file") or ""):
            return h["file"]
    return None
