import { buildJob, send, getCaught, clientLog, PLAYER, SIGN_PLAYER_RE } from "./common.js";

self.addEventListener("error", (e) => clientLog(`фон: ${e.message} @ ${e.filename}:${e.lineno}`));
self.addEventListener("unhandledrejection", (e) => clientLog(`фон (promise): ${e.reason?.stack || e.reason}`));

// ---------- перехват видеопотоков во вкладках ----------
const FILE_RE = /\.(mp4|webm|mov|m4v|mkv)(\?|#|$)/i;
const SEGMENT_RE = /\.(ts|m4s|aac|m4a)(\?|#|$)|[/_-](seg|segment|frag|fragment|chunk)[-_]?\d/i;
const DRM_RE = /widevine|playready|fairplay|\/license|drm.*licen[cs]e/i;
const IGNORE_HOSTS = /googlevideo\.com|doubleclick|googlesyndication|^ads?\./i;

const reqHeaders = new Map(); // requestId -> {Referer, Origin, User-Agent}

chrome.webRequest.onSendHeaders.addListener((d) => {
  if (d.tabId < 0) return;
  const h = {};
  for (const { name, value } of d.requestHeaders || []) {
    const n = name.toLowerCase();
    if (n === "referer") h.Referer = value;
    else if (n === "origin") h.Origin = value;
    else if (n === "user-agent") h["User-Agent"] = value;
  }
  if (!h.Referer && d.documentUrl) h.Referer = d.documentUrl;
  reqHeaders.set(d.requestId, h);
  if (reqHeaders.size > 2000) reqHeaders.delete(reqHeaders.keys().next().value);
}, { urls: ["<all_urls>"] }, ["requestHeaders", "extraHeaders"]);

chrome.webRequest.onHeadersReceived.addListener((d) => {
  if (d.tabId < 0 || IGNORE_HOSTS.test(new URL(d.url).hostname)) return;
  if (DRM_RE.test(d.url) && d.method === "POST") return void markDrm(d.tabId);

  if (d.type === "sub_frame" && SIGN_PLAYER_RE.test(d.url)) {
    return void addItem(d.tabId, { url: d.url, kind: PLAYER, headers: reqHeaders.get(d.requestId) || {}, time: Date.now(), master: false });
  }

  const type = (d.responseHeaders || []).find((h) => h.name.toLowerCase() === "content-type")?.value?.toLowerCase() || "";
  let kind = null;
  if (/mpegurl/.test(type) || /\.m3u8(\?|#|$)/i.test(d.url) || /\/api\/playlist\/(master|media)\//.test(d.url)) kind = "HLS";
  else if (/dash\+xml/.test(type) || /\.mpd(\?|#|$)/i.test(d.url)) kind = "DASH";
  else if ((type.startsWith("video/") || FILE_RE.test(d.url)) && !SEGMENT_RE.test(d.url)) kind = "Файл";
  if (!kind) return;

  const headers = reqHeaders.get(d.requestId) || {};
  addItem(d.tabId, { url: d.url, kind, headers, time: Date.now(), master: false }).then(() => {
    if (kind === "HLS") checkMaster(d.tabId, d.url, headers);
  });
}, { urls: ["<all_urls>"] }, ["responseHeaders"]);

// Очередь записи, чтобы параллельные запросы не затирали друг друга
let chain = Promise.resolve();
function mutate(tabId, fn) {
  chain = chain.then(async () => {
    const data = await getCaught(tabId);
    fn(data);
    data.items = data.items.slice(-30);
    await chrome.storage.session.set({ ["tab_" + tabId]: data });
    updateBadge(tabId, data);
  }).catch(() => {});
  return chain;
}

function addItem(tabId, item) {
  const norm = (u) => u.split("?")[0];
  return mutate(tabId, (data) => {
    const old = data.items.find((i) => norm(i.url) === norm(item.url));
    if (old) Object.assign(old, { url: item.url, time: item.time, headers: item.headers });
    else data.items.push(item);
  });
}

function markDrm(tabId) {
  return mutate(tabId, (data) => { data.drm = true; });
}

// Мастер-плейлист HLS содержит список качеств — именно его надо качать, чтобы взять лучшее
async function checkMaster(tabId, url) {
  try {
    const text = await (await fetch(url, { credentials: "include" })).text();
    const master = text.includes("#EXT-X-STREAM-INF");
    // обычное AES-128 шифрование HLS — не DRM, его yt-dlp снимает сам; DRM — это Widevine/FairPlay/PlayReady
    const drm = /#EXT-X-(SESSION-)?KEY:.*KEYFORMAT="(urn:uuid|com\.apple\.streamingkeydelivery|com\.microsoft)/i.test(text);
    await mutate(tabId, (data) => {
      const it = data.items.find((i) => i.url === url);
      if (it) it.master = master;
      if (drm) data.drm = true;
    });
  } catch (e) {
    // не смогли прочитать — не страшно, качалка разберётся сама; но в консоль расширения запишем
    console.log("[качалка] не прочитал плейлист", url, e);
  }
}

function updateBadge(tabId, data) {
  const n = data.items.length;
  chrome.action.setBadgeBackgroundColor({ tabId, color: data.drm ? "#ef4444" : "#22c55e" }).catch(() => {});
  chrome.action.setBadgeText({ tabId, text: n ? String(n) : data.drm ? "DRM" : "" }).catch(() => {});
}

// Новая страница во вкладке — старые пойманные потоки уже не про неё
chrome.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && info.url) chrome.storage.session.remove("tab_" + tabId);
});
chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove("tab_" + tabId));

// Страница могла открыться до установки расширения — тогда перехват её пропустил.
// По запросу окна ищем плееры прямо в разметке страницы.
async function scanTab(tabId) {
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => [...document.querySelectorAll("iframe, video, source")]
        .map((e) => e.src || e.dataset?.src || "")
        .filter((u) => /^https?:/.test(u)),
    });
    for (const url of result || []) {
      if (SIGN_PLAYER_RE.test(url)) {
        await addItem(tabId, { url, kind: PLAYER, headers: {}, time: Date.now(), master: false });
      } else if (/\.(m3u8|mpd|mp4|webm)(\?|#|$)/i.test(url)) {
        const kind = /\.m3u8/i.test(url) ? "HLS" : /\.mpd/i.test(url) ? "DASH" : "Файл";
        await addItem(tabId, { url, kind, headers: {}, time: Date.now(), master: false });
      }
    }
  } catch (e) {
    console.log("[качалка] не смог просканировать вкладку", tabId, e);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg?.type === "scan") {
    scanTab(msg.tabId).then(() => reply(true));
    return true;
  }
});

// ---------- меню правой кнопки ----------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({ id: "dl-link", title: "Скачать видео по ссылке", contexts: ["link", "video"] });
  chrome.contextMenus.create({ id: "dl-page", title: "Скачать видео с этой вкладки", contexts: ["page"] });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === "dl-link") {
      await send(await buildJob({ ...tab, url: info.linkUrl || info.srcUrl, id: -1 }, "video"));
    } else {
      await send(await buildJob(tab, "video"));
    }
    notify("Скачивание началось", tab.title);
  } catch (e) {
    if (e instanceof TypeError) notify("Качалка не отвечает", "Запусти start.bat в папке download_web");
    else { notify("Ошибка", e.message); clientLog(`меню: ${e.stack || e}`); }
  }
});

function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icon.png", title, message: String(message || "").slice(0, 200) });
}
