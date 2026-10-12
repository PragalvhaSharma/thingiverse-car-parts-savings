"""HTTP helpers shared by the website collectors (Thingiverse, eBay, DuckDuckGo).

Cloudflare, in front of thingiverse.com, and eBay's bot manager look at the TLS
handshake as well as the headers. They challenge Python's own HTTP client and
curl even when a request carries the signed-in browser's cookies and user agent:
on 12 Oct 2026 every such request for a Thing page got Cloudflare's "Just a
moment..." page, and eBay sent searches to its "Pardon Our Interruption" check.
curl_cffi makes the same requests with Chrome's handshake, and both sites then
serve their pages. session() uses curl_cffi when it is installed and requests
otherwise.
"""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlparse

IMPERSONATE = "chrome"          # curl_cffi's newest Chrome fingerprint

_next_slot = {}
_lock = threading.Lock()


def pace(host: str, delay: float) -> None:
    """Keep requests to one host at least delay seconds apart, across threads."""
    if delay <= 0:
        return
    with _lock:
        now = time.monotonic()
        start = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = start + delay
    if start > now:
        time.sleep(start - now)


def client() -> str:
    """"curl_cffi" when sessions use Chrome's TLS handshake, "requests" otherwise."""
    try:
        import curl_cffi
    except ImportError:
        return "requests"
    return curl_cffi.__name__


def errors() -> tuple:
    """The exception classes a failed request raises, for the installed clients."""
    out = []
    try:
        import requests
        out.append(requests.RequestException)
    except ImportError:
        pass
    try:
        from curl_cffi import CurlError
        out.append(CurlError)
    except ImportError:
        pass
    return tuple(out)


def browser_headers(user_agent: str) -> dict:
    """The headers Chrome sends when it opens a page of the site it is on.

    Chrome adds its client hints (sec-ch-ua...) to every request, so they are
    derived from user_agent to agree with it.
    """
    h = {
        "user-agent": user_agent,
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
                  "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "en-US,en;q=0.9",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "priority": "u=0, i",
    }
    m = re.search(r"\bChrome/(\d+)", user_agent or "")
    if m and not re.search(r"\b(?:Edg|OPR)/", user_agent):
        h["sec-ch-ua"] = '"Chromium";v="{0}", "Google Chrome";v="{0}", "Not A(Brand";v="99"'.format(m.group(1))
        h["sec-ch-ua-mobile"] = "?1" if "Mobile" in user_agent else "?0"
        for marker, platform in (("Android", "Android"), ("Macintosh", "macOS"), ("Windows", "Windows"),
                                 ("Linux", "Linux")):
            if marker in user_agent:
                h["sec-ch-ua-platform"] = '"%s"' % platform
                break
    return h


def parse_cookie(cookie: str) -> list:
    """[(name, value)] from a Cookie request header, in order."""
    out = []
    for part in (cookie or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            out.append((name.strip(), value.strip()))
    return out


def session(user_agent: str, cookie: str = "", headers=None, *, domain: str = ""):
    """A session that presents itself as the browser the cookies came from.

    cookie is that browser's Cookie header. With domain ("thingiverse.com") its
    cookies go into the session's cookie jar for that site: they are sent to the
    site and its subdomains only, and the ones the site refreshes are replaced.
    Without domain the header goes out as it is on every request.
    """
    try:
        from curl_cffi import requests as curl_requests
        s = curl_requests.Session(impersonate=IMPERSONATE)
    except ImportError:
        import requests
        s = requests.Session()
    s.headers.update(browser_headers(user_agent))
    if headers:
        s.headers.update(headers)
    if cookie and domain:
        for name, value in parse_cookie(cookie):
            s.cookies.set(name, value, domain="." + domain.lstrip("."), path="/")
    elif cookie:
        s.headers["cookie"] = cookie
    return s


def get(s, url: str, *, delay: float = 0.0, tries: int = 3, timeout: float = 30, **kw):
    """GET with per-host pacing, retrying connection errors, 429 and 5xx."""
    host = urlparse(url).netloc
    failed = errors()
    for attempt in range(tries):
        pace(host, delay)
        try:
            r = s.get(url, timeout=timeout, **kw)
        except failed:
            if attempt == tries - 1:
                raise
            time.sleep(3 * 2 ** attempt)
            continue
        if (r.status_code == 429 or r.status_code >= 500) and attempt < tries - 1:
            wait = r.headers.get("retry-after", "")
            time.sleep(min(float(wait), 120) if wait.isdigit() else 5 * 2 ** attempt)
            continue
        return r
    return r


def is_cloudflare_challenge(r) -> bool:
    """True when Cloudflare answered with its "Just a moment..." interstitial."""
    if r.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if r.status_code in (403, 503):
        head = r.text[:4000]
        return "Just a moment" in head or "challenge-platform" in head or "cf-chl" in head
    return False
