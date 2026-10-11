import { buildJob, buildJobs, send, getCaught, clientLog, norm, itemKey, PLAYER, SIGN_PLAYER_RE } from "./common.js";

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
    return void addItem(d.tabId, { url: d.url, kind: PLAYER, frameId: d.frameId, headers: reqHeaders.get(d.requestId) || {}, time: Date.now(), master: false });
  }

  const type = (d.responseHeaders || []).find((h) => h.name.toLowerCase() === "content-type")?.value?.toLowerCase() || "";
  let kind = null;
  if (/mpegurl/.test(type) || /\.m3u8(\?|#|$)/i.test(d.url) || /\/api\/playlist\/(master|media)\//.test(d.url)) kind = "HLS";
  else if (/dash\+xml/.test(type) || /\.mpd(\?|#|$)/i.test(d.url)) kind = "DASH";
  else if ((type.startsWith("video/") || FILE_RE.test(d.url)) && !SEGMENT_RE.test(d.url)) kind = "Файл";
  if (!kind) return;

  const headers = reqHeaders.get(d.requestId) || {};
  addItem(d.tabId, { url: d.url, kind, frameId: d.frameId, headers, time: Date.now(), master: false }).then(() => {
    if (kind === "HLS") checkMaster(d.tabId, d.url, headers);
  });
}, { urls: ["<all_urls>"] }, ["responseHeaders"]);

// Очередь записи, чтобы параллельные запросы не затирали друг друга
let chain = Promise.resolve();
function mutate(tabId, fn) {
  chain = chain.then(async () => {
    const data = await getCaught(tabId);
    fn(data);
    data.items = data.items.slice(-150);
    await chrome.storage.session.set({ ["tab_" + tabId]: data });
    updateBadge(tabId, data);
  }).catch(() => {});
  return chain;
}

// fresh=false — поток найден в разметке, а не в сети: свежую ссылку и заголовки уже пойманного не трогаем
function upsert(data, item, fresh = true) {
  const old = data.items.find((i) => itemKey(i) === itemKey(item));
  if (!old) return void data.items.push({ ...item, first: item.time });
  if (item.frameId != null) old.frameId = item.frameId;
  if (fresh) Object.assign(old, { url: item.url, time: item.time, headers: item.headers });
}

function addItem(tabId, item) {
  return mutate(tabId, (data) => upsert(data, item));
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
    // плейлисты качеств из мастера — чтобы понимать, что они про то же видео
    const uris = [...text.split(/\r?\n/).filter((l) => l.trim() && !l.startsWith("#")), ...[...text.matchAll(/URI="([^"]+)"/g)].map((m) => m[1])];
    const variants = !master ? [] : uris.map((u) => {
      try { return norm(new URL(u.trim(), url).href); } catch { return null; }
    }).filter(Boolean);
    await mutate(tabId, (data) => {
      const it = data.items.find((i) => i.url === url);
      if (it) Object.assign(it, { master, variants });
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
// По запросу окна ищем плееры прямо в разметке страницы — во всех её фреймах.
// Заодно узнаём про каждый фрейм: какой он по счёту на странице, длительность видео, играет ли оно,
// кадр для превью и — если получится — заголовок, который стоит на странице над плеером.
// Всё это подсказки: сайтов тысячи, и где не угадали — в окне остаётся просто «Видео N».
async function scanTab(tabId) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        const PLAYERS = "iframe, video";
        const srcOf = (e) => e.src || e.dataset?.src || "";
        const srcs = (sel) => [...document.querySelectorAll(sel)].map(srcOf).filter((u) => /^https?:/.test(u));

        // Заголовок плеера — ближайший короткий текст прямо над ним. Поднимаемся по вложенности, пока
        // блок относится только к этому плееру: текст над блоком с несколькими плеерами — общий, не его.
        const titleOf = (el) => {
          for (let node = el, up = 0; node.parentElement && up < 6; node = node.parentElement, up++) {
            for (let s = node.previousElementSibling, seen = 0; s && seen < 3; s = s.previousElementSibling) {
              // <style>, <script> и скрытые блоки на странице не видны, а «текст» у них есть — пропускаем
              if (s.matches("style, script, noscript, template, link, meta") || !s.getClientRects().length) continue;
              seen++;
              if (s.matches(PLAYERS) || s.querySelector(PLAYERS)) return "";  // выше — уже другой плеер
              const head = s.matches("h1, h2, h3, h4, h5, h6") ? s : s.querySelector("h1, h2, h3, h4, h5, h6, strong, b");
              const text = ((head || s).innerText || "").trim().replace(/\s+/g, " ");
              if (text.length >= 3 && text.length <= 100) return text;
              if (text.length > 100) return "";  // абзац описания, а не заголовок
            }
            if (node.parentElement.querySelectorAll(PLAYERS).length > 1) return "";
          }
          return "";
        };

        const videos = [...document.querySelectorAll("video")];
        const v = videos.find((x) => !x.paused) || videos[0];
        let thumb = "";
        try {
          if (v && v.currentTime > 0 && v.readyState >= 2 && v.videoWidth) {
            const c = document.createElement("canvas");
            c.width = 160;
            c.height = Math.round(160 * v.videoHeight / v.videoWidth);
            c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
            thumb = c.toDataURL("image/jpeg", 0.6);
          }
        } catch { /* чужое видео без разрешения на чтение кадров — останемся с обложкой */ }
        thumb ||= v?.poster || "";
        if (!thumb && window !== top) {  // внутри плеера обложка обычно лежит картинкой или фоном блока
          const el = document.querySelector('[class*="poster" i], [class*="preview" i], [class*="cover" i], [class*="thumb" i]');
          const bg = el && getComputedStyle(el).backgroundImage.match(/url\(["']?([^"')]+)/)?.[1];
          thumb = bg || (el?.tagName === "IMG" ? el.src : el?.querySelector("img")?.src) || "";
        }

        return {
          href: location.href,
          iframes: [...document.querySelectorAll("iframe")].map((e) => ({ src: srcOf(e), title: titleOf(e) }))
            .filter((f) => /^https?:/.test(f.src)),
          media: srcs("video, source"),
          duration: v && isFinite(v.duration) ? Math.round(v.duration) : 0,
          playing: !!v && !v.paused,
          thumb: /^(https?:|data:image\/)/.test(thumb) ? thumb : "",
        };
      },
    });
    const players = results.find((r) => r.frameId === 0)?.result?.iframes || [];
    const frames = {};
    const found = [];
    const add = (url, kind, frameId) => found.push({ url, kind, frameId, headers: {}, time: Date.now(), master: false });
    for (const { frameId, result: r } of results) {
      if (!r) continue;
      const order = players.findIndex((p) => p.src === r.href);
      frames[frameId] = { href: r.href, duration: r.duration, playing: r.playing, thumb: r.thumb, order, title: players[order]?.title || "" };
      r.iframes = r.iframes.map((f) => f.src);
      if (frameId && SIGN_PLAYER_RE.test(r.href)) add(r.href, PLAYER, frameId);
      for (const url of r.iframes) if (SIGN_PLAYER_RE.test(url)) add(url, PLAYER);
      for (const url of r.media) {
        if (!/\.(m3u8|mpd|mp4|webm)(\?|#|$)/i.test(url)) continue;
        add(url, /\.m3u8/i.test(url) ? "HLS" : /\.mpd/i.test(url) ? "DASH" : "Файл", frameId);
      }
    }
    await mutate(tabId, (data) => {
      data.frames = frames;
      for (const item of found) upsert(data, item, false);
    });
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
      let sent = 0;
      for (const job of await buildJobs(tab, "video")) {
        // уже скачанное из меню не перекачиваем; «скачать ещё раз» есть в окне расширения
        try { await send(job); sent++; } catch (e) { if (!e.duplicate) throw e; notify("Пропущено", e.message); }
      }
      if (!sent) return;
    }
    notify("Скачивание началось", tab.title);
  } catch (e) {
    if (e.duplicate) return notify("Пропущено", e.message);
    notify("Не получилось скачать", e.message);
    clientLog(`меню: ${e.stack || e}`);
  }
});

function notify(title, message) {
  chrome.notifications.create({ type: "basic", iconUrl: "icon.png", title, message: String(message || "").slice(0, 200) });
}
