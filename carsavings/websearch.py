"""The fallback source: a DuckDuckGo web search (paper step 3; 38 Sep 2 rows).

Used only when eBay returns no listings for a query. DuckDuckGo's HTML endpoint
is searched for "<query> buy price" and a US-dollar price is read from each
result's title and snippet. The results carry no photos, so the matcher judges
them from their titles and snippets, and they are not kept as a review pool.

The query is posted the way DuckDuckGo's own search form posts it, through the
system's curl when one is installed. DuckDuckGo answers the GET form of the URL
with a 202 bot check every time, and most requests from Python's HTTP client the
same way, while it serves curl's.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import threading
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from . import net

DDG = "https://html.duckduckgo.com/html/"
DDG_HEADERS = {"referer": "https://html.duckduckgo.com/", "accept-language": "en-US,en;q=0.9"}
POOL_SIZE = 10

_ANCHOR = re.compile(r"<a[^>]+class=\"[^\"]*\bresult__a\b[^\"]*\"[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>",
                     re.I | re.S)
_SNIPPETS = tuple(re.compile(r"class=\"[^\"]*\bresult__snippet\b[^\"]*\"[^>]*>(.*?)</%s>" % tag, re.I | re.S)
                  for tag in ("a", "div", "span"))
_FOREIGN_DOLLAR = re.compile(r"\b(?:C|CA|CAD|AU|AUD|NZ|NZD|HK|SG|SGD)\s*\$\s*[-+]?\d[\d,]*(?:\.\d+)?", re.I)
_PRICE = re.compile(r"(?:US\s*)?\$\s*([-+]?\d[\d,]*(?:\.\d+)?)|([-+]?\d[\d,]*(?:\.\d+)?)\s*(?:USD|US dollars?)",
                    re.I)


def clean_html(value: str) -> str:
    value = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", value or "", flags=re.I | re.S)
    value = html.unescape(re.sub(r"<[^>]+>", " ", value))
    return " ".join(value.split())


def decode_url(value: str) -> str:
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<target>."""
    url = html.unescape(value or "")
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://duckduckgo.com" + url
    target = parse_qs(urlparse(url).query).get("uddg")
    return unquote(target[0]) if target else url


def host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def usd_price(text: str) -> float:
    cleaned = _FOREIGN_DOLLAR.sub(" ", (text or "").replace(",", ""))
    m = _PRICE.search(cleaned)
    if not m:
        return 0.0
    raw = next((g for g in m.groups() if g), "")
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if value > 0 else 0.0


def parse_results(page: str, limit: int = POOL_SIZE) -> list:
    """Priced results in DuckDuckGo's order: [{p, s, t, u, im, snip}]."""
    anchors = list(_ANCHOR.finditer(page or ""))
    out = []
    for n, m in enumerate(anchors):
        if len(out) >= limit:
            break
        title = clean_html(m.group(2))
        url = decode_url(m.group(1))
        if not title or not url.startswith("http") or "duckduckgo.com" in host(url):
            continue
        end = anchors[n + 1].start() if n + 1 < len(anchors) else m.end() + 2500
        chunk = page[m.end():end]
        snippet = ""
        for pat in _SNIPPETS:
            s = pat.search(chunk)
            if s:
                snippet = clean_html(s.group(1))
                break
        price = usd_price(title + " " + snippet)
        if price <= 0:
            continue
        out.append({"p": round(price, 2), "s": 0.0, "t": title, "u": url, "im": "", "snip": snippet[:300]})
    return out


def make_pool(results) -> list:
    ranked = sorted(results[:POOL_SIZE], key=lambda x: x["p"])
    return [dict(x, i=i) for i, x in enumerate(ranked, 1)]


class WebSearch:
    def __init__(self, settings):
        self.s = settings
        self._local = threading.local()
        self._warned = False

    def session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = net.session(self.s.user_agent)
            self._local.session = s
        return s

    def search(self, q) -> list:
        """A numbered, cheapest-first pool of priced web results.

        [] when DuckDuckGo answered with nothing priced; None when it could not
        be reached or answered with a bot check, so the caller does not cache
        the query as having no listing.
        """
        status, page = self.fetch(q)
        if status == 200 and "result__a" in page:
            return make_pool(parse_results(page))
        if status == 200 and ("no-results" in page or "No results." in page):
            return []
        self._warn(page if status == 0 else
                   "HTTP %d%s" % (status, "" if status != 200 else ", not a result page"))
        return None

    def fetch(self, q):
        """(HTTP status, page) of DuckDuckGo's results for q; (0, reason) when unreachable."""
        form = {"q": q[:100] + " buy price", "kl": "us-en", "b": ""}
        net.pace(urlparse(DDG).netloc, max(self.s.http_delay, 2.0))
        curl = shutil.which("curl")
        if curl:
            cmd = [curl, "-sS", "--max-time", "30", "-A", self.s.user_agent, "--data", urlencode(form),
                   "-w", "\n%{http_code}"]
            for k, v in DDG_HEADERS.items():
                cmd += ["-H", "%s: %s" % (k, v)]
            try:
                done = subprocess.run(cmd + [DDG], capture_output=True, timeout=60, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                return 0, "curl: %s" % exc
            page, _, code = done.stdout.decode("utf-8", "replace").rpartition("\n")
            if done.returncode or not code.strip().isdigit():
                return 0, "curl exit %d: %s" % (done.returncode, done.stderr.decode("utf-8", "replace").strip()[:200])
            return int(code), page
        try:
            r = self.session().post(DDG, data=form, headers=DDG_HEADERS, timeout=30)
        except net.errors() as exc:
            return 0, str(exc)
        return r.status_code, r.text

    def _warn(self, why):
        if not self._warned:
            self._warned = True
            print("warning: DuckDuckGo fallback unavailable (%s); rows with no eBay listing stay "
                  "unmatched for this run and are retried on the next one" % why, file=sys.stderr)
