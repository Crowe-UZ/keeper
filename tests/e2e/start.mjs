// Проверка запуска: программа не работает -> открыли окно расширения -> браузер сам её поднял.
// Это единственное место, где браузеры отличаются друг от друга: каждый ищет программу по своей записи в реестре.
//
// Запуск: программа должна быть ЗАКРЫТА (значок у часов -> Выход), затем в этой папке:
//   node start.mjs                                  — в браузере, который ставит puppeteer
//   BROWSER_EXE="C:\...\msedge.exe" node start.mjs  — в Edge, Яндекс Браузере или настоящем Chrome
import puppeteer from "puppeteer";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const EXT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..", "extension");
const ID = "haolabffcigdogdogoglgmgjkjibjnfc";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const up = () => fetch("http://127.0.0.1:5577/status", { headers: { "X-Kachalka": "1" } }).then((r) => r.ok).catch(() => false);

if (await up()) {
  console.log("Программа уже работает — закрой её (значок у часов -> Выход) и повтори: проверяем именно запуск.");
  process.exit(1);
}
const browser = await puppeteer.launch({
  headless: true,
  pipe: true, enableExtensions: [EXT],
  executablePath: process.env.BROWSER_EXE || undefined,
});
let ok = false;
let shown = "";
let name = process.env.BROWSER_EXE || "браузер puppeteer";
try {
  name = await browser.version();
  await browser.waitForTarget((t) => t.type() === "service_worker" && t.url().includes(ID), { timeout: 15000 });
  const popup = await browser.newPage();
  await popup.goto(`chrome-extension://${ID}/popup.html`);
  for (let i = 0; i < 80 && !ok; i++) {  // до 40 секунд: первый запуск распаковывает библиотеки
    await sleep(500);
    ok = await up();
  }
  shown = await popup.$eval("#err", (e) => e.innerText.trim()).catch(() => "");
} finally {
  await browser.close();
}
console.log(`${ok ? "PASS" : "FAIL"}  ${name}: окно расширения запустило программу${shown ? "  -> " + shown : ""}`);
process.exit(ok ? 0 : 1);
