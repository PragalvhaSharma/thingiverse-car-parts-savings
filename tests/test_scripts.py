"""The scripts around the pipeline: review decisions folded back into the
table (apply_review), and the helpers capture_candidates, harvest_things and
doctor are built on. Sep 2 rows are the fixtures."""

import contextlib
import csv
import hashlib
import io
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import helpers

from carsavings.config import Settings
from carsavings.inputs import read_seeds
from carsavings.savings import contrib, per_print, totals
from carsavings.schema import read_rows, write_rows

apply_review = helpers.load_script("apply_review")

# counted repair, assembly and upgrade rows; unmatched with a pool of 10; refused
# by the judge; a toy; the scale guard; unmatched without a pool; oversize; a toy
SAMPLE = ("3013735", "5998784", "7186295", "5019865", "6234665", "5770631", "2803111",
          "1780063", "6702563", "4142698")

POOL_PICK = {"v": "accept", "u": "https://www.ebay.com/itm/226380802646", "p": 8.99, "s": 10.15,
             "t": "2x Car Sun Visor Clip Grey Holder Bracket Clips LC6269261B77 For Mazda 3 5 6 CX9 (For: Mazda)",
             "im": "https://i.ebayimg.com/images/g/abc/s-l500.webp", "src": "pool", "ts": "2026-09-03T14:02:11Z"}

DECISIONS = {
    "3013735": "veto",
    "5998784": {"v": "keep"},
    "7186295": {"v": "accept", "u": "https://www.ebay.com/itm/396980251599", "p": "12.50", "s": 0,
                "t": "AC knob cover set of 2", "im": "", "src": "manual", "ts": "2026-09-03T14:01:00Z"},
    "5019865": POOL_PICK,
    "6234665": "veto",
    "5770631": dict(POOL_PICK, u="https://www.ebay.com/itm/1"),
    "2803111": {"v": "accept", "u": "https://www.amazon.com/dp/B000TEST", "p": 40.25, "src": "manual",
                "ts": "2026-09-03T14:03:00Z"},
    "1780063": {"v": "accept", "u": "https://www.ebay.com/itm/2", "p": "0"},
    "6702563": {"v": "accept", "p": 12.0},
    "4142698": "keep",
}


def sample_rows():
    by_id = {r["id"]: r for r in helpers.sep2_rows()}
    return [by_id[t] for t in SAMPLE]


def quiet(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args)
    return result, out.getvalue(), err.getvalue()


class ApplyDecisions(unittest.TestCase):
    def setUp(self):
        self.rows = sample_rows()
        self.before = {r["id"]: dict(r) for r in self.rows}

    def apply(self, decisions=DECISIONS):
        counts, _, err = quiet(apply_review.apply_decisions, self.rows, decisions)
        return {r["id"]: r for r in self.rows}, counts, err

    def test_every_kind_of_decision(self):
        rows, counts, err = self.apply()
        self.assertEqual(counts, {
            "counted: vetoed, dollars removed": 1,
            "counted: confirmed": 1,
            "counted: swapped to the picked listing": 1,
            "uncounted: rescued into repair": 1,
            "uncounted: rescued into assembly": 1,
            "uncounted: vetoed": 1,
            "uncounted: picked listing ignored (no bucket for the class)": 1,
            "uncounted: keep without a picked listing (no change)": 1,
            "ignored: not a valid decision": 2,
        })
        self.assertIn("Thing 5770631", err)

        r = rows["3013735"]                       # counted, vetoed: dollars removed, listing kept
        self.assertEqual((r["bkt"], r["spp"], r["tot"], r["human_review"]), ("", 0.0, 0.0, "vetoed"))
        self.assertEqual((r["p"], r["surl"]), (19.99, "https://www.ebay.com/itm/287546444893"))

        r = rows["5998784"]                       # counted, confirmed
        self.assertEqual((r["bkt"], r["tot"], r["human_review"]), ("assembly", 1950.2, "confirmed"))

        r = rows["7186295"]                       # counted, swapped: re-priced, same bucket
        self.assertEqual((r["bkt"], r["p"], r["spp"], r["tot"]), ("upgrade", 12.5, 12.44, 286.12))
        self.assertEqual((r["src"], r["surl"], r["lt"], r["qty"]),
                         ("eBay", "https://www.ebay.com/itm/396980251599", "AC knob cover set of 2", 2))
        self.assertEqual((r["human_review"], r["human_review_source"], r["human_review_at"], r["human_review_ship"]),
                         ("swapped", "manual", "2026-09-03T14:01:00Z", 0))

        r = rows["5019865"]                       # unmatched, rescued from its pool
        self.assertEqual((r["bkt"], r["p"], r["spp"], r["tot"], r["now"]), ("repair", 8.99, 8.77, 5209.38, 2))
        self.assertEqual((r["src"], r["qty"], r["li"], r["human_review"], r["human_review_source"],
                          r["human_review_ship"]), ("eBay", 2, POOL_PICK["im"], "accepted", "pool", 10.15))

        r = rows["6234665"]                       # refused by the judge, vetoed: still $0
        self.assertEqual((r["bkt"], r["tot"], r["p"], r["human_review"]), ("", 0.0, 13.49, "vetoed"))

        reset = {k: "" for k in apply_review.REVIEW_COLUMNS}
        self.assertEqual(rows["5770631"], dict(self.before["5770631"], **reset))   # a toy has no bucket

        r = rows["2803111"]                       # the scale guard overruled by hand
        self.assertEqual((r["bkt"], r["err"], r["sx"], r["sxu"], r["src"], r["human_review_ship"]),
                         ("assembly", "", False, 0.0, "manual", ""))
        self.assertEqual((r["spp"], r["tot"]), (per_print(40.25, 0.005), contrib(40.25, 0.005, 389)))

        for tid in ("1780063", "6702563", "4142698"):   # no price, no URL, keep on an uncounted row
            self.assertEqual(rows[tid], self.before[tid], tid)

    def test_bucket_totals_follow_the_decisions(self):
        self.assertEqual(totals(self.rows), {"repair": (1, 6044.04), "assembly": (1, 1950.2), "upgrade": (1, 391.69)})
        self.apply()
        scale = contrib(40.25, 0.005, 389)
        self.assertEqual(totals(self.rows), {"repair": (1, 5209.38), "assembly": (2, round(1950.2 + scale, 2)),
                                             "upgrade": (1, 286.12)})

    def test_a_decision_can_name_the_bucket(self):
        rows, counts, _ = self.apply({"5019865": dict(POOL_PICK, b="upgrade")})
        self.assertEqual((rows["5019865"]["bkt"], counts), ("upgrade", {"uncounted: rescued into upgrade": 1}))

    def test_a_pick_without_a_photo_keeps_the_photo_on_file(self):
        own, cand = self.before["7186295"], self.before["5019865"]["cands"][0]
        rows, _, _ = self.apply({
            "7186295": {"v": "accept", "u": own["surl"], "p": 5, "im": "", "src": "manual"},
            "5019865": {"v": "accept", "u": cand["u"], "p": 5, "src": "manual"},
            "3013735": {"v": "accept", "u": "https://www.ebay.com/itm/3", "p": 5, "im": "", "src": "manual"},
        })
        self.assertTrue(own["li"] and cand["im"])
        self.assertEqual([rows[t]["li"] for t in ("7186295", "5019865", "3013735")], [own["li"], cand["im"], ""])

    def test_norm_decision(self):
        n = apply_review.norm_decision
        self.assertEqual([n("keep"), n("veto"), n({"v": "veto"}), n("maybe"), n(None), n(3)],
                         ["keep", "veto", "veto", None, None, None])
        self.assertEqual(n({"v": "accept", "u": "https://x.test/a", "p": "7.5"})["p"], 7.5)
        self.assertIsNone(n({"v": "accept", "u": "https://x.test/a", "p": "seven"}))
        self.assertIsNone(n({"v": "accept", "u": "", "p": 5}))
        self.assertIsNone(n({"v": "accept", "u": "https://x.test/a", "p": -1}))


class ReadDecisions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return apply_review.read_decisions(path)

    def test_every_shape_the_pages_produce(self):
        export = {"exported_at": "2026-09-03T15:00:00Z", "rows": 412, "stats": {"keep": 1, "veto": 1},
                  "decisions": {"3013735": "keep", " 5998784 ": "veto", "note": "not an id"}}
        self.assertEqual(self.read("export.json", json.dumps(export)), {"3013735": "keep", "5998784": "veto"})
        self.assertEqual(self.read("state.json", json.dumps({"ok": True, "decisions": {"7186295": "veto"}})),
                         {"7186295": "veto"})
        self.assertEqual(self.read("bare.json", json.dumps({"5019865": POOL_PICK})), {"5019865": POOL_PICK})
        lines = "%s\n\n%s\n" % (json.dumps({"3013735": "keep"}),
                                json.dumps({"decisions": {"3013735": "veto", "6234665": "veto"}}))
        self.assertEqual(self.read("decisions.jsonl", lines), {"3013735": "veto", "6234665": "veto"})
        with self.assertRaises(SystemExit):
            self.read("broken.jsonl", '{"3013735": "keep"}\nnot json\n')


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.src = self.dir / "cars_final.csv"
        write_rows(self.src, sample_rows())

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_a_reviewed_copy_with_the_review_columns(self):
        digest = hashlib.sha256(self.src.read_bytes()).hexdigest()
        dec = self.dir / "review-decisions-2026-09-03.json"
        dec.write_text(json.dumps({"exported_at": "2026-09-03", "decisions": DECISIONS}), encoding="utf-8")
        code, out, _ = quiet(apply_review.main, ["--csv", str(self.src), "--decisions", str(dec)])
        reviewed = self.dir / "cars_final_reviewed.csv"
        self.assertEqual(code, 0)
        self.assertIn("wrote %s" % reviewed, out)
        self.assertEqual(hashlib.sha256(self.src.read_bytes()).hexdigest(), digest)   # input untouched
        with open(reviewed, newline="", encoding="utf-8") as f:
            self.assertEqual(next(csv.reader(f))[-4:], list(apply_review.REVIEW_COLUMNS))
        rows = {r["id"]: r for r in read_rows(reviewed)}
        self.assertEqual((rows["5019865"]["bkt"], rows["5019865"]["tot"], rows["5019865"]["human_review"],
                          rows["5019865"]["human_review_ship"]), ("repair", 5209.38, "accepted", "10.15"))
        self.assertEqual((rows["3013735"]["bkt"], rows["1780063"]["human_review"]), ("", ""))
        self.assertEqual(apply_review.default_out("out/cars_final.csv.gz"), "out/cars_final_reviewed.csv.gz")

    def test_url_applies_each_brand_only_to_its_own_rows(self):
        stored = {"ford": {"3013735": "veto", "5998784": "veto"}}      # 5998784 is a Saab
        asked = []

        def urlopen(req, timeout=None):
            key = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["key"][0]
            asked.append(key)
            return io.BytesIO(json.dumps({"ok": True, "decisions": stored.get(key, {})}).encode("utf-8"))

        out_path = self.dir / "reviewed.csv"
        with mock.patch.object(apply_review.urllib.request, "urlopen", urlopen):
            code, _out, err = quiet(apply_review.main, ["--csv", str(self.src), "--out", str(out_path),
                                                        "--url", "https://savings-review.vercel.app/"])
        self.assertEqual(code, 0)
        self.assertEqual(len(asked), len({r["br"] for r in sample_rows()}))
        self.assertIn("1 stored decisions for ford belong to no Ford row", err)
        rows = {r["id"]: r for r in read_rows(out_path)}
        self.assertEqual((rows["3013735"]["human_review"], rows["5998784"]["human_review"]), ("vetoed", ""))

    def test_url_without_a_blob_store_says_so(self):
        def urlopen(req, timeout=None):
            return io.BytesIO(b'{"ok": false, "reason": "no-blob"}')

        with mock.patch.object(apply_review.urllib.request, "urlopen", urlopen):
            code, out, err = quiet(apply_review.main, ["--csv", str(self.src), "--out", str(self.dir / "r.csv"),
                                                       "--url", "https://savings-review.vercel.app"])
        self.assertEqual(code, 0)
        self.assertIn("has no Blob store connected", err)
        self.assertIn("no decision matched a row", out)


class OtherScripts(unittest.TestCase):
    def test_capture_candidates_wants_uncounted_priceable_rows_without_a_pool(self):
        cc = helpers.load_script("capture_candidates")
        self.assertEqual({r["id"] for r in sample_rows() if cc.wants_pool(r)}, {"6234665", "2803111", "1780063"})

    def test_harvest_things_reads_the_og_error_rows_from_a_gzipped_table(self):
        ht = helpers.load_script("harvest_things")
        wanted = ht.error_ids(helpers.SEP2)
        seeds = [s for s in read_seeds(helpers.SEP2) if s["id"] in wanted]
        self.assertEqual((len(wanted), len(seeds)), (40, 40))
        self.assertIn("128092", wanted)

    def test_doctor_never_prints_a_secret(self):
        doctor = helpers.load_script("doctor")
        s = Settings(openai_api_key="sk-proj-AbCdEf123456",
                     thingiverse_cookie="PHPSESSID=q1w2e3r4t5y6; _li_dcdm_c=.thingiverse.com; cf_clearance=Zx9Yw8Vu7Ts6",
                     ebay_cookie="s=CgAD4ACBk; nonsession=BAQAAAZ1x2y3", orca_api_token="orca-tok-998877")
        said = doctor.Report(s).clean(
            "401 for sk-proj-AbCdEf123456 and sk-other-ZZZZZZZZ; PHPSESSID=q1w2e3r4t5y6 was refused, "
            "cf_clearance=Zx9Yw8Vu7Ts6 expired; eBay sent s=CgAD4ACBk; nonsession=BAQAAAZ1x2y3; "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig; token orca-tok-998877")
        for secret in ("AbCdEf123456", "ZZZZZZZZ", "q1w2e3r4t5y6", "Zx9Yw8Vu7Ts6", "CgAD4ACBk", "BAQAAAZ1x2y3",
                       "eyJhbGciOiJIUzI1NiJ9", "orca-tok-998877"):
            self.assertNotIn(secret, said)
        self.assertIn("PHPSESSID=[redacted]", said)
        # a cookie whose value is the site's own domain must not blank the site out of URLs
        url = "https://www.thingiverse.com/thing:3013735"
        self.assertIn(url, doctor.Report(s).clean("Cloudflare challenged %s." % url))


if __name__ == "__main__":
    unittest.main()
