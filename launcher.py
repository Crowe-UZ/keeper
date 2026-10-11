"""Мост между Chrome и качалкой (native messaging).

Расширение не может само запустить программу на компьютере — зато Chrome умеет запускать «хост»,
прописанный в реестре. Этот файл и есть хост: расширение его зовёт, он поднимает server.py в фоне
(если тот ещё не работает), отвечает и сразу завершается.

  launcher.py            — так его запускает Chrome (через host.bat)
  launcher.py --install  — прописать хост в Chrome (делает install.bat, один раз)
"""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_NAME = "com.kachalka.host"
PORT = int(os.environ.get("KACHALKA_PORT", 5577))
SETTINGS_FILE = HERE / "settings.json"
UPDATE_EVERY = 24 * 3600


def note(msg):
    (HERE / "logs").mkdir(exist_ok=True)
    with open(HERE / "logs" / "launcher.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def extension_id():
    """ID расширения Chrome вычисляет из ключа в manifest.json — считаем так же."""
    key = json.loads((HERE / "extension" / "manifest.json").read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def install():
    import winreg
    manifest = HERE / "native_host.json"
    manifest.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "Kachalka video: starts the local downloader",
        "path": str(HERE / "host.bat"),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id()}/"],
    }, indent=2), encoding="utf-8")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(manifest))
    print(f"OK: host registered for extension {extension_id()}")


def running():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def update_ytdlp():
    """Раз в сутки, перед запуском качалки: сайты меняются, старый yt-dlp перестаёт качать."""
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        settings = {}
    if time.time() - settings.get("ytdlp_checked", 0) < UPDATE_EVERY:
        return
    SETTINGS_FILE.write_text(json.dumps({**settings, "ytdlp_checked": time.time()}, ensure_ascii=False), encoding="utf-8")
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "-q", "yt-dlp[default]"],
                           capture_output=True, text=True, timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        note(f"обновление yt-dlp: код {r.returncode} {r.stderr.strip()[-300:]}")
    except Exception as e:
        note(f"обновление yt-dlp не удалось: {e!r}")


def start_server():
    args = [str(HERE / ".venv" / "Scripts" / "pythonw.exe"), str(HERE / "server.py"), "--auto"]
    # качалка должна пережить и этот скрипт, и закрытие Chrome
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
    kw = dict(cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        subprocess.Popen(args, creationflags=flags, **kw)
    except OSError:  # Chrome не разрешил выйти из своей группы процессов — запускаем как есть
        subprocess.Popen(args, creationflags=flags & ~subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)


def reply(data):
    raw = json.dumps(data).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(raw)) + raw)
    sys.stdout.buffer.flush()


def main():
    size = sys.stdin.buffer.read(4)  # сообщение расширения: 4 байта длины + JSON; содержимое не важно
    if len(size) == 4:
        sys.stdin.buffer.read(struct.unpack("<I", size)[0])
    try:
        started = not running()
        if started:
            update_ytdlp()
            start_server()
            note("качалка запущена по просьбе расширения")
        reply({"ok": True, "started": started})
    except Exception as e:
        note(f"сбой запуска: {e!r}")
        reply({"ok": False, "error": repr(e)})


if __name__ == "__main__":
    install() if "--install" in sys.argv else main()
