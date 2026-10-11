"""Окно Windows «Выбор папки» — отдельным процессом: `python -m kachalka.folder_dialog <с какой папки начать>`.

Отдельным, потому что оконная библиотека tkinter рассчитана на один поток на всю программу, а запросы
расширения приходят каждый в своём: второе окно из другого потока роняло программу целиком.
Печатает выбранную папку (UTF-8); ничего не выбрали — пустую строку.
"""
import sys


def main():
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # поверх Chrome
    chosen = filedialog.askdirectory(parent=root, initialdir=sys.argv[1] if len(sys.argv) > 1 else None,
                                     title="Куда сохранять файлы?")
    root.destroy()
    sys.stdout.buffer.write((chosen or "").encode("utf-8"))


if __name__ == "__main__":
    main()
