export const API = "http://127.0.0.1:5577";

// X-Kachalka — пропуск расширения: без него сервер запрос не примет
export const HEADERS = { "Content-Type": "application/json", "X-Kachalka": "1" };
export const api = (path, opts = {}) => fetch(API + path, { ...opts, headers: HEADERS });

export const PLAYER = "Плеер GetCourse";
// iframe плеера GetCourse: адрес плеера меняется, путь — нет
export const SIGN_PLAYER_RE = /\/sign-player\/?\?(?:[^#]*&)?json=/;

// Сайты, которые yt-dlp понимает сам по ссылке на страницу — там перехват не нужен
const KNOWN = [
  "youtube.com", "youtu.be", "vk.com", "vkvideo.ru", "rutube.ru", "tiktok.com", "instagram.com",
  "x.com", "twitter.com", "twitch.tv", "dzen.ru", "ok.ru", "facebook.com", "reddit.com",
  "vimeo.com", "dailymotion.com", "bilibili.com", "pinterest.com", "coub.com",
];

export function isKnownSite(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\.|^m\./, "");
    return KNOWN.some((d) => host === d || host.endsWith("." + d));
  } catch { return false; }
}

export async function getCaught(tabId) {
  const key = "tab_" + tabId;
  return (await chrome.storage.session.get(key))[key] || { items: [], drm: false };
}

// Лучший пойманный поток: мастер-плейлист HLS (все качества) > плеер GetCourse > DASH > любой m3u8 > файл;
// при равенстве — свежий
export function pickBest(items) {
  const rank = (i) => (i.master ? 5 : i.kind === PLAYER ? 4 : i.kind === "DASH" ? 3 : i.kind === "HLS" ? 2 : 1);
  return [...items].sort((a, b) => rank(b) - rank(a) || b.time - a.time)[0];
}

async function cookiesFor(urls) {
  const seen = new Map();
  for (const url of urls) {
    for (const c of await chrome.cookies.getAll({ url })) seen.set(c.domain + c.path + c.name, c);
  }
  return [...seen.values()].map(({ domain, path, secure, expirationDate, name, value }) =>
    ({ domain, path, secure, expirationDate, name, value }));
}

// Что качать для вкладки: известный сайт -> ссылка страницы; иначе пойманный поток; иначе всё равно страница
export async function buildJob(tab, mode, item) {
  const caught = await getCaught(tab.id);
  const known = isKnownSite(tab.url);
  const chosen = item || (!known && caught.items.length ? pickBest(caught.items) : null);
  const urls = chosen ? [tab.url, chosen.url] : [tab.url];
  const reason = item ? "выбран вручную из списка"
    : chosen ? "лучший пойманный поток"
    : known ? "известный сайт — качаем по ссылке страницы"
    : "потоков не поймано — отдаём ссылку страницы yt-dlp";
  return {
    url: chosen ? chosen.url : tab.url,
    title: tab.title,
    page_url: tab.url,
    mode,
    sniffed: !!chosen,
    headers: chosen ? chosen.headers : {},
    cookies: await cookiesFor(urls),
    // для лога: что расширение видело на вкладке и почему выбрало именно это
    debug: {
      reason,
      extension: chrome.runtime.getManifest().version,
      drm_suspected: caught.drm,
      caught: caught.items.map(({ url, kind, master, time }) => ({ url, kind, master, time: new Date(time).toISOString() })),
    },
  };
}

// Ошибки расширения — в общий журнал качалки (если она запущена)
export function clientLog(message, level = "error") {
  api("/client-log", {
    method: "POST",
    body: JSON.stringify({ message, level }),
  }).catch(() => {});
  (level === "error" ? console.error : console.log)("[качалка]", message);
}

export async function send(job) {
  const r = await api("/download", {
    method: "POST",
    body: JSON.stringify(job),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error);
  return data;
}
