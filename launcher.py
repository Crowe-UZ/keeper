"""Мост между Chrome и качалкой (native messaging).

Расширение не может само запустить программу на компьютере — зато Chrome умеет запускать «хост»,
прописанный в реестре. Этот файл и есть хост: расширение его зовёт, он поднимает server.py в фоне
(если тот ещё не работает), отвечает и сразу завершается.

  launcher.py            — так его запускает Chrome (через host.bat)
  launcher.py --install  — прописать хост в Chrome (делает install.bat, один раз)
  launcher.py --update   — обновить yt-dlp; программа зовёт это сама, когда закрывается по простою
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
UPDATING = HERE / "logs" / "updating.lock"  # идёт обновление yt-dlp


def runtime(here, exe):
    """Каким Python запускать программу. Установщик кладёт свой в папку python; у копии из репозитория —
    окружение .venv, которое создаёт install.bat."""
    own = here / "python" / exe
    return own if own.is_file() else here / ".venv" / "Scripts" / exe


def note(msg):
    (HERE / "logs").mkdir(exist_ok=True)
    with open(HERE / "logs" / "launcher.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def extension_id():
    """ID расширения Chrome вычисляет из ключа в manifest.json — считаем так же."""
    key = json.loads((HERE / "extension" / "manifest.json").read_text(encoding="utf-8"))["key"]
    digest = hashlib.sha256(base64.b64decode(key)).hexdigest()[:32]
    return digest.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def write_next_steps(here):
    """Страница «последний шаг» из установщика: подставить в неё папку, которую человек выбрал при установке.
    У копии из репозитория такой страницы нет."""
    import html
    page = here / "next-steps.html"
    if page.is_file():
        text = page.read_text(encoding="utf-8")
        page.write_text(text.replace("{{EXTENSION_DIR}}", html.escape(str(here / "extension"))), encoding="utf-8")


# Браузеры, в которых работает расширение. Все они на основе Chromium: расширение одно и то же, отличается
# только место в реестре, где браузер ищет программу-помощника. Номер расширения везде один — он считается из ключа.
BROWSERS = {"Chrome": r"Google\Chrome", "Edge": r"Microsoft\Edge", "Яндекс Браузер": r"Yandex\YandexBrowser"}


def host_keys():
    """Записи реестра (в разделе текущего пользователя), по которым браузеры находят программу."""
    return [rf"Software\{vendor}\NativeMessagingHosts\{HOST_NAME}" for vendor in BROWSERS.values()]


def install():
    import winreg
    write_next_steps(HERE)
    manifest = HERE / "native_host.json"
    manifest.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "Kachalka video: starts the local downloader",
        "path": str(HERE / "host.bat"),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id()}/"],
    }, indent=2), encoding="utf-8")
    for key in host_keys():
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(manifest))
    sys.stdout.write(f"OK: host registered for extension {extension_id()}\n")  # ответ для install.bat


def running():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def update_ytdlp():
    """Обновить yt-dlp (сайты меняются, старый перестаёт качать). Запускается отдельным процессом, когда программа
    закрылась по простою: тогда файлы yt-dlp никто не читает. На время обновления ставится отметка UPDATING —
    запуск программы её дожидается."""
    try:
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # настроек ещё нет или файл испорчен
        settings = {}
    if time.time() - settings.get("ytdlp_checked", 0) < UPDATE_EVERY or running():
        return
    UPDATING.write_text(str(os.getpid()), encoding="utf-8")
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "-q", "yt-dlp[default]"],
                           capture_output=True, text=True, timeout=600, creationflags=subprocess.CREATE_NO_WINDOW)
        note(f"обновление yt-dlp: код {r.returncode} {r.stderr.strip()[-300:]}")
        if r.returncode == 0:  # отметку о проверке ставим только после удачи: иначе повтора не было бы сутки
            tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".new")
            tmp.write_text(json.dumps({**settings, "ytdlp_checked": time.time()}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, SETTINGS_FILE)
    except (OSError, subprocess.SubprocessError) as e:
        note(f"обновление yt-dlp не удалось: {e!r}")
    finally:
        UPDATING.unlink(missing_ok=True)


def wait_for_update():
    """Если прямо сейчас обновляется yt-dlp — подождать: программа, стартовавшая посреди замены его файлов, не запустится."""
    deadline = time.time() + 120
    while UPDATING.exists() and time.time() < deadline:
        if time.time() - UPDATING.stat().st_mtime > 900:  # отметка осталась от прерванного обновления
            UPDATING.unlink(missing_ok=True)
            break
        time.sleep(0.5)


def start_server():
    wait_for_update()
    args = [str(runtime(HERE, "pythonw.exe")), str(HERE / "server.py"), "--auto"]
    # программа должна пережить и этот скрипт, и закрытие Chrome
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB
    (HERE / "logs").mkdir(exist_ok=True)
    # Окна у программы нет, поэтому всё, что она напечатает при неудачном запуске (не тот Python, сломанная
    # библиотека), пишем в файл: иначе причину было бы негде увидеть
    with open(HERE / "logs" / "server-stderr.log", "ab") as errors:
        kw = dict(cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, close_fds=True)
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
            start_server()
            note("качалка запущена по просьбе расширения")
        reply({"ok": True, "started": started})
    except Exception as e:  # noqa: BLE001 — Chrome ждёт ответа в любом случае: о сбое сообщаем ему и пишем в лог
        note(f"сбой запуска: {e!r}")
        reply({"ok": False, "error": repr(e)})


if __name__ == "__main__":
    if "--install" in sys.argv:
        install()
    elif "--update" in sys.argv:
        update_ytdlp()
    else:
        main()
