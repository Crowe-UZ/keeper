// Сквозная проверка: настоящее расширение в браузере + настоящая программа. Каждая кнопка окна.
//
// Запуск: программа должна работать (start.bat), затем в этой папке: npm install && npm test
// NO_WINDOWS=1 — не нажимать кнопки, которые открывают окна Windows.
//
// Видео для проверки создаётся на месте (ffmpeg: цветные полосы и тон, два варианта качества) и раздаётся отсюда же:
// раньше тест качал чужие тестовые ролики из интернета и «падал», когда тот сервер тормозил.
import puppeteer from "puppeteer";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const EXT = resolve(HERE, "..", "..", "extension");
const SITE = join(HERE, "site");

// Два тестовых «урока» в HLS: у каждого общий список качеств (master.m3u8) и два качества, звук вшит в видео —
// как на платформах курсов. Создаются один раз, лежат в site/gen (в git не входят).
function makeVideo(n, seconds, tone) {
  const dir = join(SITE, "gen", String(n));
  if (existsSync(join(dir, "master.m3u8"))) return;
  mkdirSync(dir, { recursive: true });
  execFileSync("ffmpeg", ["-v", "error", "-y",
    "-f", "lavfi", "-i", `testsrc2=size=1280x720:rate=25:duration=${seconds}`,
    "-f", "lavfi", "-i", `sine=frequency=${tone}:duration=${seconds}`,
    "-map", "0:v", "-map", "1:a", "-map", "0:v", "-map", "1:a",
    "-s:v:0", "640x360", "-b:v:0", "300k", "-s:v:1", "1280x720", "-b:v:1", "900k",
    "-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-c:a", "aac", "-b:a", "96k",
    "-f", "hls", "-hls_time", "4", "-hls_playlist_type", "vod", "-master_pl_name", "master.m3u8",
    "-hls_segment_filename", join(dir, "q%v_%03d.ts"),
    "-var_stream_map", "v:0,a:0 v:1,a:1", join(dir, "q%v.m3u8")], { cwd: dir, stdio: "inherit" });
}
makeVideo(1, 40, 440);
makeVideo(2, 30, 660);

// Тестовый «урок»: страница с двумя плеерами в iframe и их видео. Отдаём всё сами.
// Куски лучшего качества (q1_…) отдаём с задержкой: иначе маленький ролик скачивается за миг и тест не успевает
// увидеть «скачивается» и нажать «Отмена». Плеер на странице играет младшее качество и задержки не замечает.
const SLOW = 6000;
const TYPES = { ".html": "text/html; charset=utf-8", ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t" };
const site = createServer(async (req, res) => {
  const file = normalize(join(SITE, decodeURIComponent(new URL(req.url, "http://x").pathname)));
  if (!file.startsWith(SITE) || !TYPES[extname(file)] || !existsSync(file)) return res.writeHead(404).end();
  if (/[\\/]q1_\d+\.ts$/.test(file)) await new Promise((r) => setTimeout(r, SLOW));
  res.writeHead(200, { "Content-Type": TYPES[extname(file)] }).end(await readFile(file));
}).listen(5612, "127.0.0.1");
const ID = "haolabffcigdogdogoglgmgjkjibjnfc";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failed = 0;
const check = (name, ok, extra = "") => { if (!ok) failed++; console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  -> " + extra : ""}`); };

const browser = await puppeteer.launch({
  headless: true,
  pipe: true, enableExtensions: [EXT],
  args: ["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
});
try {
  const lesson = await browser.newPage();
  await lesson.goto("http://127.0.0.1:5612/page.html");
  const swTarget = await browser.waitForTarget((t) => t.type() === "service_worker" && t.url().includes(ID), { timeout: 15000 });
  const sw = await swTarget.worker();
  await sleep(8000);  // плееры грузят плейлисты и начинают играть
  const tabId = await sw.evaluate(async () => (await chrome.tabs.query({ url: "http://127.0.0.1:5612/*" }))[0].id);

  const popup = await browser.newPage();
  await popup.setViewport({ width: 420, height: 900 });
  const errors = [];
  popup.on("pageerror", (e) => errors.push(String(e)));
  await popup.goto(`chrome-extension://${ID}/popup.html?tab=${tabId}`);
  await sleep(4000);

  const text = (sel) => popup.$eval(sel, (e) => e.innerText.trim()).catch(() => null);
  const jobs = () => popup.$$eval(".job", (els) => els.map((j) => ({
    title: j.querySelector(".title").innerText.trim(), st: j.querySelector(".st").innerText.trim(),
    acts: [...j.querySelectorAll(".acts button")].map((b) => b.innerText.trim()),
  })));
  const waitJob = async (pred, limit = 180000) => {
    for (const t = Date.now(); Date.now() - t < limit; await sleep(400)) {
      const j = (await jobs()).find(pred);
      if (j) return j;
    }
    return null;
  };
  const clickAct = (titlePart, label) => popup.evaluate((titlePart, label) => {
    const job = [...document.querySelectorAll(".job")].find((j) => j.querySelector(".title").innerText.includes(titlePart));
    const b = job && [...job.querySelectorAll(".acts button")].find((x) => x.innerText.includes(label));
    if (b) b.click();
    return !!b;
  }, titlePart, label);
  // отметить вверху, что нужно получить (видео / звук / текст), — как это делает человек, кликами по переключателям
  const choose = async (...want) => {
    // сначала включаем нужное, потом выключаем лишнее: последний включённый переключатель снять нельзя
    for (const pass of [true, false]) {
      for (const w of ["video", "audio", "text"]) {
        const on = await popup.$eval(`#want .chip[data-want="${w}"]`, (c) => c.classList.contains("on"));
        if (on !== want.includes(w) && want.includes(w) === pass) await popup.click(`#want .chip[data-want="${w}"]`);
      }
    }
  };
  const cardBtn = async (n, ...want) => { await choose(...want); await popup.click(`.video[data-n="${n}"] button`); };

  // ---- окно: состояние, список видео ----
  check("программа отвечает (значок состояния скрыт, когда всё в порядке)", await popup.$eval("#state", (e) => e.classList.contains("on")));
  // как в жизни: вкладка с видео на виду (в фоновой вкладке браузер ставит видео на паузу), окно расширения — поверх неё
  await lesson.bringToFront();
  await sleep(5000);
  await popup.bringToFront();
  const cards = await popup.$$eval(".video", (els) => els.map((e) => ({
    name: e.querySelector(".name").innerText.trim(), meta: e.querySelectorAll(".info .u")[1].innerText.trim(),
    thumb: e.querySelector(".thumb").classList.contains("has"),
  })));
  console.log("      карточки:", JSON.stringify(cards));
  check("найдено 2 видео", cards.length === 2);
  check("видео 1: заголовок со страницы, без кода стилей", cards[0]?.name === "1. Часть первая", cards[0]?.name);
  check("видео 2: заголовка нет -> «Видео 2»", cards[1]?.name === "Видео 2", cards[1]?.name);
  check("превью-кадры есть", cards.every((c) => c.thumb));
  check("длительность и «играет»", cards.every((c) => /\d:\d\d/.test(c.meta) && c.meta.includes("играет")), cards.map((c) => c.meta).join(" | "));
  check("в строке видео нет технических слов", cards.every((c) => !/HLS|DASH|качеств/.test(c.meta)), cards.map((c) => c.meta).join(" | "));
  check("большая кнопка: скачать все", (await text("#go")) === "⬇ Скачать все (2)", await text("#go"));
  check("по умолчанию отмечено только «Видео»", (await popup.$$eval("#want .chip.on", (c) => c.map((x) => x.dataset.want))).join() === "video");
  await popup.click('#want .chip[data-want="video"]');
  check("последний переключатель снять нельзя", (await popup.$$("#want .chip.on")).length === 1);
  // ---- настройки: открываются шестерёнкой, выбор форматов запоминается ----
  check("настройки по умолчанию свёрнуты", await popup.$eval("#settings", (x) => x.hidden));
  await popup.click("#gear");
  check("шестерёнка открывает настройки", !(await popup.$eval("#settings", (x) => x.hidden)));
  check("форматы по умолчанию: mp4, m4a, txt", (await popup.$$eval("#fVideo, #fAudio, #fText", (x) => x.map((e) => e.value))).join() === "mp4,m4a,txt");
  await popup.select("#fText", "srt");
  await sleep(300);
  check("выбранный вид текста запоминается", (await sw.evaluate(async () => (await chrome.storage.local.get("formats")).formats?.text)) === "srt");
  await popup.select("#fText", "txt");
  await popup.click("#gear");
  check("шестерёнка сворачивает настройки обратно", await popup.$eval("#settings", (x) => x.hidden));
  check("папка показана", !!(await text("#folderPath")), await text("#folderPath"));

  // ---- «Подробнее» не схлопывается при обновлении списка ----
  await popup.click("#caught summary");
  await sleep(3000);
  check("«Подробнее» остаётся открытым", await popup.$eval("#caught details", (d) => d.open));
  check("в «Подробнее» есть потоки", (await popup.$$("#caught details .item")).length >= 2);
  check("для каждого видео выбран поток со всеми качествами", (await popup.$$eval("#caught details .tag.best", (t) => t.filter((x) => x.innerText.includes("все качества")).length)) === 2);

  // ---- подсветка плеера на странице ----
  await popup.hover('.video[data-n="1"]');
  await sleep(700);
  check("наведение подсвечивает второй плеер", (await lesson.$eval("#p2", (f) => f.style.outline)).includes("solid"));
  check("первый плеер не подсвечен", !(await lesson.$eval("#p1", (f) => f.style.outline)));
  await popup.hover("h1");
  await sleep(700);
  check("подсветка снимается", !(await lesson.$eval("#p2", (f) => f.style.outline)));

  // ---- качество ----
  await popup.click("#gear");
  await popup.select("#quality", "480");
  await popup.click("#gear");
  await sleep(300);
  check("качество запоминается", (await sw.evaluate(async () => (await chrome.storage.local.get("quality")).quality)) === 480);
  await popup.click("#gear");
  await popup.select("#quality", "0");  // дальше качаем лучшее качество: оно у тестового сервера «медленное»
  await popup.click("#gear");

  // ---- видео 2: скачать -> отмена -> повторить -> готово ----
  await cardBtn(1, "video");
  let j = await waitJob((x) => x.title.includes("видео 2") && x.st.startsWith("скачивается"), 60000);
  check("видео 2 качается, файл назван «… — видео 2»", !!j, j?.title);
  check("у загрузки есть «Отмена»", !!j?.acts.some((a) => a.includes("Отмена")));
  await clickAct("видео 2", "Отмена");
  j = await waitJob((x) => x.title.includes("видео 2") && x.st.startsWith("отменено"), 30000);
  check("«Отмена» отменяет", !!j, (await text("#err")) || "");
  check("у отменённой есть «Повторить»", !!j?.acts.some((a) => a.includes("Повторить")));
  await clickAct("видео 2", "Повторить");
  j = await waitJob((x) => x.title.includes("видео 2") && (x.st.startsWith("готово") || x.st.startsWith("ошибка")));
  check("«Повторить» докачивает до конца", !!j?.st.startsWith("готово"), j?.st);
  check("у готовой есть «Открыть файл» и «Показать в папке»", ["Открыть файл", "Показать в папке"].every((l) => j?.acts.some((a) => a.includes(l))), j?.acts.join(","));

  // ---- повторная загрузка того же видео ----
  await cardBtn(1, "video");
  await sleep(2500);
  const notice = await text("#notice span");
  check("уже скачанное не качается второй раз", !!notice?.startsWith("Уже скачано"), notice);
  check("новой задачи не появилось", (await jobs()).length === 1);
  await popup.click("#notice button");
  await sleep(2500);
  check("«Скачать ещё раз» запускает загрузку", (await jobs()).length === 2);
  await popup.evaluate(() => document.querySelector('.job [data-act="/cancel"]')?.click());
  await waitJob((x) => x.st.startsWith("отменено"), 30000);

  // ---- звук ----
  await cardBtn(1, "audio");
  j = await waitJob((x) => x.title.startsWith("🎵") && (x.st.startsWith("готово") || x.st.startsWith("ошибка")));
  check("«Звук» даёт звуковой файл", !!j?.st.startsWith("готово"), j?.st);

  // ---- сочетание: звук + текст одной задачей (в тестовом ролике речи нет — проверяем, что этап распознавания проходит) ----
  await popup.evaluate(() => document.querySelector('#queue [data-act="/clear"]')?.click());
  await sleep(1500);
  await popup.click("#notice button").catch(() => {});
  await cardBtn(0, "audio", "text");
  j = await waitJob((x) => x.title.startsWith("🎵📝") && (x.st.includes("готово") || x.st.includes("ошибка")), 300000);
  check("«Звук + Текст» — одна задача с обоими значками, доходит до конца", !!j?.st.includes("готово"), j?.st);
  check("в статусе названо сочетание", !!j?.st.startsWith("звук + текст"), j?.st);
  await choose("video");

  // ---- верхняя кнопка: все видео, уже скачанное пропускается ----
  await choose("video");
  await popup.click("#go");
  j = await waitJob((x) => x.title.includes("1. Часть первая") && x.st.startsWith("скачивается"), 60000);
  check("«Все видео» ставит в очередь видео 1 с названием в имени файла", !!j, j?.title);
  check("…а скачанное видео 2 пропускает с предупреждением", !!(await text("#notice span"))?.startsWith("Уже скачано"), await text("#notice span"));
  check("счётчик в шапке загрузок", (await text("#queue .section span"))?.includes("качается 1"), await text("#queue .section span"));
  await clickAct("1. Часть первая", "Отмена");
  await waitJob((x) => x.title.includes("1. Часть первая") && x.st.startsWith("отменено"), 30000);

  // ---- кнопки, которые открывают окна Windows (сам запрос должен пройти без ошибки) ----
  // эти кнопки открывают окна Windows поверх остальных — при NO_WINDOWS=1 их не трогаем
  for (const [name, fn] of process.env.NO_WINDOWS ? [] : [
    ["«Показать в папке» у готовой", () => clickAct("видео 2", "Показать в папке")],
    ["«Открыть папку»", () => popup.click("#folder")],
    ["«Файлы для разработчика»", async () => { await popup.$eval("#more", (d) => { d.open = true; }); await popup.click("#logs"); }],
  ]) {
    await fn();
    await sleep(1500);
    check(name, !(await text("#err")), (await text("#err")) || "");
  }

  // ---- «Очистить» ----
  await popup.click('#queue [data-act="/clear"]');
  await sleep(2000);
  check("«Очистить» убирает завершённые", (await jobs()).length === 0, JSON.stringify(await jobs()));

  // ---- F5 на странице урока: старые видео не должны остаться в списке рядом с новыми ----
  await lesson.bringToFront();
  await lesson.reload();
  await sleep(9000);
  await popup.bringToFront();
  await sleep(3500);
  check("после F5 в списке по-прежнему 2 видео, а не 4", (await popup.$$(".video")).length === 2, String((await popup.$$(".video")).length));
  check("после F5 большая кнопка качает 2 видео", (await text("#go")) === "⬇ Скачать все (2)", await text("#go"));

  check("ошибок скриптов в окне нет", errors.length === 0, errors.join(" | "));
  await popup.screenshot({ path: "popup.png", fullPage: true });
} finally {
  await browser.close();
  site.close();
}
console.log(failed ? `\nПРОВАЛЕНО: ${failed}` : "\nВСЁ ПРОШЛО");
process.exit(failed ? 1 : 0);
