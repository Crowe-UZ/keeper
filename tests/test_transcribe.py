"""Распознавание речи: выбор модели по языку, сборка текста из кусков, что качать и какие файлы отдавать."""
import tempfile
import unittest
from pathlib import Path

from kachalka import outputs, transcribe


class Language(unittest.TestCase):
    def test_cyrillic_speech_is_russian(self):
        self.assertEqual(transcribe.guess_language("Самый большой по ассортименту. Гипермаркеты."), "ru")

    def test_latin_speech_goes_to_the_multilingual_model(self):
        self.assertEqual(transcribe.guess_language("Ask not what your country can do for you."), "multi")

    def test_russian_with_a_few_english_words_is_still_russian(self):
        self.assertEqual(transcribe.guess_language("Это называется positioning и unique selling proposition, запомните."), "ru")

    def test_ukrainian_is_left_to_the_multilingual_model(self):
        self.assertEqual(transcribe.guess_language("Це дуже цікава і важлива історія про її життя, їжу та єдність"), "multi")

    def test_no_letters_means_we_could_not_tell(self):
        self.assertIsNone(transcribe.guess_language("... 123 !"))

    def test_explicit_choice_is_never_overridden(self):
        self.assertEqual(transcribe.pick_model("ru", "Hello there"), "ru")
        self.assertEqual(transcribe.pick_model("multi", "Привет"), "multi")

    def test_auto_follows_the_sample_and_defaults_to_russian(self):
        self.assertEqual(transcribe.pick_model("auto", "Hello there, general"), "multi")
        self.assertEqual(transcribe.pick_model("auto", "Привет всем"), "ru")
        self.assertEqual(transcribe.pick_model("auto", ""), "ru")


class LanguageProbe(unittest.TestCase):
    def test_short_recording_is_listened_from_the_start(self):
        self.assertEqual(transcribe.probe_start(0), 0)  # длительность узнать не удалось
        self.assertEqual(transcribe.probe_start(90), 0)

    def test_long_recording_skips_the_intro(self):
        # заставка с музыкой и приветствие на другом языке — в начале; язык записи слушаем чуть дальше
        self.assertEqual(transcribe.probe_start(240), 60)
        self.assertEqual(transcribe.probe_start(3600), transcribe.PROBE_SKIP)

    def test_silent_middle_falls_back_to_the_start(self):
        heard = {90: "", 0: "Hello and welcome to the course"}
        self.assertEqual(transcribe.probe_language(lambda start: heard[start], 3600), "multi")

    def test_speech_found_after_the_intro_decides(self):
        calls = []

        def hear(start):
            calls.append(start)
            return "Сегодня мы разберём третью тему курса"

        self.assertEqual(transcribe.probe_language(hear, 3600), "ru")
        self.assertEqual(calls, [transcribe.PROBE_SKIP])  # начало второй раз не слушаем

    def test_no_speech_anywhere_means_russian(self):
        self.assertEqual(transcribe.probe_language(lambda start: "", 3600), "ru")


class Text(unittest.TestCase):
    def test_pieces_are_joined_into_one_paragraph(self):
        self.assertEqual(transcribe.to_text([(0.0, 2.0, "Привет."), (2.1, 2.0, "Как дела?")]), "Привет. Как дела?\n")

    def test_a_long_pause_starts_a_new_paragraph(self):
        self.assertEqual(transcribe.to_text([(0.0, 2.0, "Первая мысль."), (6.0, 2.0, "Вторая мысль.")]),
                         "Первая мысль.\n\nВторая мысль.\n")

    def test_a_very_long_paragraph_is_split_at_a_sentence_end(self):
        pieces = [(i * 3.0, 2.9, "Это предложение из нескольких слов подряд.") for i in range(40)]
        paragraphs = transcribe.to_text(pieces).strip().split("\n\n")
        self.assertGreater(len(paragraphs), 1)
        self.assertTrue(all(p.endswith(".") for p in paragraphs))
        self.assertTrue(all(len(p) < 1200 for p in paragraphs))

    def test_empty_pieces_are_skipped(self):
        self.assertEqual(transcribe.to_text([(0.0, 1.0, "  "), (1.0, 1.0, "Текст.")]), "Текст.\n")
        self.assertEqual(transcribe.to_text([]), "")


class WhatToDownload(unittest.TestCase):
    def test_video_is_downloaded_whenever_it_is_wanted(self):
        self.assertEqual(outputs.download_mode(["video"]), "video")
        self.assertEqual(outputs.download_mode(["text", "audio", "video"]), "video")

    def test_audio_without_video_downloads_mp3(self):
        self.assertEqual(outputs.download_mode(["audio"]), "audio")
        self.assertEqual(outputs.download_mode(["audio", "text"]), "audio")

    def test_text_alone_downloads_only_a_temporary_sound_track(self):
        self.assertEqual(outputs.download_mode(["text"]), "text")

    def test_unknown_and_empty_requests_mean_video(self):
        self.assertEqual(outputs.clean_want(None), ["video"])
        self.assertEqual(outputs.clean_want([]), ["video"])
        self.assertEqual(outputs.clean_want(["text", "junk", "video", "text"]), ["video", "text"])

    def test_label_names_what_was_asked(self):
        self.assertEqual(outputs.want_label(["video"]), "видео")
        self.assertEqual(outputs.want_label(["text", "video"]), "видео + текст")


class FileNames(unittest.TestCase):
    def test_free_name_is_used_as_is_and_taken_one_gets_a_number(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(outputs.free_path(d, "Урок 1", ".txt"), Path(d) / "Урок 1.txt")
            (Path(d) / "Урок 1.txt").write_text("x", encoding="utf-8")
            self.assertEqual(outputs.free_path(d, "Урок 1", ".txt"), Path(d) / "Урок 1 (2).txt")

    def test_text_sits_next_to_the_video_even_if_the_video_exists(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Урок 1.mp4").write_text("x", encoding="utf-8")
            self.assertEqual(outputs.free_path(d, "Урок 1", ".txt"), Path(d) / "Урок 1.txt")


if __name__ == "__main__":
    unittest.main()
