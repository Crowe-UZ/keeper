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
      throw new Error(`Качалка не установлена: запусти install.bat в папке download_web и перезагрузи расширение (${e.message})`);
    }
    if (!r?.ok) throw new Error("Качалка не запустилась: " + (r?.error || "причина в logs\\launcher.log"));
    for (let i = 0; i < 60; i++) {
      try { await api("/status"); return; } catch { await new Promise((ok) => setTimeout(ok, 250)); }
    }
    throw new Error("Качалка не запустилась — посмотри logs\\kachalka.log в папке download_web");
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
  return (await chrome.storage.session.get(key))[key] || { items: [], drm: false, frames: {} };
}

export const norm = (u) => u.split("?")[0];
// Один и тот же поток запрашивается много раз с разными токенами — сравниваем без них.
// Плееры GetCourse отличаются только параметром json=, их сравниваем целиком
export const itemKey = (i) => (i.kind === PLAYER ? i.url : norm(i.url));

// Потоки вкладки -> отдельные видео. Каждый плеер живёт в своём iframe: один iframe = одно видео.
// Видео прямо на странице (без iframe) делим по мастер-плейлистам: качества мастера — то же видео.
// Порядок — как плееры идут на странице, иначе — в каком порядке поймали
export function groupVideos(caught) {
  const frames = caught.frames || {};
  const masters = caught.items.filter((i) => i.master && !i.frameId);
  const groups = new Map();
  for (const it of caught.items) {
    let key = "f" + it.frameId;
    if (!it.frameId) {
      const m = it.kind === "HLS" && !it.master && masters.find((m) => m.variants?.includes(norm(it.url)));
      key = "u" + itemKey(m || it);
    }
    if (!groups.has(key)) groups.set(key, { frameId: it.frameId || 0, items: [] });
    groups.get(key).items.push(it);
  }
  const all = [...groups.values()];
  const onPage = all.filter((g) => !g.frameId).length;
  const videos = all.map((g) => {
    const meta = g.frameId ? frames[g.frameId] : onPage === 1 ? frames[0] : null;
    return {
      ...g,
      best: pickBest(g.items),
      duration: meta?.duration || 0,
      playing: !!meta?.playing,
      thumb: meta?.thumb || "",
      title: (g.frameId && meta?.title) || "",
      // адрес iframe плеера — чтобы подсветить его на странице
      src: g.frameId ? meta?.href : g.items.find((i) => i.kind === PLAYER)?.url,
      order: g.frameId && meta?.order >= 0 ? meta.order : Infinity,
      first: Math.min(...g.items.map((i) => i.first ?? i.time)),
    };
  }).sort((a, b) => (a.order - b.order) || (a.first - b.first));
  // одинаковый заголовок у нескольких видео — значит, поймали общий текст страницы, а не название ролика
  for (const v of videos) {
    if (v.title && videos.filter((x) => x.title === v.title).length > 1) v.shared = true;
  }
  for (const v of videos) if (v.shared) v.title = "";
  return videos;
}

// Как видео называется в списке: номер есть всегда, название — когда его удалось найти на странице
export function videoName(videos, n) {
  return videos[n].title ? `${n + 1}. ${videos[n].title}` : `Видео ${n + 1}`;
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
  (level === "error" ? console.error : console.log)("[качалка]", message);
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
