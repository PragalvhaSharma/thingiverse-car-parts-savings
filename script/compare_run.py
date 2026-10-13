#!/usr/bin/env python3
"""Compare a pipeline run with what the Sep 2 run recorded for the same Things.

    python pipeline.py --csv examples/smoke.csv --workers 2
    python script/compare_run.py
    python script/compare_run.py --csv out/cars_final.csv --expected examples/expected_sep2.json

For each Thing in both tables it prints the Sep 2 value and this run's value of
each field, marking the ones that differ, then how often each field agreed. The
model is not deterministic and eBay listings change from day to day, so queries,
listings and prices are expected to drift; the class, the standalone flag, the
tier and the bucket are the fields to look at first, and the print cost when
the slicer ran. Sep 2 priced plastic at $25/kg, so its print cost and total are
re-priced at this run's PLA price (--pla, else PLA_USD_PER_KG) and differ only
where the grams do. It is a report, not a test, and always exits 0.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from carsavings.config import Settings  # noqa: E402
from carsavings.savings import BUCKETS, contrib  # noqa: E402
from carsavings.schema import read_rows  # noqa: E402

FIELDS = ("cls", "asm", "tier", "part", "q", "src", "p", "pc", "ms", "bkt", "tot", "err")
TOLERANCE = {"p": 0.005, "pc": 0.0005, "tot": 0.005}
WIDTH = 44
SEP2_PLA_USD_PER_KG = 25.0


def reprice(old: dict, usd_per_kg: float) -> dict:
    """A Sep 2 row with its print cost and total at usd_per_kg of PLA instead of $25."""
    if usd_per_kg == SEP2_PLA_USD_PER_KG or old.get("pc") in (None, ""):
        return old
    new = dict(old)
    new["pc"] = round(float(old["pc"]) * usd_per_kg / SEP2_PLA_USD_PER_KG, 3)
    if old.get("bkt") in BUCKETS:
        new["tot"] = contrib(old["p"], new["pc"], old["dl"])
    return new


def same(field, a, b) -> bool:
    if field in TOLERANCE:
        try:
            return abs(float(a or 0) - float(b or 0)) <= TOLERANCE[field]
        except (TypeError, ValueError):
            return False
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    return " ".join(str(a or "").split()).lower() == " ".join(str(b or "").split()).lower()


def show(value) -> str:
    if isinstance(value, float):
        text = format(value, ",.3f").rstrip("0").rstrip(".")
    else:
        text = " ".join(str(value).split())
    text = text or "-"
    return text if len(text) <= WIDTH else text[:WIDTH - 1] + "~"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="out/cars_final.csv", help="this run's table (default: %(default)s)")
    ap.add_argument("--expected", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                                                       "examples", "expected_sep2.json"),
                    help="Sep 2 rows by id (default: examples/expected_sep2.json)")
    ap.add_argument("--pla", type=float, help="PLA $/kg this run used (default: PLA_USD_PER_KG)")
    ap.add_argument("--env-file", help="settings file (default: .env in the repo)")
    a = ap.parse_args(argv)
    usd_per_kg = a.pla if a.pla is not None else Settings.from_env(a.env_file).pla_usd_per_kg

    with open(a.expected, encoding="utf-8") as fh:
        expected = json.load(fh)
    rows = read_rows(a.csv)
    agree = {f: 0 for f in FIELDS}
    n = 0
    if usd_per_kg != SEP2_PLA_USD_PER_KG:
        print("Sep 2 print costs and totals re-priced from $25/kg to $%s/kg of PLA" % format(usd_per_kg, "g"))
    for row in rows:
        old = expected.get(row["id"])
        if old is None:
            continue
        old = reprice(old, usd_per_kg)
        n += 1
        print("\nthing:%s  %s  (%s %s)" % (row["id"], show(row["t"] or old.get("t", "")), row["br"], row["mdl"]))
        print("  %-5s %-*s %-*s" % ("", WIDTH, "Sep 2", WIDTH, "this run"))
        for f in FIELDS:
            ok = same(f, old.get(f, ""), row.get(f, ""))
            agree[f] += ok
            print("  %-5s %-*s %-*s %s" % (f, WIDTH, show(old.get(f, "")), WIDTH, show(row.get(f, "")),
                                           "" if ok else "<- differs"))
    missing = sorted(set(expected) - {r["id"] for r in rows}, key=int)
    print("\n%d Things compared%s" % (n, "; not in this run: " + ", ".join(missing) if missing else ""))
    if n:
        print("agreement: " + "  ".join("%s %d/%d" % (f, agree[f], n) for f in FIELDS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
