#!/usr/bin/env python3
"""Fold the review site's decisions back into the pipeline table.

Reviewers work on the brand pages (b/<brand>.html). Each page keeps one decision
per Thing id, in the browser and, on the deployed site, through
/api/state?key=<brand slug>:

    "keep"                                   a counted row, confirmed by hand
    "veto"                                   the row's listing is not the same part
    {v: "accept", u, p, s, t, im, src, ts}   a listing the reviewer picked, from the
                                             row's candidate pool (src "pool") or
                                             typed in by hand (src "manual")

This script applies them with the page's own arithmetic and writes a new table.
The input table is never changed.

    counted row    veto    removed: bkt "", spp 0, tot 0
                   keep    confirmed, dollars unchanged
                   accept  swapped: re-priced at the picked listing, same bucket
    uncounted row  veto    vetoed, stays at $0
                   accept  rescued: priced at the picked listing, in the page's
                           bucketOf(): a spare part goes to repair (assembly when
                           it is not sold on its own), an accessory to upgrade

A rescued row has err, sx and sxu cleared, because the reviewer has overruled
the model. A decision may also carry b (repair, assembly or upgrade) to put a
rescued row in another bucket; the pages never write it, so set it by editing
the JSON. The listing's shipping is recorded and, as everywhere in the study,
not added to the price. A pick that carries no photo keeps the photo already on
file for that listing: the row's own, or the matching candidate's.

Four columns record what happened: human_review (confirmed, vetoed, swapped or
accepted), human_review_at (the decision's timestamp), human_review_source
(pool or manual) and human_review_ship.

    python script/apply_review.py --csv out/cars_final.csv --decisions review-decisions-2026-09-03.json
    python script/apply_review.py --csv out/cars_final.csv --url https://savings-review.vercel.app
    python script/build_dashboard.py --csv out/cars_final_reviewed.csv --out site/cars

--decisions takes a brand page's Export JSON, a saved /api/state answer, the
{decisions: ...} object the page keeps in localStorage, a bare {id: decision}
map, or JSON lines of any of these; repeat it for several files. --url fetches
every brand's stored decisions from the deployment and applies each brand's
only to that brand's rows. When an id has decisions from several sources, the
last one wins: --url first, then the --decisions files in the order given.
Apply decisions to the pipeline's own table, not to an already reviewed one.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from carsavings.savings import BUCKETS, bucket_for, contrib, per_print, totals  # noqa: E402
from carsavings.schema import read_rows, write_rows  # noqa: E402
from carsavings.site import assign_slugs  # noqa: E402
from carsavings.verify import listing_units  # noqa: E402

REVIEW_COLUMNS = ("human_review", "human_review_at", "human_review_source", "human_review_ship")


def norm_decision(v):
    """The page's normDecision(): "keep", "veto", a usable accept, or None."""
    if v in ("keep", "veto"):
        return v
    if isinstance(v, dict):
        if v.get("v") in ("keep", "veto"):
            return v["v"]
        if v.get("v") == "accept" and v.get("u"):
            try:
                p = float(v.get("p"))
            except (TypeError, ValueError):
                return None
            if p > 0:
                return dict(v, p=p)
    return None


def decision_map(obj) -> dict:
    """{id: raw decision} from an export, a state answer, localStorage or a bare map."""
    if isinstance(obj, dict) and isinstance(obj.get("decisions"), dict):
        obj = obj["decisions"]
    if not isinstance(obj, dict):
        return {}
    return {str(k).strip(): v for k, v in obj.items() if str(k).strip().isdigit()}


def read_decisions(path) -> dict:
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        return decision_map(json.loads(text))
    except ValueError:
        pass
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.update(decision_map(json.loads(line)))
        except ValueError:
            raise SystemExit("%s line %d is not JSON" % (path, n)) from None
    return out


def fetch_state(base, slug):
    """(decisions, reason) for one brand from the deployment's /api/state."""
    url = "%s/api/state?%s" % (base.rstrip("/"), urllib.parse.urlencode({"key": slug}))
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "apply_review.py"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (401, 403):
            hint = (" (Vercel Deployment Protection is on: turn it off for the deployment, or use each "
                    "brand page's Export JSON with --decisions)")
        raise SystemExit("GET %s answered HTTP %d%s" % (url, exc.code, hint)) from None
    except (urllib.error.URLError, ValueError) as exc:
        raise SystemExit("GET %s failed: %s" % (url, exc)) from None
    if not isinstance(data, dict) or not data.get("ok"):
        return {}, (data.get("reason") if isinstance(data, dict) else None) or "not ok"
    return decision_map(data), ""


def bucket_of(row) -> str:
    """The page's bucketOf()."""
    if row["bkt"] in BUCKETS:
        return row["bkt"]
    return bucket_for(row["cls"], row["asm"])


def listing_photo(row, d) -> str:
    """The picked listing's photo: the decision's own, else the one on file for the same URL."""
    if d.get("im"):
        return d["im"]
    if d["u"] == row.get("surl") and row.get("li"):
        return row["li"]
    return next((c["im"] for c in row.get("cands") or [] if c.get("u") == d["u"] and c.get("im")), "")


def use_listing(row, d) -> None:
    photo = listing_photo(row, d)
    row["p"] = round(float(d["p"]), 2)
    row["surl"] = d["u"]
    row["lt"] = d.get("t") or ""
    row["li"] = photo
    host = urllib.parse.urlparse(d["u"]).netloc.lower()
    row["src"] = "eBay" if host == "ebay.com" or host.endswith(".ebay.com") else "manual"
    if row.get("cands"):
        row["now"] = next((c["i"] for c in row["cands"] if c.get("u") == d["u"]), 0)
    row["qty"] = listing_units(row["lt"]) if row["lt"] else 0
    row["human_review_at"] = d.get("ts") or ""
    row["human_review_source"] = d.get("src") or ""
    ship = d.get("s")
    row["human_review_ship"] = "" if ship in (None, "") else ship


def apply_decisions(rows, decisions) -> dict:
    """Apply {id: decision} to rows in place. Returns {action: count}."""
    counts = {}

    def note(action):
        counts[action] = counts.get(action, 0) + 1

    for row in rows:
        raw = decisions.get(row["id"])
        if raw is None:
            continue
        d = norm_decision(raw)
        if d is None:
            note("ignored: not a valid decision")
            continue
        counted = row["bkt"] in BUCKETS and row.get("human_review") != "accepted"
        if d == "keep" and not counted:
            note("uncounted: keep without a picked listing (no change)")
            continue
        for k in REVIEW_COLUMNS:
            row[k] = ""
        if d == "veto":
            row["human_review"] = "vetoed"
            if counted:
                row.update(bkt="", spp=0.0, tot=0.0)
                note("counted: vetoed, dollars removed")
            else:
                note("uncounted: vetoed")
        elif d == "keep":
            row["human_review"] = "confirmed"
            note("counted: confirmed")
        elif counted:
            use_listing(row, d)
            row["spp"] = per_print(row["p"], row["pc"])
            row["tot"] = contrib(row["p"], row["pc"], row["dl"])
            row["human_review"] = "swapped"
            note("counted: swapped to the picked listing")
        else:
            b = d.get("b") if d.get("b") in BUCKETS else bucket_of(row)
            if not b:
                print("warning: Thing %s: a listing was picked, but a %r row has no bucket; left uncounted"
                      % (row["id"], row["cls"] or "unclassified"), file=sys.stderr)
                note("uncounted: picked listing ignored (no bucket for the class)")
                continue
            use_listing(row, d)
            row.update(bkt=b, spp=per_print(row["p"], row["pc"]), tot=contrib(row["p"], row["pc"], row["dl"]),
                       err="", sx=False, sxu=0.0)
            row["human_review"] = "accepted"
            note("uncounted: rescued into %s" % b)
    return counts


def default_out(path) -> str:
    name = str(path)
    for ext in (".csv.gz", ".csv"):
        if name.endswith(ext):
            return name[:-len(ext)] + "_reviewed" + ext
    return name + "_reviewed.csv"


def money(x) -> str:
    return "$" + format(float(x), ",.2f")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="out/cars_final.csv", help="pipeline table (default: %(default)s)")
    ap.add_argument("--decisions", action="append", default=[], metavar="FILE",
                    help="exported decisions (JSON or JSON lines); repeatable")
    ap.add_argument("--url", help="deployment to read /api/state from, e.g. https://savings-review.vercel.app")
    ap.add_argument("--out", help="reviewed table (default: <csv>_reviewed.csv)")
    a = ap.parse_args(argv)
    if not a.decisions and not a.url:
        ap.error("pass --decisions FILE and/or --url")

    rows = read_rows(a.csv)
    ids = {r["id"] for r in rows}
    decisions, sources = {}, []

    if a.url:
        brands = sorted({r["br"] for r in rows})
        slugs = assign_slugs(brands)
        ids_of = {}
        for r in rows:
            ids_of.setdefault(r["br"], set()).add(r["id"])
        got, reasons = 0, {}
        for br in brands:
            found, reason = fetch_state(a.url, slugs[br])
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
            mine = {k: v for k, v in found.items() if k in ids_of[br]}
            if len(mine) < len(found):
                print("note: %d stored decisions for %s belong to no %s row in this table; skipped"
                      % (len(found) - len(mine), slugs[br], br), file=sys.stderr)
            decisions.update(mine)
            got += len(mine)
        if reasons.get("no-blob") == len(brands):
            print("warning: %s has no Blob store connected (every brand answered no-blob), so decisions "
                  "live only in reviewers' browsers: export them from each brand page and pass the files "
                  "with --decisions" % a.url, file=sys.stderr)
        elif reasons:
            print("note: %s" % ", ".join("%d brands answered %s" % (n, r) for r, n in sorted(reasons.items())),
                  file=sys.stderr)
        sources.append("%s: %d decisions over %d brands" % (a.url, got, len(brands)))

    for path in a.decisions:
        found = read_decisions(path)
        decisions.update(found)
        sources.append("%s: %d decisions" % (path, len(found)))

    unknown = sorted(set(decisions) - ids, key=int)
    if unknown:
        print("warning: %d decisions name Things that are not in %s (first: %s)"
              % (len(unknown), a.csv, ", ".join(unknown[:5])), file=sys.stderr)

    before = totals(rows)
    counts = apply_decisions(rows, decisions)
    after = totals(rows)

    out = a.out or default_out(a.csv)
    write_rows(out, rows, extra_columns=REVIEW_COLUMNS)

    for s in sources:
        print("read " + s)
    print("\n%-9s %26s %26s" % ("", "before review", "after review"))
    for b in BUCKETS:
        print("%-9s %18s %7s %18s %7s" % (b, money(before[b][1]), "(%s)" % format(before[b][0], ","),
                                          money(after[b][1]), "(%s)" % format(after[b][0], ",")))
    # every counted row sits in exactly one bucket, so the three totals add up
    print("%-9s %18s %7s %18s %7s" % ("combined",
                                      money(sum(v[1] for v in before.values())),
                                      "(%s)" % format(sum(v[0] for v in before.values()), ","),
                                      money(sum(v[1] for v in after.values())),
                                      "(%s)" % format(sum(v[0] for v in after.values()), ",")))
    print()
    if counts:
        for action, n in sorted(counts.items()):
            print("  %-58s %6s" % (action, format(n, ",")))
    else:
        print("  no decision matched a row")
    print("\nwrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
