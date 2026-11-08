"""Tests for the word comparison in serial_audio.asr and the take check verdicts that use it.

    python -m unittest tests.test_asr
"""
from __future__ import annotations

import unittest

from serial_audio.asr import words
from serial_audio.takecheck import classify


class WordsTest(unittest.TestCase):
    def same(self, spoken: str, written: str) -> None:
        self.assertEqual(words(spoken), words(written), f"{spoken!r} vs {written!r}")

    def test_plain_words(self):
        self.assertEqual(words("Don’t stop, Simon!"), ["don't", "stop", "simon"])
        self.assertEqual(words("the dogs' bowls"), ["the", "dogs", "bowls"])

    def test_cardinals(self):
        self.same("a hundred and fifty", "150")
        self.same("one hundred fifty", "150")
        self.same("twenty-five", "25")
        self.same("two thousand three hundred and twelve", "2,312")
        self.same("two hundred and fifty thousand", "250,000")
        self.same("a thousand leagues", "1000 leagues")
        self.same("one million two hundred thousand", "1,200,000")
        self.same("twenty thousand leagues under the sea", "20,000 leagues under the sea")

    def test_years_and_times(self):
        self.same("in eighteen sixty-six", "in 1866")
        self.same("at ten thirty", "at 10:30")
        self.same("nineteen hundred", "1900")

    def test_ordinals(self):
        self.same("the twenty-first of May", "the 21st of May")
        self.same("the first time", "the 1st time")
        self.same("his hundredth day", "his 100th day")
        self.same("the third", "the 3rd")

    def test_symbols(self):
        self.same("thirty degrees fifteen minutes", "30° 15'")
        self.same("fifty percent", "50%")
        self.same("five dollars", "$5")
        self.same("three point five", "3.5")

    def test_separate_numbers_stay_apart(self):
        self.assertEqual(words("one, two, three"), ["1", "2", "3"])
        self.assertEqual(words("in 1866 two ships"), ["in", "1866", "2", "ships"])
        self.assertEqual(words("two and three"), ["2", "and", "3"])
        self.assertEqual(words("a man"), ["a", "man"])

    def test_number_formatting_is_not_flagged(self):
        expected = words("The Nautilus lay at a depth of a hundred and fifty metres, thirty degrees south.")
        verdict = classify(expected, "The Nautilus lay at a depth of 150 metres, 30° south.")
        self.assertEqual(verdict["verdict"], "ok", verdict)
        self.assertEqual(verdict["wer"], 0.0)

    def test_a_loop_is_still_flagged(self):
        expected = words("Yeah, right.")
        verdict = classify(expected, "Right, right, right, right, right.")
        self.assertEqual(verdict["verdict"], "flagged", verdict)


if __name__ == "__main__":
    unittest.main()
