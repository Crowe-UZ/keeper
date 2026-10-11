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
DEFAULT_SAVE_DIR = Path.home() / "Downloads" / "Videos"


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


def load_save_dir():
    saved = load_settings().get("save_dir")
    if isinstance(saved, str) and Path(saved).is_dir():
        return Path(saved)
    return DEFAULT_SAVE_DIR  # папку не выбирали, или её с тех пор удалили


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
