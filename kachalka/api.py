"""HTTP для расширения: приём задач, состояние очереди, кнопки окна."""
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
from .state import jobs, lock, summary, tasks
from .history import already_downloaded, job_key
from .system import FFMPEG_DIR
from .windows import open_in_front, open_path


PUBLIC_FIELDS = ("url", "title", "status", "percent", "speed", "eta", "done_mb", "quality", "error", "mode")


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
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        if path == "/status":
            with lock:
                items = [{"id": k, "has_log": bool(v.get("log_file")), "has_file": bool(v.get("file")), **{f: v[f] for f in PUBLIC_FIELDS}} for k, v in jobs.items()]
            return self._send(200, {"jobs": items[::-1][:30], "summary": summary(), "folder": str(state.save_dir), "ffmpeg": bool(FFMPEG_DIR),
                                    "ytdlp": YTDLP_VERSION})
        if path == "/open-folder":
            open_path(state.save_dir)
            return self._send(200, {"ok": True})
        if path == "/choose-folder":
            return self._choose_folder()
        if path in ("/open-file", "/show-file"):
            job = jobs.get(query.removeprefix("id="))
            f = job and job.get("file")
            if not f or not os.path.exists(f):
                return self._send(404, {"error": "файл не найден — его переместили или удалили"})
            if path == "/open-file":
                open_path(f)  # откроется в плеере по умолчанию
            else:
                open_in_front(lambda: subprocess.Popen(["explorer", "/select,", f]))  # Проводник с выделенным файлом
            return self._send(200, {"ok": True})
        if path == "/open-logs":
            open_path(LOG_DIR)
            return self._send(200, {"ok": True})
        if path == "/open-log":
            job = jobs.get(query.removeprefix("id="))
            if job and job.get("log_file"):
                open_path(job["log_file"])
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "лог не найден"})
        if path == "/clear":
            with lock:
                for k in [k for k, v in jobs.items() if v["status"].startswith(FINAL)]:
                    del jobs[k]
            return self._send(200, {"ok": True})
        if path in ("/cancel", "/retry"):
            job_id = query.removeprefix("id=")
            with lock:
                job = jobs.get(job_id)
                if not job:
                    return self._send(404, {"error": "такой загрузки уже нет в списке"})
                if path == "/cancel" and not job["status"].startswith(FINAL):
                    job["cancel"] = True  # работающую загрузку прервёт её же обработчик прогресса
                    job["status"] = "отменено" if job["status"] == "в очереди" else "отмена…"
                    job["speed"] = ""
                elif path == "/retry" and job["status"].startswith(("ошибка", "отменено")):
                    job.update(cancel=False, status="в очереди", error=None, percent=0, speed="", eta=None, done_mb=0)
                    tasks.put(job_id)
            log.info(f"[{job_id}] {'отмена' if path == '/cancel' else 'повтор'} по просьбе пользователя")
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/client-log":
            return self._client_log()
        if self.path != "/download":
            return self._send(404, {"error": "not found"})
        try:
            data = self._read_json()
        except ValueError as e:
            log.warning(f"кривой запрос /download: {e}")
            return self._send(400, {"error": f"кривой запрос: {e}"})
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            log.warning(f"отклонена ссылка не http(s): {url[:200]!r}")
            return self._send(400, {"error": "нужна ссылка http(s)"})
        new = {"url": url, "title": data.get("title") or url, "page_url": data.get("page_url") or "",
               "mode": data.get("mode", "video"), "sniffed": bool(data.get("sniffed"))}
        key = job_key(new)
        if not data.get("force"):  # «Скачать ещё раз» в окне расширения шлёт force
            with lock:
                busy = any(v["key"] == key and not v["status"].startswith(FINAL) for v in jobs.values())
            done = None if busy else already_downloaded(key)
            if busy or done:
                why = "Это видео уже качается" if busy else f"Уже скачано: {Path(done).name}"
                log.info(f"повтор не начат ({why}): {new['title']!r}")
                return self._send(409, {"error": why, "duplicate": True})
        job_id = uuid.uuid4().hex[:8]
        with lock:
            jobs[job_id] = {
                **new,
                "key": key,
                "max_height": data.get("max_height") if data.get("max_height") in HEIGHTS else 0,
                "cancel": False,
                "headers": {k: v for k, v in (data.get("headers") or {}).items() if k in ("Referer", "Origin", "User-Agent")},
                "cookies": data.get("cookies") or [],
                "debug": data.get("debug") or {},
                "save_dir": str(state.save_dir),
                "status": "в очереди", "percent": 0, "speed": "", "eta": None, "done_mb": 0, "quality": "", "file": None, "error": None,
                "log_file": None,
            }
        log.info(f"[{job_id}] новая задача ({data.get('mode', 'video')}{', поток' if data.get('sniffed') else ''}): "
                 f"{data.get('title')!r} <- {url[:200]}")
        tasks.put(job_id)
        self._send(200, {"id": job_id})

    def _choose_folder(self):
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
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) or b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise ValueError(f"не UTF-8 ({e})")
        except json.JSONDecodeError as e:
            raise ValueError(f"не JSON ({e})")

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
