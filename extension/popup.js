import {
  api, apiUp, startServer, isDown, buildJob, buildJobs, send, getCaught, getQuality, getWant, getLanguage, WANT,
  groupVideos, videoName, videoTitle, isKnownSite, clientLog,
} from "./common.js";

window.addEventListener("error", (e) => clientLog(`окно: ${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener("unhandledrejection", (e) => clientLog(`окно (promise): ${e.reason?.stack || e.reason}`));

const $ = (id) => document.getElementById(id);

// Файлы расширения на диске обновились, а Chrome держит старые — перезагружаемся сами, без кнопки ⟳
const onDisk = await fetch(chrome.runtime.getURL("manifest.json"), { cache: "no-store" }).then((r) => r.json()).catch(() => null);
if (onDisk && onDisk.version !== chrome.runtime.getManifest().version) {
  document.body.textContent = "Keeper обновился — открой это окно ещё раз.";
  setTimeout(() => chrome.runtime.reload(), 1200);
  await new Promise(() => {});  // дальше старый код не выполняем
}

// обычно работаем с вкладкой, на которой открыли окно; ?tab=N — когда окно открыто отдельной страницей (для проверок)
const asPage = +new URLSearchParams(location.search).get("tab");
const [tab] = asPage ? [await chrome.tabs.get(asPage)] : await chrome.tabs.query({ active: true, currentWindow: true });
$("tabTitle").textContent = tab?.title || "";
$("tabTitle").title = tab?.title || "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function showError(e) {
  $("err").textContent = e.message;
  if (!isDown(e)) clientLog(`окно: ${e.stack || e}`);
}

async function run(fn) {
  $("err").textContent = "";
  $("notice").innerHTML = "";
  try { await fn(); refresh(); } catch (e) { showError(e); }
}

// ----- состояние качалки: работает / запускается / не отвечает -----
const STATES = { on: "работает", starting: "запускается…", off: "не запущен — нажми" };
function setState(s) {
  $("state").className = "state " + s;
  $("stateText").textContent = STATES[s];
  $("state").title = s === "off" ? "Нажми, чтобы запустить" : "";
}

// Качалка сама закрывается, когда ей нечего делать, — при открытии окна будим её
let wake = null;
function wakeServer() {
  setState("starting");
  wake ||= startServer()
    .then(() => { $("err").textContent = ""; refresh(); })
    .catch((e) => { setState("off"); showError(e); });
}
$("state").onclick = () => {
  if (!$("state").classList.contains("off")) return;
  wake = null;
  wakeServer();
};

// ----- что поймали на этой вкладке -----
// ссылки потоков обновляются постоянно, а разметку перерисовываем, только когда она изменилась —
// поэтому кнопки берут видео и потоки отсюда, а не из момента отрисовки
let videos = [];
let items = [];

// innerHTML трогаем, только если разметка изменилась: иначе раз в секунду сбрасывались бы наведение и нажатия
function draw(el, html) {
  if (el._html === html) return false;
  el._html = html;
  el.innerHTML = html;
  return true;
}

const kindName = (it) => (it.master ? "HLS · все качества" : it.kind);
const clock = (s) => [s >= 3600 ? Math.floor(s / 3600) : null, Math.floor(s / 60) % 60, s % 60]
  .filter((x) => x !== null).map((x, i) => (i ? String(x).padStart(2, "0") : x)).join(":");

async function renderCaught() {
  const caught = await getCaught(tab.id);
  const known = isKnownSite(tab.url);
  videos = groupVideos(caught);
  items = [...caught.items].sort((a, b) => b.time - a.time);
  const many = !known && videos.length > 1;
  let hint = known
    ? ""
    : many
      ? `На странице ${videos.length} видео: скачай все или выбери ниже. Наведи на строку — видео подсветится на странице.`
      : caught.items.length
        ? ""
        : "Не вижу видео. Включи его (▶), подожди пару секунд и открой это окно снова.";
  if (caught.drm) hint += '<br><span class="warn">⚠ Видео защищено от копирования — скачать, скорее всего, не получится.</span>';
  $("hint").innerHTML = hint;
  $("go").textContent = many ? `⬇ Скачать все (${videos.length})` : "⬇ Скачать";

  const best = new Set(videos.map((v) => v.best));
  const html = !items.length ? "" : `<div class="section">Видео на странице<span>${videos.length > 1 ? videos.length : ""}</span></div>` + videos.map((v, n) => `
    <div class="item video ${v.playing ? "playing" : ""}" data-n="${n}">
      <div class="thumb">${n + 1}</div>
      <div class="info">
        <span class="name u" title="${esc(videoName(videos, n))}">${esc(videoName(videos, n))}</span>
        <span class="u">${esc([v.duration ? clock(v.duration) : "", v.playing ? "играет" : ""].filter(Boolean).join(" · "))}</span>
      </div>
      <button class="small primary" title="Скачать это видео — то, что отмечено вверху">⬇ Скачать</button>
    </div>`).join("") + `
    <details><summary>Скачалось не то? Все найденные ссылки (${items.length})</summary>` + items.map((it, i) => `
    <div class="item">
      <span class="tag ${best.has(it) ? "best" : ""}">${esc(kindName(it))}${best.has(it) ? " ★" : ""}</span>
      <span class="u" title="${esc(it.url)}">${esc(new URL(it.url).hostname + new URL(it.url).pathname)}</span>
      <button class="small" data-i="${i}">⬇</button>
    </div>`).join("") + "</details>";
  const open = $("caught").querySelector("details")?.open;
  if (draw($("caught"), html)) {
    if (open) $("caught").querySelector("details").open = true;
    $("caught").querySelectorAll(".video").forEach((row) => {
      const n = +row.dataset.n;
      row.onmouseenter = () => highlight(videos[n], true);
      row.onmouseleave = () => highlight(videos[n], false);
      row.querySelectorAll("button").forEach((b) => {
        b.onclick = () => run(async () => submit([await buildJob(tab, videos[n].best, {
          title: videoTitle(tab, videos, n),
          why: `видео ${n + 1} из ${videos.length} — выбрано в списке`,
        })]));
      });
    });
    $("caught").querySelectorAll("button[data-i]").forEach((b) => {
      b.onclick = () => run(async () => submit([await buildJob(tab, items[b.dataset.i])]));
    });
  }
  // превью меняется, пока видео играет, — обновляем его мимо перерисовки, чтобы строка не мигала
  $("caught").querySelectorAll(".video").forEach((row) => {
    const thumb = videos[row.dataset.n]?.thumb || "";
    const el = row.querySelector(".thumb");
    if (el._src === thumb) return;
    el._src = thumb;
    el.style.backgroundImage = thumb ? `url("${thumb.replace(/["\\\n]/g, "")}")` : "";
    el.classList.toggle("has", !!thumb);
  });
}

// Отправить задачи качалке. Уже скачанные она не берёт — про них спрашиваем, качать ли ещё раз
async function submit(jobs, force = false) {
  const skipped = [];
  for (const job of jobs) {
    try {
      await send(job, force);
    } catch (e) {
      if (!e.duplicate) throw e;
      skipped.push({ job, why: e.message });
    }
  }
  if (!skipped.length) return;
  const text = skipped.length === 1 ? skipped[0].why : `Пропущено ${skipped.length} видео: уже скачаны или качаются`;
  $("notice").innerHTML = `<span>${esc(text)}</span><button class="small">Скачать ещё раз</button>`;
  $("notice").querySelector("button").onclick = () => run(() => submit(skipped.map((s) => s.job), true));
}

// Показать на странице, какой плеер соответствует строке списка
function highlight(video, on) {
  if (!video?.src) return;
  chrome.scripting.executeScript({
    target: { tabId: tab.id },
    args: [video.src, on],
    func: (src, on) => {
      const frame = [...document.querySelectorAll("iframe")].find((e) => e.src === src);
      if (!frame) return;
      clearTimeout(frame._kachalka);
      frame.style.outline = on ? "4px solid #6d5efc" : "";
      if (!on) return;
      frame.scrollIntoView({ block: "center", behavior: "smooth" });
      frame._kachalka = setTimeout(() => { frame.style.outline = ""; }, 5000);  // окно могли закрыть, не убрав мышь
    },
  }).catch(() => {});
}

// ----- очередь загрузок -----
const WAITING = ["подготовка", "склейка", "звук из видео", "ждёт распознавания", "отмена…"];  // идёт работа, но процента нет — полоска «бегает»
const WITH_PERCENT = ["скачивается", "загрузка модели", "распознавание"];  // у этих этапов есть процент
// как этапы называются для человека (качалка отдаёт короткие служебные названия)
const STAGE = {
  "подготовка": "начинаю", "склейка": "собираю файл", "звук из видео": "вынимаю звук",
  "загрузка модели": "скачиваю распознавание речи (один раз)", "распознавание": "распознаю речь",
  "ждёт распознавания": "ждёт своей очереди на распознавание",
};
const ICON = { video: "🎬", audio: "🎵", text: "📝" };
const NAME = { video: "видео", audio: "звук", text: "текст" };
const FINISHED = /^(готово|ошибка|отменено)/;  // такие «Очистить» убирает из списка
const CANCELLABLE = ["в очереди", "подготовка", "скачивается", "ждёт распознавания", "загрузка модели", "распознавание"];  // склейку прервать нельзя — она и так секунды

function jobHtml(j) {
  const busy = j.status === "скачивается";
  const counting = WITH_PERCENT.includes(j.status);
  const want = (j.want || [j.mode]).map((w) => NAME[w]).join(" + ");
  const done = j.status.startsWith("готово");
  const eta = busy && j.eta ? "осталось " + (j.eta >= 60 ? Math.floor(j.eta / 60) + " мин" : Math.round(j.eta) + " с") : "";
  const extra = [j.quality, busy && j.done_mb ? j.done_mb + " МБ" : "", busy ? j.speed : "", eta].filter(Boolean).join(" · ");
  const several = (j.want || []).length > 1;  // одно — понятно по значку; сочетание называем словами
  const cls = done ? "done" : j.error ? "failed" : WAITING.includes(j.status) ? "waiting" : "";
  return `
    <div class="job ${cls}">
      <div class="top">
        <div class="title u" title="${esc(j.title)}">${(j.want || [j.mode]).map((w) => ICON[w]).join("")} ${esc(j.title)}</div>
        <div class="pct">${counting ? j.percent + "%" : ""}</div>
      </div>
      <div class="bar"><div style="width:${j.percent}%"></div></div>
      <div class="st">${several ? esc(want) + " · " : ""}${esc(STAGE[j.status] || j.status)}${extra ? " · " + esc(extra) : ""}${j.error ? " — " + esc(j.error) : ""}</div>
      <div class="acts">${
        CANCELLABLE.includes(j.status) ? `<button class="ghost small" data-act="/cancel" data-id="${esc(j.id)}">✕ Отмена</button>` : ""
      }${
        j.error || j.status === "отменено" ? `<button class="ghost small" data-act="/retry" data-id="${esc(j.id)}">↻ Повторить</button>` : ""
      }${
        done && j.has_file ? `<button class="ghost small" data-act="/open-file" data-id="${esc(j.id)}">▶ Открыть</button><button class="ghost small" data-act="/show-file" data-id="${esc(j.id)}">📂 В папке</button>` : ""
      }${j.has_log ? `<button class="ghost small" data-act="/open-log" data-id="${esc(j.id)}">Лог</button>` : ""}</div>
    </div>`;
}

async function refresh() {
  let data;
  try {
    data = await (await api("/status")).json();
  } catch (e) {
    if (!isDown(e)) return showError(e);
    return wake ? undefined : wakeServer();  // будим один раз; не вышло — дальше только по нажатию на статус
  }
  const { jobs, ytdlp, summary: s, folder } = data;
  setState("on");
  wake = null;
  $("folderPath").textContent = folder;
  $("folderPath").title = folder;
  $("ver").textContent = `v${chrome.runtime.getManifest().version} · yt-dlp ${ytdlp}`;
  const counts = [s.active ? `качается ${s.active}` : "", s.queued ? `в очереди ${s.queued}` : "", s.done ? `готово ${s.done}` : "", s.failed ? `ошибок ${s.failed}` : ""]
    .filter(Boolean).join(" · ");
  draw($("queue"), !jobs.length ? "" : `<div class="section">Загрузки<span class="u">${counts}</span>${jobs.some((j) => FINISHED.test(j.status)) ? '<button class="ghost small" data-act="/clear" title="Убрать из списка готовые и неудавшиеся (файлы остаются)">Убрать готовые</button>' : ""}</div>`
    + jobs.map(jobHtml).join(""));
}

// кнопки внутри очереди перерисовываются — ловим нажатия на самом списке
$("queue").onclick = (e) => {
  const b = e.target.closest("[data-act]");
  if (!b) return;
  run(async () => {
    const r = await api(b.dataset.act + (b.dataset.id ? "?id=" + b.dataset.id : ""));
    if (!r.ok) throw new Error((await r.json()).error);
  });
};

// Только вкладки, где есть что качать: известный сайт или пойманное видео. Раньше брались все открытые вкладки,
// и программа шла на страницы почты и банка с их cookies
const allTabs = [];
for (const t of await chrome.tabs.query({})) {
  if (!/^https?:/.test(t.url)) continue;
  if (isKnownSite(t.url) || (await getCaught(t.id)).items.length) allTabs.push(t);
}
$("all").textContent = `Скачать со всех вкладок с видео (${allTabs.length})`;
$("all").disabled = !allTabs.length;

$("go").onclick = () => run(async () => submit(await buildJobs(tab)));
$("all").onclick = () => run(async () => {
  const jobs = [];
  for (const t of allTabs) jobs.push(...await buildJobs(t));
  await submit(jobs);
});

// что получить: три переключателя, хотя бы один всегда включён; выбор запоминается
let want = await getWant();
function showWant() {
  document.querySelectorAll("#want .chip").forEach((c) => c.classList.toggle("on", want.includes(c.dataset.want)));
  $("languageRow").hidden = !want.includes("text");  // язык речи нужен только для текста
}
document.querySelectorAll("#want .chip").forEach((c) => {
  c.onclick = () => {
    const next = WANT.filter((w) => (w === c.dataset.want ? !want.includes(w) : want.includes(w)));
    if (!next.length) return;  // последний снять нельзя: качать «ничего» незачем
    want = next;
    chrome.storage.local.set({ want });
    showWant();
  };
});
showWant();
$("language").value = await getLanguage();
$("language").onchange = () => chrome.storage.local.set({ language: $("language").value });

$("quality").value = String(await getQuality());
$("quality").onchange = () => chrome.storage.local.set({ quality: +$("quality").value });
$("logs").onclick = () => run(() => apiUp("/open-logs"));
$("folder").onclick = () => run(() => apiUp("/open-folder"));
$("chooseFolder").onclick = () => run(async () => {
  $("chooseFolder").textContent = "Выбери в окне Windows…";
  try { await apiUp("/choose-folder"); } finally { $("chooseFolder").textContent = "Сменить"; }
});

const scan = () => chrome.runtime.sendMessage({ type: "scan", tabId: tab.id }).catch(() => {});
refresh();
await scan();
renderCaught();
setInterval(() => { refresh(); renderCaught(); }, 1000);
setInterval(scan, 2000);  // длительность и «играет сейчас» меняются, пока окно открыто
