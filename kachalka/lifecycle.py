"""Значок в трее и завершение: выход по простою, перезапуск при обновлении кода, уборка."""
import sys
from pathlib import Path
import logging
import os
import subprocess
import threading
import time

from .config import (ICON, IDLE_EXIT, LOG_DIR, NO_WINDOW, ROOT, log)
from . import state
from .state import jobs, lock, stopping, summary
from .children import BREAKAWAY
from .files import clean_temp
from .windows import open_path


def status_line():
    s = summary()
    if s["active"] or s["queued"]:
        return f"В работе: {s['active']} · в очереди: {s['queued']}"
    return "Загрузок нет"


def run_tray():
    """Значок у часов: видно, что качалка работает, и есть откуда её закрыть."""
    try:
        import pystray
        from PIL import Image
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: status_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть папку с файлами", lambda: open_path(state.save_dir), default=True),
            pystray.MenuItem("Логи", lambda: open_path(LOG_DIR)),
            pystray.MenuItem("Выход", lambda: shutdown("выход из меню в трее")),
        )
        state.tray = pystray.Icon("kachalka", Image.open(ICON), "VideoTranskeeper", menu)
    except Exception as e:  # noqa: BLE001 — значок необязателен: нет библиотеки, нет иконки, нет рабочего стола — качаем и без него
        return log.warning(f"значок в трее недоступен ({e!r}) — работаю без него")
    threading.Thread(target=tray_status, daemon=True).start()
    state.tray.run()


def tray_status():
    shown = None
    while True:
        time.sleep(2)
        line = status_line()
        if line != shown:
            shown = line
            state.tray.title = "VideoTranskeeper — " + line.lower()
            state.tray.update_menu()


def idle_watch():
    """Запущенная расширением качалка не висит в памяти зря: нет дел 10 минут — закрывается."""
    while True:
        time.sleep(15)
        s = summary()
        if s["active"] or s["queued"]:
            state.last_activity = time.time()
        elif time.time() - state.last_activity > IDLE_EXIT:
            start_update()
            shutdown(f"{IDLE_EXIT // 60} минут без дела")


def start_update():
    """Перед выходом по простою запустить обновление yt-dlp отдельным процессом: оно проверит, пора ли (раз в сутки),
    и сделает своё дело, когда нас уже не будет. Раньше обновление шло перед запуском, и окно ждало до 90 секунд."""
    python = Path(sys.executable).with_name("python.exe")
    flags = subprocess.DETACHED_PROCESS | NO_WINDOW
    # Обновление должно нас пережить, а дочерние процессы теперь закрываются вместе с нами (children.py) —
    # отвязываем его. Если отвязка запрещена (нас самих запустили связанными) — запускаем как раньше
    for extra in (BREAKAWAY, 0):
        try:
            subprocess.Popen([str(python), str(ROOT / "launcher.py"), "--update"], cwd=ROOT, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags | extra)
            return
        except OSError as e:
            log.warning(f"не смог запустить обновление yt-dlp: {e!r}")


def code_watch():
    """Код качалки на диске обновился, а работает старый — закрываемся, как только нет загрузок:
    расширение при следующем обращении поднимет уже новую качалку."""
    def stamp():
        files = [ROOT / "server.py", *sorted((ROOT / "kachalka").glob("*.py"))]
        return [(f.name, f.stat().st_mtime) for f in files]

    loaded = stamp()
    while True:
        time.sleep(3)
        try:
            changed = stamp() != loaded
        except OSError:  # файл в этот миг переписывают — посмотрим на следующем круге
            continue
        s = summary()
        if changed and not s["active"] and not s["queued"]:
            shutdown("код качалки обновился — перезапуск")


def kill_children():
    """ffmpeg, запущенный для склейки, сам не остановится — а его недоделанный файл держит папку .tmp."""
    ps = (f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={os.getpid()} and Name like 'ff%'\" "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15,
                       creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"не смог остановить ffmpeg: {e!r}")


def shutdown(reason):
    """Закрыться чисто: прервать загрузки, убрать недокачанные куски, снять значок."""
    if stopping.is_set():
        return
    stopping.set()
    interrupted = summary()["active"]
    log.info(f"Качалка закрывается: {reason}" + (f" (прервано загрузок: {interrupted})" if interrupted else ""))
    deadline = time.time() + 5
    while summary()["active"] and time.time() < deadline:
        time.sleep(0.2)
    if interrupted:
        kill_children()
    with lock:
        dirs = {v["save_dir"] for v in jobs.values()} | {str(state.save_dir)}
    clean_temp(dirs)
    if state.tray:
        try:
            state.tray.stop()
        except Exception as e:  # noqa: BLE001 — мы уже выходим: сбой снятия значка не должен помешать выходу
            log.debug(f"значок не снялся: {e!r}")
    logging.shutdown()
    os._exit(0)


def thread_crash(args):
    log.error(f"СБОЙ ПОТОКА {args.thread.name if args.thread else '?'}: {args.exc_type.__name__}: {args.exc_value}",
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
