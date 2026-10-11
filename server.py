"""Локальный сервер-качалка: принимает ссылку от расширения Chrome и скачивает через yt-dlp.

Запуск: её поднимает само расширение (launcher.py, после install.bat) — без окна, со значком в трее;
start.bat — запасной способ, с окном.
Слушает только 127.0.0.1 и принимает запросы только от расширений Chrome.

Два вида задач:
  - ссылка на страницу (YouTube, VK, ...) — yt-dlp сам находит видео;
  - пойманный расширением поток (m3u8/mpd/mp4) с купленного курса и т.п. —
    качаем его с заголовками и cookies вкладки, будто это делает сам браузер.

Логи (папка logs рядом с этим файлом):
  - kachalka.log          — общий журнал: запуск, каждая задача, итог, ошибки;
  - jobs/<дата>_<id>.log  — всё про одну загрузку: что прислало расширение, полный вывод yt-dlp,
                            что реально получилось (ffprobe). Значения cookies не пишутся никогда.

Сам код — в пакете kachalka/: этот файл только запускает его части.
"""
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from kachalka import state
from kachalka.api import Handler
from kachalka.children import tie_children
from kachalka.config import AUTO, HOST, JOB_LOG_DIR, LOG_DIR, PORT, WORKERS, log
from kachalka.download import reporter, worker
from kachalka.files import clean_stale_temp
from kachalka.joblog import drop_old_logs
from kachalka.lifecycle import code_watch, idle_watch, run_tray, shutdown, thread_crash
from kachalka.system import FFMPEG_DIR, environment


class OneCopyServer(ThreadingHTTPServer):
    """Порт достаётся только одной запущенной копии. Обычный HTTPServer разрешает повторное занятие адреса,
    и на Windows вторая копия спокойно садилась на тот же порт: запросы шли первой, а вторая мешала ей."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def start(target):
    threading.Thread(target=target, daemon=True).start()


def main():
    threading.excepthook = thread_crash  # любые падения фоновых потоков — в kachalka.log
    try:
        server = OneCopyServer((HOST, PORT), Handler)
    except OSError as e:
        log.error(f"Не могу занять порт {PORT}: {e}. Похоже, качалка уже запущена.")
        sys.exit(1)
    if not tie_children():
        log.warning("не смог связать дочерние процессы с собой: после аварии ffmpeg может остаться работать")
    clean_stale_temp([state.save_dir])  # куски от прошлых запусков, закрытых грубо; работающие копии не трогаем
    drop_old_logs(JOB_LOG_DIR)
    for _ in range(WORKERS):
        start(worker)
    for part in (reporter, server.serve_forever, run_tray):
        start(part)
    if AUTO:
        start(idle_watch)
        start(code_watch)
    env = environment()
    log.info(f"Качалка запущена: http://{HOST}:{PORT}" + (" (расширением, закроется сама)" if AUTO else ""))
    log.info(f"Файлы: {state.save_dir} | Логи: {LOG_DIR}")
    log.info(f"yt-dlp {env['yt-dlp']} | Python {env['python']} | одновременно качается: {WORKERS}")
    log.info(f"ffmpeg: {env['ffmpeg']}")
    log.info(f"JS-движок для YouTube: {env['js']}")
    if not FFMPEG_DIR:
        log.warning("ffmpeg НЕ НАЙДЕН — качество будет хуже, mp3 недоступен")
    log.info("Остановить: Ctrl+C или «Выход» в меню значка у часов")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown("Ctrl+C")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("программа не смогла запуститься")  # без окна эту ошибку больше негде увидеть
        raise
