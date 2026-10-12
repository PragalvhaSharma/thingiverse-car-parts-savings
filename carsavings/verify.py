"""Paper step 3, verification: the matcher and the judge.

Two more GPT-5.6 Luna passes decide whether a price can be trusted.

The matcher compares the Thing's photos with every candidate listing's photo and
title and accepts a listing only if it shows the same physical part for the same
vehicle. The pool is numbered cheapest first, and the pipeline takes the lowest
numbered listing the matcher accepts: the least expensive real product a
consumer could buy instead.

The judge then sees both photographs and the price and rules on whether the
price is plausible for a part of that kind and size, refusing prices that are
implausibly high or low and listings priced at the wrong scale (an assembly
against an exact part, a whole vehicle).

Both write their reasoning, which stays on the row (mr and jr). The checks are
strict in one direction: a refused or unmatched part counts $0 as unknown,
never as an estimated price.
"""

from __future__ import annotations

import re

from .classify import clean, vehicle

MATCH_PHOTOS = 3
JUDGE_PHOTOS = 2
# The input sheet's vehicle came from an automatic attribution upstream: on the smoke
# rows it gives 2023-present for a 1987-1993 Mustang part, 1994-1998 for a 79-86 Saab.
SHEET_VEHICLE = "Vehicle on the input sheet (attributed automatically; its generation and years can be wrong)"

MATCH_SYSTEM = """You are the matcher in a study that prices 3-D printed car parts against real listings a consumer could buy instead. You are shown the printed part (its Thingiverse title, the part and vehicle it was identified as, its pricing tier and its photos) and numbered candidate listings found by searching for it, cheapest first, each with its price, title and photo.

Accept a candidate only if its photo and title show the same physical part for the same vehicle, at the unit the pricing tier targets:
- exact_part: the part itself (a pack of several of the same part is acceptable).
- smallest_kit: the part itself, or the small kit that contains it.
- whole_device_part_b: the part itself, or the device or assembly that contains it.
- commercial_equivalent: a commercial product that performs the same function on this vehicle.
- no_equivalent: the closest purchasable item that performs the same function on this vehicle.

The vehicle is the one the printed part was identified for, from the Thing's own title, description and photos. The input sheet's vehicle was attributed to each Thing automatically, and its generation and years are sometimes wrong: when they disagree with the Thing or with the vehicle the part was identified for, follow the Thing.

Refuse near-misses: a similar part for a different model or generation, a different mounting, size or shape, a different kind of part, a whole vehicle, a scale model or toy, or a larger assembly when the tier is exact_part. Refuse when the photo and title do not let you tell. When in doubt, refuse: an unmatched part is carried at $0 as unknown, while a wrong match puts a wrong price into the totals.

accepted: the numbers of every candidate that passes; an empty list when none does.
reason: two or three sentences. Name the cheapest accepted candidate by its number and say what in its photo and title shows it is the same part, and why the cheaper candidates fail. When none passes, say why the candidates fail."""

JUDGE_SYSTEM = """You are the judge in a study that prices 3-D printed car parts against real listings a consumer could buy instead. A matcher has chosen the listing below as the same part as the printed Thing. You see the Thing's photos, the listing's photo and title, the part as identified, its pricing tier, and the listing's item price (shipping excluded).

Decide whether that price can be trusted as what a consumer would pay instead of printing this part.
- The listing must be the same kind of item at the scale the tier targets. For exact_part, a listing for a larger assembly or for a different kind of part fails the exact-part price guardrail. For smallest_kit and whole_device_part_b the containing kit or unit is acceptable, but a whole vehicle, an unrelated assembly or a different part fails the part-scale/type guardrail.
- The price must be plausible for that item at its size, in either direction. Refuse prices implausibly high (a $335 listing for a pair of interior door-handle pulls) or implausibly low (a placeholder price, a deposit, a fraction of what the item sells for).
- A multi-pack of the same part is acceptable; do not refuse it for the quantity alone.
- The vehicle is the one the part was identified for. The input sheet's generation and years were attributed automatically and are sometimes wrong; when they disagree with the Thing, follow the Thing.

ok: true when the price can be trusted, false otherwise.
reason: one or two sentences that begin with the price, for example "$19.99 is plausible for a small snap-in headlight wire-opening cap. The listing matches the named part and is not priced at whole-headlight or whole-vehicle scale." or "$335.05 is implausibly high for a pair of interior door-handle pulls." """

_MATCH = None
_JUDGE = None


def match_schema():
    global _MATCH
    if _MATCH is None:
        from typing import List

        from pydantic import BaseModel, Field

        class Match(BaseModel):
            accepted: List[int] = Field(description="numbers of the candidates that show the same part")
            reason: str = Field(description="why")

        _MATCH = Match
    return _MATCH


def judge_schema():
    global _JUDGE
    if _JUDGE is None:
        from pydantic import BaseModel, Field

        class Judgment(BaseModel):
            ok: bool = Field(description="the price can be trusted")
            reason: str = Field(description="why, beginning with the price")

        _JUDGE = Judgment
    return _JUDGE


def money(x) -> str:
    return "$%.2f" % float(x or 0)


def thing_text(h, seed, c) -> str:
    lines = [
        "Printed part: %s" % (" - ".join(x for x in (c.get("part"), c.get("of")) if x) or "(not named)"),
        "Thing %s: %s" % (h["id"], h.get("t") or seed.get("title") or ""),
        "%s: %s" % (SHEET_VEHICLE, vehicle(seed)),
        "Class: %s; not sold standalone: %s; pricing tier: %s"
        % (c.get("cls"), "yes" if c.get("asm") else "no", c.get("tier") or "none"),
        "Search query: %s" % c.get("q", ""),
    ]
    return "\n".join(lines)


def _thing_photos(llm, h, n):
    parts = []
    for url in (h.get("images") or [])[:n]:
        part = llm.image_part(url)
        if part:
            parts.append(part)
    return parts


def candidate_text(cand, src) -> str:
    ship = " + %s shipping" % money(cand["s"]) if cand.get("s") else ""
    text = "Candidate %d: %s%s: %s" % (cand["i"], money(cand["p"]), ship, cand["t"])
    if src != "eBay":
        bits = [b for b in (cand.get("snip"), cand.get("u")) if b]
        if bits:
            text += " (" + " | ".join(bits) + ")"
    return text


def match(llm, h, seed, c, cands, src) -> dict:
    parts = [{"type": "text", "text": thing_text(h, seed, c) + "\nPhotos of the printed Thing:"}]
    parts.extend(_thing_photos(llm, h, MATCH_PHOTOS))
    parts.append({"type": "text", "text": "Candidate listings from %s, cheapest first:" % src})
    for cand in cands:
        part = llm.image_part(cand["im"]) if cand.get("im") else None
        parts.append({"type": "text", "text": candidate_text(cand, src) + ("" if part else " (no photo)")})
        if part:
            parts.append(part)
    r = llm.parse(match_schema(), MATCH_SYSTEM, parts)
    return {"accepted": sorted({int(i) for i in r.accepted}), "reason": clean(r.reason)}


def judge(llm, h, seed, c, cand, src) -> dict:
    parts = [{"type": "text", "text": thing_text(h, seed, c) + "\nPhotos of the printed Thing:"}]
    parts.extend(_thing_photos(llm, h, JUDGE_PHOTOS))
    listing = "Listing chosen by the matcher (%s): %s\nItem price: %s (shipping excluded)" % (src, cand["t"], money(cand["p"]))
    if src != "eBay" and cand.get("snip"):
        listing += "\nSnippet: %s" % cand["snip"]
    photo = llm.image_part(cand["im"]) if cand.get("im") else None
    parts.append({"type": "text", "text": listing + ("\nListing photo:" if photo else "\n(no listing photo)")})
    if photo:
        parts.append(photo)
    r = llm.parse(judge_schema(), JUDGE_SYSTEM, parts)
    return {"ok": bool(r.ok), "reason": clean(r.reason)}


def verify(llm, h, seed, c, pool) -> dict:
    """Matcher, then judge on the cheapest accepted candidate.

    Returns {choice, accepted, mr, judge}; choice is the candidate number (0 when
    nothing was accepted) and judge is {ok, reason} or None.
    """
    cands = pool.get("cands") or []
    if not cands:
        return {"choice": 0, "accepted": [], "mr": "", "judge": None}
    m = match(llm, h, seed, c, cands, pool["src"])
    accepted = [i for i in m["accepted"] if 1 <= i <= len(cands)]
    if not accepted:
        return {"choice": 0, "accepted": [], "mr": m["reason"], "judge": None}
    choice = accepted[0]
    j = judge(llm, h, seed, c, cands[choice - 1], pool["src"])
    return {"choice": choice, "accepted": accepted, "mr": m["reason"], "judge": j}


# ------------------------------------------------------------- multi-unit flag

_QTY = re.compile(r"\b(\d+)\s*(?:pcs|pack)\b|\b(\d+)x\b|\bset of (\d+)\b|\b(\d+)\s*pieces\b", re.I)
_PAIR = re.compile(r"\bpair\b", re.I)


def listing_units(title) -> int:
    """Units a listing advertises ("4pcs", "set of 2", "pair"), 0 when it reads as one.

    Only a flag for reviewers: the price is never divided by it.
    """
    ns = [int(next(g for g in m.groups() if g)) for m in _QTY.finditer(title or "")]
    ns = [n for n in ns if 2 <= n <= 20]
    if ns:
        return ns[0]
    return 2 if _PAIR.search(title or "") else 0
