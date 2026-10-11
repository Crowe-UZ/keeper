"""Файлы загрузок: имена, временная папка, cookies для yt-dlp, проверка готового файла."""
import contextlib
import os
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import (NO_WINDOW)
from .system import FFMPEG_DIR


# Своё имя временной папки: обычную «.tmp» в папке загрузок могла завести другая программа, а мы при старте
# стирали в ней всё подряд. LEGACY_TEMP — как папка называлась раньше: из неё убираем только заведомо своё.
TEMP_NAME = ".keeper-tmp"
LEGACY_TEMP = ".tmp"
# Cookies вкладок — в своей папке внутри временной папки Windows, у каждой запущенной копии своя (по номеру
# процесса): после грубого выхода следующий запуск находит и стирает оставшееся
COOKIE_HOME = Path(tempfile.gettempdir()) / "keeper-cookies"


def temp_dir(save_dir):
    """Куски недокачанных видео — в скрытой подпапке, чтобы не путать с готовыми. У каждой запущенной копии
    там своя папка (по номеру процесса): иначе вторая копия при старте стирала чужое недокачанное."""
    hidden = Path(save_dir) / TEMP_NAME
    mine = hidden / str(os.getpid())
    mine.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(AttributeError, OSError):  # не Windows или нет прав — папка просто останется видимой
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    return mine


def process_alive(pid):
    """Работает ли сейчас процесс с таким номером."""
    try:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    except (AttributeError, OSError):  # не Windows: осторожно считаем живым — чужое не трогаем
        return True


RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)$", re.I)  # имена устройств Windows: файл с таким именем молча теряется


def safe_name(name, keep=""):
    """Имя файла, которое Windows примет. keep — хвост (« — видео 2», « — часть 3»), который должен уцелеть,
    даже если название длинное: иначе у нескольких видео с одной страницы имена совпадали."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    keep = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", keep)
    name = (name[:150 - len(keep)].rstrip(" .") + keep) if keep else name[:150]
    if RESERVED.match(name):
        name += "_"
    return name or "video"


def glob_escape(s):
    return re.sub(r"([\[\]*?])", r"[\1]", s)


def unique_template(title, save_dir):
    """Шаблон имени по заголовку вкладки; если такой файл уже есть — добавляем (2), (3)..."""
    base = safe_name(title)
    n = 1
    def taken(nm):  # занято, если есть готовый или недокачанный файл с таким именем
        return any(True for d in (Path(save_dir), temp_dir(save_dir)) for _ in d.glob(glob_escape(nm) + ".*"))
    while taken(base if n == 1 else f"{base} ({n})"):
        n += 1
    name = base if n == 1 else f"{base} ({n})"
    # имя относительное: папки задаёт "paths" (готовое — в папку задачи, куски — в её .tmp)
    return name.replace("%", "%%") + ".%(ext)s"


def write_cookies(cookies):
    """Cookies из Chrome -> временный файл в формате Netscape, который понимает yt-dlp."""
    if not cookies:
        return None
    mine = COOKIE_HOME / str(os.getpid())
    mine.mkdir(parents=True, exist_ok=True)
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8", dir=mine)
    f.write("# Netscape HTTP Cookie File\n")
    for c in cookies:
        domain = c["domain"]
        f.write("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expirationDate", 0))),
            c["name"],
            c["value"],
        ]) + "\n")
    f.close()
    return f.name


def remove_parts(save_dir, names):
    """Убрать недокачанные куски прерванной загрузки — иначе «Повторить» увидит их и назовёт файл «… (2)».
    yt-dlp после сбоя ещё секунду-другую держит файл открытым, поэтому пробуем несколько раз."""
    import gc
    for _ in range(15):
        left = [p for name in names for p in temp_dir(save_dir).glob(glob_escape(name) + ".*")]
        if not left:
            return
        gc.collect()  # брошенный yt-dlp открытый файл закрывается, когда его объект собран
        for part in left:
            with contextlib.suppress(OSError):  # ещё занят — попробуем на следующем круге
                part.unlink()
        time.sleep(0.3)


def clean_temp(dirs):
    """Убрать свои недокачанные куски и cookies (при выходе)."""
    for d in [*(Path(d) / TEMP_NAME for d in dirs), COOKIE_HOME]:
        shutil.rmtree(d / str(os.getpid()), ignore_errors=True)


def _of_running_copy(item):
    return item.is_dir() and item.name.isdigit() and process_alive(int(item.name))


def _sweep(folder, everything):
    """Убрать из папки то, что осталось от копий, которых уже нет. everything — папка целиком наша: убираем
    и всё прочее; иначе трогаем только подпапки с номером процесса."""
    for item in (folder.iterdir() if folder.is_dir() else []):
        if _of_running_copy(item):
            continue
        if item.is_dir() and (everything or item.name.isdigit()):
            shutil.rmtree(item, ignore_errors=True)
        elif everything and not item.is_dir():
            with contextlib.suppress(OSError):  # занят — уберём при следующем запуске
                item.unlink()


def clean_stale_temp(dirs):
    """Убрать куски и cookies, оставшиеся от копий, которых уже нет (при старте). Работающие копии не трогаем."""
    _sweep(COOKIE_HOME, everything=True)
    for d in dirs:
        _sweep(Path(d) / TEMP_NAME, everything=True)
        old = Path(d) / LEGACY_TEMP
        _sweep(old, everything=False)
        with contextlib.suppress(OSError):  # не пустая — значит там чужое, пусть лежит
            old.rmdir()


def writable(folder):
    """Можно ли сохранять в эту папку: пробуем на деле — права Windows иначе надёжно не узнать."""
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def probe(path):
    """Что реально лежит в файле: разрешение, кодеки, битрейт — чтобы проверить «идеальное качество»."""
    if not FFMPEG_DIR:
        return None
    try:
        out = subprocess.run(
            [str(Path(FFMPEG_DIR) / "ffprobe"), "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,bit_rate:format=duration,size,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, timeout=30, encoding="utf-8", creationflags=NO_WINDOW)
        return json.loads(out.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:  # нет ffprobe, завис или вернул не JSON
        return {"error": repr(e)}


def describe_probe(p):
    if not p or "streams" not in p:
        return ""
    parts = []
    for s in p["streams"]:
        if s.get("codec_type") == "video":
            num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
            fps = round(int(num) / int(den or 1)) if num.isdigit() and (den or "1").isdigit() and int(den or 1) else "?"
            parts.append(f"{s.get('height')}p{fps} {s.get('codec_name')}")
        elif s.get("codec_type") == "audio":
            parts.append(s.get("codec_name"))
    fmt = p.get("format", {})
    if fmt.get("bit_rate"):
        parts.append(f"{int(fmt['bit_rate']) // 1000} кбит/с")
    return " · ".join(parts)
