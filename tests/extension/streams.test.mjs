// Логика расширения про пойманные потоки: что считать видео, как отличать одно видео от другого,
// как собирать потоки в видео. Запуск: node --test tests/extension
import assert from "node:assert/strict";
import { test } from "node:test";
import { PLAYER, classify, fullSize, groupVideos, itemKey, norm, trimItems } from "../../extension/streams.js";

const item = (url, more = {}) => ({ url, kind: "HLS", frameId: 0, headers: {}, time: 1, master: false, ...more });

test("плейлисты и файлы узнаются по типу ответа и по адресу", () => {
  assert.equal(classify({ url: "https://cdn/x/master.m3u8?t=1" }), "HLS");
  assert.equal(classify({ url: "https://cdn/x/get", type: "application/vnd.apple.mpegurl" }), "HLS");
  assert.equal(classify({ url: "https://cdn/x/manifest.mpd" }), "DASH");
  assert.equal(classify({ url: "https://cdn/x/movie.mp4", type: "video/mp4", size: 50_000_000 }), "Файл");
  assert.equal(classify({ url: "https://cdn/x/movie.mkv" }), "Файл");
  assert.equal(classify({ url: "https://cdn/x/logo.png", type: "image/png" }), null);
});

test("куски потока и служебные файлы — не видео", () => {
  for (const url of ["https://cdn/x/seg-12.ts", "https://cdn/x/chunk_0004.m4s", "https://cdn/x/init.mp4",
    "https://cdn/x/init-v1-a1.mp4", "https://cdn/x/segment12.mp4", "https://cdn/x/frag-3.mp4"]) {
    assert.equal(classify({ url, type: "video/mp4" }), null, url);
  }
  // «видео» в 40 КБ — это заставка или значок предпросмотра, а не ролик
  assert.equal(classify({ url: "https://cdn/x/hover.mp4", type: "video/mp4", size: 40_000 }), null);
  // размер неизвестен — не гадаем, оставляем
  assert.equal(classify({ url: "https://cdn/x/movie.mp4", type: "video/mp4" }), "Файл");
});

test("полный размер файла берётся из ответа, а не из размера одного куска", () => {
  const h = (o) => Object.entries(o).map(([name, value]) => ({ name, value }));
  assert.equal(fullSize(h({ "Content-Length": "5000" }), 200), 5000);
  assert.equal(fullSize(h({ "Content-Range": "bytes 0-1023/90000000", "Content-Length": "1024" }), 206), 90000000);
  assert.equal(fullSize(h({ "Content-Range": "bytes 0-1023/*", "Content-Length": "1024" }), 206), 0);  // неизвестен
  assert.equal(fullSize([], 200), 0);
});

test("токены в адресе не делают поток новым, а номер видео — делает", () => {
  assert.equal(norm("https://cdn/v/master.m3u8?token=abc&expires=1"), norm("https://cdn/v/master.m3u8?token=zzz&expires=2"));
  assert.notEqual(norm("https://cdn/play.m3u8?id=1&token=a"), norm("https://cdn/play.m3u8?id=2&token=a"));
  assert.equal(norm("https://cdn/play.m3u8?token=a&id=1"), norm("https://cdn/play.m3u8?id=1&sig=b"));
  assert.equal(norm("https://cdn/v/master.m3u8#t=5"), "https://cdn/v/master.m3u8");
});

test("плееры GetCourse сравниваются целиком", () => {
  const a = { kind: PLAYER, url: "https://p/sign-player/?json=AAA" };
  assert.notEqual(itemKey(a), itemKey({ ...a, url: "https://p/sign-player/?json=BBB" }));
});

test("когда потоков слишком много, главный плейлист и плеер не вытесняются", () => {
  const master = item("https://cdn/v/master.m3u8", { master: true, time: 1 });
  const player = item("https://p/sign-player/?json=A", { kind: PLAYER, time: 2 });
  const rest = Array.from({ length: 200 }, (_, n) => item(`https://cdn/v/${n}.m3u8`, { time: 10 + n }));
  const kept = trimItems([master, player, ...rest], 150);
  assert.equal(kept.length, 150);
  assert.ok(kept.includes(master) && kept.includes(player));
  assert.ok(kept.includes(rest.at(-1)) && !kept.includes(rest[0]));  // из прочих уходят самые старые
  assert.deepEqual(trimItems([master, player], 150), [master, player]);
});

test("качества одного видео — одно видео", () => {
  const master = item("https://cdn/v/1/master.m3u8", { master: true, variants: ["https://cdn/v/1/q0.m3u8", "https://cdn/v/1/q1.m3u8"] });
  const videos = groupVideos({ items: [master, item("https://cdn/v/1/q0.m3u8?t=1"), item("https://cdn/v/1/q1.m3u8?t=2")], frames: {} });
  assert.equal(videos.length, 1);
  assert.equal(videos[0].best, master);
});

test("плейлист не прочитался — его качества всё равно одно видео, а не три", () => {
  const master = item("https://cdn/v/1/master.m3u8", { unread: true });
  const caught = { items: [master, item("https://cdn/v/1/q0.m3u8"), item("https://cdn/v/1/hd/index.m3u8")], frames: {} };
  assert.equal(groupVideos(caught).length, 1);
});

test("…но соседнее видео с ним не сливается", () => {
  const caught = { items: [item("https://cdn/v/1/master.m3u8", { unread: true }), item("https://cdn/v/2/master.m3u8", { unread: true }),
    item("https://cdn/v/2/q0.m3u8")], frames: {} };
  assert.equal(groupVideos(caught).length, 2);
  // плейлист в корне сайта «папкой видео» не считаем: иначе слилось бы всё с этого сервера
  const root = { items: [item("https://cdn/a.m3u8", { unread: true }), item("https://cdn/other/b.m3u8")], frames: {} };
  assert.equal(groupVideos(root).length, 2);
});

test("каждый плеер во фрейме — отдельное видео", () => {
  const caught = { items: [item("https://cdn/a/master.m3u8", { frameId: 5 }), item("https://cdn/a/q0.m3u8", { frameId: 5 }),
    item("https://cdn/b/master.m3u8", { frameId: 7 })], frames: { 5: { order: 1 }, 7: { order: 0 } } };
  const videos = groupVideos(caught);
  assert.deepEqual(videos.map((v) => v.frameId), [7, 5]);  // порядок — как плееры стоят на странице
});

test("защита от копирования помечает своё видео, а не все на вкладке", () => {
  const caught = { items: [item("https://cdn/a/master.m3u8", { frameId: 5, drm: true }), item("https://cdn/b/master.m3u8", { frameId: 7 }),
    item("https://cdn/c/master.m3u8", { frameId: 9 })], frames: {}, drmFrames: { 9: true } };
  assert.deepEqual(groupVideos(caught).map((v) => [v.frameId, v.drm]), [[5, true], [7, false], [9, true]]);
});
