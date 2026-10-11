"""Приём задач: проверка запроса, одна задача — один обработчик, повтор и очистка не трогают работающее."""
import unittest

from kachalka import api, download
from kachalka.state import jobs, lock

GOOD = {"url": "https://example.com/v", "title": "Урок", "page_url": "https://example.com/p", "want": ["video", "text"],
        "headers": {"Referer": "https://example.com/", "X-Evil": "1"}, "max_height": 720, "language": "ru",
        "cookies": [{"domain": ".example.com", "name": "sid", "value": "1"}]}


class ParseJob(unittest.TestCase):
    def test_good_request_is_normalised(self):
        job = api.parse_job(GOOD)
        self.assertEqual(job["want"], ["video", "text"])
        self.assertEqual(job["mode"], "video")
        self.assertEqual(job["headers"], {"Referer": "https://example.com/"})  # чужие заголовки не проходят
        self.assertEqual((job["max_height"], job["language"]), (720, "ru"))

    def test_defaults(self):
        job = api.parse_job({"url": "https://example.com/v"})
        self.assertEqual((job["want"], job["language"], job["max_height"], job["cookies"]), (["video"], "auto", 0, []))
        self.assertEqual(job["title"], "https://example.com/v")

    def test_old_extension_field_mode_still_works(self):
        self.assertEqual(api.parse_job({"url": "https://e.com/v", "mode": "audio"})["want"], ["audio"])

    def test_only_http_links(self):
        for url in ("ftp://example.com/v", "file:///C:/secret.txt", "", None, "javascript:alert(1)"):
            with self.assertRaises(ValueError):
                api.parse_job({**GOOD, "url": url})

    def test_wrong_shapes_are_refused_not_crashed_on(self):
        for bad in ([], "text", None, 5):
            with self.assertRaises(ValueError):
                api.parse_job(bad)
        for field, value in (("headers", ["x"]), ("cookies", {"a": 1}), ("cookies", [["x"]]), ("cookies", [{"name": "sid"}]),
                             ("cookies", [{"domain": 1, "name": "a", "value": "b"}])):
            with self.assertRaises(ValueError):
                api.parse_job({**GOOD, field: value})

    def test_unknown_quality_and_language_fall_back(self):
        job = api.parse_job({**GOOD, "max_height": 999, "language": "klingon"})
        self.assertEqual((job["max_height"], job["language"]), (0, "auto"))


class Claim(unittest.TestCase):
    def setUp(self):
        with lock:
            jobs["t1"] = {"status": "в очереди", "cancel": False}

    def tearDown(self):
        with lock:
            jobs.pop("t1", None)

    def test_a_job_is_taken_by_one_worker_only(self):
        self.assertIs(download.claim("t1"), jobs["t1"])
        self.assertIsNone(download.claim("t1"))  # второй обработчик с тем же номером задачу не получает

    def test_released_job_is_not_taken_again_unless_requeued(self):
        job = download.claim("t1")
        job["status"] = "готово"
        download.release("t1", job)
        self.assertIsNone(download.claim("t1"))

    def test_retry_pressed_during_cleanup_starts_when_the_worker_lets_go(self):
        from kachalka.state import tasks
        job = download.claim("t1")
        job.update(status="в очереди", retry_wanted=True)  # так помечает задачу «Повторить», пока обработчик ещё убирает
        self.assertIsNone(download.claim("t1"))  # пока обработчик не закончил, вторая попытка не начинается
        before = tasks.qsize()
        download.release("t1", job)
        self.assertEqual(tasks.qsize(), before + 1)  # отпустил — задача сама встала в очередь
        self.assertIs(download.claim(tasks.get_nowait()), job)

    def test_cancelled_while_waiting_is_not_taken(self):
        jobs["t1"]["status"] = "отменено"
        self.assertIsNone(download.claim("t1"))

    def test_removed_job_is_not_taken(self):
        self.assertIsNone(download.claim("нет такой"))


if __name__ == "__main__":
    unittest.main()
