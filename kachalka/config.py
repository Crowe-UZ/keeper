"""Константы, настройки и общий журнал качалки."""
import json
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path



ROOT = Path(__file__).resolve().parent.parent  # папка проекта: server.py, settings.json, logs, extension
HOST, PORT = "127.0.0.1", int(os.environ.get("KACHALKA_PORT", 5577))
SETTINGS_FILE = ROOT / "settings.json"


def downloads_folder():
    """Настоящая папка «Загрузки» этого пользователя. Её переносят на другой диск и в OneDrive, поэтому
    спрашиваем у Windows, а не считаем, что она лежит в C:\\Users\\имя\\Downloads."""
    try:
        import ctypes
        import uuid
        guid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
        found = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(found)) == 0 and found.value:
            path = Path(found.value)
            ctypes.windll.ole32.CoTaskMemFree(found)
            return path
    except (AttributeError, OSError, ValueError):  # не Windows или система не ответила — берём привычное место
        pass
    return Path.home() / "Downloads"


DEFAULT_SAVE_DIR = downloads_folder() / "Videos"


def extension_id():
    """Номер нашего расширения — Chrome вычисляет его из ключа в manifest.json, считаем так же.
    По нему программа отличает своё расширение от любого другого."""
    import base64
    import hashlib
    try:
        key = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))["key"]
    except (OSError, ValueError, KeyError):
        return ""
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def write_atomic(path, text):
    """Записать файл целиком или не записать вовсе: обрыв посреди записи не должен оставить полфайла
    (битые настройки молча откатывали выбранную папку, битая история — память о скачанном)."""
    tmp = path.with_name(path.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # файла ещё нет или он испорчен — работаем с умолчаниями
        return {}


def save_settings(**fields):
    write_atomic(SETTINGS_FILE, json.dumps({**load_settings(), **fields}, ensure_ascii=False))


def first_usable(folders):
    """Первая папка из списка, которая есть или которую удаётся создать. Не подошла ни одна — папка downloads
    рядом с программой: туда install.bat уже писал. Программа обязана запуститься в любом случае."""
    for folder in [*folders, ROOT / "downloads"]:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except OSError:  # диск отключён, нет прав, на пути файл — пробуем следующую
            continue
    return ROOT / "downloads"


def load_save_dir():
    """Куда сохранять: выбранная папка, если она на месте; иначе «Загрузки\\Videos»; иначе папка в профиле.
    Раньше сохранённая папка на отключённом диске не давала программе запуститься вовсе."""
    saved = load_settings().get("save_dir")
    chosen = [Path(saved)] if isinstance(saved, str) and Path(saved).is_dir() else []
    return first_usable([*chosen, DEFAULT_SAVE_DIR, Path.home() / "Keeper"])


WORKERS = 3  # сколько видео качается одновременно, остальные ждут в очереди
AUTO = "--auto" in sys.argv  # запущена расширением: без окна, сама закрывается, когда нечего делать
IDLE_EXIT = 10 * 60  # секунд без загрузок и без открытого окна расширения
ICON = ROOT / "extension" / "icon.png"
NO_WINDOW = subprocess.CREATE_NO_WINDOW  # без окна у качалки нет консоли — чтобы ffprobe и прочие не мигали чёрным окном

if sys.stdout is None:  # pythonw: консоли нет вообще, а yt-dlp и print в неё пишут
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

LOG_DIR = ROOT / "logs"
JOB_LOG_DIR = LOG_DIR / "jobs"
JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_FORMAT = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
log = logging.getLogger("kachalka")
log.setLevel(logging.DEBUG)
_file = RotatingFileHandler(LOG_DIR / "kachalka.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
_file.setFormatter(LOG_FORMAT)
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
_console.setLevel(logging.INFO)
log.addHandler(_file)
log.addHandler(_console)

ACTIVE = ("подготовка", "скачивается", "склейка", "звук из видео", "ждёт распознавания", "загрузка модели",
          "распознавание", "отмена…")  # статусы, при которых задача занимает обработчик
FINAL = ("готово", "ошибка", "отменено")  # с этого начинается статус завершённой задачи
HEIGHTS = (2160, 1440, 1080, 720, 480, 360)  # допустимые ограничения качества; 0 — без ограничения
HISTORY_FILE = ROOT / "history.json"  # что уже скачано — чтобы не качать второй раз
