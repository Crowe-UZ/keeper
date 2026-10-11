"""Дочерние процессы (ffmpeg, окно выбора папки) не переживают программу — даже если она упала."""
import ctypes
from ctypes import wintypes

KILL_ON_CLOSE = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
BREAKAWAY_OK = 0x800    # JOB_OBJECT_LIMIT_BREAKAWAY_OK: процесс, запущенный с BREAKAWAY, живёт сам по себе
BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB — для обновления yt-dlp: оно как раз должно нас пережить
EXTENDED_LIMITS = 9     # JobObjectExtendedLimitInformation

_job = None  # держим до конца работы: Windows закрывает его вместе с нами и тогда останавливает дочерние процессы


class _Basic(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _Extended(ctypes.Structure):
    _fields_ = [("Basic", _Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def tie_children():
    """Связать с собой всё, что мы запустим: при любом нашем завершении Windows остановит и их.
    Раньше ffmpeg переживал аварию программы и держал недописанный файл. Возвращает, получилось ли."""
    global _job
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError):  # не Windows
        return False
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k.CreateJobObjectW(None, None)
    if not job:
        return False
    info = _Extended()
    info.Basic.LimitFlags = KILL_ON_CLOSE | BREAKAWAY_OK
    if not (k.SetInformationJobObject(job, EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info))
            and k.AssignProcessToJobObject(job, k.GetCurrentProcess())):
        return False
    _job = job
    return True
