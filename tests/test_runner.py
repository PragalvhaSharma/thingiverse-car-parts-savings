"""The whole pipeline on fakes: every kind of row, a fatal stop, and a resumed run.

Thingiverse, eBay, DuckDuckGo and the three model calls are replaced by fakes,
so this runs offline; the stages, the cache, the pool bookkeeping and step 5
are the real code.
"""

import contextlib
import io
import tempfile
import threading
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts the repo on sys.path)

from carsavings import run
from carsavings.config import Settings
from carsavings.ebay import EbayBlocked
from carsavings.llm import LLMError
from carsavings.savings import LLM_ERROR_PREFIX, OG_ERROR, UNMATCHED_JR
from carsavings.schema import read_rows, write_rows

WIRE_CAP = "Toyota Prius headlight wire opening cap"


def harvest(tid, **kw):
    h = {"id": tid, "ok": True, "err": "", "via": "api", "t": "Thing %s" % tid, "by": "maker",
         "desc": "a printed part", "tags": [], "ti": "https://cdn.thingiverse.com/site/img/default/x.webp",
         "images": [], "files": [], "files_via": "", "infill": "20%", "dl": None, "zip_tried": True,
         "at": "2026-10-10"}
    h.update(kw)
    return h


def listing(p, n, title=None):
    return {"p": p, "s": 1.5, "t": title or "listing %d" % n, "u": "https://www.ebay.com/itm/%09d" % n,
            "im": "https://i.ebayimg.com/images/g/IMG%d/s-l500.webp" % n}


HARVESTS = {
    "101": harvest("101"),
    "102": harvest("102"),
    "103": {"id": "103", "ok": False, "err": OG_ERROR, "via": "page", "dl": None},
    "104": harvest("104"),
    "105": harvest("105"),
    "106": harvest("106"),
    "107": harvest("107"),
    "108": harvest("108"),
    "109": harvest("109"),
    "110": harvest("110"),
}

CLASSES = {
    "101": ("spare_part", False, "exact_part", WIRE_CAP),
    "102": ("toy_decor_other", False, "no_equivalent", ""),
    "104": ("spare_part", True, "whole_device_part_b", "Saab 900 headlight assembly"),
    "105": ("accessory", False, "commercial_equivalent", "Kia EV6 door handle pulls"),
    "106": ("spare_part", False, "exact_part", "obscure clip only sold online"),
    "107": ("spare_part", False, "exact_part", "a clip nobody sells"),
    "108": ("accessory", False, "no_equivalent", ""),
    "110": ("spare_part", False, "exact_part", WIRE_CAP),
}

EBAY_RESULTS = {
    WIRE_CAP: [listing(25.0, 1, "Headlight cap 2 pcs"), listing(19.99, 2, "Prius headlight wire cap")],
    "Saab 900 headlight assembly": [listing(89.0, 3), listing(120.0, 4)],
    "Kia EV6 door handle pulls": [listing(335.05, 5, "Door handle pull pair")],
}

WEB_RESULTS = {
    "obscure clip only sold online": [{"i": 1, "p": 33.0, "s": 0.0, "t": "Clip at a parts shop",
                                       "u": "https://parts.example.com/clip", "im": "", "snip": "In stock"}],
    "a clip nobody sells": None,      # DuckDuckGo unreachable
}

VERDICTS = {
    "101": {"choice": 1, "accepted": [1, 2], "mr": "1 is the same cap", "judge": {"ok": True, "reason": "$19.99 is plausible."}},
    "110": {"choice": 1, "accepted": [1], "mr": "1 is the same cap", "judge": {"ok": True, "reason": "$19.99 is plausible."}},
    "104": {"choice": 0, "accepted": [], "mr": "none shows the e-code headlight", "judge": None},
    "105": {"choice": 1, "accepted": [1], "mr": "1 is the pulls", "judge": {"ok": False, "reason": "$335.05 is implausibly high."}},
    "106": {"choice": 1, "accepted": [1], "mr": "the shop's clip", "judge": {"ok": True, "reason": "$33.00 is plausible."}},
}


class Calls:
    def __init__(self):
        self.lock = threading.Lock()
        self.log = []

    def add(self, *what):
        with self.lock:
            self.log.append(what)

    def of(self, kind):
        return sorted(x[1] for x in self.log if x[0] == kind)


class FakeThingiverse:
    def __init__(self, calls):
        self.calls = calls

    def harvest(self, tid, need_downloads=True, zip_fallback=True):
        self.calls.add("harvest", tid)
        h = dict(HARVESTS[tid])
        if h.get("ok") and need_downloads and h.get("dl") is None:
            h["dl"] = self._download_count(tid)
        return h

    def _download_count(self, tid):
        return 777

    def session(self):
        return None


class FakeEbay:
    def __init__(self, calls, blocked=False):
        self.calls, self.blocked = calls, blocked

    def search(self, q):
        self.calls.add("ebay", q)
        if self.blocked:
            raise EbayBlocked("eBay challenged the session")
        return [dict(x) for x in EBAY_RESULTS.get(q, [])]


class FakeWeb:
    def __init__(self, calls):
        self.calls = calls

    def search(self, q):
        self.calls.add("web", q)
        pool = WEB_RESULTS.get(q, [])
        return None if pool is None else [dict(x) for x in pool]


def fake_classify(calls):
    def classify(llm, h, seed):
        calls.add("classify", h["id"])
        if h["id"] == "109":
            raise LLMError(LLM_ERROR_PREFIX + "Error code: 400 - invalid_image_format")
        cls, asm, tier, q = CLASSES[h["id"]]
        return {"cls": cls, "asm": asm, "part": "" if cls == "toy_decor_other" else "the part",
                "of": "" if cls == "toy_decor_other" else "the car", "tier": "" if cls == "toy_decor_other" else tier,
                "q": q, "files": [], "reason": "because", "photos": 0, "model": "fake"}
    return classify


def fake_verify(calls):
    def verify(llm, h, seed, c, pool):
        calls.add("verify", h["id"])
        return dict(VERDICTS[h["id"]])
    return verify


def seeds():
    out = []
    for tid in sorted(HARVESTS):
        out.append({"id": tid, "url": "https://www.thingiverse.com/thing:%s" % tid,
                    "dl": None if tid == "110" else int(tid),
                    "br": "Toyota", "mdl": "Prius", "gen": "", "yrs": "", "title": ""})
    return out


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(cache_dir=self.tmp.name, openai_api_key="test-key", http_delay=0)

    def tearDown(self):
        self.tmp.cleanup()

    def run_pipeline(self, ebay_blocked=False):
        calls = Calls()
        runner = run.Runner(self.settings, skip_slice=True, run_date="2026-10-10")
        runner.tv, runner.ebay, runner.web = FakeThingiverse(calls), FakeEbay(calls, ebay_blocked), FakeWeb(calls)
        with mock.patch.object(run, "classify", fake_classify(calls)), \
                mock.patch.object(run, "verify", fake_verify(calls)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rows = runner.run(seeds(), workers=3)
        return {r["id"]: r for r in rows}, calls, rows

    def test_every_kind_of_row(self):
        rows, calls, ordered = self.run_pipeline()
        self.assertEqual([r["id"] for r in ordered], sorted(HARVESTS))

        r = rows["101"]                                   # matched on eBay, counted
        self.assertEqual((r["src"], r["p"], r["surl"], r["lt"]),
                         ("eBay", 19.99, "https://www.ebay.com/itm/000000002", "Prius headlight wire cap"))
        self.assertEqual((r["bkt"], r["ms"], r["pc"], r["spp"], r["tot"]), ("repair", "default", 0.42, 19.57, 1976.57))
        self.assertEqual((r["now"], r["at"], [c["p"] for c in r["cands"]]), (1, "2026-10-10", [19.99, 25.0]))
        self.assertEqual((r["li"], r["mr"], r["jr"], r["qty"]),
                         ("https://i.ebayimg.com/images/g/IMG2/s-l500.webp", "1 is the same cap", "$19.99 is plausible.", 0))
        self.assertEqual((r["t"], r["by"], r["dl_src"]), ("Thing 101", "maker", "sheet"))
        self.assertNotIn("judge_ok", read_back(ordered, self.tmp.name)[0])

        r = rows["102"]                                   # toy: classified, never searched
        self.assertEqual((r["cls"], r["ms"], r["pc"], r["src"], r["tier"], r["q"], r["bkt"]),
                         ("toy_decor_other", "skipped", 0.42, "", "", "", ""))
        self.assertEqual(run.outcome(r), "toy")

        r = rows["103"]                                   # Thingiverse served its default tags
        self.assertEqual((r["err"], r["cls"], r["t"], run.outcome(r)), (OG_ERROR, "", "", "deleted"))

        r = rows["104"]                                   # searched, nothing accepted: pool kept
        self.assertEqual((r["src"], r["jr"], r["mr"], r["now"], len(r["cands"]), r["bkt"]),
                         ("none", UNMATCHED_JR, "none shows the e-code headlight", 0, 2, ""))
        self.assertEqual(run.outcome(r), "unmatched")

        r = rows["105"]                                   # accepted, refused by the judge
        self.assertEqual((r["src"], r["p"], r["jr"], r["bkt"], r["now"], r["qty"]),
                         ("eBay", 335.05, "$335.05 is implausibly high.", "", 1, 2))
        self.assertEqual(run.outcome(r), "refused")

        r = rows["106"]                                   # eBay empty, DuckDuckGo priced it
        self.assertEqual((r["src"], r["p"], r["surl"], r["li"], r["cands"], r["now"], r["bkt"], r["tot"]),
                         ("DuckDuckGo web", 33.0, "https://parts.example.com/clip", "", [], "", "repair", 3453.48))

        r = rows["107"]                                   # eBay empty, DuckDuckGo unreachable
        self.assertEqual((r["src"], r["jr"], r["cands"], r["bkt"]), ("none", UNMATCHED_JR, [], ""))

        r = rows["108"]                                   # no query: nothing to search
        self.assertEqual((r["src"], r["jr"], r["bkt"]), ("none", UNMATCHED_JR, ""))

        r = rows["109"]                                   # the model call failed
        self.assertTrue(r["err"].startswith(LLM_ERROR_PREFIX))
        self.assertEqual((r["pc"], r["ms"], run.outcome(r)), (0.42, "", "llm_error"))

        r = rows["110"]                                   # same query as 101: one eBay search
        self.assertEqual((r["dl"], r["dl_src"], r["bkt"], r["tot"]), (777, "thingiverse", "repair", 15205.89))
        self.assertEqual(calls.of("ebay").count(WIRE_CAP), 1)
        self.assertEqual(calls.of("web"), ["a clip nobody sells", "obscure clip only sold online"])
        self.assertEqual(calls.of("verify"), ["101", "104", "105", "106", "110"])

    def test_second_run_comes_from_the_cache(self):
        first, _, _ = self.run_pipeline()
        second, calls, _ = self.run_pipeline()
        self.assertEqual(calls.of("harvest"), ["103"])              # failed harvests are retried
        self.assertEqual(calls.of("classify"), ["109"])             # so are failed model calls
        self.assertEqual(calls.of("ebay"), ["a clip nobody sells"])  # and an unreachable DuckDuckGo
        self.assertEqual(calls.of("web"), ["a clip nobody sells"])
        self.assertEqual(calls.of("verify"), [])
        for tid in first:
            self.assertEqual(first[tid], second[tid], tid)

    def test_refresh_redoes_a_stage_and_the_ones_after_it(self):
        self.run_pipeline()
        self.assertEqual(run.refresh_set("search"), frozenset({"search", "verify", "slice"}))
        calls = Calls()
        runner = run.Runner(self.settings, refresh=run.refresh_set("verify"), skip_slice=True)
        runner.tv, runner.ebay, runner.web = FakeThingiverse(calls), FakeEbay(calls), FakeWeb(calls)
        with mock.patch.object(run, "classify", fake_classify(calls)), \
                mock.patch.object(run, "verify", fake_verify(calls)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runner.run(seeds(), workers=2)
        self.assertEqual(calls.of("verify"), ["101", "104", "105", "106", "110"])
        self.assertEqual(calls.of("classify"), ["109"])

    def test_a_blocked_session_stops_the_run(self):
        with self.assertRaises(EbayBlocked):
            self.run_pipeline(ebay_blocked=True)

    def test_summary_of_a_run(self):
        _, _, ordered = self.run_pipeline()
        text = run.summary(ordered)
        self.assertIn("10 Things, 3 counted.", text)
        self.assertIn("no verified listing 3 | judge refused 1 | toys/decor 1", text)
        self.assertIn("deleted Things 1 | LLM errors 1 | other errors 0", text)


def read_back(rows, tmp):
    path = "%s/out.csv" % tmp
    write_rows(path, rows)
    return read_rows(path)


if __name__ == "__main__":
    unittest.main()
