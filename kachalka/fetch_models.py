"""Скачать модели распознавания заранее: `python -m kachalka.fetch_models ru multi`.

Этим пользуется установщик, если в нём отметили «текст из видео»: тогда при первом запросе текста ждать нечего.
Неудача не страшна — та же модель скачается сама при первом запросе текста. Код возврата: 0 — всё скачано,
1 — что-то не скачалось, 2 — названа неизвестная модель.
"""
import sys

from .models import MODELS, Cancelled, ensure_model, ensure_vad


def main(names, out=sys.stdout):
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        out.write(f"Неизвестная модель: {', '.join(unknown)}. Есть: {', '.join(MODELS)}.\n")
        return 2

    def progress(percent):
        out.write(f"\r  {percent:5.1f}%")
        out.flush()

    failed = 0
    try:
        ensure_vad()
    except (OSError, RuntimeError, Cancelled) as e:
        out.write(f"Детектор речи не скачался: {e}\n")
        failed = 1
    for name in names:
        out.write(f"{MODELS[name]['title']}, {MODELS[name]['size_mb']} МБ\n")
        try:
            ensure_model(name, progress)
            out.write("\r  готово      \n")
        except (OSError, RuntimeError, Cancelled) as e:  # нет сети, не сошлась сумма, прервали — скачается позже само
            out.write(f"\n  не скачалась: {e}\n")
            failed = 1
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
