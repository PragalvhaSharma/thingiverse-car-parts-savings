"""cars_final.csv: the pipeline's output table, its column order and its types.

The column order is the order of the rows embedded in the deployed brand pages,
which were written straight from this table: the 30 scored fields, then the
generation and production years from the input sheet, then the candidate pool
(at, now, cands) on rows whose eBay results were captured. A few working
columns follow at the end (grams, print hours, longest bounding-box side,
infill, the files that make up one printable unit, and where the download count
came from). The site ignores them; they are there so a row can be audited.

    id    Thing id                  p     accepted listing price (item only)
    t     Thing name (scraped)      pc    print cost (filament g x $/kg)
    by    creator                   spp   saving per print = round(max(p-pc,0),2)
    bkt   repair|assembly|upgrade   dl    downloads
          |excluded_scale|""        tot   round(spp x dl, 2)
    cls   spare_part|accessory|     surl  accepted listing URL
          toy_decor_other           src   eBay | DuckDuckGo web | none
    asm   not sold standalone       sx    scale guard fired
    part  the part, named           sxu   what an sx row would have carried
    of    what it belongs to        ms    orcaslicer|mixed|estimator|default|
    tier  pricing target tier             implausible|skipped
    q     search query              lt    listing title   li  listing photo
    qty   units the listing         ti    Thing thumbnail
          advertises (flag only)    mr    matcher reasoning   jr  judge reasoning
    err   why the row cannot count  br/mdl/gen/yrs  vehicle, from the sheet
"""

from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path

CORE = ("id", "url", "t", "by", "bkt", "cls", "asm", "part", "of", "tier", "q",
        "p", "pc", "spp", "dl", "tot", "surl", "src", "sx", "sxu", "ms", "lt",
        "qty", "li", "ti", "mr", "jr", "err", "br", "mdl")
SHEET = ("gen", "yrs")
POOL = ("at", "now", "cands")
EXTRA = ("g", "hrs", "mm", "infill", "files", "dl_src")
COLUMNS = CORE + SHEET + POOL + EXTRA

FLOATS = frozenset({"p", "pc", "spp", "tot", "sxu"})
INTS = frozenset({"dl", "qty"})
BOOLS = frozenset({"asm", "sx"})
OPTIONAL_FLOATS = frozenset({"g", "hrs", "mm"})
JSON_COLUMNS = frozenset({"cands", "files"})


def thing_url(thing_id) -> str:
    return "https://www.thingiverse.com/thing:%s" % thing_id


def _open(path, mode):
    """Text-mode open; a .gz path is read or written gzip-compressed."""
    if str(path).endswith(".gz"):
        return gzip.open(path, mode + "t", newline="", encoding="utf-8")
    return open(path, mode, newline="", encoding="utf-8")


def blank_row(thing_id) -> dict:
    row = {k: "" for k in COLUMNS}
    row.update(id=str(thing_id), url=thing_url(thing_id), asm=False, sx=False,
               p=0.0, pc=0.0, spp=0.0, tot=0.0, sxu=0.0, dl=0, qty=0,
               g=None, hrs=None, mm=None, cands=[], files=[])
    return row


def to_cell(key, value):
    if key in JSON_COLUMNS:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False) if value else ""
    if key in BOOLS:
        return "true" if value else "false"
    if value is None:
        return ""
    return value


def from_cell(key, text):
    text = text if text is not None else ""
    if key in JSON_COLUMNS:
        return json.loads(text) if text.strip() else []
    if key in BOOLS:
        return text.strip().lower() in ("true", "1", "yes")
    if key in FLOATS:
        return float(text) if text.strip() else 0.0
    if key in INTS:
        return int(float(text)) if text.strip() else 0
    if key in OPTIONAL_FLOATS:
        return float(text) if text.strip() else None
    if key == "now":
        return int(text) if text.strip() else ""
    return text


def write_rows(path, rows, extra_columns=()) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = list(COLUMNS) + [c for c in extra_columns if c not in COLUMNS]
    with _open(path, "w") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([to_cell(k, r.get(k, "")) for k in header])


def read_rows(path) -> list:
    """Read a pipeline table back with its types. Unknown columns are kept as text."""
    out = []
    with _open(path, "r") as f:
        for raw in csv.DictReader(f):
            row = {k: from_cell(k, raw.get(k)) for k in COLUMNS}
            for k, v in raw.items():
                if k is not None and k not in row:
                    row[k] = v if v is not None else ""
            out.append(row)
    return out
