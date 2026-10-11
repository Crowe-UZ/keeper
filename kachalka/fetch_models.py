"""Скачать модели распознавания заранее: `python -m kachalka.fetch_models ru multi`.

Этим пользуется установщик, если в нём отметили «текст из видео»: тогда при первом запросе текста ждать нечего.
Неудача не страшна — та же модель скачается сама при первом запросе текста. Код возврата: 0 — всё скачано,
1 — что-то не скачалось, 2 — названа неизвестная модель.

С ключом --lines вывод — для установщика, а не для человека: по строке на событие, только латиница и цифры
(«12» — проценты, «done», «failed»). Проценты установщик показывает в своём окне.
"""
import sys

from .models import MODELS, Cancelled, ensure_model, ensure_vad

FAILURES = (OSError, RuntimeError, Cancelled)  # нет сети, не сошлась сумма, прервали — скачается позже само


def for_people(out):
    def progress(percent):
        out.write(f"\r  {percent:5.1f}%")
        out.flush()

    return {"start": lambda spec: out.write(f"{spec['title']}, {spec['size_mb']} МБ\n"), "progress": progress,
            "done": lambda: out.write("\r  готово      \n"), "failed": lambda e: out.write(f"\n  не скачалась: {e}\n")}


def for_installer(out):
    shown = [-1]

    def line(text):
        out.write(text + "\n")
        out.flush()  # вывод идёт в трубу: без этого установщик увидел бы всё разом в самом конце

    def progress(percent):
        if int(percent) != shown[0]:  # не чаще раза на процент
            shown[0] = int(percent)
            line(str(shown[0]))

    return {"start": lambda spec: None, "progress": progress, "done": lambda: line("done"), "failed": lambda e: line("failed")}


def main(args, out=sys.stdout):
    names = [a for a in args if a != "--lines"]
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        out.write(f"Неизвестная модель: {', '.join(unknown)}. Есть: {', '.join(MODELS)}.\n")
        return 2
    say = for_installer(out) if "--lines" in args else for_people(out)
    failed = 0
    for name in names:
        say["start"](MODELS[name])
        try:
            ensure_vad()
            ensure_model(name, say["progress"])
            say["done"]()
        except FAILURES as e:
            say["failed"](e)
            failed = 1
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
