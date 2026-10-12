#!/usr/bin/env python3
"""Rebuild the Sep 2 dashboard from its own rows and byte-compare the result
with the pages that were deployed on Vercel.

The methods-doc folder holds the deployed pages (all.html, index.html and
brands/<slug>.html, fetched from savings-review on 2026-09-10) and
combined_rows.json, the 9,190 rows embedded in the 78 brand pages. The rows are
put back in the order of Romain's validated-matches sheet, which is the order
the pipeline processed them in, and the site is rebuilt with the Sep 2 date and
source name.

all.html's prose said capture_candidates.py "has only reached 247 of the 4,862
uncounted rows". That 247 was a stale hand-typed number (1,411 uncounted rows
actually have pools), so the builder computes it by default; this script pins
it to 247, and builds with as_deployed=True (the deployed funnel and wording,
which the builder otherwise corrects), to reproduce the deployed bytes.

    python script/compare_sep2.py --methods-doc <dir> --sheet data/inputs/validated_matches_2026-08-13.csv
"""

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from carsavings import site  # noqa: E402

PAYLOAD = re.compile(r'<script id="payload" type="application/json">(.*?)</script>', re.S)


def load_rows(methods_doc, sheet):
    with open(os.path.join(methods_doc, "combined_rows.json"), encoding="utf-8") as f:
        rows = json.load(f)
    by_id = {}
    for r in rows:
        by_id[r["id"]] = {k: v for k, v in r.items() if not k.startswith("_")}
    with open(sheet, newline="", encoding="utf-8") as f:
        order = [rec["thing_id"].strip() for rec in csv.DictReader(f)]
    missing = [i for i in order if i not in by_id]
    extra = set(by_id) - set(order)
    if missing or extra:
        raise SystemExit("sheet and rows disagree: %d missing, %d extra" % (len(missing), len(extra)))
    return [by_id[i] for i in order]


def payload(data: bytes):
    return json.loads(PAYLOAD.search(data.decode("utf-8")).group(1))


def json_diffs(a, b, path="$", out=None, limit=6):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append("%s: type %s vs %s (%r vs %r)" % (path, type(a).__name__, type(b).__name__, a, b))
    elif isinstance(a, dict):
        if list(a) != list(b):
            out.append("%s: keys %s vs %s" % (path, list(a), list(b)))
        for k in a:
            if k in b:
                json_diffs(a[k], b[k], "%s.%s" % (path, k), out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append("%s: length %d vs %d" % (path, len(a), len(b)))
        for i, (x, y) in enumerate(zip(a, b, strict=False)):     # lengths may differ; reported above
            json_diffs(x, y, "%s[%d]" % (path, i), out, limit)
            if len(out) >= limit:
                break
    elif a != b:
        out.append("%s: %r vs %r" % (path, a, b))
    return out


def compare(name, built: Path, deployed: Path):
    a, b = built.read_bytes(), deployed.read_bytes()
    if a == b:
        return True, "%s: identical (%s bytes)" % (name, format(len(a), ","))
    i = next((k for k, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    lines = ["%s: DIFFERENT (built %d bytes, deployed %d bytes, first difference at byte %d)"
             % (name, len(a), len(b), i),
             "    built:    %r" % a[max(0, i - 60):i + 60],
             "    deployed: %r" % b[max(0, i - 60):i + 60]]
    try:
        for d in json_diffs(payload(a), payload(b)):
            lines.append("    payload " + d)
    except Exception as exc:  # pragma: no cover - diagnostics only
        lines.append("    (payload not comparable: %s)" % exc)
    return False, "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--methods-doc", required=True, help="folder with combined_rows.json and the deployed pages")
    ap.add_argument("--sheet", required=True, help="Romain's validated_matches CSV (row order)")
    ap.add_argument("--out", help="where to build (default: a temporary folder)")
    a = ap.parse_args(argv)

    rows = load_rows(a.methods_doc, a.sheet)
    out = Path(a.out or tempfile.mkdtemp(prefix="sep2-site-"))
    site.build_site(rows, out, generated="2026-09-02", source="cars9k_final.csv", pool_prose_count=247,
                    as_deployed=True)

    md = Path(a.methods_doc)
    results = [compare("all.html", out / "all.html", md / "all.html"),
               compare("index.html", out / "index.html", md / "index.html")]
    built = sorted(p.name for p in (out / "b").glob("*.html"))
    deployed = sorted(p.name for p in (md / "brands").glob("*.html"))
    if built != deployed:
        results.append((False, "brand pages: built %d, deployed %d; only built: %s; only deployed: %s"
                        % (len(built), len(deployed), sorted(set(built) - set(deployed))[:5],
                           sorted(set(deployed) - set(built))[:5])))
    same = 0
    for name in deployed:
        if name in built:
            ok, msg = compare("b/" + name, out / "b" / name, md / "brands" / name)
            same += ok
            if not ok:
                results.append((ok, msg))
    results.append((same == len(deployed), "brand pages: %d of %d identical" % (same, len(deployed))))

    for ok, msg in results:
        print(("OK   " if ok else "DIFF ") + msg)
    print("built in %s" % out)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
