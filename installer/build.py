"""Собрать установщик VideoTranskeeper-Setup-<версия>.exe: один файл, внутри свой Python, библиотеки, ffmpeg и расширение.

Запуск из папки проекта:  .venv\\Scripts\\python.exe installer\\build.py
Нужен Inno Setup 6 (winget install JRSoftware.InnoSetup) и интернет при первом запуске: Python и ffmpeg
скачиваются в installer/cache и сверяются по контрольной сумме. Результат — в installer/out.
Модели распознавания внутрь не кладутся: установщик предлагает скачать их галочками.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE, STAGE, OUT = HERE / "cache", HERE / "stage", HERE / "out"

# Что кладём внутрь. Версии закреплены: сменил ссылку — смени и сумму (sha256 скачанного файла).
PYTHON = ("python.tar.gz",
          "https://github.com/astral-sh/python-build-standalone/releases/download/20261009/"
          "cpython-3.12.15%2B20261009-x86_64-pc-windows-msvc-install_only.tar.gz",
          "194c8f0d9e0de6f1c4576e5dfa67819a48c764be87608b74b8ad43387a57b93b")
# Сборка LGPL: её можно раздавать вместе с программой, приложив лицензию. Кодировать H.264 она не умеет,
# но нам и не нужно: видео склеивается без пережатия, звук — AAC и MP3.
FFMPEG = ("ffmpeg.zip",
          "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-10-13-04/"
          "ffmpeg-n9.0.2-25-g67b60c310b-win64-lgpl-shared-9.0.zip",
          "a68c86127a4185bcb9035d39d54f8b6873681896b860af999d8987be3c696de4")
LIBRARIES = ["yt-dlp[default]", "pystray", "pillow", "sherpa-onnx==1.13.8"]  # те же, что ставит install.bat
APP_FILES = ["server.py", "launcher.py", "host.bat", "README.md"]
ISCC_PLACES = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
               Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe"]


def say(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(name, url, expected):
    """Файл из кэша или из интернета; сумма сверяется в обоих случаях — чужое внутрь установщика не попадёт."""
    path = CACHE / name
    if not path.exists():
        say(f"скачиваю {name}…")
        CACHE.mkdir(exist_ok=True)
        part = path.with_name(name + ".part")
        with urllib.request.urlopen(url, timeout=60) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f)
        part.replace(path)
    got = sha256(path)
    if got != expected:
        raise SystemExit(f"{name}: контрольная сумма не сошлась ({got}). Удали файл из installer/cache и проверь ссылку.")
    return path


def run(args, **kw):
    subprocess.run([str(a) for a in args], check=True, **kw)


def stage_python():
    say("Python и библиотеки…")
    with tarfile.open(fetch(*PYTHON)) as tar:
        tar.extractall(STAGE, filter="data")  # в архиве одна папка python
    python = STAGE / "python" / "python.exe"
    run([python, "-m", "pip", "install", "-q", "--no-warn-script-location", "--disable-pip-version-check", *LIBRARIES])
    # проверяем сразу: всё ли загружается именно этим Python (tkinter — для окна выбора папки)
    run([python, "-c", "import yt_dlp, pystray, PIL, sherpa_onnx, tkinter"])
    return python


def stage_ffmpeg():
    say("ffmpeg…")
    with zipfile.ZipFile(fetch(*FFMPEG)) as z:
        for item in z.infolist():
            inner = item.filename.partition("/")[2]  # без верхней папки с названием сборки
            keep = inner == "LICENSE.txt" or (inner.startswith("bin/") and not inner.endswith(("/", "ffplay.exe")))
            if keep:
                target = STAGE / "ffmpeg" / inner
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(z.read(item))
    run([STAGE / "ffmpeg" / "bin" / "ffmpeg.exe", "-version"], stdout=subprocess.DEVNULL)


def stage_app():
    say("программа и расширение…")
    for name in APP_FILES:
        shutil.copy2(ROOT / name, STAGE / name)
    shutil.copytree(ROOT / "kachalka", STAGE / "kachalka", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "extension", STAGE / "extension")
    shutil.copy2(HERE / "next-steps.html", STAGE / "next-steps.html")
    shutil.copy2(HERE / "THIRD-PARTY.txt", STAGE / "THIRD-PARTY.txt")


def make_art():
    """Значок и картинки мастера установки — из значка расширения, чтобы установщик выглядел как само расширение."""
    from PIL import Image
    art = HERE / "art"
    art.mkdir(exist_ok=True)
    icon = Image.open(ROOT / "extension" / "icon.png").convert("RGBA")
    icon.save(art / "keeper.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128)])

    def canvas(size, color, icon_side, top):
        """Картинка заданного размера: фон и значок по центру по ширине."""
        image = Image.new("RGB", size, color)
        small = icon.resize((icon_side, icon_side), Image.LANCZOS)
        image.paste(small, ((size[0] - icon_side) // 2, top), small)
        return image

    canvas((147, 147), (255, 255, 255), 119, 14).save(art / "small.bmp")   # в углу каждой страницы
    # полоса слева на первой и последней странице: тёмный фон, значок и название
    from PIL import ImageDraw, ImageFont
    side = canvas((410, 797), (24, 24, 37), 128, 250)
    try:
        font = ImageFont.truetype(str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf"), 38)
        ImageDraw.Draw(side).text((205, 440), "VideoTranskeeper", font=font, fill=(255, 255, 255), anchor="mm")
    except OSError:  # нет такого шрифта — полоса останется без подписи, значка достаточно
        pass
    side.save(art / "side.bmp")


def check_stage(python):
    """Собранная программа видит свой ffmpeg и свои библиотеки — проверяем до упаковки, а не у пользователя."""
    code = ("import sys; from kachalka import system; from pathlib import Path; "
            "own = Path(sys.argv[1]); assert Path(system.FFMPEG_DIR) == own, system.FFMPEG_DIR; "
            "import launcher; assert launcher.runtime(launcher.HERE, 'pythonw.exe').is_file()")
    env = {**os.environ, "KACHALKA_PORT": "5599"}
    run([python, "-c", code, STAGE / "ffmpeg" / "bin"], cwd=STAGE, env=env)
    for leftover in ("logs", "settings.json", "history.json", "downloads"):  # следы проверки в установщик не кладём
        path = STAGE / leftover
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
    for cache in STAGE.rglob("__pycache__"):
        if "site-packages" not in cache.parts and "Lib" not in cache.parts:
            shutil.rmtree(cache, ignore_errors=True)


def main():
    iscc = next((p for p in ISCC_PLACES if p.is_file()), None)
    if not iscc:
        raise SystemExit("Не найден Inno Setup 6. Поставь его:  winget install JRSoftware.InnoSetup")
    if not (HERE / "keeper.iss").read_bytes().startswith(b"\xef\xbb\xbf"):
        # без этой пометки Inno Setup читает сценарий в кодировке Windows, и русский текст установщика превращается в кашу
        raise SystemExit("installer/keeper.iss должен быть сохранён как «UTF-8 со спецификацией (BOM)».")
    version = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))["version"]
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    python = stage_python()
    stage_ffmpeg()
    stage_app()
    check_stage(python)
    make_art()
    say("упаковываю (несколько минут)…")
    run([iscc, "/Q", f"/DAppVersion={version}", HERE / "keeper.iss"])
    result = OUT / f"VideoTranskeeper-Setup-{version}.exe"
    say(f"Готово: {result} ({result.stat().st_size / 1048576:.0f} МБ), sha256 {sha256(result)}")


if __name__ == "__main__":
    main()
