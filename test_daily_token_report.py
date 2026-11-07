"""Offline tests: every network call is stubbed. Run: python3 -m unittest -v"""
import csv
import os
import tempfile
import unittest
from unittest import mock

import daily_token_report as d

A = "0x" + "a" * 40
B = "0x" + "b" * 40
C = "0x" + "c" * 40


def verdict(v="ALLOW", grade="A", risk=10, flags=()):
    return {"checked": True, "verdict": v, "grade": grade, "risk": risk, "flags": list(flags), "why": ""}


def no_network(*a, **k):
    raise AssertionError("test tried a real network call")


class Base(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(d.urllib.request, "urlopen", side_effect=no_network),
                  mock.patch.object(d, "GAP_SECONDS", 0), mock.patch.object(d.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp()
        self.hist = os.path.join(self.tmp, "history.csv")
        self.rep = os.path.join(self.tmp, "report.md")

    def write(self, name, text):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return p


class Watchlist(Base):
    def test_parse(self):
        p = self.write("w.txt", "# my coins\n%s aero\nbsc %s cake  # comment\n\nnot-an-address\n%s\n%s again\n"
                       % (A.upper().replace("0X", "0x"), B, C, A))
        got = d.read_watchlist(p)
        self.assertEqual(got, [("base", A, "aero"), ("bsc", B, "cake"), ("base", C, "")])

    def test_short_address_skipped(self):
        p = self.write("w.txt", "0x1234\nbase\n")
        self.assertEqual(d.read_watchlist(p), [])


class Changes(unittest.TestCase):
    def row(self, **kw):
        return d.to_row("2026-11-07", "base", A, "", verdict(**kw))

    def test_first(self):
        self.assertEqual(d.changes(None, self.row()), ["first check"])

    def test_same(self):
        self.assertEqual(d.changes(self.row(), self.row(risk=15)), [])

    def test_worse(self):
        c = d.changes(self.row(), self.row(v="BLOCK", grade="F", risk=99, flags=["HONEYPOT_CONFIRMED"]))
        self.assertIn("verdict ALLOW -> BLOCK (worse)", c)
        self.assertIn("grade A -> F", c)
        self.assertIn("risk 10 -> 99", c)
        self.assertIn("new flags: HONEYPOT_CONFIRMED", c)

    def test_better_and_cleared(self):
        c = d.changes(self.row(v="LIMIT", grade="C", flags=["OWNER_CAN_MINT"]), self.row())
        self.assertIn("verdict LIMIT -> ALLOW", c)
        self.assertIn("flags cleared: OWNER_CAN_MINT", c)

    def test_unchecked_is_reported_not_silent(self):
        r = d.to_row("2026-11-07", "base", A, "", {"checked": False, "why": "oracle unavailable (http 0)"})
        self.assertEqual(d.changes(self.row(), r), ["NOT CHECKED today (oracle unavailable (http 0))"])

    def test_missing_risk(self):
        self.assertEqual(d.changes(self.row(risk=None), self.row()), [])


class Run(Base):
    def test_two_days(self):
        watch = [("base", A, "aero"), ("bsc", B, "")]
        day1 = {A: verdict(), B: verdict("LIMIT", "C", 50)}
        day2 = {A: verdict("BLOCK", "F", 99, ["HONEYPOT_CONFIRMED"]), B: verdict("LIMIT", "C", 52)}
        res, n = d.run(watch, self.hist, self.rep, "x", date="2026-11-06", check=lambda o, t, key, chain: day1[t])
        self.assertEqual(n, 0)
        self.assertIn("No changes", open(self.rep).read())
        res, n = d.run(watch, self.hist, self.rep, "x", date="2026-11-07", check=lambda o, t, key, chain: day2[t])
        self.assertEqual(n, 1)
        rep = open(self.rep).read()
        self.assertIn("Changed since last check (1)", rep)
        self.assertIn("Base aero 0xaaaa...aaaa", rep)
        self.assertIn("verdict ALLOW -> BLOCK (worse)", rep)
        rows = list(csv.DictReader(open(self.hist)))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2]["flags"], "HONEYPOT_CONFIRMED")

    def test_unchecked_day_compares_against_last_checked(self):
        watch = [("base", A, "")]
        d.run(watch, self.hist, self.rep, "x", date="2026-11-05", check=lambda *a, **k: verdict())
        d.run(watch, self.hist, self.rep, "x", date="2026-11-06",
              check=lambda *a, **k: {"checked": False, "why": "oracle unavailable (http 503)"})
        self.assertIn("NOT CHECKED", open(self.rep).read())
        _, n = d.run(watch, self.hist, self.rep, "x", date="2026-11-07", check=lambda *a, **k: verdict())
        self.assertEqual(n, 0)


class Oracle(unittest.TestCase):
    def test_ok(self):
        body = {"verdict": "BLOCK", "grade": "F", "risk": {"overall": 99},
                "recommendation": {"reason_codes": ["HONEYPOT_CONFIRMED"], "why": "sells revert"}}
        with mock.patch.object(d, "http_json", return_value=(200, body)) as m:
            v = d.check_token("https://o", A, chain="bsc")
        self.assertEqual(v["verdict"], "BLOCK")
        self.assertEqual(m.call_args[1]["headers"]["X-Free-Trial"], "1")
        self.assertIn("chain=bsc", m.call_args[0][0])

    def test_key_header(self):
        with mock.patch.object(d, "http_json", return_value=(402, None)) as m:
            v = d.check_token("https://o", A, key="k1")
        self.assertEqual(m.call_args[1]["headers"]["X-Subscription-Key"], "k1")
        self.assertFalse(v["checked"])
        self.assertIn("prepaid key", v["why"])

    def test_free_used_up(self):
        with mock.patch.object(d, "http_json", return_value=(402, None)):
            self.assertIn("CWX_KEY", d.check_token("https://o", A)["why"])

    def test_429_waits_retry_after_then_succeeds(self):
        import io
        err = d.urllib.error.HTTPError("u", 429, "rate", {"Retry-After": "12"}, io.BytesIO(b"{}"))
        ok = mock.MagicMock()
        ok.__enter__.return_value.status = 200
        ok.__enter__.return_value.read.return_value = b'{"verdict": "ALLOW"}'
        with mock.patch.object(d.urllib.request, "urlopen", side_effect=[err, ok]), \
                mock.patch.object(d.time, "sleep") as sl:
            self.assertTrue(d.check_token("https://o", A)["checked"])
        sl.assert_called_once_with(12)

    def test_down(self):
        with mock.patch.object(d, "http_json", return_value=(0, None)):
            self.assertEqual(d.check_token("https://o", A)["why"], "oracle unavailable (http 0)")


class Main(Base):
    def setUp(self):
        super().setUp()
        self.w = self.write("w.txt", "%s\n" % A)
        self.args = [self.w, "--history", self.hist, "--report", self.rep]

    def test_once_a_day_guard(self):
        with mock.patch.object(d, "check_token", return_value=verdict()) as m:
            self.assertEqual(d.main(self.args), 0)
            self.assertEqual(d.main(self.args), 0)
            self.assertEqual(m.call_count, 1)
            d.main(self.args + ["--force"])
            self.assertEqual(m.call_count, 2)

    def test_exit_2_when_unchecked(self):
        with mock.patch.object(d, "check_token", return_value={"checked": False, "why": "x"}):
            self.assertEqual(d.main(self.args), 2)

    def test_telegram_needs_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(d.main(self.args + ["--telegram"]), 1)

    def test_telegram_only_on_change(self):
        env = {"TG_BOT_TOKEN": "t", "TG_CHAT": "1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(d, "send_telegram", return_value=True) as s:
            with mock.patch.object(d, "check_token", return_value=verdict()):
                d.main(self.args + ["--telegram"])
            self.assertEqual(s.call_count, 0)
            with mock.patch.object(d, "check_token", return_value=verdict("BLOCK", "F", 99)):
                d.main(self.args + ["--telegram", "--force"])
            self.assertEqual(s.call_count, 1)
            self.assertIn("1 of 1 changed", s.call_args[0][2])

    def test_telegram_failure_exit_3(self):
        env = {"TG_BOT_TOKEN": "t", "TG_CHAT": "1"}
        with mock.patch.dict(os.environ, env), mock.patch.object(d, "http_json", return_value=(401, None)), \
                mock.patch.object(d, "check_token", return_value=verdict()):
            self.assertEqual(d.main(self.args + ["--telegram", "--always-send"]), 3)

    def test_cap(self):
        lines = "".join("0x%040x\n" % i for i in range(1, 40))
        w = self.write("big.txt", lines)
        with mock.patch.object(d, "check_token", return_value=verdict()) as m:
            d.main([w, "--history", self.hist, "--report", self.rep])
        self.assertEqual(m.call_count, d.MAX_TOKENS)

    def test_empty_watchlist(self):
        self.assertEqual(d.main([self.write("e.txt", "# nothing\n"), "--history", self.hist]), 1)


if __name__ == "__main__":
    unittest.main()
