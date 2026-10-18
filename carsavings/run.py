"""The pipeline, end to end: harvest, classify, search, verify, slice, savings.

    python pipeline.py --csv examples/smoke.csv --workers 2
    python pipeline.py --ids 3013735 5998784 --skip-slice
    python pipeline.py --csv data/inputs/validated_matches_2026-08-13.csv --site

Each Thing goes through the paper's steps:

    harvest   thingiverse.py  name, creator, description, photos, model files, downloads
    classify  classify.py     steps 1-2: class, standalone flag, part, tier, search query
    search    ebay.py         step 3: the top 10 active eBay listings, cheapest first
              websearch.py    (DuckDuckGo, only when eBay has no listing at all)
    verify    verify.py       step 3: the matcher, then the judge
    slice     slicing.py      step 4: grams from OrcaSlicer, priced at $13.99/kg of PLA
    savings   savings.py      step 5: the guards, saving per print, bucket

Harvest to verify run in worker threads (--workers). There is one slicer, so
step 4 runs on the main thread one Thing at a time, as each Thing comes back
from the workers.

Every stage's result is cached under cache/<stage>/, so a stopped run resumes
where it left off, and --refresh <stage> redoes that stage and every stage
after it. The run stops, rather than writing rows from a bad session, when
Thingiverse or eBay challenges the cookies, when OpenAI refuses the key or the
model, or when the slicer cannot be reached. A failure that belongs to one
Thing (a deleted Thing, an LLM call that failed four times, a server error)
is written on that row's err and the row counts $0.

Output: out/cars_final.csv in input order (schema.py lists the columns), and
with --site the review site under site/cars.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import sys
import threading
import traceback
from pathlib import Path
from urllib.parse import urlparse

from . import net
from .cache import STAGES, Cache, digest
from .classify import classify
from .config import Settings
from .ebay import Ebay, EbayBlocked, SearchError, make_pool
from .inputs import read_seeds
from .llm import LLM, LLMError, LLMFatal
from .savings import (BUCKETS, LISTING_SOURCES, LLM_ERROR_PREFIX, OG_ERROR, PRICEABLE, SCALE_BUCKET,
                      TOY, UNMATCHED_JR, finalize, print_cost, totals)
from .schema import blank_row, write_rows
from .slicing import SlicerUnavailable, cost_unit, local_files, open_slicer, pick_entries
from .thingiverse import HarvestError, Thingiverse, ThingiverseBlocked
from .verify import listing_units, verify
from .websearch import WebSearch

FATAL = (ThingiverseBlocked, EbayBlocked, LLMFatal, SlicerUnavailable)
EBAY = "eBay"
WEB = "DuckDuckGo web"


class Stopped(Exception):
    """Another worker hit a fatal error, so this one stops at its next stage."""


def today() -> str:
    return datetime.date.today().isoformat()


def refresh_set(stage) -> frozenset:
    """--refresh <stage>: that stage and every stage after it."""
    if not stage:
        return frozenset()
    return frozenset(STAGES[STAGES.index(stage):])


def harvest_cached(tv, cache, seed, refresh=frozenset()) -> dict:
    """The harvest for one seed, from cache/harvest/<id>.json when it is there.

    Only successful harvests are cached; a Thing that came back with
    Thingiverse's default og tags is tried again on the next run.
    """
    tid = seed["id"]
    h = None if "harvest" in refresh else cache.get("harvest", tid)
    if h is None:
        h = tv.harvest(tid, need_downloads=seed.get("dl") is None)
        if h.get("ok"):
            cache.put("harvest", tid, h)
    elif h.get("ok") and seed.get("dl") is None and h.get("dl") is None:
        # cached from a sheet that had the count; this input does not
        h["dl"] = tv._download_count(tid)
        cache.put("harvest", tid, h)
    return h


class Runner:
    def __init__(self, settings, *, refresh=frozenset(), skip_slice=False, run_date=None):
        self.s = settings
        self.cache = Cache(settings.cache_dir)
        self.refresh = frozenset(refresh)
        self.skip_slice = skip_slice
        self.today = run_date or today()
        self.tv = Thingiverse(settings)
        self.ebay = Ebay(settings)
        self.web = WebSearch(settings)
        self.llm = LLM(settings, session_for=self._session_for)
        self.transient = (HarvestError, SearchError) + net.errors()
        self.stop = threading.Event()
        self._print = threading.Lock()
        self._keys = threading.Lock()
        self._key_locks = {}
        self._searched = {}
        self._zipped = set()

    # ------------------------------------------------------------ helpers

    def _session_for(self, url):
        host = urlparse(url).netloc.lower()
        if host == "thingiverse.com" or host.endswith(".thingiverse.com"):
            return self.tv.session()
        return None

    def say(self, msg):
        with self._print:
            print(msg, flush=True)

    def _get(self, stage, key):
        return None if stage in self.refresh else self.cache.get(stage, key)

    def _lock_for(self, key):
        with self._keys:
            return self._key_locks.setdefault(key, threading.Lock())

    def _check(self):
        if self.stop.is_set():
            raise Stopped()

    def start_row(self, seed) -> dict:
        row = blank_row(seed["id"])
        row.update(br=seed.get("br", ""), mdl=seed.get("mdl", ""), gen=seed.get("gen", ""),
                   yrs=seed.get("yrs", ""), pc=self.s.default_print_cost)
        if seed.get("dl") is not None:
            row["dl"], row["dl_src"] = int(seed["dl"]), "sheet"
        else:
            row["dl"], row["dl_src"] = 0, "missing"
        return row

    @staticmethod
    def fill_from_harvest(row, h):
        if row["dl_src"] != "sheet" and h.get("dl") is not None:
            row["dl"], row["dl_src"] = int(h["dl"]), "thingiverse"
        if h.get("ok"):
            row.update(t=h.get("t", ""), by=h.get("by", ""), ti=h.get("ti", ""),
                       infill=h.get("infill", ""))

    # ------------------------------------------------------------ stages

    def classification(self, h, seed) -> dict:
        c = self._get("classify", h["id"])
        if c is None:
            c = classify(self.llm, h, seed)
            self.cache.put("classify", h["id"], c)
        return c

    def search(self, q) -> dict:
        """{q, src, cands, at, listings} for one query, shared by every Thing that asks it."""
        key = digest(q, self.s.ebay_params)
        with self._lock_for(("search", key)):
            if key in self._searched:
                return self._searched[key]
            pool = self._get("search", key)
            if pool is None:
                pool = self._search(q)
                if pool.pop("_keep", True):
                    self.cache.put("search", key, pool)
            self._searched[key] = pool
            return pool

    def _search(self, q) -> dict:
        listings = self.ebay.search(q)
        pool = {"q": q, "src": EBAY, "cands": make_pool(listings), "at": self.today,
                "listings": len(listings)}
        if pool["cands"]:
            return pool
        web = self.web.search(q)
        if web is None:                       # DuckDuckGo unreachable: try again next run
            pool.update(src="none", cands=[], _keep=False)
        elif web:
            pool.update(src=WEB, cands=web)
        else:
            pool.update(src="none", cands=[])
        return pool

    def verification(self, h, seed, c, pool) -> dict:
        key = digest(h["id"], c["cls"], c["asm"], c["part"], c["of"], c["tier"], c["q"], pool["src"],
                     [(x["u"], x["p"]) for x in pool["cands"]])
        v = self._get("verify", key)
        if v is None:
            v = verify(self.llm, h, seed, c, pool)
            self.cache.put("verify", key, v)
        return v

    @staticmethod
    def apply_verdict(row, pool, v):
        if not v.get("choice"):
            row.update(src="none", mr=v.get("mr", ""), jr=UNMATCHED_JR)
            return
        cand = next(x for x in pool["cands"] if x["i"] == v["choice"])
        judge = v.get("judge") or {}
        row.update(src=pool["src"], p=float(cand["p"]), surl=cand["u"], lt=cand["t"],
                   li=(cand.get("im") or "") if pool["src"] == EBAY else "",
                   mr=v.get("mr", ""), jr=judge.get("reason", ""))
        row["judge_ok"] = bool(judge.get("ok"))
        if pool["src"] == EBAY:
            row["now"] = v["choice"]

    # ------------------------------------------------------------ one Thing

    def process(self, seed):
        """Harvest to verify for one Thing. Returns (row, harvest, classification)."""
        row = self.start_row(seed)
        self._check()
        try:
            h = harvest_cached(self.tv, self.cache, seed, self.refresh)
        except self.transient as exc:
            row["err"] = "harvest failed: %s" % exc
            return row, None, None
        self.fill_from_harvest(row, h)
        if not h.get("ok"):
            row["err"] = h.get("err") or OG_ERROR
            return row, h, None

        self._check()
        try:
            c = self.classification(h, seed)
        except LLMError as exc:
            row["err"] = str(exc)
            return row, h, None
        row.update(cls=c["cls"], asm=bool(c["asm"]), part=c["part"], of=c["of"],
                   tier=c["tier"], q=c["q"])
        if c["cls"] == TOY:
            row["ms"] = "skipped"
            return row, h, c
        if not c["q"]:
            row.update(src="none", jr=UNMATCHED_JR)
            return row, h, c

        self._check()
        try:
            pool = self.search(c["q"])
        except self.transient as exc:
            row["err"] = "search failed: %s" % exc
            return row, h, c
        if pool["src"] == EBAY:
            row.update(cands=pool["cands"], at=pool["at"], now=0)
        if not pool["cands"]:
            row.update(src="none", jr=UNMATCHED_JR)
            return row, h, c

        self._check()
        try:
            v = self.verification(h, seed, c, pool)
        except LLMError as exc:
            row["err"] = str(exc)
            return row, h, c
        self.apply_verdict(row, pool, v)
        return row, h, c

    def guarded(self, seed):
        try:
            return self.process(seed)
        except FATAL:
            raise
        except Stopped:
            raise
        except Exception as exc:                  # a bug or an odd page: lose the row, not the run
            with self._print:
                print("error on Thing %s:" % seed["id"], file=sys.stderr)
                traceback.print_exc()
            row = self.start_row(seed)
            row["err"] = "pipeline error: %s: %s" % (type(exc).__name__, exc)
            return row, None, None

    # ------------------------------------------------------------ step 4

    def unit_files(self, h, c):
        """(entries, [{name, path}]) for the files that make up one printable unit."""
        tid = h["id"]
        dest = Path(self.s.cache_dir) / "files" / tid
        entries = pick_entries(h, (c or {}).get("files"))
        if not entries and not h.get("zip_tried"):
            self.tv.zip_files(h)
            self.cache.put("harvest", tid, h)
            entries = pick_entries(h, (c or {}).get("files"))
        if not entries:
            return [], []
        files = local_files(self.tv, h, entries, dest)
        if len(files) < len(entries) and tid not in self._zipped:
            # a listed link failed (or a cached path is gone): take the "download all" zip
            self._zipped.add(tid)
            got = self.tv.download_zip(tid, dest)
            if got:
                h["files"] = [{"name": f["name"], "size": f["size"], "urls": [], "path": f["path"]}
                              for f in got]
                h["files_via"], h["zip_tried"] = "zip", True
                self.cache.put("harvest", tid, h)
                entries = pick_entries(h, (c or {}).get("files"))
                files = local_files(None, h, entries, dest)
        return entries, files

    def slice_row(self, slicer, row, h, c):
        try:
            entries, files = self.unit_files(h, c)
        except self.transient as exc:
            print("warning: files of %s not downloaded (%s); flat default print cost" % (row["id"], exc),
                  file=sys.stderr)
            entries, files = ["?"], []
        backend = self.s.slicer_backend if slicer is not None else "none"
        key = digest(row["id"], [(f["name"], Path(f["path"]).stat().st_size) for f in files],
                     row["infill"], backend)
        res = self._get("slice", key)
        if res is None:
            res = cost_unit(files, slicer=slicer, infill=row["infill"],
                            work_dir=Path(self.s.cache_dir) / "work" / row["id"])
            if res["ms"] != "default" or not entries:
                self.cache.put("slice", key, res)
            for note in res.get("notes") or []:
                print("  note %s: %s" % (row["id"], note), file=sys.stderr)
        row.update(ms=res["ms"], mm=res.get("mm"), hrs=res.get("hrs"), files=res.get("files") or [])
        if res["ms"] == "default":
            row["g"], row["pc"] = None, self.s.default_print_cost
        else:
            row["g"], row["pc"] = res["g"], print_cost(res["g"], self.s.pla_usd_per_kg)

    def finish(self, slicer, row, h, c):
        """Step 4 (main thread), then step 5."""
        if not row["err"] and row["cls"] in PRICEABLE:
            if self.skip_slice or h is None:
                row.update(ms="default", pc=self.s.default_print_cost, g=None)
            else:
                self.slice_row(slicer, row, h, c)
        finalize(row, bed_mm=self.s.bed_mm, scale_usd_per_g=self.s.scale_usd_per_g)
        row["qty"] = listing_units(row["lt"]) if row["lt"] else 0
        return row

    # ------------------------------------------------------------ the run

    def run(self, seeds, workers=4) -> list:
        n = len(seeds)
        rows = [None] * n
        slicer = None if self.skip_slice else open_slicer(self.s)
        if not self.skip_slice:
            self.say("slicer: %s" % ("OrcaSlicer via %s" % self.s.slicer_backend if slicer
                                     else "none (geometry estimate for every print)"))
        done = 0
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
                futures = {ex.submit(self.guarded, seed): i for i, seed in enumerate(seeds)}
                try:
                    for fut in concurrent.futures.as_completed(futures):
                        i = futures[fut]
                        row, h, c = fut.result()
                        rows[i] = self.finish(slicer, row, h, c)
                        done += 1
                        self.say("[%d/%d] %s  %s" % (done, n, row["id"], describe(row)))
                except BaseException:
                    self.stop.set()
                    for f in futures:
                        f.cancel()
                    raise
        finally:
            if slicer is not None:
                slicer.close()
        return rows


# ---------------------------------------------------------------- reporting

def money(x) -> str:
    return "$" + format(float(x), ",.2f")


def describe(row) -> str:
    if row["err"]:
        return "not counted: " + row["err"][:110]
    if row["cls"] == TOY:
        return "toy/decor: not priced"
    bits = [row["cls"] + (" (not sold standalone)" if row["asm"] else "")]
    if row["src"] in LISTING_SOURCES:
        bits.append("%s %s" % (row["src"], money(row["p"])))
    if row["bkt"] in BUCKETS:
        bits.append("%s %s" % (row["bkt"], money(row["tot"])))
    elif row["src"] in LISTING_SOURCES:
        bits.append("judge refused the price")
    else:
        bits.append("no verified listing")
    bits.append("print %s %s" % (row["ms"], "$%.3f" % row["pc"]))
    return " | ".join(bits)


def outcome(row) -> str:
    """Why a row is or is not counted, in one word."""
    if row["bkt"] in BUCKETS:
        return "counted"
    if row["bkt"] == SCALE_BUCKET:
        return "scale"
    if row["ms"] == "implausible":
        return "oversize"
    if row["err"] == OG_ERROR:
        return "deleted"
    if row["err"].startswith(LLM_ERROR_PREFIX):
        return "llm_error"
    if row["err"]:
        return "other_error"
    if row["cls"] == TOY:
        return "toy"
    if row["src"] in LISTING_SOURCES:
        return "refused"
    return "unmatched"


def summary(rows) -> str:
    counted = [r for r in rows if r["bkt"] in BUCKETS]
    n = {}
    for r in rows:
        k = outcome(r)
        n[k] = n.get(k, 0) + 1
    lines = ["%d Things, %d counted. Downloads %s (%s on counted rows)."
             % (len(rows), len(counted), format(sum(r["dl"] for r in rows), ","),
                format(sum(r["dl"] for r in counted), ","))]
    t = totals(rows)
    for b, (k, total) in t.items():
        lines.append("  %-9s %18s  %6s parts" % (b, money(total), format(k, ",")))
    # every counted row sits in exactly one bucket, so the three totals add up
    lines.append("  %-9s %18s  %6s parts" % ("combined", money(sum(v[1] for v in t.values())),
                                             format(sum(v[0] for v in t.values()), ",")))
    scale = [r for r in rows if r["bkt"] == SCALE_BUCKET]
    lines.append("not counted: no verified listing %d | judge refused %d | toys/decor %d"
                 % (n.get("unmatched", 0), n.get("refused", 0), n.get("toy", 0)))
    lines.append("             oversize guard %d | scale guard %d (%s held out)"
                 % (n.get("oversize", 0), len(scale), money(sum(r["sxu"] for r in scale))))
    lines.append("             deleted Things %d | LLM errors %d | other errors %d"
                 % (n.get("deleted", 0), n.get("llm_error", 0), n.get("other_error", 0)))
    priceable = [r for r in rows if r["cls"] in PRICEABLE]
    ms = {}
    for r in priceable:
        if r["ms"]:
            ms[r["ms"]] = ms.get(r["ms"], 0) + 1
    if ms:
        lines.append("print cost from: " + ", ".join("%s %d" % kv for kv in sorted(ms.items())))
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def output_path(out) -> Path:
    out = Path(out)
    if out.suffix == ".csv" or out.name.endswith(".csv.gz"):
        return out
    return out / "cars_final.csv"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pipeline.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", help="input sheet: Romain's validated-matches export, the Prius sheet, "
                                  "a CSV with an id or url column, or a text file of ids/URLs")
    ap.add_argument("--ids", nargs="+", default=[], help="Thing ids or URLs (added after --csv)")
    ap.add_argument("--limit", type=int, help="only the first N Things")
    ap.add_argument("--out", default="out", help="output folder or .csv path (default: out/cars_final.csv)")
    ap.add_argument("--workers", type=int, default=4, help="Things processed in parallel (default: 4)")
    ap.add_argument("--refresh", choices=STAGES, help="redo this stage and every stage after it")
    ap.add_argument("--skip-slice", action="store_true",
                    help="no slicing or downloads: every print costs the flat default (30 g = $0.42)")
    ap.add_argument("--site", nargs="?", const="site/cars", help="also build the review site (default: site/cars)")
    ap.add_argument("--generated", help="date printed on the site (default: today)")
    ap.add_argument("--env-file", help="settings file (default: .env in the repo)")
    a = ap.parse_args(argv)

    settings = Settings.from_env(a.env_file)
    seeds = read_seeds(a.csv, a.ids)
    if a.limit:
        seeds = seeds[:a.limit]
    if not seeds:
        ap.error("no Things to run: pass --csv and/or --ids")
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is not set: copy .env.example to .env and fill it in "
              "(python script/doctor.py checks every setting).", file=sys.stderr)
        return 2

    out_csv = output_path(a.out)
    print("%d Things -> %s  (model %s, cache %s)" % (len(seeds), out_csv, settings.openai_model,
                                                     settings.cache_dir), flush=True)
    runner = Runner(settings, refresh=refresh_set(a.refresh), skip_slice=a.skip_slice)
    try:
        rows = runner.run(seeds, workers=a.workers)
    except FATAL as exc:
        print("\nstopped: %s" % exc, file=sys.stderr)
        print("Everything finished so far is cached in %s; rerun the same command to resume."
              % settings.cache_dir, file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\ninterrupted; finished stages are cached, rerun to resume.", file=sys.stderr)
        return 130

    write_rows(out_csv, rows)
    print("\nwrote %s\n%s" % (out_csv, summary(rows)))
    if a.site:
        from .site import build_site

        info = build_site(rows, a.site, generated=a.generated or today(), source=out_csv.name)
        print("site: %(out)s (%(brands)d brand pages, %(pools)d pools)" % info)
    return 0


if __name__ == "__main__":
    sys.exit(main())
