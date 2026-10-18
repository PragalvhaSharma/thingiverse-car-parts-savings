#!/usr/bin/env python3
"""Build the review site (cars/index.html, all.html, b/<brand>.html, pools/)
from a pipeline table.

    python script/build_dashboard.py --csv out/cars_final.csv --out site/cars

Deploy the site/ folder to Vercel afterwards (site/api holds the review-state
and image-proxy functions). Rebuild from a reviewed table the same way:

    python script/build_dashboard.py --csv out/cars_final_reviewed.csv --out site/cars

The pages correct the deployed site's funnel and the sentences that contradict
the paper (carsavings/site.py lists them); --as-deployed keeps the deployed
funnel and wording, which is how script/compare_sep2.py reproduces the Sep 2
pages byte for byte.
"""

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from carsavings import schema, site  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default="out/cars_final.csv", help="pipeline table (default: %(default)s)")
    ap.add_argument("--out", default="site/cars", help="output folder (default: %(default)s)")
    ap.add_argument("--generated", help="date shown on the pages, YYYY-MM-DD (default: today)")
    ap.add_argument("--source", help="table name shown in the page meta (default: the CSV file name)")
    ap.add_argument("--pool-prose-count", type=int,
                    help="number printed in all.html's 'has only reached N of the M uncounted rows' "
                         "(default: the rows that actually have a pool)")
    ap.add_argument("--as-deployed", action="store_true",
                    help="keep the deployed pages' funnel and wording instead of the corrected ones")
    a = ap.parse_args(argv)

    rows = schema.read_rows(a.csv)
    if not rows:
        print("no rows in %s" % a.csv, file=sys.stderr)
        return 1
    info = site.build_site(rows, a.out,
                           generated=a.generated or datetime.date.today().isoformat(),
                           source=a.source or os.path.basename(a.csv),
                           pool_prose_count=a.pool_prose_count, as_deployed=a.as_deployed)
    print("built %(out)s: %(things)d things, %(counted)d counted, %(brands)d brand pages, "
          "%(pools)d pools" % info)
    # every counted row sits in exactly one bucket, so the three totals add up
    total = info["repair"] + info["assembly"] + info["upgrade"]
    print("repair $%s · assembly $%s · upgrade $%s · combined $%s"
          % (format(info["repair"], ",.2f"), format(info["assembly"], ",.2f"), format(info["upgrade"], ",.2f"),
             format(total, ",.2f")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
