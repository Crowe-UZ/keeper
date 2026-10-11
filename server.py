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
import sys
import threading
import time
from http.server import ThreadingHTTPServer

from kachalka import state
from kachalka.api import Handler
from kachalka.config import AUTO, HOST, LOG_DIR, PORT, WORKERS, log
from kachalka.download import reporter, worker
from kachalka.files import clean_stale_temp
from kachalka.lifecycle import code_watch, idle_watch, run_tray, shutdown, thread_crash
from kachalka.system import FFMPEG_DIR, environment


def start(target):
    threading.Thread(target=target, daemon=True).start()


def main():
    threading.excepthook = thread_crash  # любые падения фоновых потоков — в kachalka.log
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        log.error(f"Не могу занять порт {PORT}: {e}. Похоже, качалка уже запущена.")
        sys.exit(1)
    clean_stale_temp([state.save_dir])  # куски от прошлых запусков, закрытых грубо; работающие копии не трогаем
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
    main()
