"""The Sep 2 table, re-derived: every stored figure follows from the code's rules.

data/sep2/cars9k_final.csv.gz holds the 9,190 rows embedded in the deployed
review pages; data/sep2/deployed_sha256.json holds the hashes of those pages.
"""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import DEPLOYED, EXPECTED, ROMAIN, sep2_rows

from carsavings import savings, site, thingiverse
from carsavings.inputs import read_seeds
from carsavings.run import outcome, summary
from carsavings.verify import listing_units


def reprice_misses(page, rows):
    """Rows [p, pc, dl, tot] that the page's own contrib() prices off tot, run in node."""
    start = page.index("  function contrib(r, price) {")
    begin = page.find("  function round2(x) {")
    script = page[begin if 0 <= begin < start else start:page.index("\n  }\n", start) + 5] + (
        "var rows = JSON.parse(require('fs').readFileSync(0, 'utf8')), n = 0;\n"
        "rows.forEach(function (x) { if (contrib({pc: x[1], dl: x[2]}, x[0]) !== x[3]) n++; });\n"
        "console.log(n);\n")
    out = subprocess.run(["node", "-e", script], input=json.dumps(rows), capture_output=True,
                         text=True, check=True)
    return int(out.stdout)


class Sep2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = sep2_rows()
        cls.by_id = {r["id"]: r for r in cls.rows}

    def test_site_rebuild_is_byte_identical_to_the_deployment(self):
        want = json.loads(DEPLOYED.read_text(encoding="utf-8"))
        self.assertEqual(len(want), 80)
        with tempfile.TemporaryDirectory() as tmp:
            info = site.build_site(self.rows, tmp, generated="2026-09-02", source="cars9k_final.csv",
                                   pool_prose_count=247, as_deployed=True)
            out = Path(tmp)
            pages = {str(p.relative_to(out)) for p in out.rglob("*.html")}
            differ = [k for k, h in want.items()
                      if not (out / k).is_file() or hashlib.sha256((out / k).read_bytes()).hexdigest() != h]
        self.assertEqual(differ, [])
        self.assertEqual(pages, set(want))
        self.assertEqual((info["things"], info["counted"], info["brands"]), (9190, 4328, 78))

    def test_default_build_corrects_the_funnel_and_the_text(self):
        slugs = site.assign_slugs(sorted({r["br"] for r in self.rows}))
        funnel = site.full_meta(self.rows, slugs.__getitem__, "2026-09-02", "x")["funnel"]
        deployed = site.full_meta(self.rows, slugs.__getitem__, "2026-09-02", "x", as_deployed=True)["funnel"]
        self.assertEqual([s["n"] for s in funnel], [9190, 8985, 8962, 4552, 4328])
        self.assertEqual([s["n"] for s in deployed], [9190, 8985, 8962, 4512, 4488, 4328])
        with tempfile.TemporaryDirectory() as tmp:
            info = site.build_site(self.rows, tmp, generated="2026-09-02", source="cars9k_final.csv")
            index = (Path(tmp) / "index.html").read_text(encoding="utf-8")
            every = (Path(tmp) / "all.html").read_text(encoding="utf-8")
            brands = [p.read_text(encoding="utf-8") for p in sorted((Path(tmp) / "b").glob("*.html"))]
        self.assertEqual((info["repair"], info["assembly"], info["upgrade"]),
                         (15634661.23, 8473815.37, 7880644.36))
        self.assertIn("Assembly-only is a third total, kept out of the repair figure", index)
        self.assertNotIn("subset", index)
        self.assertNotIn("sensitivity", index)
        self.assertIn("function round2(x)", every)
        self.assertNotIn("usd(rep + asm + upg)", every)
        self.assertEqual(len(brands), 78)
        for page in brands:
            self.assertIn("eBay&rsquo;s <em>active</em> listings", page)
            self.assertIn("function round2(x)", page)
            for gone in ("<em>sold</em>", "sensitivity", "reported separately", '"Combined"',
                         "combined_total", "usd(s.repair + s.upgrade)", "--all"):
                self.assertNotIn(gone, page)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_review_pages_reprice_like_the_pipeline(self):
        # The paper: the pipeline and the review tool agree on all 4,328 counted rows.
        # Each counted row, re-priced on the page at its own listing price, must land on tot.
        rows = [[r["p"], r["pc"], r["dl"], r["tot"]] for r in self.rows if r["bkt"] in savings.BUCKETS]
        self.assertEqual(len(rows), 4328)
        slugs = site.assign_slugs(sorted({r["br"] for r in self.rows}))
        meta = site.full_meta(self.rows, slugs.__getitem__, "2026-09-02", "x")
        name = min(slugs)
        mine = [r for r in self.rows if r["br"] == name]
        pages = {
            "brand": site.render_brand(name, slugs[name], mine, "2026-09-02"),
            "all": site.render_all(self.rows, slugs, meta),
            "brand as deployed": site.render_brand(name, slugs[name], mine, "2026-09-02", as_deployed=True),
            "all as deployed": site.render_all(self.rows, slugs, meta, as_deployed=True),
        }
        for kind, page in pages.items():
            with self.subTest(page=kind):
                # deployed, Math.round(x * 100) / 100 is a cent off on 173 of them
                self.assertEqual(reprice_misses(page, rows), 173 if kind.endswith("deployed") else 0)

    def test_counted_rows_follow_the_arithmetic(self):
        counted = [r for r in self.rows if r["bkt"] in savings.BUCKETS]
        self.assertEqual(len(counted), 4328)
        for r in counted:
            self.assertEqual(r["spp"], savings.per_print(r["p"], r["pc"]), r["id"])
            self.assertEqual(r["tot"], savings.contrib(r["p"], r["pc"], r["dl"]), r["id"])
            self.assertEqual(r["bkt"], savings.bucket_for(r["cls"], r["asm"]), r["id"])

    def test_half_cent_rows_round_like_python(self):
        # p - pc lands on a half cent here; half-up rounding would give 147.67 and $303,461.85
        top = self.by_id["2905219"]
        self.assertEqual((top["spp"], top["tot"]), (147.66, 303441.3))
        self.assertEqual(savings.contrib(top["p"], top["pc"], top["dl"]), 303441.3)

    def test_bucket_totals(self):
        self.assertEqual(savings.totals(self.rows), {
            "repair": (2197, 15634661.23),
            "assembly": (1168, 8473815.37),
            "upgrade": (963, 7880644.36),
        })
        self.assertEqual(sum(r["dl"] for r in self.rows), 3210304)
        self.assertEqual(sum(r["dl"] for r in self.rows if r["bkt"] in savings.BUCKETS), 1707875)

    def test_scale_guard_rows(self):
        scale = [r for r in self.rows if r["bkt"] == savings.SCALE_BUCKET]
        self.assertEqual(len(scale), 24)
        grams = re.compile(r"^not printable as-is: ([0-9.]+) g of filament ")
        for r in scale:
            self.assertTrue(r["sx"], r["id"])
            self.assertEqual((r["spp"], r["tot"]), (0.0, 0.0), r["id"])
            self.assertEqual(r["sxu"], savings.contrib(r["p"], r["pc"], r["dl"]), r["id"])
            g = float(grams.match(r["err"]).group(1))
            self.assertEqual(savings.scale_error(g, r["p"]), r["err"])
        self.assertEqual(round(sum(r["sxu"] for r in scale), 2), 2888323.02)
        self.assertIn("(1,583 $/g)", self.by_id["1803934"]["err"])

    def test_oversize_rows(self):
        over = [r for r in self.rows if r["ms"] == "implausible"]
        self.assertEqual(len(over), 117)
        length = re.compile(r"part is (\d+) mm long")
        for r in over:
            mm = float(length.search(r["err"]).group(1))
            self.assertGreater(mm, 256)
            self.assertEqual(r["err"], savings.oversize_error(mm))
            self.assertEqual((r["pc"], r["bkt"], r["tot"]), (0.0, "", 0.0), r["id"])
        self.assertEqual(self.by_id["6702563"]["p"], 21.77)

    def test_multi_unit_flag(self):
        with_title = [r for r in self.rows if r["lt"]]
        self.assertEqual(len(with_title), 4639)
        for r in with_title:
            self.assertEqual(r["qty"], listing_units(r["lt"]), r["id"])
        self.assertEqual([r["id"] for r in self.rows if r["qty"] and not r["lt"]], [])
        self.assertEqual(sum(1 for r in self.rows if r["qty"] > 0), 583)

    def test_thumbnails(self):
        card = re.compile(r"^https://resize\.thingiverse\.com/\?url=(https://cdn\.thingiverse\.com/[^&]+)&w=292&")
        n = 0
        for r in self.rows:
            if not r["ti"]:
                continue
            m = card.match(r["ti"])
            image = m.group(1) if m else r["ti"]   # og:image as served, a placeholder, or webp/gif
            self.assertEqual(thingiverse.thumb_url(image), r["ti"], r["id"])
            n += 1
        self.assertEqual(n, 9134)

    def test_outcomes_and_summary(self):
        n = {}
        for r in self.rows:
            k = outcome(r)
            n[k] = n.get(k, 0) + 1
        self.assertEqual(n, {"counted": 4328, "unmatched": 4433, "refused": 224, "toy": 23,
                             "oversize": 117, "scale": 24, "deleted": 40, "llm_error": 1})
        text = summary(self.rows)
        self.assertIn("9190 Things, 4328 counted. Downloads 3,210,304 (1,707,875 on counted rows).", text)
        self.assertIn("$15,634,661.23", text)
        self.assertIn("scale guard 24 ($2,888,323.02 held out)", text)

    def test_romain_sheet_gives_the_sep2_seeds(self):
        seeds = read_seeds(ROMAIN)
        self.assertEqual([s["id"] for s in seeds], [r["id"] for r in self.rows])
        for s, r in zip(seeds, self.rows, strict=True):
            self.assertEqual((s["dl"], s["br"], s["mdl"], s["gen"], s["yrs"]),
                             (r["dl"], r["br"], r["mdl"], r["gen"], r["yrs"]), s["id"])

    def test_expected_sample_is_taken_from_the_table(self):
        expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
        self.assertEqual(len(expected), 17)
        for tid, want in expected.items():
            for k, v in want.items():
                self.assertEqual(self.by_id[tid][k], v, (tid, k))


if __name__ == "__main__":
    unittest.main()
