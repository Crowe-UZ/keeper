// Пойманные потоки: что считать видео, как отличать одно от другого, как собирать потоки в видео.
// Здесь нет обращений к Chrome — только расчёты, поэтому этот файл проверяется тестами (tests/extension).

export const PLAYER = "Плеер GetCourse";
// iframe плеера GetCourse: адрес плеера меняется, путь — нет
export const SIGN_PLAYER_RE = /\/sign-player\/?\?(?:[^#]*&)?json=/;

const FILE_RE = /\.(mp4|webm|mov|m4v|mkv)(\?|#|$)/i;
// Куски потока и его служебные файлы: «seg-12.ts», «chunk_0004.m4s», «init.mp4», «init-v1-a1.mp4»
const SEGMENT_RE = /\.(ts|m4s|aac|m4a)(\?|#|$)|[/_-](seg|segment|frag|fragment|chunk)[-_]?\d|[/_-]init([-_][\w-]+)?\.\w+(\?|#|$)/i;
const MIN_FILE = 300 * 1024;  // «видео» меньше этого — заставка или значок предпросмотра, а не ролик

// Что это за ответ сервера: "HLS", "DASH", "Файл" или null — не видео. Одно правило и для перехвата запросов,
// и для ссылок из разметки страницы. size — полный размер файла, 0 — неизвестен (тогда не гадаем).
export function classify({ url, type = "", size = 0 }) {
  if (/mpegurl/.test(type) || /\.m3u8(\?|#|$)/i.test(url) || /\/api\/playlist\/(master|media)\//.test(url)) return "HLS";
  if (/dash\+xml/.test(type) || /\.mpd(\?|#|$)/i.test(url)) return "DASH";
  if (!(type.startsWith("video/") || FILE_RE.test(url)) || SEGMENT_RE.test(url)) return null;
  return size && size < MIN_FILE ? null : "Файл";
}

// Полный размер файла по заголовкам ответа. Плеер часто просит файл кусками (ответ 206): тогда размер всего
// файла стоит в Content-Range после «/», а Content-Length — это размер одного куска.
export function fullSize(headers, status) {
  const get = (name) => (headers || []).find((h) => h.name.toLowerCase() === name)?.value || "";
  const total = get("content-range").match(/\/(\d+)\s*$/)?.[1];
  return Number(status === 206 ? total : get("content-length")) || 0;
}

// Параметры адреса, которые называют само видео. Остальные (токены, подписи, срок действия) меняются
// от запроса к запросу, и по ним один поток выглядел бы как сотня разных.
const IDENTITY = /^(id|v|vid|video|video_?id|file|f|name|path|clip|media_?id|movie|episode|lesson|item)$/i;

// Адрес потока без изменчивой части: по нему узнаём «тот же самый поток»
export function norm(u) {
  const [path, query = ""] = u.split("#")[0].split(/\?(.*)/s);
  const kept = query.split("&").filter((p) => IDENTITY.test(p.split("=")[0])).sort();
  return kept.length ? path + "?" + kept.join("&") : path;
}

// Плееры GetCourse отличаются только параметром json=, их сравниваем целиком
export const itemKey = (i) => (i.kind === PLAYER ? i.url : norm(i.url));

// Потоков на вкладке держим не больше limit. Уходят самые старые, но главный плейлист (все качества) и плеер —
// в последнюю очередь: раньше на длинном уроке сотня плейлистов качеств вытесняла тот единственный, что нужен.
export function trimItems(items, limit = 150) {
  if (items.length <= limit) return items;
  const precious = (i) => (i.master || i.kind === PLAYER ? 1 : 0);
  const drop = new Set([...items].sort((a, b) => precious(a) - precious(b) || a.time - b.time).slice(0, items.length - limit));
  return items.filter((i) => !drop.has(i));
}

// «Папка» плейлиста: всё, что лежит в ней и глубже, — скорее всего его качества. Корень сайта папкой видео не считаем.
function folderOf(url) {
  const folder = norm(url).split("?")[0].replace(/[^/]*$/, "");
  try { return new URL(folder).pathname === "/" ? null : folder; } catch { return null; }
}

// Потоки вкладки -> отдельные видео. Каждый плеер живёт в своём iframe: один iframe = одно видео.
// Видео прямо на странице (без iframe) делим по главным плейлистам: качества из него — то же видео.
// Если плейлист прочитать не удалось (unread), его качества узнаём по папке: иначе одно видео становилось тремя.
// Порядок — как плееры идут на странице, иначе — в каком порядке поймали
export function groupVideos(caught) {
  const frames = caught.frames || {};
  const drmFrames = caught.drmFrames || {};
  const onTop = caught.items.filter((i) => !i.frameId);
  const masters = onTop.filter((i) => i.master);
  const unread = onTop.filter((i) => i.unread && folderOf(i.url));
  const groups = new Map();
  for (const it of caught.items) {
    let key = "f" + it.frameId;
    if (!it.frameId) {
      const hls = it.kind === "HLS" && !it.master;
      const m = hls && (masters.find((m) => m.variants?.includes(norm(it.url))) || unread.find((u) => norm(it.url).startsWith(folderOf(u.url))));
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
      // защита от копирования — у этого видео: по его плейлисту или по запросу лицензии из его плеера
      drm: g.items.some((i) => i.drm) || !!drmFrames[g.frameId],
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

// Лучший пойманный поток: главный плейлист HLS (все качества) > плеер GetCourse > DASH > любой m3u8 > файл;
// при равенстве — свежий
export function pickBest(items) {
  const rank = (i) => (i.master ? 5 : i.kind === PLAYER ? 4 : i.kind === "DASH" ? 3 : i.kind === "HLS" ? 2 : 1);
  return [...items].sort((a, b) => rank(b) - rank(a) || b.time - a.time)[0];
}
