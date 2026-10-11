"""Скачивание: настройки yt-dlp, выполнение задачи, обработчики очереди, отчёт о прогрессе."""
import json
import os
import re
import time
import traceback

import yt_dlp
from .config import (ACTIVE, log)
from .state import jobs, lock, stopping, summary, tasks, update
from .files import describe_probe, probe, remove_parts, temp_dir, unique_template, write_cookies
from .getcourse import GETCOURSE_LESSON_RE, SIGN_PLAYER_RE, getcourse_info, getcourse_players
from .history import remember
from .joblog import YtdlpLog, close_job_logger, job_logger, redact_cookies
from .notify import toast
from .system import FFMPEG_DIR, JS_RUNTIMES, environment


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
