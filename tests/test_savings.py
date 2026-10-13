"""Paper step 5 on hand-made rows: the guards, the buckets and the rounding."""

import unittest

import helpers  # noqa: F401  (puts the repo on sys.path)

from carsavings import savings
from carsavings.config import Settings
from carsavings.schema import blank_row


def row(**kw):
    r = blank_row("3013735")
    r.update(cls="spare_part", asm=False, src="eBay", p=19.99, pc=0.431, dl=309, judge_ok=True,
             g=17.24, mm=40.0, ms="orcaslicer")
    r.update(kw)
    return r


class Finalize(unittest.TestCase):
    def test_counted_repair_row(self):
        r = savings.finalize(row())
        self.assertEqual((r["bkt"], r["spp"], r["tot"], r["err"]), ("repair", 19.56, 6044.04, ""))

    def test_buckets(self):
        self.assertEqual(savings.finalize(row(asm=True))["bkt"], "assembly")
        self.assertEqual(savings.finalize(row(cls="accessory"))["bkt"], "upgrade")
        self.assertEqual(savings.finalize(row(src="DuckDuckGo web"))["bkt"], "repair")

    def test_oversize_guard_keeps_the_listing_visible(self):
        r = savings.finalize(row(mm=272.0, p=21.77))
        self.assertEqual((r["bkt"], r["ms"], r["pc"], r["tot"]), ("", "implausible", 0.0, 0.0))
        self.assertEqual(r["err"], "not printable as-is: part is 272 mm long (> consumer FDM bed) — "
                                   "scale-broken export or reference scan")
        self.assertEqual(r["p"], 21.77)

    def test_scale_guard(self):
        r = savings.finalize(row(g=0.2, p=129.8, pc=0.005, dl=100))
        self.assertEqual((r["bkt"], r["sx"], r["spp"], r["tot"]), (savings.SCALE_BUCKET, True, 0.0, 0.0))
        self.assertEqual(r["sxu"], savings.contrib(129.8, 0.005, 100))
        self.assertEqual(r["err"], "not printable as-is: 0.20 g of filament against a $130 listing "
                                   "(649 $/g) — scale-broken export or reference scan")

    def test_scale_message_groups_thousands(self):
        self.assertIn("against a $950 listing (1,583 $/g)", savings.scale_error(0.6, 949.99))

    def test_scale_guard_skips_rows_without_grams(self):
        # the flat default has no grams, so a $1,043 listing on a default row still counts
        r = savings.finalize(row(g=None, ms="default", pc=0.75, p=1043.28, dl=275))
        self.assertEqual((r["bkt"], r["tot"]), ("repair", 286695.75))

    def test_rows_that_do_not_count(self):
        refused = savings.finalize(row(judge_ok=False))
        self.assertEqual((refused["bkt"], refused["p"], refused["tot"]), ("", 19.99, 0.0))
        self.assertEqual(savings.finalize(row(src="none", p=0.0))["bkt"], "")
        self.assertEqual(savings.finalize(row(cls="toy_decor_other"))["bkt"], "")
        self.assertEqual(savings.finalize(row(err=savings.LLM_ERROR_PREFIX + "boom"))["bkt"], "")
        self.assertEqual(savings.finalize(row(p=0.0))["bkt"], "")

    def test_rounding_is_pythons(self):
        self.assertEqual(savings.per_print(19.99, 0.005), 19.98)
        self.assertEqual(savings.per_print(1.95, 0.465), 1.48)
        self.assertEqual(savings.per_print(150.0, 2.335), 147.66)
        self.assertEqual(savings.per_print(0.5, 0.75), 0.0)
        self.assertEqual(savings.contrib(19.99, 0.005, 243), 4855.14)

    def test_print_cost(self):
        self.assertEqual(savings.print_cost(30, 25), 0.75)
        self.assertEqual(savings.print_cost(17.24, 25), 0.431)
        self.assertEqual(Settings().default_print_cost, 0.42)  # the flat 30 g fallback at $13.99/kg
        self.assertEqual(Settings(pla_usd_per_kg=25).default_print_cost, 0.75)  # at Sep 2's $25/kg


if __name__ == "__main__":
    unittest.main()
