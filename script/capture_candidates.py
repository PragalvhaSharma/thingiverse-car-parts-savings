#!/usr/bin/env python3
"""Capture eBay candidate pools for uncounted rows, so reviewers can rescue them.

A brand page lets a reviewer pick the right listing for a row the model left
uncounted, from that row's candidate pool: the top 10 US-dollar eBay listings
for its query, cheapest first. The pipeline stores the pool of every Thing it
searches on eBay (cands, at, now), so a fresh run needs nothing from here. This
script fills pools in on a table that lacks them: an older table, rows whose
search ran before pools were kept, or rows whose eBay search came back empty
on the day of the run. For that last case pass --refresh: the pipeline cached
the empty result, and without --refresh it is read back instead of searched.

For every uncounted spare part or accessory with a search query and no pool,
the query is searched on eBay (or read from cache/search/, which the pipeline
shares), and the pool is stored on the row with its capture date. now is the
number of the candidate that is the row's current listing, 0 when none is.
Nothing is re-scored. A pool found here is written to cache/search/ as well,
so a later pipeline run over the same Things verifies against it.

    python script/capture_candidates.py --csv out/cars_final.csv
    python script/capture_candidates.py --csv out/cars_final.csv --limit 200
    python script/capture_candidates.py --csv out/cars_final.csv --refresh
    python script/build_dashboard.py --csv out/cars_final_pools.csv --out site/cars
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from carsavings.cache import Cache, digest  # noqa: E402
from carsavings.config import Settings  # noqa: E402
from carsavings.ebay import Ebay, EbayBlocked, make_pool  # noqa: E402
from carsavings.run import EBAY, today  # noqa: E402
from carsavings.savings import BUCKETS, PRICEABLE  # noqa: E402
from carsavings.schema import read_rows, write_rows  # noqa: E402


def wants_pool(row) -> bool:
    return (row["cls"] in PRICEABLE and row["bkt"] not in BUCKETS and bool(row["q"])
            and not row["cands"])


def default_out(path) -> str:
    name = str(path)
    for ext in (".csv.gz", ".csv"):
        if name.endswith(ext):
            return name[:-len(ext)] + "_pools" + ext
    return name + "_pools.csv"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="out/cars_final.csv", help="pipeline table (default: %(default)s)")
    ap.add_argument("--out", help="new table (default: <csv>_pools.csv; pass the input path to update it)")
    ap.add_argument("--limit", type=int, help="search at most N queries this time")
    ap.add_argument("--refresh", action="store_true", help="search again even when a pool is cached")
    ap.add_argument("--env-file", help="settings file (default: .env in the repo)")
    a = ap.parse_args(argv)

    s = Settings.from_env(a.env_file)
    cache, ebay = Cache(s.cache_dir), Ebay(s)
    rows = read_rows(a.csv)
    todo = [r for r in rows if wants_pool(r)]
    print("%d of %d rows are uncounted spare parts or accessories with a query and no pool"
          % (len(todo), len(rows)), flush=True)

    pools, searched, filled, empty, code = {}, 0, 0, 0, 0
    for k, row in enumerate(todo, 1):
        q = row["q"]
        key = digest(q, s.ebay_params)
        if key not in pools:
            pool = None if a.refresh else cache.get("search", key)
            how = "cache"
            if pool is not None and pool.get("src") != EBAY:
                pool = {"src": pool.get("src"), "cands": []}     # eBay had no US-dollar listing then
            if pool is None:
                if a.limit is not None and searched >= a.limit:
                    print("stopped at --limit %d searches; rerun to continue" % a.limit)
                    break
                searched += 1
                how = "eBay"
                try:
                    listings = ebay.search(q)
                except EbayBlocked as exc:
                    print("\nstopped: %s" % exc, file=sys.stderr)
                    code = 3
                    break
                except Exception as exc:                 # SearchError or a network error: skip this query
                    print("[%d/%d] %s  search failed: %s" % (k, len(todo), row["id"], exc), file=sys.stderr)
                    pools[key] = None
                    continue
                pool = {"q": q, "src": EBAY, "cands": make_pool(listings), "at": today(),
                        "listings": len(listings)}
                if pool["cands"]:
                    cache.put("search", key, pool)
            pools[key] = (pool, how)
        if pools[key] is None:
            continue
        pool, how = pools[key]
        if not pool.get("cands"):
            empty += 1
            print("[%d/%d] %s  no US-dollar eBay listing (%s)" % (k, len(todo), row["id"], how), flush=True)
            continue
        row["cands"] = pool["cands"]
        row["at"] = pool.get("at") or today()
        row["now"] = next((c["i"] for c in pool["cands"] if c["u"] == row["surl"]), 0)
        filled += 1
        print("[%d/%d] %s  %d candidates, cheapest $%.2f (%s)"
              % (k, len(todo), row["id"], len(pool["cands"]), pool["cands"][0]["p"], how), flush=True)

    out = a.out or default_out(a.csv)
    write_rows(out, rows)
    print("\n%d rows got a pool, %d had no US-dollar listing, %d eBay searches made\nwrote %s"
          % (filled, empty, searched, out))
    return code


if __name__ == "__main__":
    sys.exit(main())
