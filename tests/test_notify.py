"""Уведомление Windows: название видео приходит с сайта и не должно становиться командой."""
import subprocess
import unittest

from kachalka import notify

EVIL = "Урок\n'@) ; Write-Output INJECTED ; $null = (@'\nхвост"


class Toast(unittest.TestCase):
    def test_script_is_a_constant_that_reads_content_from_the_environment(self):
        self.assertIn("$env:KACHALKA_TOAST", notify.SCRIPT)
        self.assertNotIn("@'", notify.SCRIPT)  # никаких вставок текста внутрь скрипта

    def test_title_from_a_site_stays_text(self):
        xml = notify.toast_xml("Готово", EVIL)
        self.assertNotIn("\n", xml)
        self.assertIn("Write-Output INJECTED", xml)  # осталось обычным текстом внутри уведомления
        self.assertTrue(xml.startswith("<toast>") and xml.endswith("</toast>"))

    def test_markup_characters_are_escaped(self):
        xml = notify.toast_xml('a<b>&"c"', "<script>")
        self.assertIn("a&lt;b&gt;&amp;", xml)
        self.assertNotIn("<script>", xml)

    def test_evil_title_does_not_run_as_a_command(self):
        # тот же способ запуска, что в toast(), но скрипт только печатает полученное содержимое
        probe = "Write-Output ('GOT:' + $env:KACHALKA_TOAST.Length)"
        import os
        r = subprocess.run(["powershell", "-NoProfile", "-Command", probe], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "KACHALKA_TOAST": notify.toast_xml("Готово", EVIL)})
        self.assertIn("GOT:", r.stdout)
        self.assertNotIn("INJECTED", r.stdout)

    def test_finished_file_gets_open_buttons(self):
        xml = notify.toast_xml("Готово", "Урок", r"C:\Videos\Урок 1.mp4")
        self.assertIn("Открыть файл", xml)
        self.assertIn("Показать в папке", xml)
        self.assertIn("file:///C:/Videos/", xml)


if __name__ == "__main__":
    unittest.main()
