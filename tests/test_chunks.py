"""Подготовка речи к распознаванию: склейка коротких кусков, деление слишком длинных, отсев мусора, стыки в тексте."""
import unittest

from kachalka import transcribe

RATE = transcribe.RATE


def piece(start, seconds):
    return start, [0.1] * int(seconds * RATE)


def spans(chunks):
    return [(round(start, 2), round(len(samples) / RATE, 2)) for start, samples in chunks]


class Merge(unittest.TestCase):
    def test_short_neighbours_become_one_chunk(self):
        # три фразы с паузами по полсекунды — модель распознаёт их одним куском, а не тремя обрывками
        got = list(transcribe.merge_pieces([piece(0, 3), piece(3.5, 4), piece(8, 5)]))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][0], 0)
        self.assertAlmostEqual(len(got[0][1]) / RATE, 3 + 0.3 + 4 + 0.3 + 5, places=2)  # паузы сжаты до 0,3 с

    def test_a_long_pause_keeps_chunks_apart(self):
        got = list(transcribe.merge_pieces([piece(0, 3), piece(10, 4)]))
        self.assertEqual(spans(got), [(0, 3.0), (10, 4.0)])

    def test_chunk_never_grows_past_the_limit(self):
        got = list(transcribe.merge_pieces([piece(i * 6.2, 6) for i in range(8)]))
        self.assertGreater(len(got), 1)
        self.assertTrue(all(len(samples) / RATE <= transcribe.MAX_CHUNK for _, samples in got))

    def test_nothing_in_nothing_out(self):
        self.assertEqual(list(transcribe.merge_pieces([])), [])


class Split(unittest.TestCase):
    def test_normal_piece_is_left_alone(self):
        self.assertEqual(len(transcribe.split_long([0.0] * (20 * RATE))), 1)

    def test_overlong_piece_is_cut_into_equal_parts(self):
        # пение или речь поверх музыки: детектор не нашёл паузы, кусок вырос — модели на такую длину не рассчитаны
        parts = transcribe.split_long([0.0] * (50 * RATE))
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(len(p) / RATE <= transcribe.MAX_PIECE for p in parts))
        self.assertEqual(sum(len(p) for p in parts), 50 * RATE)


class Noise(unittest.TestCase):
    def test_lone_filler_is_dropped(self):
        for text in ("Mmm.", "Угу.", "Yeah.", "ммм", "Hm"):
            self.assertTrue(transcribe.is_filler(text, 0.8), text)

    def test_real_short_answer_is_kept(self):
        self.assertFalse(transcribe.is_filler("Да, конечно.", 0.8))
        self.assertFalse(transcribe.is_filler("Нет.", 0.8))

    def test_filler_inside_long_speech_is_kept(self):
        self.assertFalse(transcribe.is_filler("Угу.", 6.0))


class Joins(unittest.TestCase):
    def test_forced_cut_does_not_start_a_new_sentence(self):
        # кусок оборвался на полуслове без паузы — следующий не должен начинаться с заглавной
        text = transcribe.to_text([(0.0, 20.0, "Мы говорили о том, что рынок"), (20.1, 5.0, "Растёт быстрее, чем мы думали.")])
        self.assertEqual(text, "Мы говорили о том, что рынок растёт быстрее, чем мы думали.\n")

    def test_new_sentence_after_a_full_stop_keeps_its_capital(self):
        text = transcribe.to_text([(0.0, 5.0, "Первое предложение."), (5.1, 5.0, "Второе предложение.")])
        self.assertEqual(text, "Первое предложение. Второе предложение.\n")

    def test_abbreviation_in_capitals_is_not_lowered(self):
        text = transcribe.to_text([(0.0, 20.0, "Это называется"), (20.1, 3.0, "JTBD, запомните.")])
        self.assertIn("называется JTBD", text)

    def test_paragraph_has_a_hard_ceiling(self):
        pieces = [(i * 3.0, 2.95, "слова без точки в конце и без паузы между кусками") for i in range(80)]
        self.assertTrue(all(len(p) <= 1700 for p in transcribe.to_text(pieces).strip().split("\n\n")))


class Russian(unittest.TestCase):
    def test_bulgarian_is_not_taken_for_russian(self):
        # болгарская кириллица без букв ы, э, ё — её умеет многоязычная модель, русская на ней ошибается
        bg = "Това е много интересна история за нашия град и за хората които живеят в него от много години насам"
        self.assertEqual(transcribe.guess_language(bg), "multi")

    def test_long_russian_sample_is_russian(self):
        ru = "Сегодня мы поговорим о том, как устроен рынок и почему это важно для каждого из нас, это первый шаг"
        self.assertEqual(transcribe.guess_language(ru), "ru")

    def test_short_russian_sample_without_special_letters_is_still_russian(self):
        self.assertEqual(transcribe.guess_language("Самый большой по ассортименту."), "ru")


class Threads(unittest.TestCase):
    def test_threads_follow_the_number_of_cores(self):
        self.assertEqual(transcribe.threads_for(12), 4)  # больше четырёх скорости не добавляет — замерено
        self.assertEqual(transcribe.threads_for(4), 2)   # два ядра с гиперпотоками: не занимаем всё
        self.assertEqual(transcribe.threads_for(2), 1)
        self.assertEqual(transcribe.threads_for(None), 2)


if __name__ == "__main__":
    unittest.main()
