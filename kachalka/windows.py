"""Открыть папку или файл поверх остальных окон."""
import os
import threading
import time
from pathlib import Path

from .config import (log)


# Windows не даёт фоновой программе вывести окно вперёд: папка открывалась позади Chrome.
# Поэтому после открытия находим появившееся окно и выводим его вперёд сами.


def _titled_windows():
    """Видимые окна верхнего уровня с заголовком: {hwnd: заголовок}."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            size = user32.GetWindowTextLengthW(hwnd)
            if size:
                buf = ctypes.create_unicode_buffer(size + 1)
                user32.GetWindowTextW(hwnd, buf, size + 1)
                found[hwnd] = buf.value
        return True

    user32.EnumWindows(each, 0)
    return found


def _to_front(hwnd):
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    for fn in (user32.SetForegroundWindow, user32.BringWindowToTop, user32.IsIconic):
        fn.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: окно было свёрнуто
    # право менять активное окно есть у потока активного окна — на миг подключаемся к нему
    me, front = kernel32.GetCurrentThreadId(), user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    user32.AttachThreadInput(me, front, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(me, front, False)
    if user32.GetForegroundWindow() != hwnd:  # не вышло — запасной приём: «нажатие» Alt снимает запрет
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() != hwnd:  # и так нет — свернуть и развернуть: развёрнутое окно Windows активирует сама
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    # что бы ни вышло с активацией — поднять окно над остальными (на это запрета нет): «поверх всех» и сразу обратно
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND] + [ctypes.c_int] * 4 + [ctypes.c_uint]
    for after in (-1, -2):  # HWND_TOPMOST, HWND_NOTOPMOST
        user32.SetWindowPos(hwnd, after, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  # не двигать, не менять размер, показать


def open_in_front(opener, title=None):
    """Открыть (opener) и вывести появившееся окно вперёд. title — заголовок окна, если оно могло
    быть открыто и раньше (Проводник тогда нового окна не создаёт, а показывает старое)."""
    try:
        before = _titled_windows()
    except (AttributeError, OSError) as e:  # не Windows — откроем как получится, без вывода вперёд
        log.debug(f"список окон недоступен: {e!r}")
        before = None
    opener()
    if before is None:
        return

    def raise_it():
        started = time.time()
        while time.time() - started < 4:
            time.sleep(0.15)
            now = _titled_windows()
            fresh = [h for h in now if h not in before]
            old = [h for h, t in now.items() if title and t == title] if time.time() - started > 0.7 else []
            if fresh or old:
                hwnd = (fresh or old)[0]
                log.debug(f"вывожу вперёд окно {now[hwnd]!r} ({'новое' if fresh else 'уже было открыто'}; "
                          f"новых {len(fresh)}: {[now[h] for h in fresh][:5]})")
                return _to_front(hwnd)
        log.debug(f"окно для вывода вперёд не нашлось (заголовок {title!r})")

    def safely():
        try:
            raise_it()
        except Exception as e:  # noqa: BLE001 — верх фонового потока: окно уже открыто, сбой вывода вперёд не должен ничего ронять
            log.warning(f"не смог вывести окно вперёд: {e!r}", exc_info=True)

    threading.Thread(target=safely, daemon=True).start()


def open_path(path):
    """Папка или файл — в программе по умолчанию, поверх остальных окон."""
    path = Path(path)
    open_in_front(lambda: os.startfile(path), title=path.name if path.is_dir() else None)
