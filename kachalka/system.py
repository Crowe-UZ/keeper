"""Что стоит на компьютере: ffmpeg, JS-движок, версии — для запуска yt-dlp и для логов."""
import functools
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from yt_dlp.version import __version__ as YTDLP_VERSION
from .config import NO_WINDOW, ROOT
from . import state


def find_ffmpeg(root=ROOT):
    """Папка с ffmpeg и ffprobe. Сначала свой — его кладёт установщик: с ним программа проверена, а у человека
    может стоять старый или урезанный. Потом тот, что есть в системе, потом поставленный через winget."""
    own = root / "ffmpeg" / "bin"
    if (own / "ffmpeg.exe").is_file():
        return str(own)
    path = shutil.which("ffmpeg")
    if path:
        return str(Path(path).parent)
    winget = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for exe in winget.glob("*FFmpeg*/**/bin/ffmpeg.exe"):
        return str(exe.parent)
    return None


FFMPEG_DIR = find_ffmpeg()
# YouTube без JS-движка отдаёт не все форматы — подключаем тот, что есть в системе
JS_RUNTIMES = {name: {"path": None} for name in ("deno", "node") if shutil.which(name)}


@functools.cache  # версии не меняются, пока программа работает; без этого ffmpeg и node запускались на каждую задачу
def tool_version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
        return out.splitlines()[0].strip() if out else "?"
    except (OSError, subprocess.SubprocessError) as e:
        return f"не найден ({e.__class__.__name__})"


def environment():
    ffmpeg = str(Path(FFMPEG_DIR) / "ffmpeg") if FFMPEG_DIR else None
    return {
        "python": sys.version.split()[0],
        "yt-dlp": YTDLP_VERSION,
        "ffmpeg": tool_version((ffmpeg, "-version")) if ffmpeg else "НЕ НАЙДЕН",
        "js": {n: tool_version((n, "--version")) for n in JS_RUNTIMES} or "НЕТ",
        "os": platform.platform(),
        "save_dir": str(state.save_dir),
    }
