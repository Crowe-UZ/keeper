"""GetCourse: поиск плеера на странице урока и разбор плеера экстрактором yt-dlp."""
import re



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
