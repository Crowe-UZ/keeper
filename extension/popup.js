import { api, buildJob, send, getCaught, pickBest, isKnownSite, clientLog } from "./common.js";

window.addEventListener("error", (e) => clientLog(`окно: ${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener("unhandledrejection", (e) => clientLog(`окно (promise): ${e.reason?.stack || e.reason}`));

const $ = (id) => document.getElementById(id);
const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
$("tabTitle").textContent = tab?.title || "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function showError(e) {
  if (e instanceof TypeError) {
    $("err").textContent = "Качалка не запущена — открой start.bat в папке download_web";
  } else {
    $("err").textContent = e.message;
    clientLog(`окно: ${e.stack || e}`);
  }
}

async function run(fn) {
  $("err").textContent = "";
  try { await fn(); refresh(); } catch (e) { showError(e); }
}

// ----- что поймали на этой вкладке -----
async function renderCaught() {
  const caught = await getCaught(tab.id);
  const known = isKnownSite(tab.url);
  const best = pickBest(caught.items);
  let hint = known
    ? "Сайт известен — качаю по ссылке в максимальном качестве."
    : caught.items.length
      ? "Поймал видеопоток на странице — качаю лучший."
      : "Видео не поймано. Включи ролик (▶) на странице, подожди пару секунд и открой это окно снова.";
  if (caught.drm) hint += '<br><span class="warn">⚠ Похоже, видео защищено DRM — скорее всего, скачать не выйдет.</span>';
  $("hint").innerHTML = hint;

  if (!caught.items.length) return void ($("caught").innerHTML = "");
  const items = [...caught.items].sort((a, b) => b.time - a.time);
  $("caught").innerHTML = `<div class="section">Пойманные потоки (${items.length})</div>` + items.map((it, i) => `
    <div class="item">
      <span class="tag ${it === best ? "best" : ""}">${it.master ? "HLS · все качества" : esc(it.kind)}${it === best ? " ★" : ""}</span>
      <span class="u" title="${esc(it.url)}">${esc(new URL(it.url).hostname + new URL(it.url).pathname)}</span>
      <button class="small alt" data-i="${i}">⬇</button>
    </div>`).join("");
  $("caught").querySelectorAll("button[data-i]").forEach((b) => {
    b.onclick = () => run(async () => send(await buildJob(tab, "video", items[b.dataset.i])));
  });
}

// ----- очередь загрузок -----
async function refresh() {
  try {
    const { jobs, ytdlp, summary: s, folder } = await (await api("/status")).json();
    $("folderPath").textContent = folder;
    $("folderPath").title = folder;
    $("summary").textContent = jobs.length
      ? `Качается: ${s.active} · В очереди: ${s.queued} · Готово: ${s.done}` + (s.failed ? ` · Ошибок: ${s.failed}` : "")
      : "";
    $("ver").textContent = `расширение ${chrome.runtime.getManifest().version} · yt-dlp ${ytdlp}`;
    $("jobs").innerHTML = jobs.map((j) => {
      const busy = j.status === "скачивается";
      const eta = busy && j.eta ? "осталось " + (j.eta >= 60 ? Math.floor(j.eta / 60) + " мин" : Math.round(j.eta) + " с") : "";
      const extra = [j.quality, busy ? j.percent + "%" : "", busy && j.done_mb ? j.done_mb + " МБ" : "", busy ? j.speed : "", eta]
        .filter(Boolean).join(" · ");
      return `
      <div class="job">
        <div class="title" title="${esc(j.title)}">${j.mode === "audio" ? "🎵" : "🎬"} ${esc(j.title)}</div>
        <div class="bar"><div style="width:${j.percent}%"></div></div>
        <div class="st ${j.error ? "bad" : ""}">${esc(j.status)}${extra ? " · " + esc(extra) : ""} ${esc(j.error || "")}
          ${j.has_file && j.status.startsWith("готово") ? `<span class="loglink" data-open="${esc(j.id)}">▶ открыть</span> <span class="loglink" data-show="${esc(j.id)}">📂 показать в папке</span>` : ""}
          ${j.has_log ? `<span class="loglink" data-log="${esc(j.id)}">лог</span>` : ""}</div>
      </div>`;
    }).join("");
    for (const [attr, path] of [["open", "/open-file"], ["show", "/show-file"]]) {
      $("jobs").querySelectorAll(`[data-${attr}]`).forEach((a) => {
        a.onclick = () => run(async () => {
          const r = await api(path + "?id=" + a.dataset[attr]);
          if (!r.ok) throw new Error((await r.json()).error);
        });
      });
    }
    $("jobs").querySelectorAll("[data-log]").forEach((a) => {
      a.onclick = () => api("/open-log?id=" + a.dataset.log).catch(showError);
    });
  } catch (e) {
    showError(e);
  }
}

const allTabs = (await chrome.tabs.query({})).filter((t) => /^https?:/.test(t.url));
$("all").textContent = `Все вкладки (${allTabs.length})`;

$("video").onclick = () => run(async () => send(await buildJob(tab, "video")));
$("audio").onclick = () => run(async () => send(await buildJob(tab, "audio")));
$("all").onclick = () => run(async () => {
  for (const t of allTabs) await send(await buildJob(t, "video"));
});
$("logs").onclick = () => api("/open-logs").catch(showError);
$("chooseFolder").onclick = () => run(async () => {
  $("chooseFolder").textContent = "Выбери папку в окне Windows…";
  try { await api("/choose-folder"); } finally { $("chooseFolder").textContent = "Сменить"; }
});
$("folder").onclick = () => api("/open-folder").catch(showError);
$("clear").onclick = () => run(() => api("/clear"));

await chrome.runtime.sendMessage({ type: "scan", tabId: tab.id }).catch(() => {});
renderCaught();
refresh();
setInterval(() => { refresh(); renderCaught(); }, 1000);
