"""Paper steps 1 and 2: what the Thing is, and what a consumer would buy instead.

One multimodal call per Thing (GPT-5.6 Luna, like every AI step) reads the
Thing's title, description and tags, the vehicle it was matched to on the input
sheet, the names of its model files and up to four of its photos, and answers
in a fixed structure:

    cls    spare_part | accessory | toy_decor_other                 step 1
    asm    a spare part that is not sold on its own, so the only    step 1
           purchase is a larger unit that contains it
    part   the physical part, named                                  step 2
    of     what it belongs to (the vehicle, or the unit containing it)
    tier   the smallest purchasable replacement, decided before any search
    q      the eBay search query for that replacement
    files  the model files that make up one printable unit (used in step 4)

Toys and decorations stop here; they are never searched or priced.
"""

from __future__ import annotations

import re

from .savings import TOY

CLASSES = ("spare_part", "accessory", "toy_decor_other")
TIERS = ("exact_part", "smallest_kit", "whole_device_part_b", "commercial_equivalent", "no_equivalent")
MAX_PHOTOS = 4
DESC_CHARS = 4000

SYSTEM = """You are the first step of a study that measures how much money people save by 3-D printing car parts from Thingiverse instead of buying them. You are shown one Thingiverse design (a "Thing"): its title, description and tags, the vehicle it was matched to, the names of its 3-D model files, and its photos. Fill in every field.

cls: what the Thing is.
- spare_part: a functional replacement for a component the vehicle shipped with (a sun-visor clip, a headlight retainer ring, a hose connector, a trim clip, a knob, a cover, a cap, a bracket).
- accessory: a functional addition or upgrade the vehicle did not ship with (a phone mount, paddle-shifter extensions, a fender vent, a cup-holder insert, a gauge pod).
- toy_decor_other: scale models and parts for scale models or RC cars, badges, emblems, ornaments, keychains, display pieces, and anything else that repairs or improves nothing on a real vehicle.

asm: true only for a spare_part that is not sold as a standalone item, so that the only thing a consumer can buy to replace it is a larger unit that contains it. Example: an AdBlue-injector hose connector whose only purchasable alternative is the complete injector. False when the part is sold on its own, and false for accessories and toys.

part: the physical part, named in a few words ("Sun visor clip", "Knob extender for HVAC knob", "Fender vent insert").

of: what the part belongs to. The vehicle ("Kia EV6"), or, when the part is a piece of a larger unit, that unit and the vehicle ("Saab 900 headlight assembly (e-code) 1979-1986").

tier: the smallest thing a consumer could buy that replaces the part. Decide it now, before any search.
- exact_part: the identical part is sold on its own.
- smallest_kit: the part is sold only inside a small kit (a clip set, a repair kit); the kit is what gets priced.
- whole_device_part_b: the part is sold only inside a whole device or assembly (a headlight, a mirror, an injector, a door handle); the containing unit is what gets priced, as an upper bound.
- commercial_equivalent: for accessories, the commercial product that performs the same function.
- no_equivalent: no purchasable equivalent can be identified.
A spare part with asm true is smallest_kit or whole_device_part_b.

q: the eBay search query a buyer would type to find that purchasable item: make, model, generation or years when known, then the item, worded the way sellers title their listings ("Mazda 121 sun visor clip", "Saab 900 e-code headlight wire opening cap", "Kia EV6 HVAC knob extender", "AC Cobra carpet stay kit"). Give a query for no_equivalent too: the closest item a buyer would search for. No quotes or search operators, under 80 characters.

files: the names, copied exactly from the file list, of the model files that make up one printable unit of the part. Leave out duplicate copies of the same piece; keep only one of several alternate versions or sizes (the standard one, or the one that fits this vehicle); leave out test prints, calibration pieces and previews. Keep every distinct piece when the part is printed in several pieces. Return an empty list when no model files are listed.

reason: one or two sentences explaining the class, the flag and the tier.

For toy_decor_other, set asm to false and tier to no_equivalent, and leave part, of and q empty."""

_SCHEMA = None


def schema():
    """The structured answer (built on first use so the module imports without pydantic)."""
    global _SCHEMA
    if _SCHEMA is None:
        from typing import List, Literal

        from pydantic import BaseModel, Field

        class Classification(BaseModel):
            cls: Literal["spare_part", "accessory", "toy_decor_other"] = Field(description="step 1 class")
            asm: bool = Field(description="spare part not sold standalone")
            part: str = Field(description="the physical part, named")
            of: str = Field(description="the vehicle, or the unit that contains the part")
            tier: Literal["exact_part", "smallest_kit", "whole_device_part_b",
                          "commercial_equivalent", "no_equivalent"] = Field(description="pricing tier")
            q: str = Field(description="eBay search query")
            files: List[str] = Field(description="model files making up one printable unit")
            reason: str = Field(description="why")

        _SCHEMA = Classification
    return _SCHEMA


def _size(n) -> str:
    n = int(n or 0)
    if n <= 0:
        return "size unknown"
    if n < 1024 * 1024:
        return "%d KB" % max(1, round(n / 1024))
    return "%.1f MB" % (n / 1024 / 1024)


def vehicle(seed) -> str:
    bits = [" ".join(x for x in (seed.get("br", ""), seed.get("mdl", "")) if x)]
    if seed.get("gen"):
        bits.append("generation " + seed["gen"])
    if seed.get("yrs"):
        bits.append("years " + seed["yrs"])
    return "; ".join(b for b in bits if b) or "not given"


def model_files(h) -> list:
    from .thingiverse import is_model_name

    return [f for f in h.get("files") or [] if is_model_name(f.get("name", ""))]


def describe(h, seed) -> str:
    title = h.get("t") or seed.get("title") or ""
    lines = ["Thing %s: %s" % (h["id"], title)]
    if h.get("by"):
        lines.append("Creator: %s" % h["by"])
    lines.append("Vehicle (from the input sheet): %s" % vehicle(seed))
    if h.get("tags"):
        lines.append("Tags: %s" % ", ".join(t for t in h["tags"] if t))
    desc = (h.get("desc") or "").strip()
    if len(desc) > DESC_CHARS:
        desc = desc[:DESC_CHARS].rsplit(" ", 1)[0] + " ..."
    lines.append("Description:\n%s" % (desc or "(none)"))
    files = model_files(h)
    if files:
        lines.append("Model files:")
        lines.extend("- %s (%s)" % (f["name"], _size(f.get("size"))) for f in files)
    else:
        lines.append("Model files: none listed")
    return "\n".join(lines)


def clean(text) -> str:
    return " ".join(str(text or "").split())


def clean_query(q) -> str:
    q = clean(q).strip("\"' ")
    q = re.sub(r"[\"]+", "", q)
    return q[:120].strip()


def pick_files(names, h) -> list:
    """The classifier's choice, kept only where it names a real model file."""
    files = model_files(h)
    by_name = {f["name"].lower(): f["name"] for f in files}
    by_base = {}
    for f in files:
        by_base.setdefault(f["name"].rsplit("/", 1)[-1].lower(), f["name"])
    out = []
    for n in names or []:
        key = clean(n).lower()
        name = by_name.get(key) or by_base.get(key.rsplit("/", 1)[-1])
        if name and name not in out:
            out.append(name)
    return out


def classify(llm, h, seed) -> dict:
    """Steps 1 and 2 for one harvested Thing. Raises llm.LLMError on failure."""
    parts = [{"type": "text", "text": describe(h, seed)}]
    for url in (h.get("images") or [])[:MAX_PHOTOS]:
        part = llm.image_part(url)
        if part:
            parts.append(part)
    r = llm.parse(schema(), SYSTEM, parts)
    out = {"cls": r.cls, "asm": bool(r.asm), "part": clean(r.part), "of": clean(r.of),
           "tier": r.tier, "q": clean_query(r.q), "files": pick_files(r.files, h),
           "reason": clean(r.reason), "photos": len(parts) - 1, "model": llm.s.openai_model}
    if out["cls"] == TOY:
        out.update(asm=False, part="", of="", tier="", q="")
    return out
