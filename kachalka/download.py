"""Скачивание: настройки yt-dlp, выполнение задачи, обработчики очереди, отчёт о прогрессе."""
import contextlib
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

import yt_dlp
from .config import ACTIVE, log
from .errors import UserError
from .state import jobs, lock, stopping, summary, tasks, update
from .files import describe_probe, probe, remove_parts, safe_name, temp_dir, unique_template, write_cookies
from .getcourse import GETCOURSE_LESSON_RE, SIGN_PLAYER_RE, getcourse_info, getcourse_players
from .history import downloaded_files, remember
from .joblog import YtdlpLog, close_job_logger, job_logger, redact_cookies
from .notify import toast
from .outputs import clean_formats, from_local, make_outputs, want_label
from .system import FFMPEG_DIR, JS_RUNTIMES, environment
from .textahead import TEXT_FORMAT, TextAhead


# Слова должны стоять отдельно, а не внутри пути или имени файла: «/login-video/x» и «Урок 403.mp4» — не отказ сайта
LOGIN_RE = re.compile(r"http error 40[13]\b|(?<![/\\\w.-])(?:sign in|log ?in|private|forbidden|unauthorized|cookies|"
                      r"members[- ]only)(?![/\\\w.-])", re.I)


def wants_login(msg):
    """Похоже ли сообщение yt-dlp на отказ сайта без входа. Только тогда есть смысл пробовать ещё раз с cookies вкладки."""
    return bool(LOGIN_RE.search(msg))


def friendly_error(msg):
    """Сообщение yt-dlp -> понятная причина. Ищем целые слова и коды ответа, а не буквы где попало:
    «403» в названии файла — не отказ сайта."""
    if re.search(r"\bdrm\b", msg, re.I):
        return "Видео защищено от копирования — скачать его нельзя."
    if re.search(r"unsupported url", msg, re.I):
        return "Не нашёл видео на странице. Включи его (▶), подожди пару секунд и нажми «Повторить»."
    if wants_login(msg):
        return "Сайт не отдал видео. Проверь, что ты вошёл на сайт, обнови страницу (F5), включи видео и скачай снова."
    if re.search(r"\bffmpeg\b", msg, re.I):
        return "Не получилось обработать файл. Проверь, что установлен ffmpeg (шаг 2 инструкции), и нажми «Повторить»."
    return msg[:300]


def explain(error):
    """Текст ошибки для человека: свои сообщения — как есть, чужие (yt-dlp, сайт) — через переводчик."""
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(error)).replace("ERROR: ", "")
    return msg[:300] if isinstance(error, UserError) else friendly_error(msg)


def progress_hook(job_id, job, lg):
    """Обработчик прогресса yt-dlp: обновляет задачу и прерывает загрузку при отмене или закрытии качалки."""
    last_log = {"t": 0}

    def downloading(d):
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

    def hook(d):
        if stopping.is_set() or job.get("cancel"):
            raise yt_dlp.utils.DownloadCancelled("загрузка отменена" if job.get("cancel") else "качалка закрывается")
        if d["status"] == "downloading":
            downloading(d)
        elif d["status"] == "finished":
            lg.info(f"часть скачана: {d.get('filename')}")
            update(job_id, status="склейка", speed="")

    return hook


def cancel_hook(job):
    """Обработчик шагов после скачивания (склейка, вынимание звука): там обработчик прогресса уже молчит,
    и «Отмена» раньше ждала конца всей обработки. Прерываем перед очередным шагом."""
    def hook(d):
        if d.get("status") == "started" and (stopping.is_set() or job.get("cancel")):
            raise yt_dlp.utils.DownloadCancelled("загрузка отменена" if job.get("cancel") else "качалка закрывается")

    return hook


# При равном разрешении берём H.264 + AAC: такой mp4 играет в любом плеере, телевизоре и редакторе.
# Выше 1080p у YouTube H.264 нет — там останется VP9/AV1, разрешение важнее.
SORT = ["fps", "hdr:12", "vcodec:h264", "acodec:m4a"]
# Звук: отдельная дорожка AAC (ложится в .m4a без пережатия), иначе любая отдельная дорожка; если звук вшит
# в видео — вариант не выше 480p (у самых нижних звук бывает урезан), затем 720p, затем самый лёгкий.
AUDIO_FORMAT = "ba[acodec^=mp4a]/ba/b[height<=480]/b[height<=720]/w"


def pick_format(mode, max_height, has_ffmpeg, private=False, formats=None):
    """Какой формат просить у yt-dlp. max_height — ограничение качества из окна расширения (0 — максимум):
    это предпочтение, а не фильтр — нет такого разрешения, возьмётся ближайшее; считается по меньшей стороне,
    так что вертикальное видео не путает. private — пойманный поток или урок курса: в файл ничего не дописываем."""
    res = f"res:{max_height}" if max_height else "res"
    chosen = clean_formats(formats)
    if mode == "text":
        return {"format": TEXT_FORMAT}  # самый лёгкий звук как есть: он временный, перекодировать незачем
    if mode == "audio":
        codec = {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}  # AAC ложится без пережатия
        if chosen["audio"] == "mp3":
            codec = {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "2"}
        return {"format": AUDIO_FORMAT, **({"postprocessors": [codec]} if has_ffmpeg else {})}
    if not has_ffmpeg:
        return {"format": "b", "format_sort": [res]}  # без ffmpeg склеивать нечем — берём готовый файл со звуком
    # лучшее видео + лучший звук, склеить без пережатия
    fmt = {"format": "bv*+ba/b", "format_sort": [res, *SORT], "merge_output_format": "mp4"}
    if chosen["video"] == "best":  # максимальное качество: любой кодек, а контейнер, в который ложится всё
        fmt = {"format": "bv*+ba/b", "format_sort": [res, "fps"], "merge_output_format": "mkv"}
    if not private:
        # название и описание — в файл. У пойманного потока «описание» — служебная ссылка плеера с номером
        # пользователя и его адресом, такое в файл не пишем
        fmt["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]
    return fmt


def fragments_for(active):
    """Сколько кусков HLS качать одновременно в одной загрузке. Запас — 16 соединений на все загрузки сразу:
    три загрузки по 16 к одному серверу кончались массовыми обрывами."""
    return max(4, 16 // max(1, active))


def retry_pause(n):
    """Пауза перед повтором оборвавшегося куска, секунд: растёт, но остаётся короткой.
    Имя аргумента — n: yt-dlp передаёт номер попытки именно под этим именем."""
    return min(2 ** n, 10)


def build_opts(job_id, job, cookie_file, lg):
    tmp = temp_dir(job["save_dir"])
    private = job["sniffed"] or job_kind(job["url"]) != "page"

    def pause(n):  # имя аргумента задаёт yt-dlp: он зовёт pause(n=номер попытки)
        # после «Отмены» потоки загрузки не должны спать перед повтором: пока они живы, недокачанный файл занят,
        # его не удалить, и «Повторить» получил бы имя с «(2)»
        return 0 if job.get("cancel") or stopping.is_set() else retry_pause(n)

    opts = {
        "outtmpl": unique_template(job["title"], job["save_dir"]) if job["sniffed"] else "%(title).150B [%(id)s].%(ext)s",
        # когда нужен только текст, звуковая дорожка в папку с видео не попадает вовсе — остаётся во временной
        "paths": {"home": str(tmp) if job["mode"] == "text" else job["save_dir"], "temp": str(tmp)},
        "logger": YtdlpLog(lg),
        "verbose": True,
        "noprogress": True,
        "progress_hooks": [progress_hook(job_id, job, lg)],
        "postprocessor_hooks": [cancel_hook(job)],
        "noplaylist": True,
        "windowsfilenames": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": fragments_for(summary()["active"]),  # HLS качаем кусками параллельно
        "skip_unavailable_fragments": False,  # не скачался кусок — это ошибка, а не «готово» с дырой в видео
        "retry_sleep_functions": {"fragment": pause, "http": pause},
        "socket_timeout": 10,
        "http_headers": job["headers"],
        **pick_format(job["mode"], job["max_height"], bool(FFMPEG_DIR), private, job.get("formats")),
    }
    optional = {"js_runtimes": JS_RUNTIMES, "cookiefile": cookie_file, "ffmpeg_location": FFMPEG_DIR}
    opts.update({k: v for k, v in optional.items() if v})
    return opts


def describe_format(f):
    return (f"{f.get('format_id')} {f.get('ext')} {f.get('resolution') or ''} {f.get('fps') or ''}fps "
            f"v={f.get('vcodec')} a={f.get('acodec')} tbr={f.get('tbr')}")


def job_kind(url):
    """Что за ссылка: плеер GetCourse, страница урока GetCourse или любая другая страница/поток."""
    if SIGN_PLAYER_RE.match(url):
        return "player"
    return "lesson" if GETCOURSE_LESSON_RE.search(url) else "page"


def plan_attempts(job, kind, cookie_file):
    """С какими cookies пробовать, по порядку. Для обычных сайтов сначала без cookies (YouTube не любит
    чужие сессии), при отказе — с cookies вкладки. Закрытый контент — сразу с ними: без входа он не отдаётся."""
    if job["sniffed"] or kind != "page":
        return [cookie_file]
    return [None, cookie_file] if cookie_file else [None]


def text_ahead(job, kind):
    """Распознавать ли текст параллельно со скачиванием видео: нужно и то и другое, и видео одно
    (на странице урока с несколькими плеерами — по очереди, как раньше)."""
    return job["mode"] == "video" and "text" in job["want"] and kind != "lesson"


def log_start(job_id, job, lg):
    lg.info(f"=== задача {job_id} ===")
    lg.info(f"окружение: {json.dumps(environment(), ensure_ascii=False)}")
    lg.info(f"url: {job['url']}")
    lg.info(f"вкладка: {job['title']} | {job['page_url']}")
    lg.info(f"нужно: {want_label(job['want'])} | качаем: {job['mode']} | пойманный поток: {job['sniffed']} | "
            f"качество: {job['max_height'] or 'максимум'} | язык речи: {job['language']}")
    lg.info(f"заголовки: {job['headers']}")
    lg.info(f"cookies ({len(job['cookies'])} шт., только имена): {redact_cookies(job['cookies'])}")
    if job["debug"]:
        lg.info(f"от расширения: {json.dumps(job['debug'], ensure_ascii=False)}")


def extract_infos(ydl, job, kind, lg):
    """Описания видео, которые надо скачать: обычно одно, на странице урока GetCourse — по одному на плеер."""
    url = job["url"]
    if kind == "player":
        lg.info("GetCourse: ссылка на плеер")
        return [getcourse_info(ydl, url, lg)]
    if kind == "page":
        return [ydl.extract_info(url, download=False)]
    lg.info("GetCourse: страница урока, ищу плееры")
    players = getcourse_players(ydl, url, {**job["headers"], "Referer": url}, lg)
    if not players:
        raise UserError("Не нашёл видео на странице урока. Включи его (▶), подожди пару секунд и скачай снова.")
    return [getcourse_info(ydl, pu, lg) for pu in players]


def name_download(ydl, job_id, job, info, n, total, named, names):
    """Назначить имя файлу очередного видео. Возвращает имя для текста, если качаем только звук под распознавание.
    Назначенные имена копим в names: по ним убираются недокачанные куски и временная звуковая дорожка."""
    part = "" if total == 1 else f" — часть {n}"
    name = safe_name(job["title"], keep=part)
    if job["mode"] == "text":  # сам звук временный и лежит во временной папке под номером задачи; имя нужно только тексту
        names.append(f"{job_id}-{n}")
        ydl.params["outtmpl"]["default"] = f"{job_id}-{n}.%(ext)s"
        return safe_name(name if named else f"{info.get('title') or job['title']} [{info.get('id')}]")
    if named:
        # имя закрепляется за задачей: «Повторить» и попытка с cookies пишут в тот же файл, и yt-dlp
        # продолжает его с места обрыва, а не качает заново под именем «… (2)»
        kept = job.setdefault("templates", {})
        template = kept[n] = kept.get(n) or unique_template(name, job["save_dir"])
        names.append(template.removesuffix(".%(ext)s").replace("%%", "%"))
        ydl.params["outtmpl"]["default"] = template
    else:
        update(job_id, title=info.get("title") or job["title"])
    return None


def download_infos(ydl, job_id, job, infos, named, names, lg):
    """Скачать каждое видео. named — имя файла берём из заголовка вкладки (а не из названия на сайте).
    Возвращает пары (что скачалось, имя для текста)."""
    results = []
    for n, info in enumerate(infos, 1):
        lg.info(f"видео {n}/{len(infos)}: extractor={info.get('extractor')} id={info.get('id')} title={info.get('title')!r}")
        lg.info(f"доступно форматов: {len(info.get('formats') or [])}")
        for f in info.get("formats") or []:
            lg.debug("  формат: " + describe_format(f))
        stem = name_download(ydl, job_id, job, info, n, len(infos), named, names)
        result = ydl.process_ie_result(info, download=True)
        for f in result.get("requested_formats") or [result]:
            lg.info(f"выбран формат: {describe_format(f)}")
        results.append((result, stem))
    return results


def finish_result(job_id, job, result, stem, lg, ahead=None):
    """Довести одно скачанное видео до того, что просили. Возвращает (файлы {вид: путь}, описание для окна)."""
    path = ((result.get("requested_downloads") or [{}])[0].get("filepath")) or result.get("filepath")
    if not path or not os.path.exists(path):
        raise UserError("Файл скачался, но пропал — возможно, его удалил антивирус. Нажми «Повторить».")
    local = result.get("local")
    files, note = from_local(job_id, job, Path(path), lg) if local else make_outputs(job_id, job, Path(path), stem, lg, ahead)
    about = []
    # временную звуковую дорожку описывать незачем — её уже не будет; готовый файл с диска — только если просили видео
    if job["mode"] != "text" and (not local or "video" in files):
        p = probe(path)
        if p:
            lg.info(f"ffprobe итога: {json.dumps(p, ensure_ascii=False)}")
        about.append(describe_probe(p))
    for kind, file in files.items():
        lg.info(f"готов файл ({kind}): {file} ({os.path.getsize(file) / 1024 / 1024:.1f} МБ)")
    return files, " · ".join(x for x in [*about, note] if x)


def finish_ok(job_id, job, results, ahead, started, lg):
    done = [finish_result(job_id, job, result, stem, lg, ahead) for result, stem in results]
    files, about = done[0] if done else ({}, "")
    first = next((str(files[k]) for k in ("video", "audio", "text") if k in files), None)
    got = [w for w in job["want"] if any(w in f for f, _ in done)]
    parts = f" ({len(done)} видео)" if len(done) > 1 else ""
    update(job_id, status="готово" + parts, percent=100, speed="", eta=None, file=first, quality=about)
    log.info(f"[{job_id}] ГОТОВО за {time.time() - started:.0f} с (просили: {want_label(job['want'])}; "
             f"вышло: {want_label(got) or 'ничего'}): {first} {about}")
    if first:
        remember(job, first)
    # заголовок уведомления говорит правду: если вышло не всё, что просили, — так и написано
    if got == job["want"]:
        toast("Готово: " + want_label(got), job["title"], first)
    elif got:
        toast("Готово не всё: есть " + want_label(got), f"{job['title']} — {about}", first)
    else:
        toast("Речи в видео не нашлось", job["title"])


def finish_failed(job_id, job, error, lg):
    """Любой сбой задачи становится её статусом: «отменено», если отменял пользователь, иначе «ошибка»."""
    msg, shown = re.sub(r"\x1b\[[0-9;]*m", "", str(error)).replace("ERROR: ", ""), explain(error)
    if job.get("cancel"):
        update(job_id, status="отменено", error=None, speed="", percent=0)
        lg.info("загрузка отменена пользователем")
        return log.info(f"[{job_id}] отменено")
    update(job_id, status="ошибка", error=shown, speed="")
    lg.error(f"ОШИБКА: {msg}")
    log.error(f"[{job_id}] ОШИБКА: {msg[:500]} | лог: {job.get('log_file')}")
    if not stopping.is_set():
        toast("Не получилось скачать", f"{job['title']}\n{shown}")


def attempt(job_id, job, kind, cookie_file, names, lg):
    """Одна попытка скачать задачу — с заданными cookies или без них.
    Возвращает (что скачалось, параллельное распознавание текста или None)."""
    if job.get("cancel"):  # «Отмена» пришла, пока задача готовилась: не затираем её статусом «подготовка»
        raise yt_dlp.utils.DownloadCancelled("загрузка отменена")
    update(job_id, status="подготовка", error=None)
    ahead = None
    if text_ahead(job, kind):
        ahead = TextAhead(job_id, job, cookie_file, lambda ydl: extract_infos(ydl, job, kind, lg)[0], lg)
    try:
        with yt_dlp.YoutubeDL(build_opts(job_id, job, cookie_file, lg)) as ydl:
            infos = extract_infos(ydl, job, kind, lg)
            named = job["sniffed"] or kind != "page"
            results = download_infos(ydl, job_id, job, infos, named, names, lg)
    except BaseException:
        if ahead:
            ahead.drop()  # видео не скачалось — его текст в этой попытке не нужен
        raise
    if job["mode"] != "text":
        names.clear()  # всё докачано — убирать нечего; временную звуковую дорожку уберём после распознавания
    return results, ahead


def run_attempts(job_id, job, cookie_file, names, lg):
    kind = job_kind(job["url"])
    attempts = plan_attempts(job, kind, cookie_file)
    for i, cf in enumerate(attempts):
        lg.info(f"попытка {i + 1}/{len(attempts)}: {'с cookies' if cf else 'без cookies'}")
        try:
            return attempt(job_id, job, kind, cf, names, lg)
        except Exception as e:  # noqa: BLE001 — причина записана; последняя попытка, отмена и закрытие пробрасываются дальше
            lg.warning(f"попытка {i + 1} не удалась: {e}\n{traceback.format_exc()}")
            # вторая попытка — только если сайт отказал без входа: нехватку места, сбой склейки или защиту
            # от копирования cookies не вылечат, а всё скачанное пришлось бы качать заново
            if i == len(attempts) - 1 or stopping.is_set() or job.get("cancel") or not wants_login(str(e)):
                raise
            log.info(f"[{job_id}] без cookies не вышло, пробую с cookies")


def conclude(job_id, job, outcome, cookie_file, names, started, lg):
    """Довести задачу до конца: звук из видео, текст, итоговый статус и уборка. outcome — что вернуло скачивание
    либо исключение, которым оно кончилось."""
    try:
        if isinstance(outcome, BaseException):
            raise outcome
        results, ahead = outcome
        finish_ok(job_id, job, results, ahead, started, lg)  # уже без повторных попыток скачивания
    except Exception as e:  # noqa: BLE001 — yt-dlp и сайты бросают что угодно; любой сбой задачи становится её статусом и записью в лог
        finish_failed(job_id, job, e, lg)
    finally:
        if cookie_file:
            with contextlib.suppress(OSError):  # файл cookies уже убрали — остальная уборка всё равно нужна
                os.unlink(cookie_file)
        if clean_after(job, outcome):
            remove_parts(job["save_dir"], names)
            job.pop("templates", None)  # имя свободно: при «Повторить» оно подбирается заново
        close_job_logger(job_id)
        forget_secrets(job)
        release(job_id, job)  # в самом конце: до этого «Повторить» ждёт, а «Убрать готовые» задачу не трогает


def forget_secrets(job):
    """Готовой задаче cookies и заголовки вкладки больше не нужны — не держим их в памяти до закрытия программы.
    У неудавшейся и отменённой остаются: без них «Повторить» не скачает закрытое видео."""
    if job["status"].startswith("готово"):
        job["cookies"], job["headers"] = [], {}


def clean_after(job, outcome):
    """Убирать ли недокачанное. После сбоя — нет: «Повторить» продолжит с того же места (всё равно уберётся
    при закрытии программы). После отмены и после успеха — да."""
    return bool(job.get("cancel")) or not isinstance(outcome, BaseException)


VIDEO_FILES = (".mp4", ".mkv", ".webm", ".mov")
AUDIO_FILES = (".m4a", ".mp3", ".opus", ".ogg", ".aac", ".wav")


def local_source(job):
    """Уже скачанный файл этого же видео, из которого можно сделать всё, что просят, — тогда качать незачем.
    Из видео получается и звук, и текст; из звука — только текст и звук. «Скачать ещё раз» качает заново."""
    if job.get("force"):
        return None
    found = [Path(f) for f in downloaded_files(job["key"])]
    video = next((f for f in found if f.suffix.lower() in VIDEO_FILES), None)
    audio = next((f for f in found if f.suffix.lower() in AUDIO_FILES), None)
    return video or (None if "video" in job["want"] else audio)


def use_local(job_id, job, media, lg):
    """Вместо скачивания взять готовый файл с диска. Возвращает то же, что и попытка скачивания."""
    lg.info(f"это видео уже скачано — беру готовый файл, без загрузки: {media}")
    log.info(f"[{job_id}] уже скачано, делаю из готового файла: {media.name}")
    update(job_id, save_dir=str(media.parent))  # новые файлы лягут рядом с ним
    return [({"filepath": str(media), "local": True}, None)], None


def claim(job_id):
    """Взять задачу в работу. Возвращает её, только если она ждёт очереди и никем не взята: один и тот же номер
    может лежать в очереди дважды (отмена, потом «Повторить»), а выполнять задачу должен один обработчик."""
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("running") or job["status"] != "в очереди" or stopping.is_set():
            return None
        job["running"] = True
        return job


def release(job_id, job):
    """Отпустить задачу. Если за это время нажали «Повторить» — теперь её можно ставить в очередь."""
    with lock:
        job["running"] = False
        if job.pop("retry_wanted", False):
            tasks.put(job_id)


def run_download(job_id):
    job = jobs[job_id]
    lg = job_logger(job_id)
    started = time.time()
    log_start(job_id, job, lg)
    cookie_file = write_cookies(job["cookies"])
    names = []  # имена файлов, которые мы сами назначили: по ним в конце убираем недокачанное и временное
    local = local_source(job)
    try:
        outcome = use_local(job_id, job, local, lg) if local else run_attempts(job_id, job, cookie_file, names, lg)
    except Exception as e:  # noqa: BLE001 — причину разберёт conclude: она станет статусом задачи и записью в лог
        # Дальше несём только текст ошибки. Сама ошибка держит за собой всё, что было открыто в момент сбоя, —
        # в том числе недокачанный файл: пока она жива, его не удалить, и «Повторить» получал имя с «(2)»
        outcome = (UserError if isinstance(e, UserError) else RuntimeError)(str(e))
    finish = (job_id, job, outcome, cookie_file, names, started, lg)
    if "text" in job["want"] and not isinstance(outcome, BaseException):
        # распознавание идёт своим потоком: место в очереди загрузок освобождается для следующего видео
        threading.Thread(target=conclude, args=finish, daemon=True).start()
    else:
        conclude(*finish)


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
        except Exception:  # noqa: BLE001 — верх фонового потока: отчёт не должен умирать от одного сбоя; он записан
            log.exception("сбой отчёта о прогрессе (загрузки не затронуты)")


def report_once():
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
        job = claim(job_id)  # задачу могли отменить, убрать из списка или уже взять, пока она ждала очереди
        try:
            if job:
                run_download(job_id)
        except Exception:  # noqa: BLE001 — верх обработчика очереди: один сбой не должен остановить остальные загрузки; он записан
            log.exception(f"[{job_id}] непредвиденный сбой обработчика")
            # задача не должна навсегда остаться «в очереди»: тогда программа никогда не закроется по простою
            update(job_id, status="ошибка", error="Внутренний сбой программы — подробности в журнале (кнопка «Логи»).", speed="")
            release(job_id, job)
        finally:
            tasks.task_done()
