"""Общее изменяемое состояние качалки: задачи, очередь, текущая папка, признак завершения."""
import queue
import threading
import time

from .config import ACTIVE, load_save_dir


jobs = {}  # id -> данные задачи (см. do_POST)
lock = threading.Lock()
tasks = queue.Queue()
stopping = threading.Event()  # качалка закрывается: загрузки прервать, новые не начинать
last_activity = time.time()  # когда расширение последний раз что-то спрашивало
tray = None
save_dir = load_save_dir()  # текущая папка (уже создана); меняется кнопкой «Сменить», задача запоминает свою


def update(job_id, **fields):
    with lock:
        jobs[job_id].update(fields)


def summary():
    with lock:
        st = [v["status"] for v in jobs.values()]
    active = sum(s in ACTIVE for s in st)
    return {"active": active, "queued": st.count("в очереди"),
            "done": sum(s.startswith("готово") for s in st), "failed": st.count("ошибка")}
