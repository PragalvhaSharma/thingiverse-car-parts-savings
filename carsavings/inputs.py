"""The Things to run, read from an input sheet or a plain list of ids/URLs.

Three shapes are recognised from the header row:

  Romain's validated-matches export (thing_id, thing_url, Downloads, title,
      final_brand, final_model, final_generation, production_years, ...): the
      sheet the Sep 2 run used for all 9,190 Things.
  The Toyota Prius sheet (downloads, title, thing_url, "Brand & Model", ...).
  Anything else: a CSV with an id / thing_id / url / thing_url column, or a
      text file with one Thing id or URL per line.

A pipeline table (id, br, mdl, gen, yrs, dl, ...) reads as the last shape, so
a run's own output can be fed back in. Any of these may be gzipped (.gz).

Each Thing becomes a seed: {id, url, dl, br, mdl, gen, yrs, title}. dl is None
when the input has no download count; the harvest step then reads it from
Thingiverse.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
from pathlib import Path

from .schema import thing_url

# The 78 brands of the Sep 2 corpus plus a few common ones, used to split a
# combined "Brand & Model" cell. The longest matching prefix wins.
KNOWN_BRANDS = (
    "AC", "Acura", "Alfa Romeo", "Alpine", "Ariel", "Aston Martin", "Audi", "BMW", "BYD",
    "Bentley", "Bugatti", "Buick", "Cadillac", "Chery", "Chevrolet", "Chrysler", "Citroen",
    "Cupra", "Dacia", "Daewoo", "Daihatsu", "Dodge", "Eagle", "FSO", "Ferrari", "Fiat",
    "Fisker", "Ford", "GMC", "Geely", "Genesis", "Holden", "Honda", "Hummer", "Hyundai",
    "Ineos", "Infiniti", "Isuzu", "Jaguar", "Jeep", "Kgm", "Kia", "Lada", "Lamborghini",
    "Lancia", "Land Rover", "Lexus", "Lincoln", "Lotus", "MG", "MINI", "Maserati", "Mazda",
    "McLaren", "Mercedes-Benz", "Mercury", "Mitsubishi", "Nissan", "Oldsmobile", "Opel",
    "Perodua", "Peugeot", "Plymouth", "Polestar", "Pontiac", "Porsche", "Proton", "Ram",
    "Renault", "Rivian", "Rolls-Royce", "SEAT", "Saab", "Saturn", "Scion", "Skoda", "Subaru",
    "Suzuki", "TVR", "Tesla", "Toyota", "Vauxhall", "Volkswagen", "Volvo", "Xpeng", "smart",
)


def parse_thing_id(value: str) -> str:
    """Accept a full URL, thing:<id> or a bare id and return the numeric id."""
    value = (value or "").strip()
    m = re.search(r"thing:(\d+)", value)
    if m:
        return m.group(1)
    m = re.search(r"(\d{3,})", value)
    if m:
        return m.group(1)
    raise ValueError("no Thingiverse thing id in %r" % value)


def split_brand_model(text: str):
    """'Land Rover Defender' -> ('Land Rover', 'Defender'); unknown brands split at the first space."""
    text = " ".join((text or "").split())
    low = text.lower()
    best = ""
    for brand in KNOWN_BRANDS:
        b = brand.lower()
        if (low == b or low.startswith(b + " ")) and len(brand) > len(best):
            best = brand
    if best:
        return best, text[len(best):].strip()
    head, _, rest = text.partition(" ")
    return head, rest.strip()


def _downloads(value):
    value = (value or "").strip().replace(",", "")
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _seed(thing_id, dl=None, br="", mdl="", gen="", yrs="", title="", url=""):
    return {"id": str(thing_id), "url": url or thing_url(thing_id), "dl": dl,
            "br": (br or "").strip(), "mdl": (mdl or "").strip(), "gen": (gen or "").strip(),
            "yrs": (yrs or "").strip(), "title": (title or "").strip()}


def _pick(rec, lower, *names):
    for n in names:
        if n in lower:
            return rec.get(lower[n]) or ""
    return ""


def _from_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    fields = reader.fieldnames or []
    lower = {f.strip().lower(): f for f in fields if f}
    if not any(k in lower for k in ("thing_id", "id", "thing_url", "url")):
        return None
    seeds = []
    for rec in reader:
        ref = _pick(rec, lower, "thing_id", "id") or _pick(rec, lower, "thing_url", "url")
        try:
            tid = parse_thing_id(ref)
        except ValueError:
            continue
        br = _pick(rec, lower, "final_brand", "brand", "br")
        mdl = _pick(rec, lower, "final_model", "model", "mdl")
        if not br:
            combined = _pick(rec, lower, "brand & model", "brand and model")
            if combined:
                br, mdl = split_brand_model(combined)
        seeds.append(_seed(
            tid,
            dl=_downloads(_pick(rec, lower, "downloads", "download_count", "dl")),
            br=br, mdl=mdl,
            gen=_pick(rec, lower, "final_generation", "generation", "gen"),
            yrs=_pick(rec, lower, "production_years", "years", "yrs"),
            title=_pick(rec, lower, "title", "t"),
        ))
    return seeds


def _from_lines(text):
    seeds = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            seeds.append(_seed(parse_thing_id(line.split(",")[0])))
        except ValueError:
            continue        # a header line or a note
    return seeds


def read_text(path) -> str:
    """A text file, gunzipped when its name ends in .gz."""
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as f:
            return f.read()
    return Path(path).read_text(encoding="utf-8-sig")


def read_seeds(path=None, ids=()) -> list:
    """Seeds from an input file and/or explicit ids, in input order, first copy of each id kept."""
    seeds = []
    if path:
        text = read_text(path)
        first = text.splitlines()[0] if text.strip() else ""
        parsed = _from_csv(text) if "," in first else None
        seeds.extend(parsed if parsed is not None else _from_lines(text))
    for ref in ids or ():
        seeds.append(_seed(parse_thing_id(str(ref))))
    out, seen = [], set()
    for s in seeds:
        if s["id"] not in seen:
            seen.add(s["id"])
            out.append(s)
    return out
