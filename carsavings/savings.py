"""Paper step 5: what a print saves, which bucket it lands in, and the guards.

    spp = round(max(p - pc, 0), 2)      saving per print, clamped at zero
    tot = round(spp * dl, 2)            times the Thing's real download count

Rounding is Python's round(x, 2), applied to the per-print figure first and then
to the total. That is what the Sep 2 run did: it reproduces the stored spp and
tot of all 4,328 counted rows and the sxu of all 24 scale-guard rows. The review
pages re-price a swapped row in JavaScript with the same rule (round2() in
site.py), so swapping a row to its own price leaves every total unchanged.

The deployed pages used Math.round(x * 100) / 100, which build_dashboard.py
--as-deployed keeps. It rounds a cent high on 173 of the 4,328 counted rows, all
at the per-print step: 166 amounts that print as a half cent but are stored just
below it (1.95 - 0.465 is 1.48499..., which Python rounds to 1.48 while
Math.round(148.5) gives 1.49), and 7 exact half cents, which Python rounds to the
even cent.

The three buckets are reported side by side and never added together:

    repair    a spare part you can buy on its own (the headline claim)
    assembly  a spare part sold only inside a larger assembly or kit
    upgrade   an accessory: it improves the car but replaces nothing

Two guards stop broken models from inflating the totals. The bed-size guard
drops a Thing whose model is longer than a consumer printer bed. The scale guard
drops a matched row whose listing price per printed gram is impossible (a 0.2 g
print against a $130 listing). Both keep the row visible at $0 with the reason
in err, and neither ever rescales anything.
"""

from __future__ import annotations

BUCKETS = ("repair", "assembly", "upgrade")
SCALE_BUCKET = "excluded_scale"
PRICEABLE = ("spare_part", "accessory")
TOY = "toy_decor_other"
LISTING_SOURCES = ("eBay", "DuckDuckGo web")

UNMATCHED_JR = "no strictly-verified listing — row not counted"
OG_ERROR = ("thingiverse served its default og tags, not this Thing — no usable text "
            "(harvest it via script/harvest_things.py)")
LLM_ERROR_PREFIX = "LLM call failed after retries: "


def round2(x: float) -> float:
    """Round to cents the way the Sep 2 run did (Python's round, on the float as stored)."""
    return round(float(x), 2)


def per_print(p: float, pc: float) -> float:
    """Saving per print: listing price minus print cost, never below zero."""
    return round2(max(float(p) - float(pc), 0.0))


def contrib(p: float, pc: float, dl: int) -> float:
    """Dollars one row contributes at listing price p (the pages' contrib())."""
    return round2(per_print(p, pc) * int(dl))


def bucket_for(cls: str, asm: bool) -> str:
    if cls == "accessory":
        return "upgrade"
    if cls == "spare_part":
        return "assembly" if asm else "repair"
    return ""


def print_cost(grams: float, usd_per_kg: float) -> float:
    """Plastic cost of one print, to the tenth of a cent."""
    return round(float(grams) * float(usd_per_kg) / 1000.0, 3)


def oversize_error(mm: float) -> str:
    return ("not printable as-is: part is %.0f mm long (> consumer FDM bed) — "
            "scale-broken export or reference scan" % mm)


def scale_error(grams: float, price: float) -> str:
    return ("not printable as-is: %.2f g of filament against a $%s listing (%s $/g) — "
            "scale-broken export or reference scan"
            % (grams, format(price, ",.0f"), format(price / grams, ",.0f")))


def is_counted(row: dict) -> bool:
    return row.get("bkt") in BUCKETS


def totals(rows) -> dict:
    """{bucket: (rows, dollars)} for the three buckets. They are reported side by side, never summed."""
    out = {}
    for b in BUCKETS:
        picked = [r for r in rows if r.get("bkt") == b]
        out[b] = (len(picked), round(sum(float(r.get("tot") or 0.0) for r in picked), 2))
    return out


def finalize(row: dict, *, bed_mm: float = 256.0, scale_usd_per_g: float = 100.0) -> dict:
    """Apply the guards and the arithmetic to one processed row, in place.

    Expects the stage outputs already on the row: cls/asm from classification;
    p, src, surl, lt and li from the accepted listing (src "none" and p 0 when
    nothing matched); judge_ok from the plausibility judge; g, mm, pc and ms
    from slicing. A row is counted only when it has no error, a listing was
    accepted, the judge passed it and neither guard fired. A refused row keeps
    its price and listing so a reviewer can see what was turned down.
    """
    row["bkt"] = ""
    row["spp"] = 0.0
    row["tot"] = 0.0
    row["sx"] = False
    row["sxu"] = 0.0
    if row.get("err") or row.get("cls") not in PRICEABLE:
        return row

    mm = row.get("mm")
    if mm and float(mm) > bed_mm:
        row["ms"] = "implausible"
        row["pc"] = 0.0
        row["err"] = oversize_error(float(mm))
        return row

    p = float(row.get("p") or 0.0)
    if row.get("src") not in LISTING_SOURCES or p <= 0 or not row.get("judge_ok"):
        return row

    g = row.get("g")
    if g and float(g) > 0 and p / float(g) > scale_usd_per_g:
        row["bkt"] = SCALE_BUCKET
        row["sx"] = True
        row["sxu"] = contrib(p, row["pc"], row["dl"])
        row["err"] = scale_error(float(g), p)
        return row

    row["bkt"] = bucket_for(row["cls"], bool(row.get("asm")))
    row["spp"] = per_print(p, row["pc"])
    row["tot"] = round2(row["spp"] * int(row["dl"]))
    return row
