"""Paper step 3, retrieval: the eBay search behind every price.

Each query from step 2 runs on eBay's public website (not an eBay API) with the
cookies of a signed-in browser session. The top 10 active listings are taken in
eBay's popularity order (Best Match, eBay's default sort; sold and completed
listings are not searched), each with its price, shipping charge, title, link
and photo. The pool is then put in price order and numbered, cheapest first, so
the matcher sees candidate 1 as the least expensive listing and the pipeline
takes the cheapest listing the matcher accepts.

Only US-dollar prices are read. eBay prices a page in the currency of the
session's ship-to country, so every search URL carries EBAY_EXTRA_PARAMS, by
default _fcid=1&_stpos=10001: ship to New York, ZIP 10001. That gives US-dollar
prices from Canada too, whatever the copied cookies say. A page that shows only
other currencies stops the run rather than quietly pricing nothing.

When eBay returns no listings at all, a DuckDuckGo web search is the fallback
(websearch.py).
"""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlencode

from . import net
from .config import US_SHIP_TO

SEARCH = "https://www.ebay.com/sch/i.html"
POOL_SIZE = 10
SORT_BEST_MATCH = "12"
IMAGE = "https://i.ebayimg.com/images/g/%s/s-l500.webp"
CHECK_WAITS = (5.0, 20.0)       # seconds before the 2nd and 3rd try when eBay answers with a bot check
_CHECK_URL = re.compile(r"splashui/challenge|/captcha|signin\.ebay\.")

_FOREIGN = re.compile(r"\b(?:C|CA|CAD|AU|AUD|NZ|NZD|HK|SG|SGD|MX|NT)\s*\$|\u00a3|\u20ac|\u00a5|\u20b9"
                      r"|\b(?:EUR|GBP|CHF|JPY|INR|CAD|AUD|PHP|MYR|PLN)\b", re.I)
_USD = re.compile(r"(?:US\s*)?\$\s*(\d[\d,]*(?:\.\d{1,2})?)")
_SHIP = re.compile(r"\bfree\b[^$\d]{0,24}?\b(?:delivery|shipping|postage)\b"
                   r"|\+?\s*(?:US\s*)?\$\s*(\d[\d,]*(?:\.\d+)?)\s*(?:delivery|shipping|postage)\b", re.I)
_ITEM = re.compile(r"/itm/(?:[^/?#]+/)?(\d{9,15})")
_IMG = re.compile(r"(?:thumbs/)?images/g/([^/\s\"'?]+)/s-l\d+\.\w+")
_NEW_WINDOW = re.compile(r"\s*Opens in a new window or tab\s*", re.I)
_RESULT_MARKERS = ("srp-results", "s-item", "s-card", "srp-river", "srp-save-null-search",
                   "No exact matches found")


class EbayBlocked(RuntimeError):
    """eBay challenged the session or priced the page in another currency. Fix the cookie and rerun."""


class SearchError(RuntimeError):
    """eBay failed for this one query (a server error that outlived the retries)."""


def search_url(q: str, extra: str = "") -> str:
    url = SEARCH + "?" + urlencode({"_nkw": q, "_sop": SORT_BEST_MATCH})
    extra = (extra or "").strip().lstrip("?&")
    return url + ("&" + extra if extra else "")


def parse_price(text):
    """The first US-dollar amount in a price cell ("$12.99 to $15.99" -> 12.99); None otherwise."""
    text = " ".join((text or "").split())
    if not text or _FOREIGN.search(text):
        return None
    m = _USD.search(text)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_shipping(text) -> float:
    """"+$14.13 delivery" -> 14.13; free, local pickup or not stated -> 0."""
    m = _SHIP.search(text or "")
    if not m or m.group(1) is None:
        return 0.0
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0


def item_url(href) -> str:
    m = _ITEM.search(href or "")
    return "https://www.ebay.com/itm/%s" % m.group(1) if m else ""


def clean_title(text) -> str:
    return " ".join(_NEW_WINDOW.sub(" ", text or "").split())


def image_of(item) -> str:
    for img in item.find_all("img"):
        for attr in ("src", "data-src", "data-defer-load", "srcset", "data-srcset"):
            m = _IMG.search(img.get(attr) or "")
            if m:
                return IMAGE % m.group(1)
    return ""


def _shipping_text(item, title_el) -> str:
    for el in item.select(".s-item__shipping, .s-item__logisticsCost, .s-item__freeXDays, "
                          ".s-card__attribute-row, .s-card__shipping"):
        text = el.get_text(" ", strip=True)
        if _SHIP.search(text):
            return text
    text = item.get_text(" ", strip=True)
    if title_el is not None:
        text = text.replace(title_el.get_text(" ", strip=True), " ")
    return text


def parse_results(page) -> list:
    """Listings on a search results page, in page order: [{p, s, t, u, im}].

    Handles both result layouts eBay serves (li.s-item and the newer li.s-card).
    p is None when the price is not in US dollars. Results after eBay's
    "Results matching fewer words" divider are left out.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page or "", "html.parser")
    out, seen, taken = [], set(), set()
    for li in soup.select("li.s-item, li.s-card, li[data-listingid], li[class*=srp-river-answer]"):
        classes = " ".join(li.get("class") or [])
        if "REWRITE_START" in classes:
            break
        if "srp-river-answer" in classes:
            continue
        if any(id(parent) in taken for parent in li.parents):
            continue
        link = li.select_one('a[href*="/itm/"]')
        url = item_url(link.get("href") if link else "")
        if not url and (li.get("data-listingid") or "").isdigit():
            url = "https://www.ebay.com/itm/%s" % li["data-listingid"]
        if not url or url in seen:
            continue
        title_el = li.select_one(".s-item__title, .s-card__title, [role=heading]")
        title = clean_title(title_el.get_text(" ", strip=True) if title_el else "")
        if not title or title.lower().startswith("shop on ebay"):
            continue
        price_el = li.select_one(".s-item__price, .s-card__price")
        seen.add(url)
        taken.add(id(li))
        out.append({
            "p": parse_price(price_el.get_text(" ", strip=True) if price_el else ""),
            "s": parse_shipping(_shipping_text(li, title_el)),
            "t": title,
            "u": url,
            "im": image_of(li),
        })
    return out


def make_pool(listings, size: int = POOL_SIZE) -> list:
    """The first size US-dollar listings in eBay's order, then cheapest first, numbered from 1."""
    usd = [x for x in listings if x.get("p")][:size]
    usd = sorted(usd, key=lambda x: x["p"])
    return [{"i": i, "p": round(float(x["p"]), 2), "s": round(float(x.get("s") or 0.0), 2),
             "t": x["t"], "u": x["u"], "im": x.get("im") or ""}
            for i, x in enumerate(usd, 1)]


def challenged(r, url) -> bool:
    """True when eBay answered with a bot check or a sign-in wall instead of the search."""
    final = str(getattr(r, "url", "") or url)
    head = r.text[:6000] if r.status_code in (200, 403) else ""
    return (r.status_code == 403 or bool(_CHECK_URL.search(final))
            or "Pardon Our Interruption" in head or "Security Measure" in head
            or "<title>Error Page | eBay" in head)


def unblock_advice() -> str:
    if net.client() != "curl_cffi":
        return ("Install curl_cffi (pip install -r requirements.txt): eBay challenges Python's own HTTP "
                "client whatever the cookies.")
    return ("Open ebay.com in the browser whose cookies you copied, solve any check, then copy a fresh "
            "Cookie header into EBAY_COOKIE and that browser's user agent into BROWSER_USER_AGENT.")


class Ebay:
    def __init__(self, settings):
        self.s = settings
        self._local = threading.local()
        self.check_waits = CHECK_WAITS

    def session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = net.session(self.s.user_agent, self.s.ebay_cookie, domain="ebay.com")
            self._local.session = s
        return s

    def fetch(self, q):
        """The search page for q.

        A bot check is tried again on a fresh connection after a pause, as a
        person would reload the page. One that outlasts the retries stops the
        run (EbayBlocked).
        """
        url = search_url(q, self.s.ebay_params)
        for attempt in range(len(self.check_waits) + 1):
            r = net.get(self.session(), url, delay=self.s.http_delay, timeout=45,
                        headers={"referer": "https://www.ebay.com/"})
            if not challenged(r, url):
                break
            if attempt == len(self.check_waits):
                final = str(getattr(r, "url", "") or url)
                raise EbayBlocked("eBay blocked the search (HTTP %d at %s, %d tries). %s"
                                  % (r.status_code, final[:120], attempt + 1, unblock_advice()))
            old = getattr(self._local, "session", None)
            self._local.session = None
            if old is not None and hasattr(old, "close"):
                old.close()
            time.sleep(self.check_waits[attempt])
        if r.status_code != 200:
            raise SearchError("eBay answered HTTP %d for %r" % (r.status_code, q))
        return r.text

    def search(self, q) -> list:
        """Listings for q in eBay's order (see parse_results)."""
        page = self.fetch(q)
        listings = parse_results(page)
        if not listings and not any(m in page for m in _RESULT_MARKERS):
            raise EbayBlocked("eBay returned a page that is not a search result for %r; check the "
                              "cookie with script/doctor.py" % q)
        if len(listings) >= 3 and not any(x["p"] for x in listings):
            raise EbayBlocked(
                "eBay showed prices in another currency for %r. The study uses US-dollar prices: leave "
                "EBAY_EXTRA_PARAMS empty for the US ship-to default (%s), or set eBay's ship-to to a US "
                "ZIP code in that browser and copy the cookies again." % (q, US_SHIP_TO))
        return listings
