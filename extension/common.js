export const API = "http://127.0.0.1:5577";

// X-Kachalka — пропуск расширения: без него сервер запрос не примет
export const HEADERS = { "Content-Type": "application/json", "X-Kachalka": "1" };
export const api = (path, opts = {}) => fetch(API + path, { ...opts, headers: HEADERS });

// fetch бросает TypeError, когда качалка не запущена (соединиться не с кем)
export const isDown = (e) => e instanceof TypeError;

// Расширение само программу запустить не может — просим Chrome позвать launcher.py (его прописывает install.bat),
// тот поднимает качалку в фоне. Ждём, пока она начнёт отвечать
const NATIVE_HOST = "com.kachalka.host";
let starting = null;
export function startServer() {
  starting ||= (async () => {
    let r;
    try {
      r = await chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd: "start" });
    } catch (e) {
      throw new Error(`Программа-помощник не установлена. Открой папку, куда распаковал Keeper, дважды щёлкни install.bat и открой это окно снова. (${e.message})`);
    }
    if (!r?.ok) throw new Error("Не удалось запуститься: " + (r?.error || "запусти install.bat ещё раз; подробности — в файле logs\\launcher.log"));
    for (let i = 0; i < 60; i++) {
      try { await api("/status"); return; } catch { await new Promise((ok) => setTimeout(ok, 250)); }
    }
    throw new Error("Не удалось запуститься. Запусти install.bat ещё раз; если не помогло — подробности в файле logs\\kachalka.log.");
  })().finally(() => { starting = null; });
  return starting;
}

// То же, что api, но если качалка спит — сначала будит её
export async function apiUp(path, opts) {
  try {
    return await api(path, opts);
  } catch (e) {
    if (!isDown(e)) throw e;
    await startServer();
    return api(path, opts);
  }
}

// Что считать видео и как собирать потоки в видео — в streams.js (там нет обращений к Chrome, он проверяется тестами)
import { groupVideos, pickBest, videoName } from "./streams.js";
export * from "./streams.js";

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
  return (await chrome.storage.session.get(key))[key] || { items: [], drm: false, frames: {} };
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
export async function buildJob(tab, item, { title, why } = {}) {
  const caught = await getCaught(tab.id);
  const known = isKnownSite(tab.url);
  const chosen = item || (!known && caught.items.length ? pickBest(caught.items) : null);
  const urls = chosen ? [tab.url, chosen.url] : [tab.url];
  const reason = item ? why || "выбран вручную из списка"
    : chosen ? "лучший пойманный поток"
    : known ? "известный сайт — качаем по ссылке страницы"
    : "потоков не поймано — отдаём ссылку страницы yt-dlp";
  return {
    url: chosen ? chosen.url : tab.url,
    title: title || tab.title,
    page_url: tab.url,
    want: await getWant(),
    language: await getLanguage(),
    formats: await getFormats(),
    max_height: await getQuality(),
    sniffed: !!chosen,
    headers: chosen ? chosen.headers : {},
    cookies: await cookiesFor(urls),
    // для лога: что расширение видело на вкладке и почему выбрало именно это
    debug: {
      reason,
      extension: chrome.runtime.getManifest().version,
      drm_suspected: caught.drm,
      caught: caught.items.map(({ url, kind, master, time, frameId }) => ({ url, kind, master, frameId, time: new Date(time).toISOString() })),
    },
  };
}

// Имя файла — заголовок вкладки; если видео на странице несколько, добавляем номер и название (или «видео N»)
export function videoTitle(tab, videos, n) {
  return videos.length > 1 ? `${tab.title} — ${videoName(videos, n).replace(/^В/, "в")}` : tab.title;
}

// Задачи для вкладки: если на странице несколько видео — по задаче на каждое (лучший поток каждого)
export async function buildJobs(tab) {
  const videos = isKnownSite(tab.url) ? [] : groupVideos(await getCaught(tab.id));
  if (videos.length < 2) return [await buildJob(tab)];
  const jobs = [];
  for (const [n, v] of videos.entries()) {
    jobs.push(await buildJob(tab, v.best, {
      title: videoTitle(tab, videos, n),
      why: `видео ${n + 1} из ${videos.length} на странице — лучший поток этого видео`,
    }));
  }
  return jobs;
}

// Ошибки расширения — в общий журнал качалки (если она запущена)
export function clientLog(message, level = "error") {
  api("/client-log", {
    method: "POST",
    body: JSON.stringify({ message, level }),
  }).catch(() => {});
  (level === "error" ? console.error : console.log)("[keeper]", message);
}

// Что получить из загрузки: видео, звук (mp3), текст (распознанная речь) — в любом сочетании, по файлу на каждое
export const WANT = ["video", "audio", "text"];
export async function getWant() {
  const saved = (await chrome.storage.local.get("want")).want;
  const want = WANT.filter((w) => (saved || []).includes(w));
  return want.length ? want : ["video"];
}

// Язык речи для распознавания: auto — определить самой, ru — русский, multi — английский и другие европейские
export async function getLanguage() {
  return (await chrome.storage.local.get("language")).language || "auto";
}

// В каком виде сохранять: видео mp4 | best, звук m4a | mp3, текст txt | timed | srt. Чего нет — программа возьмёт по умолчанию
export async function getFormats() {
  return (await chrome.storage.local.get("formats")).formats || {};
}

// Ограничение качества из окна расширения: высота кадра (1080, 720…), 0 — максимум
export async function getQuality() {
  return (await chrome.storage.local.get("quality")).quality || 0;
}

// force — качать, даже если это видео уже скачано. Иначе качалка откажет, и у ошибки будет duplicate
export async function send(job, force = false) {
  const r = await apiUp("/download", {
    method: "POST",
    body: JSON.stringify({ ...job, force }),
  });
  const data = await r.json();
  if (!r.ok) throw Object.assign(new Error(data.error), { duplicate: !!data.duplicate });
  return data;
}
