"""Each AI feature must declare the right task. Swap one, and extraction
would silently spend the 20-a-day flash quota that photo checks need.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from radar import ai  # noqa: E402


class TaskWiring(unittest.TestCase):
    def tasks_used(self, call):
        seen = []

        def fake_generate(key, task, body, timeout, preferred=None):
            seen.append(task)
            r = mock.Mock(status_code=200)
            r.json.return_value = {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
            return r, "m"
        g = ai.Gemini()
        g.key = "k"
        with mock.patch.object(ai.gemini_models, "generate", fake_generate), \
                mock.patch("builtins.print"):
            call(g)
        return seen

    def test_advert_extraction_is_bulk(self):
        self.assertEqual(self.tasks_used(lambda g: g.extract("Seat Leon 2007")), ["bulk"])

    def test_photos_and_verdict_are_quality(self):
        self.assertEqual(self.tasks_used(lambda g: g.assess_photos([b"jpeg"], "car")), ["quality"])
        li = mock.Mock(title="Seat Leon", price_chf=3500, km=121000, year=2007,
                       mfk_date="", region="Delémont", body="")
        self.assertEqual(self.tasks_used(lambda g: g.assess(li, 4800, 7)), ["quality"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
