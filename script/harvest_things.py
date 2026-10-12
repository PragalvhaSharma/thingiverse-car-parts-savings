#!/usr/bin/env python3
"""Harvest Things into cache/harvest/ without running the AI steps.

Use it to check the Thingiverse session on a few Things, to warm the cache
before a long run, or to retry the rows that came back with Thingiverse's
default og tags (a deleted or private Thing, or a page served without the
session). Only harvests that worked are cached, so a retry needs no flag.

    python script/harvest_things.py --ids 3013735 128092
    python script/harvest_things.py --csv data/inputs/validated_matches_2026-08-13.csv --limit 50
    python script/harvest_things.py --csv out/cars_final.csv --only-errors

Each line shows whether the Thing's own page came back and how (api: the
website's JSON routes; page: the og-tag fallback), the model files found (from
the API or the "download all" zip), the photos and the download count.
"""

import argparse
import concurrent.futures
import csv
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from carsavings.cache import Cache  # noqa: E402
from carsavings.config import Settings  # noqa: E402
from carsavings.inputs import read_seeds, read_text  # noqa: E402
from carsavings.run import harvest_cached  # noqa: E402
from carsavings.savings import OG_ERROR  # noqa: E402
from carsavings.thingiverse import Thingiverse, ThingiverseBlocked  # noqa: E402


def error_ids(path) -> set:
    """Ids of a pipeline table's rows whose harvest got the default og tags (.csv or .csv.gz)."""
    return {rec["id"] for rec in csv.DictReader(io.StringIO(read_text(path)))
            if (rec.get("err") or "") == OG_ERROR}


def line(h) -> str:
    if not h.get("ok"):
        return "DEFAULT OG TAGS: deleted, private, or the session did not reach the page"
    files = h.get("files") or []
    return "ok via %-4s files %d%s  photos %d  dl %s  %s" % (
        h.get("via") or "?", len(files), " (%s)" % h["files_via"] if h.get("files_via") else "",
        len(h.get("images") or []), "?" if h.get("dl") is None else format(h["dl"], ","),
        (h.get("t") or "")[:70])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="input sheet or pipeline table (same shapes as pipeline.py)")
    ap.add_argument("--ids", nargs="+", default=[], help="Thing ids or URLs")
    ap.add_argument("--limit", type=int, help="only the first N Things")
    ap.add_argument("--only-errors", action="store_true",
                    help="with a pipeline table: only the rows whose harvest got the default og tags")
    ap.add_argument("--refresh", action="store_true", help="harvest again even when cached")
    ap.add_argument("--workers", type=int, default=2, help="Things fetched in parallel (default: 2)")
    ap.add_argument("--env-file", help="settings file (default: .env in the repo)")
    a = ap.parse_args(argv)

    seeds = read_seeds(a.csv, a.ids)
    if a.only_errors:
        if not a.csv:
            ap.error("--only-errors needs --csv with a pipeline table")
        wanted = error_ids(a.csv)
        seeds = [s for s in seeds if s["id"] in wanted]
    if a.limit:
        seeds = seeds[:a.limit]
    if not seeds:
        ap.error("no Things to harvest")

    s = Settings.from_env(a.env_file)
    tv, cache = Thingiverse(s), Cache(s.cache_dir)
    refresh = frozenset({"harvest"}) if a.refresh else frozenset()
    n, ok, default, failed = len(seeds), 0, 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futures = {ex.submit(harvest_cached, tv, cache, seed, refresh): seed for seed in seeds}
        try:
            for k, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                tid = futures[fut]["id"]
                try:
                    h = fut.result()
                except ThingiverseBlocked:
                    raise
                except Exception as exc:
                    failed += 1
                    print("[%d/%d] %s  failed: %s" % (k, n, tid, exc), flush=True)
                    continue
                ok += bool(h.get("ok"))
                default += not h.get("ok")
                print("[%d/%d] %s  %s" % (k, n, tid, line(h)), flush=True)
        except ThingiverseBlocked as exc:
            for f in futures:
                f.cancel()
            print("\nstopped: %s" % exc, file=sys.stderr)
            return 3
    print("\n%d harvested, %d with the default og tags, %d failed (cache: %s)"
          % (ok, default, failed, os.path.join(str(s.cache_dir), "harvest")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
