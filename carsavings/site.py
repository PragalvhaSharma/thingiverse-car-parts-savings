"""The review site that ran on Vercel as savings-review.

Output layout (served under /cars/, next to site/api/state.js and img.js):

    cars/index.html         overview: the three buckets, the funnel, the rows
                            worth checking first, and the brand table
    cars/all.html           every row in one list, biggest dollars first
    cars/b/<slug>.html      one page per brand; review decisions are made here
                            and stored per brand through /api/state?key=r2-<slug>
                            (STATE_KEY; as_deployed keeps key=<slug>)
    cars/pools/<id>.json    candidate pools that all.html loads on demand

The markup and JavaScript in templates/ are the deployed pages with their data
taken out. This module computes the JSON payload each page embeds, fills in the
few numbers printed in static prose, and writes the files.

By default the build corrects what the deployed pages got wrong: the overview's
funnel (FUNNEL), the review pages' cent rounding, the sentences and summed
figures that contradict the paper and the tiles shown for a listing without a
saved photo (CORRECTIONS), and the median saving per print (full_meta). It also
gives the review state fresh keys (STATE_KEY).
With as_deployed=True it keeps all of that as deployed, and the output
built from the Sep 2 rows is byte-identical to the deployed pages;
script/compare_sep2.py checks that.
"""

from __future__ import annotations

import html
import json
import re
import statistics
import unicodedata
from pathlib import Path

from .savings import BUCKETS, PRICEABLE

TEMPLATES = Path(__file__).resolve().parent / "templates"

PLACEHOLDER = {
    "all": " /* PAYLOAD REMOVED */ ",
    "index": " /* PAYLOAD */ ",
    "brand": " /* PAYLOAD payload */ ",
}

ROW_KEYS = ("id", "url", "t", "by", "bkt", "cls", "asm", "part", "of", "tier", "q",
            "p", "pc", "spp", "dl", "tot", "surl", "src", "sx", "sxu", "ms", "lt",
            "qty", "li", "ti", "mr", "jr", "err", "br", "mdl")
HEAD_KEYS = ("id", "t", "part", "br", "mdl", "bkt", "p", "spp", "dl", "tot", "ti",
             "li", "surl", "url", "href")
BRAND_META_KEYS = ("generated", "things", "counted", "uncountedN", "poolsN",
                   "poolsCountedN", "repairN", "assemblyN", "upgradeN", "repairTotal",
                   "assemblyTotal", "upgradeTotal", "withBoth", "withEbay")
HEAD_SIZE = 40          # rows in "The rows worth checking first"
HEAD_SHARE = 40.0       # headN = fewest repair rows holding this % of the repair total
MODELS_SHOWN = 6        # models listed per brand in the index table

FUNNEL = (
    ("Things in the corpus", "one printable car part each, with its real download count"),
    ("Priceable", "spare part or accessory — toys, decor, errors and the parts stopped by the size "
                  "and scale guards drop out here"),
    ("Searched", "the part query, run on eBay (DuckDuckGo when eBay had no US-dollar listing)"),
    ("Same physical part", "strict match on titles and photos, both sides — the rest stay unknown at $0"),
    ("Counted in the total", "the judge found the price plausible; the saving is clamped at zero"),
)
FUNNEL_AS_DEPLOYED = (
    ("Things in the corpus", "one printable car part each, with its real download count"),
    ("Priceable", "spare part or accessory — toys, decor and hard errors drop out here"),
    ("eBay returned listings", "a search that came back with candidates for the matcher to judge"),
    ("Same physical part", "strict match on titles and photos, both sides — the rest are refused"),
    ("Printable at that scale", "{n} dropped: the STL slices too light to be the priced part"),
    ("Counted in the total", "judged plausible and priced above its print cost, so it carries savings"),
)

# The review pages re-price a swapped row with contrib(). Deployed, it rounded with
# Math.round(x * 100) / 100, which is a cent off Python's round(x, 2) on 173 of Sep 2's
# 4,328 counted rows; the paper says the two agree on all of them. round2 is Python's.
_CONTRIB_JS = (
    "  function contrib(r, price) {\n"
    "    var per = Math.round(Math.max(price - r.pc, 0) * 100) / 100;\n"
    "    return Math.round(per * r.dl * 100) / 100;\n"
    "  }\n",
    "  // round2 is Python's round(x, 2): x to the nearest cent, exactly half a cent to\n"
    "  // the even cent. Math.round(x * 100) / 100 rounds x * 100 instead, which lands a\n"
    "  // cent away on 173 of Sep 2's 4,328 counted rows.\n"
    "  function round2(x) {\n"
    "    var t = x * 8;   // a whole odd number only when x is exactly half a cent\n"
    "    if (t === Math.floor(t) && t % 2) {\n"
    "      var lo = Math.floor(x * 100);\n"
    "      return (lo % 2 ? lo + 1 : lo) / 100;\n"
    "    }\n"
    "    return Number(x.toFixed(2));\n"
    "  }\n"
    "  function contrib(r, price) {\n"
    "    return round2(round2(Math.max(price - r.pc, 0)) * r.dl);\n"
    "  }\n",
)

# The review pages keep their decisions in one Vercel Blob per brand, read and written
# through /api/state?key=..., and in the browser's localStorage. Deployed, the keys were
# the brand slug and tv-review-decisions-v1-<slug> (tv-all-decisions-v1 on all.html).
# The default build uses fresh keys (r2-<slug>, tv-review-decisions-r2-<slug> and
# tv-all-decisions-r2), so its pages do not load the decisions saved on the Sep 2
# pages, which stay under the old keys. script/apply_review.py --url reads the old keys.
STATE_KEY = "r2"

# Text and figures of the deployed pages that contradict the paper or show a listing
# without its photo badly, and their default replacements; all.html's last four pairs
# move it to the STATE_KEY keys. Each old text occurs exactly once in its template when
# its pair is applied.
CORRECTIONS = {
    "index": (
        # every counted part sits in exactly one bucket, so the three totals add up (the
        # paper, step 5; methods doc section 7, note 1)
        ('        <p><b>Three buckets, never summed together.</b>\n'
         '        <span class="hrepair">Repair</span> is the main claim &mdash; a spare part you print\n'
         '        instead of rebuying. <span class="hassembly">Assembly-only</span> is the subset whose\n'
         '        part is not sold on its own, so the alternative was buying the whole assembly; it is\n'
         '        the same repair story but a weaker comparison, so it is broken out.\n'
         '        <span class="hupgrade">Upgrade</span> is an accessory printed instead of bought\n'
         '        &mdash; a different claim, reported alongside and never added in. Toys and decor are\n'
         '        excluded at $0.</p>\n',
         '        <p><b>Three buckets that add up.</b> Every counted part sits in exactly one bucket,\n'
         '        so the three totals can be added without counting any part twice.\n'
         '        <span class="hrepair">Repair</span> is the main claim: a spare part you print\n'
         '        instead of rebuying. <span class="hassembly">Assembly-only</span> covers\n'
         '        parts not sold on their own, so the alternative was buying the whole assembly; it is\n'
         '        the same repair story but a weaker comparison, so it is kept out of the repair figure.\n'
         '        <span class="hupgrade">Upgrade</span> is an accessory printed instead of bought.\n'
         '        Toys and decor are excluded at $0.</p>\n'),
        ('    "Repair and upgrade are different claims and are never added together. " +\n'
         '    "Assembly-only is a subset of repair, already included in the repair figure and " +\n'
         '    "broken out so it can be removed. Median saving per print: " + usdFull(M.medianSpp) +\n',
         '    "Every counted part sits in exactly one bucket, so the three totals add up to <b>" +\n'
         '    usdFull(M.repairTotal + M.assemblyTotal + M.upgradeTotal) + "</b> across " + num(M.counted) +\n'
         '    " parts. Assembly-only is kept out of the repair figure: spare parts not " +\n'
         '    "sold on their own, priced against the smallest unit that contains them. '
         'Median saving per print: " + usdc(M.medianSpp) +\n'),
        ('      n: num(M.upgradeN) + " accessories · reported separately" },\n',
         '      n: num(M.upgradeN) + " accessories printed instead of bought" },\n'),
        ('    "Repair and upgrade are separate claims and are never summed.";\n',
         '    "Every counted part sits in exactly one bucket, so the three totals can be added.";\n'),
        # the median saving per print to the cent; usdFull rounds to whole dollars
        ('  function usdFull(n) { return "$" + Math.round(n || 0).toLocaleString("en-US"); }\n',
         '  function usdFull(n) { return "$" + Math.round(n || 0).toLocaleString("en-US"); }\n'
         '  function usdc(n) { return "$" + (n || 0).toLocaleString("en-US", '
         '{minimumFractionDigits: 2, maximumFractionDigits: 2}); }\n'),
        # a counted row can also be priced on DuckDuckGo or in review; give the eBay share
        ('num(M.counted) + " of them against a real eBay listing it could verify by photo, and " +\n',
         'num(M.counted) + " of them against a listing it could verify by photo (" +\n'
         '    pct(M.ebayN, M.counted) + " on eBay), and " +\n'),
        # a listing swapped in during review has a link but no saved photo, which the rows
        # worth checking first showed as a broken <img src="">; show a "no photo" tile
        ("  .hthumbs img {\n"
         "    width: 44px; height: 44px; object-fit: cover; border-radius: 5px;\n"
         "    background: var(--g100); border: 1px solid var(--g200);\n"
         "  }\n",
         "  .hthumbs img {\n"
         "    width: 44px; height: 44px; object-fit: cover; border-radius: 5px;\n"
         "    background: var(--g100); border: 1px solid var(--g200);\n"
         "  }\n"
         "  .hthumbs .hnoimg {\n"
         "    width: 44px; height: 44px; border-radius: 5px; display: grid; place-items: center;\n"
         "    background: var(--g100); border: 1px solid var(--g200); color: var(--g500);\n"
         "    font-family: var(--mono); font-size: 8px; line-height: 1.2; text-align: center;\n"
         "  }\n"),
        ("          '<img loading=\"lazy\" alt=\"eBay listing\" src=\"' + esc(proxy(r.li)) + '\">' +\n",
         "          (r.li ? '<img loading=\"lazy\" alt=\"eBay listing\" src=\"' + esc(proxy(r.li)) + '\">'\n"
         "                : '<span class=\"hnoimg\">no<br>photo</span>') +\n"),
        # shipping is kept on each candidate listing, and no with-shipping figure exists
        ("shipping is recorded per listing and\n"
         "        reported as a separate sensitivity, never added into the headline.",
         "shipping is recorded for each candidate\n"
         "        listing and never added into the headline."),
    ),
    "brand": (
        _CONTRIB_JS,
        # the paper's step 3: the top 10 active listings
        ("searched eBay&rsquo;s <em>sold</em> listings,", "searched eBay&rsquo;s <em>active</em> listings,"),
        ("or <b>Veto</b> if nothing\n        sold matches.", "or <b>Veto</b> if nothing\n        listed matches."),
        # three buckets, and no with-shipping figure
        ("  .how .hupgrade { color: var(--upgrade); font-weight: 600; }\n",
         "  .how .hupgrade { color: var(--upgrade); font-weight: 600; }\n"
         "  .how .hassembly { color: var(--assembly); font-weight: 600; }\n"),
        ('banked as\n'
         '        <span class="hrepair">repair</span> (spare parts) or <span class="hupgrade">upgrade</span>\n'
         '        (accessories) by its classification. Prices are the item only — no shipping\n'
         '        (the dashboard reports the with-shipping sensitivity separately).</p>',
         'banked as\n'
         '        <span class="hrepair">repair</span> (spare parts), <span class="hassembly">assembly-only</span>\n'
         '        (spare parts not sold on their own) or <span class="hupgrade">upgrade</span> (accessories)\n'
         '        by its classification. Prices are the item only — shipping is never added.</p>'),
        ("clamped at zero. Prices are the item only — shipping is reported separately. ",
         "clamped at zero. Prices are the item only — shipping is never added. "),
        # each bucket on its own, with the combined total on the overview; the review
        # report's headline and its combined_total added repair and upgrade only
        ('"<b>" + usd(s.repair + s.assembly + s.upgrade) + "</b> total · net " + fmtDelta(s.net) +',
         '"Net <b>" + fmtDelta(s.net) + "</b>" +'),
        ('        srow("Combined", usd(s.repair + s.assembly + s.upgrade) +\n'
         '          " <small>was " + usd(s.baseRepair + s.baseAssembly + s.baseUpgrade) + "</small>") +\n', ""),
        ('"<h1>Match review: <em>" + usd(s.repair + s.upgrade) + "</em> after review</h1>\\n" +',
         '"<h1>Match review: <em>" + usd(s.repair) + "</em> of repair after review</h1>\\n" +'),
        ('        card("Combined", usd(s.repair + s.assembly + s.upgrade),\n'
         '             "model said " + usd(s.baseRepair + s.baseAssembly + s.baseUpgrade)) +\n', ""),
        ("        combined_total: c(s.repair + s.upgrade),\n", ""),
        # the script has no --all flag; it works through every uncounted row by default
        ("<code>capture_candidates.py --all</code>", "<code>capture_candidates.py</code>"),
        # the pages say reviewer and checked for reviewed rows
        ("it as human-verified,", "it as checked,"),
        ("records that a human checked this row", "records that a reviewer checked this row"),
        ("are rows a human touched.", "are rows a reviewer touched."),
    ),
    "all": (
        _CONTRIB_JS,
        ("usdc(shownP ? Math.round(Math.max(shownP - r.pc, 0) * 100) / 100 : r.spp)",
         "usdc(shownP ? round2(Math.max(shownP - r.pc, 0)) : r.spp)"),
        # a listing swapped in during review has a link (surl) but no saved photo
        ('thumb(listImg(r.li), "eBay listing", "no<br>listing") +',
         'thumb(listImg(r.li), "eBay listing", r.surl ? "no<br>photo" : "no<br>listing") +'),
        (": '<div class=\"none\">no matched listing</div>') +",
         ": '<div class=\"none\">' + (swapped || r.surl ? \"no listing photo saved\" : \"no matched listing\")"
         " + \"</div>\") +"),
        # the STATE_KEY keys, the same blobs as the brand pages
        ("/api/state?key=<brand slug>, decisions keyed",
         "/api/state?key=%s-<brand slug>, decisions keyed" % STATE_KEY),
        ('var LOCAL = "tv-all-decisions-v1";', 'var LOCAL = "tv-all-decisions-%s";' % STATE_KEY),
        ('return fetch("/api/state?key=" + encodeURIComponent(slug), {cache: "no-store"})',
         'return fetch("/api/state?key=%s-" + encodeURIComponent(slug), {cache: "no-store"})' % STATE_KEY),
        ('    fetch("/api/state?key=" + encodeURIComponent(slug), {\n',
         '    fetch("/api/state?key=%s-" + encodeURIComponent(slug), {\n' % STATE_KEY),
    ),
}

# all.html ships 9k rows, so the two image URLs are stored compressed; the page
# expands them again (thumb()/ebayImg() in the template). "!" marks a URL that
# does not fit the usual shape and is stored whole.
_THUMB = re.compile(r"https://resize\.thingiverse\.com/\?url=https://cdn\.thingiverse\.com/"
                    r"(.+)&w=292&h=219&fit=contain&cbg=white&n=-1")
_EBAY_IMG = re.compile(r"https://i\.ebayimg\.com/images/g/([^/]+)/s-l500\.webp")


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "brand"


def has_pool(row) -> bool:
    return bool(row.get("cands"))


def is_counted(row) -> bool:
    return row.get("bkt") in BUCKETS


def is_priceable(row) -> bool:
    return row.get("cls") in PRICEABLE and not row.get("err")


def _compact(url, pattern):
    m = pattern.fullmatch(url)
    return m.group(1) if m else "!" + url


def _href(slug, row):
    return "b/%s.html#t%s" % (slug, row["id"])


def _short_count(n: int) -> str:
    return "%.1fM" % (n / 1e6) if n >= 1_000_000 else format(n, ",")


# ---------------------------------------------------------------- rows

def brand_row(row) -> dict:
    """A row as the brand page embeds it: every scored field, full URLs."""
    o = {k: row[k] for k in ROW_KEYS}
    if row.get("gen"):
        o["gen"] = row["gen"]
    if row.get("yrs"):
        o["yrs"] = row["yrs"]
    if has_pool(row):
        o["at"] = row.get("at") or ""
        o["now"] = int(row.get("now") or 0)
        o["cands"] = row["cands"]
    return o


def all_row(row, slug) -> dict:
    """A row as all.html embeds it: only what the list needs, falsy extras dropped."""
    o = {"i": row["id"], "t": row["t"], "bkt": row["bkt"], "p": row["p"],
         "pc": row["pc"], "spp": row["spp"], "dl": row["dl"], "tot": row["tot"],
         "url": row["url"], "surl": row["surl"], "cls": row["cls"],
         "href": _href(slug, row)}
    if row["asm"]:
        o["asm"] = 1
    o["br"] = row["br"]
    o["mdl"] = row["mdl"]
    if row["part"] and row["part"] != row["t"]:
        o["part"] = row["part"]
    if row["lt"]:
        o["lt"] = row["lt"]
    if row["q"]:
        o["q"] = row["q"]
    if has_pool(row):
        o["hp"] = 1
        if row.get("at"):
            o["at"] = row["at"]
    if row["ti"]:
        o["ti"] = _compact(row["ti"], _THUMB)
    if row["li"]:
        o["li"] = _compact(row["li"], _EBAY_IMG)
    if row["sx"]:
        o["sx"] = 1
    return o


# ---------------------------------------------------------------- meta

def core_meta(rows, generated) -> dict:
    """The 14 numbers every page carries (brand pages carry only these)."""
    counted = [r for r in rows if is_counted(r)]
    m = {
        "generated": generated,
        "things": len(rows),
        "counted": len(counted),
        "uncountedN": len(rows) - len(counted),
        "poolsN": sum(1 for r in rows if has_pool(r) and not is_counted(r)),
        "poolsCountedN": sum(1 for r in counted if has_pool(r)),
    }
    for b in BUCKETS:
        m[b + "N"] = sum(1 for r in counted if r["bkt"] == b)
    for b in BUCKETS:
        # sum([]) is the int 0, which is what the deployed pages show for empty buckets
        m[b + "Total"] = round(sum(r["tot"] for r in counted if r["bkt"] == b), 2)
    m["withBoth"] = sum(1 for r in counted if r["ti"] and r["li"])
    m["withEbay"] = sum(1 for r in counted if r["src"] == "eBay" and r["li"])
    return m


def _head_share(counted):
    rep = sorted((r["tot"] for r in counted if r["bkt"] == "repair"), reverse=True)
    total = sum(rep)
    if total <= 0:
        return 0, 0.0
    cum = 0.0
    for n, v in enumerate(rep, 1):
        cum += v
        share = round(100 * cum / total, 1)
        if share >= HEAD_SHARE:
            return n, share
    return len(rep), 100.0


def _matched(row) -> bool:
    """A listing (or, on a reviewed table, a hand-entered price) was accepted."""
    return row["src"] not in ("", "none")


def _funnel(rows, things, priceable, scale_n, counted_n, as_deployed=False):
    """The overview's funnel, as [{label, n, on, why}].

    Each stage keeps the rows of the stage above that pass one more test, so it
    only ever removes rows and it ends at the counted rows; a counted row stays
    in every stage, even one a reviewer priced by hand. Like the page's other
    figures, Priceable leaves out the rows the size and scale guards stopped
    (they carry an err).

    as_deployed rebuilds the Sep 2 funnel instead. Its stage 3 counted every
    priceable row with a query, whether or not eBay returned listings; stage 4
    counted eBay matches only, while the counted rows include 36 priced
    elsewhere; and stage 5 subtracted the scale-guard rows from stage 4, which
    had already left them out, so its 4,488 is low by 24.
    """
    if as_deployed:
        searched = sum(1 for r in rows if is_priceable(r) and r["q"])
        same_part = sum(1 for r in rows if r["src"] == "eBay" and not r["err"])
        ns = (things, priceable, searched, same_part, same_part - scale_n, counted_n)
        return [{"label": label, "n": n, "on": True, "why": why.format(n=format(scale_n, ","))}
                for (label, why), n in zip(FUNNEL_AS_DEPLOYED, ns, strict=True)]
    keep, ns = list(rows), [len(rows)]
    for test in (is_priceable, lambda r: bool(r["q"]), _matched, is_counted):
        keep = [r for r in keep if test(r) or is_counted(r)]
        ns.append(len(keep))
    return [{"label": label, "n": n, "on": True, "why": why}
            for (label, why), n in zip(FUNNEL, ns, strict=True)]


def full_meta(rows, slug_of, generated, source, as_deployed=False) -> dict:
    """Everything index.html and all.html show (both embed the same meta)."""
    m = core_meta(rows, generated)
    counted = [r for r in rows if is_counted(r)]
    m["source"] = source
    m["downloads"] = sum(r["dl"] for r in rows)
    m["priceable"] = sum(1 for r in rows if is_priceable(r))
    m["sliced"] = sum(1 for r in rows if r["ms"] in ("orcaslicer", "mixed"))
    # the median over every counted row, as the paper takes it; deployed, it left out the
    # rows with no saving
    spp = [r["spp"] for r in counted if r["spp"] > 0 or not as_deployed]
    m["medianSpp"] = round(statistics.median(spp), 2) if spp else 0
    if not as_deployed:
        m["ebayN"] = sum(1 for r in counted if r["src"] == "eBay")
    m["headN"], m["headShare"] = _head_share(counted)
    scale = [r for r in rows if r["sx"]]
    m["funnel"] = _funnel(rows, m["things"], m["priceable"], len(scale), m["counted"], as_deployed)
    m["brandsN"] = len({r["br"] for r in rows})
    m["scaleN"] = len(scale)
    m["scaleUsd"] = round(sum(r["sxu"] for r in scale), 2)
    top = sorted(counted, key=lambda r: -r["tot"])[:HEAD_SIZE]   # stable: ties keep sheet order
    m["head"] = [dict({k: r[k] for k in HEAD_KEYS if k != "href"}, href=_href(slug_of(r["br"]), r))
                 for r in top]
    return m


# ---------------------------------------------------------------- pages

def _template(name) -> str:
    return (TEMPLATES / ("%s.template.html" % name)).read_bytes().decode("utf-8")


def _swap(page, pairs, name):
    for old, new in pairs:
        n = page.count(old)
        if n != 1:
            raise ValueError("%s template: expected one %r, found %d" % (name, old, n))
        page = page.replace(old, new)
    return page


def embed(page, name, payload) -> str:
    blob = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    blob = blob.replace("</", "<\\/")   # keep a stray "</script>" in scraped text from ending the tag
    return _swap(page, [(PLACEHOLDER[name], blob)], name)


def _corrected(page, name, as_deployed):
    return page if as_deployed else _swap(page, CORRECTIONS[name], name)


def render_brand(name, slug, rows, generated, as_deployed=False) -> str:
    models = {r["mdl"] for r in rows if r["mdl"]}
    label = html.escape(name, quote=False)
    meta = core_meta(rows, generated)
    key = slug if as_deployed else "%s-%s" % (STATE_KEY, slug)
    local = "v1-" + slug if as_deployed else key
    page = _swap(_corrected(_template("brand"), "brand", as_deployed), [
        ("<title>Mazda — printable parts vs. eBay</title>",
         "<title>%s — printable parts vs. eBay</title>" % label),
        ('<div class="brand">Mazda <em>parts</em></div><div class="sub">110 things · 18 models</div>',
         '<div class="brand">%s <em>parts</em></div><div class="sub">%d things · %d models</div>'
         % (label, len(rows), len(models))),
        ("<b>56 of 110</b> rows.", "<b>%d of %d</b> rows." % (meta["counted"], meta["things"])),
        ('var STATE_URL = "/api/state?key=mazda";', 'var STATE_URL = "/api/state?key=%s";' % key),
        ('var KEY = "tv-review-decisions-v1-mazda";', 'var KEY = "tv-review-decisions-%s";' % local),
    ], "brand")
    return embed(page, "brand", {"meta": {k: meta[k] for k in BRAND_META_KEYS},
                                 "rows": [brand_row(r) for r in rows]})


def render_all(rows, slugs, meta, pool_prose_count=None, as_deployed=False) -> str:
    order = sorted(range(len(rows)), key=lambda i: (-rows[i]["tot"], i))
    reached = meta["poolsN"] if pool_prose_count is None else pool_prose_count
    page = _swap(_corrected(_template("all"), "all", as_deployed), [
        ("All <b>9,190</b> Things", "All <b>%s</b> Things" % format(meta["things"], ",")),
        ("has only reached 247 of the 4,862 uncounted rows",
         "has only reached %s of the %s uncounted rows"
         % (format(reached, ","), format(meta["uncountedN"], ","))),
    ], "all")
    return embed(page, "all", {"meta": meta,
                               "rows": [all_row(rows[i], slugs[rows[i]["br"]]) for i in order]})


def index_brand(name, slug, rows, page_bytes) -> dict:
    counted = [r for r in rows if is_counted(r)]
    models = sorted({r["mdl"] for r in rows if r["mdl"]})
    shown = ", ".join(models[:MODELS_SHOWN]) + (" …" if len(models) > MODELS_SHOWN else "")
    e = {"name": name, "href": "b/%s.html" % slug, "things": len(rows),
         "counted": len(counted), "priceable": sum(1 for r in rows if is_priceable(r)),
         "dl": sum(r["dl"] for r in rows)}
    for b in BUCKETS:
        e[b] = round(sum(r["tot"] for r in counted if r["bkt"] == b), 2)
    e["models"] = shown
    e["kb"] = round(page_bytes / 1024)
    return e


def render_index(meta, brands, as_deployed=False) -> str:
    things = format(meta["things"], ",")
    page = _swap(_corrected(_template("index"), "index", as_deployed), [
        ("Browse all 9,190 &rarr;", "Browse all %s &rarr;" % things),
        ("9,190 Things · 78 brands · 3.2M downloads", "%s Things · %d brands · %s downloads"
         % (things, meta["brandsN"], _short_count(meta["downloads"]))),
        ("How 9,190 Things become a dollar figure", "How %s Things become a dollar figure" % things),
        ("split the corpus 78 ways", "split the corpus %d ways" % meta["brandsN"]),
        ("all 9,190 rows in a single list", "all %s rows in a single list" % things),
    ], "index")
    return embed(page, "index", {"meta": meta, "brands": brands})


def assign_slugs(names):
    slugs, used = {}, set()
    for name in names:
        base = slug = slugify(name)
        n = 2
        while slug in used:
            slug, n = "%s-%d" % (base, n), n + 1
        slugs[name] = slug
        used.add(slug)
    return slugs


def build_site(rows, out_dir, *, generated, source, pool_prose_count=None, as_deployed=False) -> dict:
    """Write the whole site for rows given in sheet order. Returns a summary.

    as_deployed keeps the deployed pages' funnel and wording (see the module
    docstring); pool_prose_count pins the number in all.html's pool sentence."""
    out = Path(out_dir)
    for sub in ("b", "pools"):
        (out / sub).mkdir(parents=True, exist_ok=True)
        for old in (out / sub).iterdir():
            if old.suffix in (".html", ".json"):
                old.unlink()

    by_brand = {}
    for r in rows:
        by_brand.setdefault(r["br"], []).append(r)
    names = sorted(by_brand)
    slugs = assign_slugs(names)
    meta = full_meta(rows, slugs.__getitem__, generated, source, as_deployed)

    brands = []
    for name in names:
        data = render_brand(name, slugs[name], by_brand[name], generated, as_deployed).encode("utf-8")
        (out / "b" / (slugs[name] + ".html")).write_bytes(data)
        brands.append(index_brand(name, slugs[name], by_brand[name], len(data)))

    (out / "all.html").write_bytes(render_all(rows, slugs, meta, pool_prose_count, as_deployed).encode("utf-8"))
    (out / "index.html").write_bytes(render_index(meta, brands, as_deployed).encode("utf-8"))

    pools = 0
    for r in rows:
        if has_pool(r):
            blob = json.dumps({"cands": r["cands"]}, separators=(",", ":"), ensure_ascii=False)
            (out / "pools" / ("%s.json" % r["id"])).write_bytes(blob.encode("utf-8"))
            pools += 1
    return {"things": meta["things"], "counted": meta["counted"], "brands": len(names),
            "pools": pools, "repair": meta["repairTotal"], "assembly": meta["assemblyTotal"],
            "upgrade": meta["upgradeTotal"], "out": str(out)}
