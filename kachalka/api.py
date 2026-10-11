"""HTTP для расширения: приём задач, состояние очереди, кнопки окна."""
import contextlib
import json
import logging
import os
import subprocess
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from yt_dlp.version import __version__ as YTDLP_VERSION
from .config import (FINAL, HEIGHTS, LOG_DIR, log, save_settings)
from . import state
from .state import jobs, lock, stopping, summary, tasks
from .history import already_downloaded, job_key
from .outputs import clean_want, download_mode, want_label
from .system import FFMPEG_DIR
from .windows import open_in_front, open_path


MAX_BODY = 5 * 1024 * 1024  # запрос на скачивание — это ссылка и cookies вкладки; больше 5 МБ — не наш запрос


def parse_job(data):
    """Проверить запрос на скачивание и привести к виду задачи. Бросает ValueError с понятной причиной —
    задача с кривыми полями не должна попадать в очередь и падать уже в обработчике."""
    if not isinstance(data, dict):
        raise ValueError("ожидался объект с полями задачи")
    url = data.get("url")
    url = url.strip() if isinstance(url, str) else ""
    if not url.startswith(("http://", "https://")):
        raise ValueError("нужна ссылка http(s)")
    headers, cookies = data.get("headers") or {}, data.get("cookies") or []
    if not isinstance(headers, dict) or not isinstance(cookies, list):
        raise ValueError("заголовки или cookies не того вида")
    for c in cookies:
        if not isinstance(c, dict) or not all(isinstance(c.get(k), str) for k in ("domain", "name", "value")):
            raise ValueError("cookie без домена, имени или значения")
    asked = data.get("want") if isinstance(data.get("want"), list) else [data.get("mode")]  # mode — расширение до версии 3.4
    want = clean_want(asked)
    return {
        "url": url,
        "title": str(data.get("title") or url),
        "page_url": str(data.get("page_url") or ""),
        "want": want,
        "mode": download_mode(want),
        "sniffed": bool(data.get("sniffed")),
        "max_height": data.get("max_height") if data.get("max_height") in HEIGHTS else 0,
        "language": data.get("language") if data.get("language") in ("ru", "multi") else "auto",
        "headers": {k: v for k, v in headers.items() if k in ("Referer", "Origin", "User-Agent") and isinstance(v, str)},
        "cookies": cookies,
        "debug": data.get("debug") if isinstance(data.get("debug"), dict) else {},
        "force": bool(data.get("force")),  # «Скачать ещё раз» в окне расширения
    }


PUBLIC_FIELDS = ("url", "title", "status", "percent", "speed", "eta", "done_mb", "quality", "error", "mode", "want")


class Handler(BaseHTTPRequestHandler):
    def _allowed(self):
        # Сайты не должны иметь возможность дёргать качалку — только расширение.
        # Chrome шлёт Origin расширения не во всех запросах (в GET его нет), поэтому расширение
        # всегда добавляет заголовок X-Kachalka. Сайт такой заголовок без preflight не пошлёт,
        # а preflight (OPTIONS) от чужого Origin мы отклоняем.
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith("chrome-extension://"):
            log.warning(f"отклонён запрос {self.command} {self.path} от чужого сайта Origin={origin!r}")
            return False
        if self.command != "OPTIONS" and self.headers.get("X-Kachalka") != "1":
            log.warning(f"отклонён запрос {self.command} {self.path}: нет заголовка расширения (Origin={origin!r})")
            return False
        state.last_activity = time.time()
        return True

    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Kachalka")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204 if self._allowed() else 403, {})

    def do_GET(self):
        self._guard(self._get)

    def _get(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        handler = ROUTES.get(path)
        if not handler:
            return self._send(404, {"error": "not found"})
        getattr(self, handler)(path, query.removeprefix("id="))

    def _guard(self, handler):
        """Сбой при обработке запроса — в журнал и понятным ответом, а не обрывом соединения без следа."""
        try:
            handler()
        except Exception:  # noqa: BLE001 — верх обработчика запроса: любой сбой записан и возвращён расширению как ошибка 500
            log.exception(f"сбой при обработке запроса {self.command} {self.path}")
            with contextlib.suppress(OSError):  # соединение могло уже закрыться
                self._send(500, {"error": "Внутренний сбой программы — подробности в журнале (кнопка «Логи»)."})

    def _ok(self):
        self._send(200, {"ok": True})

    def _status(self, _path, _job_id):
        with lock:
            items = [{"id": k, "has_log": bool(v.get("log_file")), "has_file": bool(v.get("file")), **{f: v[f] for f in PUBLIC_FIELDS}} for k, v in jobs.items()]
        self._send(200, {"jobs": items[::-1][:30], "summary": summary(), "folder": str(state.save_dir), "ffmpeg": bool(FFMPEG_DIR),
                         "ytdlp": YTDLP_VERSION})

    def _open_folder(self, _path, _job_id):
        open_path(state.save_dir)
        self._ok()

    def _open_logs(self, _path, _job_id):
        open_path(LOG_DIR)
        self._ok()

    def _open_file(self, path, job_id):
        job = jobs.get(job_id)
        f = job and job.get("file")
        if not f or not os.path.exists(f):
            return self._send(404, {"error": "файл не найден — его переместили или удалили"})
        if path == "/open-file":
            open_path(f)  # откроется в плеере по умолчанию
        else:
            open_in_front(lambda: subprocess.Popen(["explorer", "/select,", f]))  # Проводник с выделенным файлом
        self._ok()

    def _open_log(self, _path, job_id):
        job = jobs.get(job_id)
        if not job or not job.get("log_file"):
            return self._send(404, {"error": "лог не найден"})
        open_path(job["log_file"])
        self._ok()

    def _clear(self, _path, _job_id):
        with lock:
            for k in [k for k, v in jobs.items() if v["status"].startswith(FINAL) and not v.get("running")]:
                del jobs[k]
        self._ok()

    def _cancel(self, _path, job_id):
        with lock:
            job = jobs.get(job_id)
            if not job:
                return self._send(404, {"error": "такой загрузки уже нет в списке"})
            if not job["status"].startswith(FINAL):
                job["cancel"] = True  # работающую загрузку прервёт её же обработчик прогресса
                job["status"] = "отменено" if job["status"] == "в очереди" else "отмена…"
                job["speed"] = ""
        log.info(f"[{job_id}] отмена по просьбе пользователя")
        self._ok()

    def _retry(self, _path, job_id):
        with lock:
            job = jobs.get(job_id)
            if not job:
                return self._send(404, {"error": "такой загрузки уже нет в списке"})
            if job["status"].startswith(("ошибка", "отменено")):
                job.update(cancel=False, status="в очереди", error=None, percent=0, speed="", eta=None, done_mb=0)
                if job.get("running"):
                    # обработчик ещё убирает за прошлой попыткой — вторая поверх неё всё испортит.
                    # В очередь задачу поставит он сам, как только закончит (download.release)
                    job["retry_wanted"] = True
                else:
                    tasks.put(job_id)
        log.info(f"[{job_id}] повтор по просьбе пользователя")
        self._ok()

    def do_POST(self):
        self._guard(self._post)

    def _post(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/client-log":
            return self._client_log()
        if self.path != "/download":
            return self._send(404, {"error": "not found"})
        if stopping.is_set():  # иначе задача принималась бы и молча пропадала вместе с программой
            return self._send(503, {"error": "Программа закрывается — попробуй ещё раз через пару секунд."})
        try:
            new = parse_job(self._read_json())
        except ValueError as e:
            log.warning(f"отклонён запрос /download: {e}")
            return self._send(400, {"error": f"Не понял запрос — обнови страницу и попробуй ещё раз. ({e})"})
        key = job_key(new)
        done = None if new["force"] else already_downloaded(key)  # чтение файла — до замка
        job_id = uuid.uuid4().hex[:8]
        with lock:  # проверка «уже качается» и постановка в очередь — одним действием: двойной щелчок не даёт дубль
            busy = not new["force"] and any(v["key"] == key and not v["status"].startswith(FINAL) for v in jobs.values())
            if not busy and not done:
                jobs[job_id] = {
                    **{k: v for k, v in new.items() if k != "force"},
                    "key": key, "cancel": False, "running": False, "save_dir": str(state.save_dir),
                    "status": "в очереди", "percent": 0, "speed": "", "eta": None, "done_mb": 0, "quality": "", "file": None,
                    "error": None, "log_file": None,
                }
        if busy or done:
            why = "Это видео уже качается" if busy else f"Уже скачано: {Path(done).name}"
            log.info(f"повтор не начат ({why}): {new['title']!r}")
            return self._send(409, {"error": why, "duplicate": True})
        log.info(f"[{job_id}] новая задача ({want_label(new['want'])}{', поток' if new['sniffed'] else ''}): "
                 f"{new['title']!r} <- {new['url'][:200]}")
        tasks.put(job_id)
        self._send(200, {"id": job_id})

    def _choose_folder(self, _path=None, _job_id=None):
        """Стандартное окно Windows «Выбор папки» — прямо на этом ПК, поверх Chrome."""
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(parent=root, initialdir=str(state.save_dir), title="Куда сохранять видео?")
        root.destroy()
        if not chosen:
            return self._send(200, {"folder": str(state.save_dir), "changed": False})
        state.save_dir = Path(chosen)
        save_settings(save_dir=str(state.save_dir))
        log.info(f"папка для видео изменена: {state.save_dir}")
        return self._send(200, {"folder": str(state.save_dir), "changed": True})

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError as e:
            raise ValueError("длина запроса не число") from e
        if not 0 <= length <= MAX_BODY:
            raise ValueError("запрос слишком большой")
        raw = self.rfile.read(length) or b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise ValueError(f"не UTF-8 ({e})") from e
        except json.JSONDecodeError as e:
            raise ValueError(f"не JSON ({e})") from e

    def _client_log(self):
        """Ошибки и события самого расширения — в тот же общий журнал."""
        try:
            data = self._read_json()
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        level = logging.WARNING if data.get("level") == "error" else logging.INFO
        log.log(level, f"[расширение] {str(data.get('message'))[:2000]}")
        self._send(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass  # стандартный шумный лог каждого HTTP-запроса не нужен


# Адреса, которые зовёт окно расширения -> метод Handler, который отвечает
ROUTES = {
    "/status": "_status",
    "/open-folder": "_open_folder",
    "/choose-folder": "_choose_folder",
    "/open-file": "_open_file",
    "/show-file": "_open_file",
    "/open-logs": "_open_logs",
    "/open-log": "_open_log",
    "/clear": "_clear",
    "/cancel": "_cancel",
    "/retry": "_retry",
}
