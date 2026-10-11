"""Файлы загрузок: имена, временная папка, cookies для yt-dlp, проверка готового файла."""
import contextlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import (NO_WINDOW)
from .system import FFMPEG_DIR


def temp_dir(save_dir):
    """Куски недокачанных видео — в скрытой подпапке .tmp, чтобы не путать с готовыми."""
    t = Path(save_dir) / ".tmp"
    t.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(AttributeError, OSError):  # не Windows или нет прав — папка просто останется видимой
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(t), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    return t


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:150] or "video"


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
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
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
    for d in dirs:
        shutil.rmtree(Path(d) / ".tmp", ignore_errors=True)


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
