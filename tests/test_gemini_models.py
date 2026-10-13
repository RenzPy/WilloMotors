"""Tests for per-task Gemini model selection.

Identical in job-scout and WilloMotors, like gemini_models.py itself.
Everything here is pinned to what the real account showed on 2026-09-29:
REAL_MODELS is the list the API returned, flash models allow 20 requests a
day and flash-lite 500, gemini-2.5-flash is listed but "no longer available
to new users", and search grounding has no free quota.

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from jobscout import ai as aimod, gemini_models as gm  # noqa: E402
except ImportError:
    from radar import ai as aimod, gemini_models as gm     # noqa: E402

REAL_MODELS = """gemini-2.5-flash gemini-2.5-pro gemini-2.5-flash-preview-tts
gemini-2.5-pro-preview-tts gemma-4-26b-a4b-it gemma-4-31b-it gemini-flash-latest
gemini-flash-lite-latest gemini-pro-latest gemini-2.5-flash-lite
gemini-2.5-flash-image gemini-3-flash-preview gemini-3.1-pro-preview
gemini-3.1-pro-preview-customtools gemini-3.1-flash-lite-preview
gemini-3.1-flash-lite gemini-3-pro-image-preview gemini-3-pro-image
nano-banana-pro-preview gemini-3.1-flash-image-preview gemini-3.1-flash-image
gemini-3.1-flash-lite-image gemini-3.5-flash gemini-3.5-flash-lite
gemini-omni-flash-preview gemini-omni-1.1-flash gemini-3.5-transcribe
gemini-3.6-flash gemini-3.7-flash gemini-3.8-flash lyria-3-clip-preview
lyria-3-pro-preview lyria-3.5 gemini-3.1-flash-tts-preview gemini-3.8-flash-tts
gemini-3.8-flash-lite-tts gemini-robotics-er-2-preview
gemini-2.5-computer-use-preview-10-2025 antigravity-preview-05-2026
antigravity-preview-09-2026 antigravity-preview-latest
deep-research-max-preview-04-2026 deep-research-preview-04-2026
deep-research-pro-preview-12-2025""".split()
FLASH = [n for n in REAL_MODELS if gm._FLASH.match(n)]
LITE = [n for n in REAL_MODELS if gm._LITE.match(n)]


class Pick(unittest.TestCase):
    def test_real_list_one_model_per_task(self):
        self.assertEqual(len(REAL_MODELS), 44)
        self.assertEqual(gm.pick(REAL_MODELS, "quality")[0], "gemini-3.8-flash")
        self.assertEqual(gm.pick(REAL_MODELS, "bulk")[0], "gemini-3.5-flash-lite")

    def test_versions_compare_as_numbers_not_text(self):
        self.assertEqual(gm.pick(["gemini-3.8-flash", "gemini-3.10-flash"])[0],
                         "gemini-3.10-flash")
        self.assertEqual(gm.pick(["gemini-3.5-flash-lite", "gemini-3.10-flash-lite"],
                                 "bulk")[0], "gemini-3.10-flash-lite")

    def test_other_families_are_never_chosen(self):
        for name in ("gemini-3.8-flash-tts", "gemini-3.1-flash-image",
                     "gemini-3-flash-preview", "gemini-omni-1.1-flash",
                     "gemini-3.5-transcribe", "gemma-4-31b-it", "gemini-2.5-pro",
                     "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-lite-preview"):
            for task in gm.TASKS:
                self.assertIsNone(gm.pick([name], task)[0], f"{task}: {name}")

    def test_pin_respected_and_normalised(self):
        self.assertEqual(gm.pick(REAL_MODELS, "quality", "gemini-3.6-flash")[0],
                         "gemini-3.6-flash")
        self.assertEqual(gm.pick(REAL_MODELS, "quality", "models/gemini-3.6-flash")[0],
                         "gemini-3.6-flash")

    def test_retired_pin_falls_back_to_auto(self):
        self.assertEqual(gm.pick(REAL_MODELS, "quality", "gemini-2.0-flash")[0],
                         "gemini-3.8-flash")

    def test_unusable_model_skips_only_that_model(self):
        self.assertEqual(gm.pick(REAL_MODELS, "quality", avoid={"gemini-3.8-flash"})[0],
                         "gemini-3.7-flash")

    def test_exhausted_quota_skips_the_whole_family(self):
        # Not 3.7: every flash version has its own 20/day allowance, and the
        # agreed behaviour is to drop to flash-lite, not to step through them.
        got, why = gm.pick(REAL_MODELS, "quality", exhausted={"gemini-3.8-flash"})
        self.assertEqual(got, "gemini-3.5-flash-lite")
        self.assertIn("out of quota", why)

    def test_bulk_pauses_rather_than_spending_the_flash_budget(self):
        got, why = gm.pick(REAL_MODELS, "bulk", exhausted={"gemini-3.5-flash-lite"})
        self.assertIsNone(got)
        self.assertIn("paused", why)

    def test_bulk_uses_flash_only_when_no_lite_is_usable(self):
        self.assertEqual(gm.pick(REAL_MODELS, "bulk", avoid=LITE)[0], "gemini-3.8-flash")

    def test_aliases_are_last_resort(self):
        self.assertEqual(gm.pick(["gemini-flash-latest", "gemini-2.5-pro"])[0],
                         "gemini-flash-latest")
        self.assertIsNone(gm.pick(["gemini-2.5-pro", "lyria-3.5"])[0])


def api_list(names, status=200, pages=1):
    calls = []
    chunks = [names[i::pages] for i in range(pages)]

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(dict(headers=headers, params=params))
        r = mock.Mock(status_code=status, ok=status == 200)
        page = int((params or {}).get("pageToken") or 0)
        body = {"models": [{"name": f"models/{n}",
                            "supportedGenerationMethods": ["generateContent"]}
                           for n in (chunks[page] if status == 200 else [])]}
        if page + 1 < pages:
            body["nextPageToken"] = str(page + 1)
        r.json.return_value = body
        return r
    return fake_get, calls


class State(unittest.TestCase):
    """Temp state file, frozen clock, captured notices."""

    T0 = 1_790_000_000.0

    def setUp(self):
        fd, self.cache = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.cache)
        self._orig = gm.CACHE_PATH
        gm.CACHE_PATH = self.cache
        gm._reset_for_tests()
        self.notices = []
        self.now = self.T0
        self.clock = mock.patch.object(gm.time, "time", lambda: self.now)
        self.clock.start()
        self.env = mock.patch.dict(os.environ, {"GEMINI_MODEL": "auto",
                                                "GEMINI_MODEL_BULK": "auto",
                                                "TG_TOKEN": "", "TG_CHAT": ""})
        self.env.start()

    def tearDown(self):
        self.clock.stop()
        self.env.stop()
        gm.CACHE_PATH = self._orig
        gm._reset_for_tests()
        if os.path.exists(self.cache):
            os.remove(self.cache)

    def resolve(self, task="quality", names=REAL_MODELS, **kw):
        fake, calls = api_list(names)
        with mock.patch.object(gm.requests, "get", fake):
            got = gm.resolve("KEY", task, notify=self.notices.append, **kw)
        return got, calls


class Resolve(State):
    def test_first_run_picks_and_announces_once_per_task(self):
        got, calls = self.resolve("quality")
        self.assertEqual(got, "gemini-3.8-flash")
        self.assertEqual(calls[0]["headers"], {"x-goog-api-key": "KEY"})
        got, calls2 = self.resolve("bulk")
        self.assertEqual(got, "gemini-3.5-flash-lite")
        self.assertEqual(len(calls2), 0, "the list is shared between tasks")
        self.assertEqual(len(self.notices), 2)
        self.resolve("quality")
        self.assertEqual(len(self.notices), 2, "no repeat notice")

    def test_restart_uses_state_file_not_the_api(self):
        self.resolve("quality")
        gm._reset_for_tests()
        got, calls = self.resolve("quality")
        self.assertEqual((got, len(calls), len(self.notices)), ("gemini-3.8-flash", 0, 1))

    def test_list_is_refreshed_daily(self):
        self.resolve("quality")
        gm._reset_for_tests()
        self.now += gm.RECHECK_SECONDS + 1
        _, calls = self.resolve("quality")
        self.assertEqual(len(calls), 1)

    def test_first_version_state_file_migrates(self):
        with open(self.cache, "w") as fh:
            fh.write('{"model": "gemini-2.0-flash", "pinned": "gemini-2.0-flash", '
                     '"reason": "x", "checked_at": 1}')
        got, _ = self.resolve("quality", preferred="gemini-2.0-flash")
        self.assertEqual(got, "gemini-3.8-flash")
        self.assertIn("gemini-2.0-flash", self.notices[0])
        self.assertIn("GEMINI_MODEL=auto", self.notices[0])

    def test_upgrade_from_first_version_reports_a_change_not_a_first_pick(self):
        # The case migration exists for: after upgrading, the notice should
        # say what the model changed FROM, not pretend this is a fresh start.
        with open(self.cache, "w") as fh:
            fh.write('{"model": "gemini-3.7-flash", "pinned": "auto", '
                     '"reason": "x", "checked_at": 1}')
        self.resolve("quality")
        self.assertIn("gemini-3.7-flash → gemini-3.8-flash", self.notices[0])

    def test_unusable_model_avoided_for_a_week_then_retried(self):
        self.resolve("quality")
        with mock.patch.object(gm.requests, "get", api_list(REAL_MODELS)[0]):
            new = gm.mark_unusable("KEY", "quality", "gemini-3.8-flash",
                                   notify=self.notices.append)
        self.assertEqual(new, "gemini-3.7-flash")
        self.assertIn("isn't available to this account", self.notices[-1])

        self.now += gm.UNUSABLE_SECONDS + 1        # a week later
        got, _ = self.resolve("quality")
        self.assertEqual(got, "gemini-3.8-flash")
        self.assertIn("gemini-3.7-flash → gemini-3.8-flash", self.notices[-1])

    def test_daily_quota_drops_to_lite_then_switches_back_after_reset(self):
        self.resolve("quality")
        reset = self.now + 3600
        with mock.patch.object(gm.requests, "get", api_list(REAL_MODELS)[0]):
            new = gm.mark_exhausted("KEY", "quality", "gemini-3.8-flash", reset,
                                    notify=self.notices.append)
        self.assertEqual(new, "gemini-3.5-flash-lite")
        self.assertIn("used up", self.notices[-1])
        self.assertIn("Swiss time", self.notices[-1])

        self.now = reset + 1                       # quota has reset
        got, _ = self.resolve("quality")
        self.assertEqual(got, "gemini-3.8-flash")
        self.assertIn("gemini-3.5-flash-lite → gemini-3.8-flash", self.notices[-1])

    def test_bulk_quota_pause_is_announced_with_the_real_reason(self):
        self.resolve("bulk")
        with mock.patch.object(gm.requests, "get", api_list(REAL_MODELS)[0]):
            new = gm.mark_exhausted("KEY", "bulk", "gemini-3.5-flash-lite",
                                    self.now + 3600, notify=self.notices.append)
        self.assertIsNone(new)
        self.assertIn("field extraction is paused", self.notices[-1])
        self.assertNotIn("GEMINI_API_KEY", self.notices[-1])

    def test_list_failure_keeps_last_model_and_never_prints_the_key(self):
        self.resolve("quality")
        gm._reset_for_tests()
        self.now += gm.RECHECK_SECONDS + 1

        def down(*a, **k):
            raise gm.requests.ConnectionError("failed for url ...?key=KEY")
        with mock.patch.object(gm.requests, "get", down), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            got = gm.resolve("KEY", "quality", notify=self.notices.append)
        self.assertEqual(got, "gemini-3.8-flash")
        self.assertNotIn("KEY", out.getvalue())
        self.assertIn("couldn't reach", gm.status()["problem"])

    def test_list_failure_with_no_history_means_no_model(self):
        def down(*a, **k):
            raise gm.requests.ConnectionError("x")
        with mock.patch.object(gm.requests, "get", down), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(gm.resolve("KEY", "quality", notify=self.notices.append))

    def test_rejected_key_is_reported_as_such(self):
        fake, _ = api_list([], status=403)
        with mock.patch.object(gm.requests, "get", fake), \
                contextlib.redirect_stdout(io.StringIO()):
            gm.resolve("KEY", "quality", notify=self.notices.append)
        self.assertIn("rejected the key", gm.status()["problem"])

    def test_pagination_is_followed(self):
        fake, calls = api_list(REAL_MODELS, pages=3)
        with mock.patch.object(gm.requests, "get", fake):
            got = gm.resolve("KEY", "quality", notify=self.notices.append)
        self.assertEqual((got, len(calls)), ("gemini-3.8-flash", 3))


class Classify429(unittest.TestCase):
    def r(self, details):
        resp = mock.Mock()
        resp.json.return_value = {"error": {"code": 429, "details": details}}
        return resp

    def test_quota_ids_decide(self):
        day = self.r([{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                       "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}])
        minute = self.r([{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                          "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
                         {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "23s"}])
        self.assertEqual(gm.classify_429(day)[0], "day")
        self.assertEqual(gm.classify_429(minute), ("minute", 23.0))

    def test_short_retry_alone_means_minute(self):
        self.assertEqual(gm.classify_429(self.r([{"@type": "x.RetryInfo",
                                                  "retryDelay": "40s"}]))[0], "minute")

    def test_no_detail_means_day(self):
        # the 429s the real account returned had only a Help link
        self.assertEqual(gm.classify_429(self.r([{"@type": "x.Help"}]))[0], "day")
        bad = mock.Mock(); bad.json.side_effect = ValueError
        self.assertEqual(gm.classify_429(bad)[0], "day")

    def test_reset_is_next_pacific_midnight(self):
        t = gm.next_reset()
        self.assertTrue(0 < t - time.time() <= 25 * 3600)
        if gm.ZoneInfo:
            from datetime import datetime
            pt = datetime.fromtimestamp(t, gm.ZoneInfo("America/Los_Angeles"))
            self.assertEqual((pt.hour, pt.minute), (0, 0))


class Generate(State):
    """The client path: a failed call is learned from and retried once."""

    def run_generate(self, statuses, task="quality", details=None):
        posted = []
        self.sent = []

        def fake_post(url, headers=None, json=None, timeout=None):
            self.sent.append((url, headers or {}))
            posted.append(url.split("/models/")[1].split(":")[0])
            r = mock.Mock(status_code=statuses[min(len(posted) - 1, len(statuses) - 1)])
            r.json.return_value = {"error": {"details": details or []}}
            return r
        with mock.patch.object(gm.requests, "get", api_list(REAL_MODELS)[0]), \
                mock.patch.object(gm.requests, "post", fake_post), \
                contextlib.redirect_stdout(io.StringIO()):
            r, model = gm.generate("KEY", task, {}, 10)
        return r, model, posted

    def test_key_travels_in_a_header_never_the_url(self):
        # In the URL it appears in exception text, which gets printed -- that
        # is how the key could reach journald. Both attempts are checked.
        self.run_generate([404, 200])
        self.assertEqual(len(self.sent), 2)
        for url, headers in self.sent:
            self.assertNotIn("KEY", url)
            self.assertEqual(headers.get("x-goog-api-key"), "KEY")

    def test_404_marks_unusable_and_retries_on_next_version(self):
        r, model, posted = self.run_generate([404, 200])
        self.assertEqual(posted, ["gemini-3.8-flash", "gemini-3.7-flash"])
        self.assertEqual((r.status_code, model), (200, "gemini-3.7-flash"))

    def test_daily_429_on_flash_retries_on_lite(self):
        r, model, posted = self.run_generate([429, 200])
        self.assertEqual(posted, ["gemini-3.8-flash", "gemini-3.5-flash-lite"])
        self.assertEqual(model, "gemini-3.5-flash-lite")

    def test_per_minute_429_does_not_switch(self):
        minute = [{"@type": "x.RetryInfo", "retryDelay": "20s"}]
        r, model, posted = self.run_generate([429], details=minute)
        self.assertEqual((posted, r.status_code), (["gemini-3.8-flash"], 429))
        self.resolve("quality")
        self.assertEqual(gm.resolve("KEY", "quality", notify=None), "gemini-3.8-flash")

    def test_never_more_than_two_attempts(self):
        r, model, posted = self.run_generate([404, 404, 404])
        self.assertEqual(len(posted), 2)

    def test_client_has_no_hardcoded_model_or_key_in_url(self):
        with open(aimod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("gemini-2.0-flash\"", src)
        self.assertNotIn('params={"key"', src)
        self.assertNotIn("google_search", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
