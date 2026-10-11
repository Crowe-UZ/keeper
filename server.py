"""Локальный сервер-качалка: принимает ссылку от расширения Chrome и скачивает через yt-dlp.

Запуск: её поднимает само расширение (launcher.py, после install.bat) — без окна, со значком в трее;
start.bat — запасной способ, с окном.
Слушает только 127.0.0.1 и принимает запросы только от расширений Chrome.

Два вида задач:
  - ссылка на страницу (YouTube, VK, ...) — yt-dlp сам находит видео;
  - пойманный расширением поток (m3u8/mpd/mp4) с купленного курса и т.п. —
    качаем его с заголовками и cookies вкладки, будто это делает сам браузер.

Логи (папка logs рядом с этим файлом):
  - kachalka.log          — общий журнал: запуск, каждая задача, итог, ошибки;
  - jobs/<дата>_<id>.log  — всё про одну загрузку: что прислало расширение, полный вывод yt-dlp,
                            что реально получилось (ffprobe). Значения cookies не пишутся никогда.
"""
import json
import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yt_dlp
from yt_dlp.version import __version__ as YTDLP_VERSION

HOST, PORT = "127.0.0.1", int(os.environ.get("KACHALKA_PORT", 5577))
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"
DEFAULT_SAVE_DIR = Path.home() / "Downloads" / "Videos"


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(**fields):
    SETTINGS_FILE.write_text(json.dumps({**load_settings(), **fields}, ensure_ascii=False), encoding="utf-8")


def load_save_dir():
    try:
        d = Path(load_settings()["save_dir"])
        if d.is_dir():
            return d
    except Exception:
        pass
    return DEFAULT_SAVE_DIR


def temp_dir(save_dir):
    """Куски недокачанных видео — в скрытой подпапке .tmp, чтобы не путать с готовыми."""
    t = Path(save_dir) / ".tmp"
    t.mkdir(parents=True, exist_ok=True)
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(t), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass
    return t


SAVE_DIR = load_save_dir()  # текущая папка; меняется кнопкой «Сменить папку», задача запоминает свою
SAVE_DIR.mkdir(parents=True, exist_ok=True)
WORKERS = 3  # сколько видео качается одновременно, остальные ждут в очереди
AUTO = "--auto" in sys.argv  # запущена расширением: без окна, сама закрывается, когда нечего делать
IDLE_EXIT = 10 * 60  # секунд без загрузок и без открытого окна расширения
ICON = Path(__file__).resolve().parent / "extension" / "icon.png"
NO_WINDOW = subprocess.CREATE_NO_WINDOW  # без окна у качалки нет консоли — чтобы ffprobe и прочие не мигали чёрным окном

if sys.stdout is None:  # pythonw: консоли нет вообще, а yt-dlp и print в неё пишут
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

LOG_DIR = Path(__file__).resolve().parent / "logs"
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

jobs = {}  # id -> данные задачи (см. do_POST)
lock = threading.Lock()
tasks = queue.Queue()
ACTIVE = ("подготовка", "скачивается", "склейка", "отмена…")  # статусы, при которых задача занимает обработчик
FINAL = ("готово", "ошибка", "отменено")  # с этого начинается статус завершённой задачи
HEIGHTS = (2160, 1440, 1080, 720, 480, 360)  # допустимые ограничения качества; 0 — без ограничения
HISTORY_FILE = Path(__file__).resolve().parent / "history.json"  # что уже скачано — чтобы не качать второй раз
stopping = threading.Event()  # качалка закрывается: загрузки прервать, новые не начинать
last_activity = time.time()  # когда расширение последний раз что-то спрашивало
tray = None


def find_ffmpeg():
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


def tool_version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout
        return out.splitlines()[0].strip() if out else "?"
    except Exception as e:
        return f"не найден ({e.__class__.__name__})"


def environment():
    ffmpeg = str(Path(FFMPEG_DIR) / "ffmpeg") if FFMPEG_DIR else None
    return {
        "python": sys.version.split()[0],
        "yt-dlp": YTDLP_VERSION,
        "ffmpeg": tool_version([ffmpeg, "-version"]) if ffmpeg else "НЕ НАЙДЕН",
        "js": {n: tool_version([n, "--version"]) for n in JS_RUNTIMES} or "НЕТ",
        "os": platform.platform(),
        "save_dir": str(SAVE_DIR),
    }


# ---------- лог одной задачи ----------

def job_logger(job_id):
    lg = logging.getLogger(f"job.{job_id}")
    if not lg.handlers:
        path = JOB_LOG_DIR / f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{job_id}.log"
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(LOG_FORMAT)
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        with lock:
            jobs[job_id]["log_file"] = str(path)
    return lg


def close_job_logger(job_id):
    lg = logging.getLogger(f"job.{job_id}")
    for h in list(lg.handlers):
        h.close()
        lg.removeHandler(h)


class YtdlpLog:
    """Весь вывод yt-dlp (включая отладочный) — в лог задачи."""

    def __init__(self, lg):
        self.lg = lg

    def debug(self, msg):
        # yt-dlp шлёт сюда и обычный вывод; строки прогресса не нужны — их тысячи
        if not msg.startswith("[download]  ") or "Destination" in msg:
            self.lg.debug("yt-dlp: " + msg)

    def info(self, msg):
        self.lg.info("yt-dlp: " + msg)

    def warning(self, msg):
        self.lg.warning("yt-dlp: " + msg)

    def error(self, msg):
        self.lg.error("yt-dlp: " + msg)


def redact_cookies(cookies):
    """Для лога: только домен и имя cookie, значение — никогда."""
    return sorted({f"{c.get('domain')}:{c.get('name')}" for c in cookies})


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
    except Exception as e:
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


# ---------- скачивание ----------

def update(job_id, **fields):
    with lock:
        jobs[job_id].update(fields)


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


def friendly_error(msg):
    low = msg.lower()
    if "drm" in low:
        return "Видео защищено DRM — такое скачать нельзя."
    if "unsupported url" in low:
        return "На странице не нашлось видео. Включи ролик (▶) и попробуй снова — расширение поймает поток."
    if any(w in low for w in ("sign in", "login", "log in", "private", "403", "401", "forbidden", "cookies")):
        return "Сайт не пустил (нужен вход или ссылка устарела). Обнови страницу, включи видео и скачай снова."
    if "ffmpeg" in low:
        return "Проблема с ffmpeg при склейке. Подробности — в логе."
    return msg[:300]


def build_opts(job_id, job, cookie_file, lg):
    last_log = {"t": 0}

    def hook(d):
        if stopping.is_set() or job.get("cancel"):
            raise yt_dlp.utils.DownloadCancelled("загрузка отменена" if job.get("cancel") else "качалка закрывается")
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            speed = re.sub(r"\x1b\[[0-9;]*m", "", d.get("_speed_str") or "").strip()  # без цветовых кодов терминала
            fields = {"status": "скачивается", "speed": speed, "eta": d.get("eta"),
                      "done_mb": round((d.get("downloaded_bytes") or 0) / 1048576)}
            if total:
                fields["percent"] = round(d["downloaded_bytes"] * 100 / total, 1)
            elif d.get("fragment_count"):
                fields["percent"] = round((d.get("fragment_index") or 0) * 100 / d["fragment_count"], 1)
            update(job_id, **fields)
            if time.time() - last_log["t"] > 10:  # прогресс в лог — раз в 10 секунд, не чаще
                last_log["t"] = time.time()
                lg.debug(f"прогресс {fields.get('percent', '?')}% скорость {fields['speed']}")
        elif d["status"] == "finished":
            lg.info(f"часть скачана: {d.get('filename')}")
            update(job_id, status="склейка", speed="")

    opts = {
        "outtmpl": unique_template(job["title"], job["save_dir"]) if job["sniffed"] else "%(title).150B [%(id)s].%(ext)s",
        "paths": {"home": job["save_dir"], "temp": str(temp_dir(job["save_dir"]))},
        "logger": YtdlpLog(lg),
        "verbose": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "noplaylist": True,
        "windowsfilenames": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 16,  # потоки HLS качаем кусками параллельно — быстрее
        "http_headers": job["headers"],
    }
    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES
    if cookie_file:
        opts["cookiefile"] = cookie_file
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR

    # ограничение качества из окна расширения; если такого разрешения у видео нет — берём лучшее, что есть
    cap = f"[height<={job['max_height']}]" if job["max_height"] else ""
    if job["mode"] == "audio":
        opts["format"] = "bestaudio/best"
        if FFMPEG_DIR:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
    elif FFMPEG_DIR:
        # максимальное разрешение/fps/битрейт: лучшее видео + лучший звук, склеить без пережатия
        opts["format"] = f"bv*{cap}+ba/b{cap}/bv*+ba/b" if cap else "bv*+ba/b"
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]
    else:
        opts["format"] = f"b{cap}/b" if cap else "b"  # без ffmpeg склеивать нечем — берём готовый файл со звуком
    return opts


# ---------- GetCourse ----------
# Урок GetCourse = страница с iframe плеера «…/sign-player/?json=…», а в плеере — ссылка на HLS.
# yt-dlp знает этот плеер только на двух адресах, а у школ бывают свои домены и новые адреса плеера,
# поэтому ищем плеер по пути, а не по домену, и зовём экстрактор yt-dlp напрямую.
SIGN_PLAYER_RE = re.compile(r"https?://[^/\s\"'<>]+/sign-player/?\?[^\"'<>\s]*json=[^\"'<>\s]+")
GETCOURSE_LESSON_RE = re.compile(r"/(?:pl/)?teach/control/lesson/view|/pl/teach/")


def fetch_text(ydl, url, headers):
    from yt_dlp.networking import Request
    with ydl.urlopen(Request(url, headers=headers)) as r:
        return r.read().decode("utf-8", "replace"), r.url


def getcourse_players(ydl, page_url, headers, lg):
    """Ссылки на плееры GetCourse со страницы урока (страница качается с cookies вкладки)."""
    import html
    text, final_url = fetch_text(ydl, page_url, headers)
    lg.info(f"GetCourse: страница урока загружена, итоговый адрес {final_url}, {len(text)} символов")
    if "/cms/system/login" in final_url:
        raise RuntimeError("GetCourse отправил на страницу входа — cookies не подошли. Обнови урок в Chrome и попробуй снова.")
    found = list(dict.fromkeys(html.unescape(u) for u in SIGN_PLAYER_RE.findall(text)))
    lg.info(f"GetCourse: найдено плееров: {len(found)} {[u.split('?')[0] for u in found]}")
    if not found:
        iframes = re.findall(r"<iframe[^>]+src=[\"']([^\"']+)", text)
        lg.info(f"GetCourse: плееров нет; iframe на странице: {[i.split('?')[0] for i in iframes]}")
    return found


def getcourse_info(ydl, player_url, lg):
    ie = ydl.get_info_extractor("GetCourseRuPlayer")
    ie.initialize()
    info = ie._real_extract(player_url)
    ydl.add_default_extra_info(info, ie, player_url)
    lg.info(f"GetCourse: плеер разобран, id={info.get('id')}, форматов {len(info.get('formats') or [])}")
    return info


def describe_format(f):
    return (f"{f.get('format_id')} {f.get('ext')} {f.get('resolution') or ''} {f.get('fps') or ''}fps "
            f"v={f.get('vcodec')} a={f.get('acodec')} tbr={f.get('tbr')}")


def run_download(job_id):
    job = jobs[job_id]
    lg = job_logger(job_id)
    started = time.time()
    lg.info(f"=== задача {job_id} ===")
    lg.info(f"окружение: {json.dumps(environment(), ensure_ascii=False)}")
    lg.info(f"url: {job['url']}")
    lg.info(f"вкладка: {job['title']} | {job['page_url']}")
    lg.info(f"режим: {job['mode']} | пойманный поток: {job['sniffed']} | качество: {job['max_height'] or 'максимум'}")
    lg.info(f"заголовки: {job['headers']}")
    lg.info(f"cookies ({len(job['cookies'])} шт., только имена): {redact_cookies(job['cookies'])}")
    if job["debug"]:
        lg.info(f"от расширения: {json.dumps(job['debug'], ensure_ascii=False)}")

    cookie_file = write_cookies(job["cookies"])
    names = []  # имена файлов, которые мы сами назначили: по ним при неудаче убираем недокачанные куски
    try:
        # для обычных сайтов сначала пробуем без cookies (YouTube не любит чужие сессии),
        # при отказе — повторяем с cookies вкладки. Пойманные потоки — сразу с cookies.
        url = job["url"]
        is_player = bool(SIGN_PLAYER_RE.match(url))
        is_lesson = bool(GETCOURSE_LESSON_RE.search(url)) and not is_player
        if job["sniffed"] or is_player or is_lesson:
            attempts = [cookie_file]  # закрытый контент — только с входом
        else:
            attempts = [None, cookie_file] if cookie_file else [None]
        for i, cf in enumerate(attempts):
            try:
                lg.info(f"попытка {i + 1}/{len(attempts)}: {'с cookies' if cf else 'без cookies'}")
                update(job_id, status="подготовка", error=None)
                with yt_dlp.YoutubeDL(build_opts(job_id, job, cf, lg)) as ydl:
                    if is_player:
                        lg.info("GetCourse: ссылка на плеер")
                        infos = [getcourse_info(ydl, url, lg)]
                    elif is_lesson:
                        lg.info("GetCourse: страница урока, ищу плееры")
                        headers = {**job["headers"], "Referer": url}
                        players = getcourse_players(ydl, url, headers, lg)
                        if not players:
                            raise RuntimeError("На странице урока не нашлось плеера GetCourse. Включи видео (▶) — расширение поймает поток.")
                        infos = [getcourse_info(ydl, pu, lg) for pu in players]
                    else:
                        infos = [ydl.extract_info(url, download=False)]
                    named = job["sniffed"] or is_player or is_lesson  # имя файла = заголовок вкладки
                    results = []
                    for n, info in enumerate(infos, 1):
                        lg.info(f"видео {n}/{len(infos)}: extractor={info.get('extractor')} id={info.get('id')} title={info.get('title')!r}")
                        lg.info(f"доступно форматов: {len(info.get('formats') or [])}")
                        if named:
                            name = job["title"] if len(infos) == 1 else f"{job['title']} — часть {n}"
                            template = unique_template(name, job["save_dir"])
                            names.append(template.removesuffix(".%(ext)s").replace("%%", "%"))
                            ydl.params["outtmpl"]["default"] = template
                        else:
                            update(job_id, title=info.get("title") or job["title"])
                        result = ydl.process_ie_result(info, download=True)
                        for f in result.get("requested_formats") or [result]:
                            lg.info(f"выбран формат: {describe_format(f)}")
                        results.append(result)
                summary = []
                for result in results:
                    path = ((result.get("requested_downloads") or [{}])[0].get("filepath")) or result.get("filepath")
                    p = probe(path) if path and os.path.exists(path) else None
                    if p:
                        lg.info(f"ffprobe итога: {json.dumps(p, ensure_ascii=False)}")
                    size = os.path.getsize(path) / 1024 / 1024 if path and os.path.exists(path) else 0
                    summary.append((path, size, describe_probe(p)))
                real = summary[0][2] if summary else ""
                parts = f" ({len(summary)} видео)" if len(summary) > 1 else ""
                update(job_id, status="готово" + parts, percent=100, speed="", file=summary[0][0] if summary else None, quality=real)
                for path, size, q in summary:
                    msg = f"[{job_id}] ГОТОВО за {time.time() - started:.0f} с: {path} ({size:.1f} МБ) {q}"
                    lg.info(msg)
                    log.info(msg)
                if summary and summary[0][0]:
                    remember(job, summary[0][0])
                    toast("Видео скачано", job["title"], summary[0][0])
                names.clear()
                return
            except Exception as e:
                lg.warning(f"попытка {i + 1} не удалась: {e}\n{traceback.format_exc()}")
                if i == len(attempts) - 1 or stopping.is_set() or job.get("cancel"):
                    raise
                log.info(f"[{job_id}] без cookies не вышло, пробую с cookies")
    except Exception as e:  # yt-dlp бросает разные исключения, показываем понятный текст
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
        if job.get("cancel"):
            update(job_id, status="отменено", error=None, speed="", percent=0)
            lg.info("загрузка отменена пользователем")
            log.info(f"[{job_id}] отменено")
        else:
            update(job_id, status="ошибка", error=friendly_error(msg), speed="")
            lg.error(f"ОШИБКА: {msg}")
            log.error(f"[{job_id}] ОШИБКА: {msg[:500]} | лог: {jobs[job_id].get('log_file')}")
            if not stopping.is_set():
                toast("Не получилось скачать", f"{job['title']}\n{friendly_error(msg)}")
    finally:
        if cookie_file:
            os.unlink(cookie_file)
        remove_parts(job["save_dir"], names)
        close_job_logger(job_id)


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
            try:
                part.unlink()
            except OSError:
                pass
        time.sleep(0.3)


# ---------- что уже скачано ----------

def job_key(job):
    """По чему узнаём то же самое видео. У пойманного потока ссылка каждый раз новая (токены),
    поэтому для него это страница + название."""
    if job["sniffed"]:
        return f"{job['mode']}|{job['page_url']}|{job['title']}"
    return f"{job['mode']}|{job['url']}"


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def remember(job, path):
    with lock:
        items = [h for h in load_history() if h.get("key") != job["key"]]
        items.append({"key": job["key"], "file": path, "time": datetime.now().isoformat(timespec="seconds")})
        HISTORY_FILE.write_text(json.dumps(items[-1000:], ensure_ascii=False, indent=1), encoding="utf-8")


def already_downloaded(key):
    """Файл, в который это видео уже скачано, — если он ещё лежит на месте."""
    for h in load_history():
        if h.get("key") == key and os.path.exists(h.get("file") or ""):
            return h["file"]
    return None


# ---------- уведомление Windows ----------

def toast(title, text, file=None):
    """Всплывающее уведомление в углу экрана. Для готового видео — с кнопками «Открыть» и «Папка»."""
    from xml.sax.saxutils import escape, quoteattr
    actions = launch = ""
    if file:
        uri = Path(file).as_uri()
        launch = f" activationType=\"protocol\" launch={quoteattr(uri)}"
        actions = (f"<actions><action content=\"Открыть\" activationType=\"protocol\" arguments={quoteattr(uri)}/>"
                   f"<action content=\"Папка\" activationType=\"protocol\" arguments={quoteattr(Path(file).parent.as_uri())}/></actions>")
    xml = (f"<toast{launch}><visual><binding template=\"ToastGeneric\"><text>{escape(title)}</text>"
           f"<text>{escape(text[:200])}</text></binding></visual>{actions}</toast>")
    # своего «приложения» у качалки в Windows нет, поэтому уведомление показываем от имени PowerShell
    ps = ("[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null\n"
          "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null\n"
          "$x = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
          f"$x.LoadXml(@'\n{xml}\n'@)\n"
          "$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe'\n"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show((New-Object Windows.UI.Notifications.ToastNotification $x))")

    def show():
        import base64
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", base64.b64encode(ps.encode("utf-16-le")).decode()],
                               capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
            if r.returncode:
                log.warning(f"уведомление Windows не показалось: {r.stderr.strip()[:300]}")
        except Exception as e:
            log.warning(f"уведомление Windows не показалось: {e!r}")

    threading.Thread(target=show, daemon=True).start()


def summary():
    with lock:
        st = [v["status"] for v in jobs.values()]
    active = sum(s in ACTIVE for s in st)
    return {"active": active, "queued": st.count("в очереди"),
            "done": sum(s.startswith("готово") for s in st), "failed": st.count("ошибка")}


def fmt_eta(sec):
    if not sec:
        return "?"
    sec = int(sec)  # yt-dlp иногда отдаёт дробное
    return f"{sec // 60} мин {sec % 60:02d} с" if sec >= 60 else f"{sec} с"


def reporter():
    """Раз в 15 секунд, пока что-то качается: что качается, сколько осталось."""
    while True:
        time.sleep(15)
        try:
            report_once()
        except Exception:
            log.exception("сбой отчёта о прогрессе (загрузки не затронуты)")


def report_once():
    if True:
        with lock:
            active = [(k, dict(v)) for k, v in jobs.items() if v["status"] in ACTIVE]
        if not active:
            return
        s = summary()
        log.info(f"--- сейчас: качается {s['active']}, в очереди {s['queued']}, готово {s['done']}, ошибок {s['failed']}")
        for k, v in active:
            log.info(f"    [{k}] {v['status']} {v['percent']}% · {v['done_mb']} МБ · {v['speed'] or '?'} · "
                     f"осталось ~{fmt_eta(v.get('eta'))} · {v['title'][:60]}")


def worker():
    while True:
        job_id = tasks.get()
        try:
            job = jobs.get(job_id)  # задачу могли отменить или убрать из списка, пока она ждала очереди
            if job and not job.get("cancel") and not stopping.is_set():
                run_download(job_id)
        except Exception:
            log.exception(f"[{job_id}] непредвиденный сбой обработчика")
        finally:
            tasks.task_done()


# ---------- HTTP для расширения ----------

PUBLIC_FIELDS = ("url", "title", "status", "percent", "speed", "eta", "done_mb", "quality", "error", "mode")


class Handler(BaseHTTPRequestHandler):
    def _allowed(self):
        # Сайты не должны иметь возможность дёргать качалку — только расширение.
        # Chrome шлёт Origin расширения не во всех запросах (в GET его нет), поэтому расширение
        # всегда добавляет заголовок X-Kachalka. Сайт такой заголовок без preflight не пошлёт,
        # а preflight (OPTIONS) от чужого Origin мы отклоняем.
        origin = self.headers.get("Origin", "")
        if origin and not origin.startswith("chrome-extension://"):
            log.warning(f"отклонён запрос {self.command} {self.path} от чужого сайта Origin={origin!r}")
            return False
        if self.command != "OPTIONS" and self.headers.get("X-Kachalka") != "1":
            log.warning(f"отклонён запрос {self.command} {self.path}: нет заголовка расширения (Origin={origin!r})")
            return False
        global last_activity
        last_activity = time.time()
        return True

    def _send(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Kachalka")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204 if self._allowed() else 403, {})

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        if path == "/status":
            with lock:
                items = [{"id": k, "has_log": bool(v.get("log_file")), "has_file": bool(v.get("file")), **{f: v[f] for f in PUBLIC_FIELDS}} for k, v in jobs.items()]
            return self._send(200, {"jobs": items[::-1][:30], "summary": summary(), "folder": str(SAVE_DIR), "ffmpeg": bool(FFMPEG_DIR),
                                    "ytdlp": YTDLP_VERSION})
        if path == "/open-folder":
            open_path(SAVE_DIR)
            return self._send(200, {"ok": True})
        if path == "/choose-folder":
            return self._choose_folder()
        if path in ("/open-file", "/show-file"):
            job = jobs.get(query.removeprefix("id="))
            f = job and job.get("file")
            if not f or not os.path.exists(f):
                return self._send(404, {"error": "файл не найден — его переместили или удалили"})
            if path == "/open-file":
                open_path(f)  # откроется в плеере по умолчанию
            else:
                open_in_front(lambda: subprocess.Popen(["explorer", "/select,", f]))  # Проводник с выделенным файлом
            return self._send(200, {"ok": True})
        if path == "/open-logs":
            open_path(LOG_DIR)
            return self._send(200, {"ok": True})
        if path == "/open-log":
            job = jobs.get(query.removeprefix("id="))
            if job and job.get("log_file"):
                open_path(job["log_file"])
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "лог не найден"})
        if path == "/clear":
            with lock:
                for k in [k for k, v in jobs.items() if v["status"].startswith(FINAL)]:
                    del jobs[k]
            return self._send(200, {"ok": True})
        if path in ("/cancel", "/retry"):
            job_id = query.removeprefix("id=")
            with lock:
                job = jobs.get(job_id)
                if not job:
                    return self._send(404, {"error": "такой загрузки уже нет в списке"})
                if path == "/cancel" and not job["status"].startswith(FINAL):
                    job["cancel"] = True  # работающую загрузку прервёт её же обработчик прогресса
                    job["status"] = "отменено" if job["status"] == "в очереди" else "отмена…"
                    job["speed"] = ""
                elif path == "/retry" and job["status"].startswith(("ошибка", "отменено")):
                    job.update(cancel=False, status="в очереди", error=None, percent=0, speed="", eta=None, done_mb=0)
                    tasks.put(job_id)
            log.info(f"[{job_id}] {'отмена' if path == '/cancel' else 'повтор'} по просьбе пользователя")
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/client-log":
            return self._client_log()
        if self.path != "/download":
            return self._send(404, {"error": "not found"})
        try:
            data = self._read_json()
        except ValueError as e:
            log.warning(f"кривой запрос /download: {e}")
            return self._send(400, {"error": f"кривой запрос: {e}"})
        url = (data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            log.warning(f"отклонена ссылка не http(s): {url[:200]!r}")
            return self._send(400, {"error": "нужна ссылка http(s)"})
        new = {"url": url, "title": data.get("title") or url, "page_url": data.get("page_url") or "",
               "mode": data.get("mode", "video"), "sniffed": bool(data.get("sniffed"))}
        key = job_key(new)
        if not data.get("force"):  # «Скачать ещё раз» в окне расширения шлёт force
            with lock:
                busy = any(v["key"] == key and not v["status"].startswith(FINAL) for v in jobs.values())
            done = None if busy else already_downloaded(key)
            if busy or done:
                why = "Это видео уже качается" if busy else f"Уже скачано: {Path(done).name}"
                log.info(f"повтор не начат ({why}): {new['title']!r}")
                return self._send(409, {"error": why, "duplicate": True})
        job_id = uuid.uuid4().hex[:8]
        with lock:
            jobs[job_id] = {
                **new,
                "key": key,
                "max_height": data.get("max_height") if data.get("max_height") in HEIGHTS else 0,
                "cancel": False,
                "headers": {k: v for k, v in (data.get("headers") or {}).items() if k in ("Referer", "Origin", "User-Agent")},
                "cookies": data.get("cookies") or [],
                "debug": data.get("debug") or {},
                "save_dir": str(SAVE_DIR),
                "status": "в очереди", "percent": 0, "speed": "", "eta": None, "done_mb": 0, "quality": "", "file": None, "error": None,
                "log_file": None,
            }
        log.info(f"[{job_id}] новая задача ({data.get('mode', 'video')}{', поток' if data.get('sniffed') else ''}): "
                 f"{data.get('title')!r} <- {url[:200]}")
        tasks.put(job_id)
        self._send(200, {"id": job_id})

    def _choose_folder(self):
        """Стандартное окно Windows «Выбор папки» — прямо на этом ПК, поверх Chrome."""
        global SAVE_DIR
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(parent=root, initialdir=str(SAVE_DIR), title="Куда сохранять видео?")
        root.destroy()
        if not chosen:
            return self._send(200, {"folder": str(SAVE_DIR), "changed": False})
        SAVE_DIR = Path(chosen)
        save_settings(save_dir=str(SAVE_DIR))
        log.info(f"папка для видео изменена: {SAVE_DIR}")
        return self._send(200, {"folder": str(SAVE_DIR), "changed": True})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) or b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError as e:
            raise ValueError(f"не UTF-8 ({e})")
        except json.JSONDecodeError as e:
            raise ValueError(f"не JSON ({e})")

    def _client_log(self):
        """Ошибки и события самого расширения — в тот же общий журнал."""
        try:
            data = self._read_json()
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        level = logging.WARNING if data.get("level") == "error" else logging.INFO
        log.log(level, f"[расширение] {str(data.get('message'))[:2000]}")
        self._send(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass  # стандартный шумный лог каждого HTTP-запроса не нужен


# ---------- открыть папку или файл поверх остальных окон ----------
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
    except Exception:
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
        except Exception as e:
            log.warning(f"не смог вывести окно вперёд: {e!r}")

    threading.Thread(target=safely, daemon=True).start()


def open_path(path):
    """Папка или файл — в программе по умолчанию, поверх остальных окон."""
    path = Path(path)
    open_in_front(lambda: os.startfile(path), title=path.name if path.is_dir() else None)


# ---------- значок в трее и завершение ----------

def status_line():
    s = summary()
    if s["active"] or s["queued"]:
        return f"Качается: {s['active']} · в очереди: {s['queued']}"
    return "Загрузок нет"


def run_tray():
    """Значок у часов: видно, что качалка работает, и есть откуда её закрыть."""
    global tray
    try:
        import pystray
        from PIL import Image
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: status_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Папка с видео", lambda: open_path(SAVE_DIR), default=True),
            pystray.MenuItem("Логи", lambda: open_path(LOG_DIR)),
            pystray.MenuItem("Выход", lambda: shutdown("выход из меню в трее")),
        )
        tray = pystray.Icon("kachalka", Image.open(ICON), "Качалка видео", menu)
    except Exception as e:
        return log.warning(f"значок в трее недоступен ({e!r}) — работаю без него")
    threading.Thread(target=tray_status, daemon=True).start()
    tray.run()


def tray_status():
    shown = None
    while True:
        time.sleep(2)
        line = status_line()
        if line != shown:
            shown = line
            tray.title = "Качалка видео — " + line.lower()
            tray.update_menu()


def idle_watch():
    """Запущенная расширением качалка не висит в памяти зря: нет дел 10 минут — закрывается."""
    global last_activity
    while True:
        time.sleep(15)
        s = summary()
        if s["active"] or s["queued"]:
            last_activity = time.time()
        elif time.time() - last_activity > IDLE_EXIT:
            shutdown(f"{IDLE_EXIT // 60} минут без дела")


def code_watch():
    """server.py на диске обновился, а работает старый — закрываемся, как только нет загрузок:
    расширение при следующем обращении поднимет уже новую качалку."""
    me = Path(__file__).resolve()
    loaded = me.stat().st_mtime
    while True:
        time.sleep(3)
        try:
            changed = me.stat().st_mtime != loaded
        except OSError:
            continue
        s = summary()
        if changed and not s["active"] and not s["queued"]:
            shutdown("код качалки обновился — перезапуск")


def kill_children():
    """ffmpeg, запущенный для склейки, сам не остановится — а его недоделанный файл держит папку .tmp."""
    ps = (f"Get-CimInstance Win32_Process -Filter \"ParentProcessId={os.getpid()} and Name like 'ff%'\" "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=15,
                       creationflags=NO_WINDOW)
    except Exception as e:
        log.warning(f"не смог остановить ffmpeg: {e!r}")


def clean_temp(dirs):
    for d in dirs:
        shutil.rmtree(Path(d) / ".tmp", ignore_errors=True)


def shutdown(reason):
    """Закрыться чисто: прервать загрузки, убрать недокачанные куски, снять значок."""
    if stopping.is_set():
        return
    stopping.set()
    interrupted = summary()["active"]
    log.info(f"Качалка закрывается: {reason}" + (f" (прервано загрузок: {interrupted})" if interrupted else ""))
    deadline = time.time() + 5
    while summary()["active"] and time.time() < deadline:
        time.sleep(0.2)
    if interrupted:
        kill_children()
    with lock:
        dirs = {v["save_dir"] for v in jobs.values()} | {str(SAVE_DIR)}
    clean_temp(dirs)
    if tray:
        try:
            tray.stop()
        except Exception:
            pass
    logging.shutdown()
    os._exit(0)


def _thread_crash(args):
    log.error(f"СБОЙ ПОТОКА {args.thread.name if args.thread else '?'}: {args.exc_type.__name__}: {args.exc_value}",
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


threading.excepthook = _thread_crash  # любые падения фоновых потоков — в kachalka.log

if __name__ == "__main__":
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        log.error(f"Не могу занять порт {PORT}: {e}. Похоже, качалка уже запущена.")
        sys.exit(1)
    clean_temp([SAVE_DIR])  # куски от прошлого запуска, если его закрыли грубо
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reporter, daemon=True).start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=run_tray, daemon=True).start()
    if AUTO:
        threading.Thread(target=idle_watch, daemon=True).start()
        threading.Thread(target=code_watch, daemon=True).start()
    env = environment()
    log.info(f"Качалка запущена: http://{HOST}:{PORT}" + (" (расширением, закроется сама)" if AUTO else ""))
    log.info(f"Файлы: {SAVE_DIR} | Логи: {LOG_DIR}")
    log.info(f"yt-dlp {env['yt-dlp']} | Python {env['python']} | одновременно качается: {WORKERS}")
    log.info(f"ffmpeg: {env['ffmpeg']}")
    log.info(f"JS-движок для YouTube: {env['js']}")
    if not FFMPEG_DIR:
        log.warning("ffmpeg НЕ НАЙДЕН — качество будет хуже, mp3 недоступен")
    print("Остановить: Ctrl+C или «Выход» в меню значка у часов")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown("Ctrl+C")
