"""Защита локального адреса программы, точные сообщения об ошибках, имена файлов Windows, повтор с cookies."""
import unittest

from kachalka import api, download, files
from kachalka.errors import UserError

MINE = "chrome-extension://" + api.EXTENSION_ID
HOST = f"127.0.0.1:{api.PORT}"


class Gate(unittest.TestCase):
    """Кто может обращаться к программе: только наше расширение и только по настоящему локальному адресу."""

    def test_our_extension_is_let_in(self):
        self.assertIsNone(api.refusal("GET", {"Host": HOST, "X-Kachalka": "1"}))
        self.assertIsNone(api.refusal("POST", {"Host": HOST, "X-Kachalka": "1", "Origin": MINE}))
        self.assertIsNone(api.refusal("GET", {"Host": f"localhost:{api.PORT}", "X-Kachalka": "1"}))

    def test_a_web_page_is_refused(self):
        self.assertIsNotNone(api.refusal("POST", {"Host": HOST, "X-Kachalka": "1", "Origin": "https://evil.example"}))
        self.assertIsNotNone(api.refusal("GET", {"Host": HOST}))  # без заголовка расширения
        self.assertIsNotNone(api.refusal("OPTIONS", {"Host": HOST, "Origin": "https://evil.example"}))

    def test_another_extension_is_refused(self):
        other = "chrome-extension://" + "a" * 32
        self.assertIsNotNone(api.refusal("POST", {"Host": HOST, "X-Kachalka": "1", "Origin": other}))

    def test_a_site_pretending_to_be_local_is_refused(self):
        # подмена адреса (DNS rebinding): страница evil.example вдруг указывает на 127.0.0.1 — заголовок Host её выдаёт
        self.assertIsNotNone(api.refusal("GET", {"Host": f"evil.example:{api.PORT}", "X-Kachalka": "1"}))
        self.assertIsNotNone(api.refusal("GET", {"X-Kachalka": "1"}))  # вовсе без Host

    def test_preflight_from_our_extension_passes(self):
        self.assertIsNone(api.refusal("OPTIONS", {"Host": HOST, "Origin": MINE}))

    def test_changing_requests_must_be_post(self):
        for path in ("/clear", "/cancel", "/retry", "/open-file", "/show-file", "/open-folder", "/open-logs", "/open-log",
                     "/choose-folder"):
            self.assertIn(path, api.ACTIONS, path)
        self.assertEqual(set(api.ROUTES), {"/status"})  # читать можно GET-запросом, менять — нельзя


class Errors(unittest.TestCase):
    def test_our_own_messages_reach_the_user_unchanged(self):
        for text in ("Для распознавания нужен ffmpeg — он достаёт звук из файла.",
                     "GetCourse просит войти заново. Обнови урок (F5) и попробуй снова.",
                     "Не получилось вынуть звук из видео (нужен ffmpeg): сбой"):
            self.assertEqual(download.explain(UserError(text)), text)

    def test_file_named_like_an_error_code_is_not_mistaken_for_one(self):
        # «403» в названии урока — не отказ сайта
        self.assertNotIn("не отдал видео", download.friendly_error("unable to open for writing: Урок 403.mp4"))
        self.assertNotIn("не отдал видео", download.friendly_error("Bad path /login-video/x"))

    def test_real_refusals_are_still_recognised(self):
        self.assertIn("не отдал видео", download.friendly_error("HTTP Error 403: Forbidden"))
        self.assertIn("не отдал видео", download.friendly_error("Sign in to confirm you are not a bot"))
        self.assertIn("защищено от копирования", download.friendly_error("This video is DRM protected"))

    def test_foreign_error_goes_through_the_translator(self):
        self.assertIn("не отдал видео", download.explain(RuntimeError("HTTP Error 401: Unauthorized")))


class Cookies(unittest.TestCase):
    def test_retry_with_cookies_only_when_the_site_asked_to_sign_in(self):
        self.assertTrue(download.wants_login("HTTP Error 403: Forbidden"))
        self.assertTrue(download.wants_login("Sign in to confirm your age"))
        self.assertTrue(download.wants_login("This video is private"))

    def test_other_failures_are_not_repeated(self):
        # иначе два гигабайта качались бы второй раз, чтобы упасть на том же месте
        for text in ("ffmpeg exited with code 1", "No space left on device", "Unsupported URL: https://x",
                     "This video is DRM protected", "certificate verify failed"):
            self.assertFalse(download.wants_login(text), text)


class WindowsNames(unittest.TestCase):
    def test_reserved_device_names_get_a_suffix(self):
        # файл с именем CON или NUL Windows «принимает» и молча теряет
        for name in ("CON", "nul", "Com1", "LPT9", "AUX", "prn"):
            self.assertEqual(files.safe_name(name), name + "_")
        self.assertEqual(files.safe_name("NUL.txt лекция"), "NUL.txt лекция")  # это уже обычное имя
        self.assertEqual(files.safe_name("Console"), "Console")

    def test_suffix_survives_a_very_long_title(self):
        long = "я" * 300
        self.assertTrue(files.safe_name(long, keep=" — видео 2").endswith(" — видео 2"))
        self.assertLessEqual(len(files.safe_name(long, keep=" — видео 2")), 150)
        self.assertEqual(files.safe_name("Урок", keep=" — часть 3"), "Урок — часть 3")


if __name__ == "__main__":
    unittest.main()
