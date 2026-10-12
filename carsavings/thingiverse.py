"""Harvest: the text, images, 3-D files and download count of each Thing.

Paper §2: the Things were collected from the Thingiverse website, not through
the Thingiverse API, using the cookies of a signed-in browser session. The
website's own front end loads a Thing from its JSON routes, and this module
calls the same routes with the same headers (cookie, bearer, referer, agent):

    /api/v2/auth/view               the bearer the site gives a visitor who is not
                                    signed in: a JWT that lasts about a day
    /api/v2/things/<id>/complete    name, creator, description, tags, print
                                    settings, default image, and zip_data: the CDN
                                    links of every model file and photo

The site's current front end (a Next.js app, as of Oct 2026) builds the
"download all files" zip in the browser from zip_data, and its /thing:<id>/zip
route now redirects to the Files page. Its older routes (/api/things/<id>, its
/files and /images) answer only a signed-in user's bearer, THINGIVERSE_WEB_TOKEN;
they are used for download counts when the input sheet has none.

When the JSON route refuses the request, the Thing page's og tags still give the
name, creator, image and summary, and the files come from the zip route if it
serves one. A Thing that has been deleted or made private is served with
Thingiverse's default og tags instead of its own; that row is kept with
err=OG_ERROR and counts $0, as in the Sep 2 run.

A Cloudflare challenge is never recorded as data. It raises ThingiverseBlocked,
which stops the run and asks for a fresh THINGIVERSE_COOKIE. Requests go out
with Chrome's TLS handshake (net.py): Cloudflare challenges Python's own HTTP
client even when it carries a valid cf_clearance cookie.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import html
import io
import json
import re
import sys
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from . import net
from .savings import OG_ERROR
from .schema import thing_url

SITE = "https://www.thingiverse.com"
DEV_API = "https://api.thingiverse.com"
VIEW_TOKEN = "/api/v2/auth/view"
TOKEN_MARGIN = 300              # seconds: a bearer this close to expiry is replaced first
# The front end's own test for a 401 that is about the bearer rather than the Thing
TOKEN_ERRORS = frozenset({"INVALID_ACCESS_TOKEN", "EXPIRED_ACCESS_TOKEN", "NO_TOKEN_PROVIDED"})
DEFAULT_OG_TITLE = "Thingiverse - The community for Open Hardware"
GEARS = "https://cdn.thingiverse.com/site/img/default/Gears_preview_card.webp"
THUMB = "https://resize.thingiverse.com/?url=%s&w=292&h=219&fit=contain&cbg=white&n=-1"
# The site's own image resizer (Cloudflare). Gallery originals run to several megabytes,
# and the model is sent at most 1,024 pixels a side anyway (llm.MAX_SIDE).
PHOTO = "https://img.thingiverse.com/cdn-cgi/image/fit=scale-down,width=1024,height=1024/%s"
_DIRECT_FORMATS = (".webp", ".gif")  # linked as they are, never through the resizer
MODEL_EXTS = (".stl", ".3mf", ".obj")
MAX_IMAGES = 6
# The site's own scripts call its JSON routes with fetch(), which sends these in place of a
# page load's headers (None drops the session's page-load header).
JSON_HEADERS = {"accept": "application/json, text/plain, */*", "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty", "priority": "u=1, i", "sec-fetch-user": None,
                "upgrade-insecure-requests": None}

_META = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*("([^"]*)"|'([^']*)')""")
_TAGS = (re.compile(r"<[^>]+>"), re.compile(r"</?[A-Za-z][^<>]*>"))
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
# A number, or a range of them ("15-20", "15% to 20").
_NUM = r"\d{1,3}(?:\.\d+)?(?!\d)"
_SPAN = r"(?<![\d.])" + _NUM + r"(?:[ \t]*%?[ \t]*(?:-|–|—|to)[ \t]*" + _NUM + r")?"
_PERCENT = re.compile("(" + _SPAN + r")[ \t]*(%?)")
# The infill in a write-up, in three forms:
#   "Infill: 20%" or "infill of 15-20 %", with no comma or full stop between, so "any infill,
#   scale 100%" names no infill;
#   "Infill" alone on a line with the value on the next, as a print-settings heading reads
#   once the tags are stripped;
#   the other way round, "15% infill" or "20% gyroid infill" (Kia EV6 knob, 7186295, says it
#   in its print notes), unless that "infill" labels a value of its own ("Scale 100% Infill 20%").
# Only the heading form reads across a line break, so "Scale: 100%" above "Infill: 25%" stays
# the scale.
_INFILL_TEXT = re.compile(
    r"\binfill\b[^0-9\n.,;!?]{0,24}?(" + _SPAN + r"[ \t]*%)"
    r"|\binfill\b[ \t]*:?[ \t]*\n[ \t]*(" + _SPAN + r"[ \t]*%)"
    r"|(" + _SPAN + r"[ \t]*%)[ \t]*(?:[a-z-]+[ \t]+)?infill\b(?![ \t]*[:=]|[ \t]*\d)", re.I)


class ThingiverseBlocked(RuntimeError):
    """Cloudflare or an expired session stopped a request. Refresh the cookie and rerun."""


class HarvestError(RuntimeError):
    """Thingiverse answered, but not with this Thing (a server error that outlived the retries)."""


def unblock_advice() -> str:
    if net.client() != "curl_cffi":
        return ("Install curl_cffi (pip install -r requirements.txt): Cloudflare challenges Python's own "
                "HTTP client even when it carries valid cookies.")
    return ("Copy a fresh Cookie header (it must contain cf_clearance) from a signed-in Chrome tab on "
            "thingiverse.com into THINGIVERSE_COOKIE, and that browser's user agent into BROWSER_USER_AGENT.")


# ---------------------------------------------------------------- pure helpers

def strip_html(text: str) -> str:
    """Plain text, one paragraph a line. The current site escapes some sections of a
    write-up twice (&lt;p&gt;), so the tags that unescaping reveals are stripped too."""
    text = text or ""
    for tags in _TAGS:
        text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", text)
        text = html.unescape(tags.sub(" ", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def jwt_exp(token: str):
    """The exp claim (epoch seconds) of a JWT, unverified; None for opaque tokens."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data["exp"]) if data.get("exp") is not None else None
    except (IndexError, KeyError, ValueError, TypeError, binascii.Error):
        return None


def is_default_title(title: str) -> bool:
    t = " ".join((title or "").split())
    return t == DEFAULT_OG_TITLE or t.startswith("Thingiverse - ")


def meta_tags(page: str) -> dict:
    """{property-or-name: content} for the page's meta tags, first occurrence wins."""
    out = {}
    for tag in _META.findall(page or ""):
        attrs = {}
        for m in _ATTR.finditer(tag):
            attrs[m.group(1).lower()] = html.unescape(m.group(3) if m.group(3) is not None else m.group(4))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in out:
            out[key] = attrs["content"].strip()
    return out


def parse_thing_page(page: str) -> dict:
    """Name, creator, image and summary from a Thing page's og tags.

    og:title reads "<name> by <creator>", so the creator is whatever follows the
    last " by " (a name that itself contains " by " keeps it, as on Sep 2).
    """
    meta = meta_tags(page)
    title = meta.get("og:title", "")
    if not title:
        m = _TITLE.search(page or "")
        title = " ".join(html.unescape(m.group(1)).split()) if m else ""
    out = {"default": not title or is_default_title(title), "t": "", "by": "",
           "image": meta.get("og:image", ""),
           "desc": meta.get("og:description") or meta.get("description") or ""}
    if out["default"]:
        return out
    name = re.sub(r"\s*[-|]\s*Thingiverse\s*$", "", title).strip()
    if " by " in name:
        name, by = name.rsplit(" by ", 1)
        out["by"] = by.strip()
    out["t"] = name.strip()
    return out


def thumb_url(image: str) -> str:
    """The review pages' thumbnail: Thingiverse's resizer over the CDN image.

    This matches every ti on Sep 2. Most rows (8,612) wrap the CDN photo in the
    resizer at card size. An og:image is already a resizer URL (628x472) and is
    kept as served (392 rows). Thingiverse's placeholders (cdn.../site/..., 112
    rows) and .webp/.gif photos (18 rows) are linked directly.
    """
    if not image:
        return ""
    if image.startswith("https://cdn.thingiverse.com/site/"):
        return image
    if image.split("?", 1)[0].lower().endswith(_DIRECT_FORMATS):
        return image
    if image.startswith("https://cdn.thingiverse.com/"):
        return THUMB % image
    return image


def photo_url(image: str) -> str:
    """A CDN photo through the site's resizer, at most 1,024 pixels a side.

    Like the site itself, GIFs and Thingiverse's placeholders are left as they are.
    """
    if not image.startswith("https://cdn.thingiverse.com/") or image.startswith("https://cdn.thingiverse.com/site/"):
        return image
    if image.split("?", 1)[0].lower().endswith(".gif"):
        return image
    return PHOTO % image


def token_refused(r) -> bool:
    """True when a 401 is about the bearer itself, False when it is about this Thing."""
    try:
        d = r.json()
    except ValueError:
        return True
    kind = d.get("type") if isinstance(d, dict) else None
    return kind in TOKEN_ERRORS if kind else True


def _percent(value):
    """An infill as OrcaSlicer wants it: 20, '20', '20 %' -> '20%'.

    A range is read at its top, '15-20%' -> '20%': more infill is more plastic, so
    the savings err low. A number marked % wins over a bare one ('3 walls, 20%').
    Anything else, or a value outside 0-100, -> ''.
    """
    if value is None or isinstance(value, bool):
        return ""
    found = list(_PERCENT.finditer(str(value)))
    if not found:
        return ""
    m = next((m for m in found if m.group(2)), found[0])
    n = max(float(x) for x in re.findall(_NUM, m.group(1)))
    if not 0 <= n <= 100:
        return ""
    return "%s%%" % (int(n) if n == int(n) else n)


def find_infill(details_parts=None, *texts) -> str:
    """The infill recommended on the Thing page, as OrcaSlicer wants it ('20%'), or ''
    when the page names none and the slicer's own setting stands.

    The page's print-settings block arrives as details_parts (a nested list with
    an "infill" field); older Things only mention it in their write-up. texts are
    searched in the order given and should be plain text (strip_html), since a tag
    between "Infill" and its value hides the value.
    """
    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and k.lower() in ("infill", "infill_density", "infill_percentage"):
                    p = _percent(v)
                    if p:
                        return p
            for v in node.values():
                p = walk(v)
                if p:
                    return p
        elif isinstance(node, list):
            for v in node:
                p = walk(v)
                if p:
                    return p
        return ""

    found = walk(details_parts) if details_parts else ""
    if found:
        return found
    for text in texts:
        for m in _INFILL_TEXT.finditer(text or ""):
            p = _percent(m.group(1) or m.group(2) or m.group(3))
            if p:
                return p
    return ""


def file_entries(items) -> list:
    """[{name, size, urls}] from a Thing's file list.

    The current site lists the files in zip_data as {name, url}, url being the
    CDN copy its Download button fetches. The older /files route gives direct_url
    (the CDN copy, no session needed), public_url (the website's download route)
    and download_url (the developer API's, its token); its "url" is the API's JSON
    description of the file, so a url on api.thingiverse.com is not a download.
    """
    out = []
    for f in items or []:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        urls = []
        for k in ("direct_url", "public_url", "download_url", "url"):
            u = f.get(k)
            if not (isinstance(u, str) and u.startswith("http")) or u in urls:
                continue
            if k == "url" and urlparse(u).netloc == "api.thingiverse.com":
                continue
            urls.append(u)
        try:
            size = int(f.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        out.append({"name": name, "size": size, "urls": urls})
    return out


def image_urls(items) -> list:
    """Full-size photo URLs from the images route (largest display variant first)."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            url = ""
            sizes = item.get("sizes") if isinstance(item.get("sizes"), list) else []
            for want in (("display", "large"), ("display", "medium"), ("preview", "large")):
                for s in sizes:
                    if isinstance(s, dict) and (s.get("type"), s.get("size")) == want and s.get("url"):
                        url = s["url"]
                        break
                if url:
                    break
            url = url or item.get("url") or ""
        else:
            url = ""
        if url.startswith("http") and url not in out and "/site/img/default/" not in url:
            out.append(url)
    return out


def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return name or "model.stl"


def is_model_name(name: str) -> bool:
    return Path(name or "").suffix.lower() in MODEL_EXTS


def _warn(msg):
    print("warning: " + msg, file=sys.stderr)


# ---------------------------------------------------------------- the website

class Thingiverse:
    def __init__(self, settings):
        self.s = settings
        self._local = threading.local()
        self._api_refused = False
        self._user_refused = False
        self._lock = threading.Lock()
        self._view = ("", 0.0)          # (view token, its expiry)
        self._view_failed = False
        self._token_lock = threading.Lock()

    # sessions are per thread; neither client's Session is meant to be shared
    def session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = net.session(self.s.user_agent, self.s.thingiverse_cookie, domain="thingiverse.com")
            self._local.session = s
        return s

    def check_token(self):
        tok = self.s.thingiverse_web_token
        exp = jwt_exp(tok) if tok else None
        if exp is not None and exp < time.time() + 10:
            raise ThingiverseBlocked(
                "THINGIVERSE_WEB_TOKEN expired at %s. Copy a fresh one (DevTools > Network > any "
                "/api/ request > Authorization header, without 'Bearer ') or leave it empty to use "
                "the page and zip fallback." % datetime.datetime.fromtimestamp(exp).isoformat(" ", "minutes"))

    def fetch(self, url, *, referer=SITE + "/", json_api=False, bearer="", timeout=60, **kw):
        headers = {"referer": referer}
        if json_api:
            headers.update(JSON_HEADERS)
        if bearer:
            headers["authorization"] = "Bearer " + bearer
        r = net.get(self.session(), url, delay=self.s.http_delay, headers=headers, timeout=timeout, **kw)
        if net.is_cloudflare_challenge(r):
            raise ThingiverseBlocked("Cloudflare challenged %s. %s" % (url, unblock_advice()))
        return r

    def view_token(self, refused="") -> str:
        """The bearer the website gives every visitor who is not signed in.

        The site's front end asks /api/v2/auth/view for one before its first API
        call. It is a JWT that lasts about a day, so a long run asks again as it
        nears expiry, or when the API refuses it (refused: the token it refused).
        "" when the site will not issue one.
        """
        with self._token_lock:
            tok, exp = self._view
            if refused and tok == refused:
                tok, exp = self._view = ("", 0.0)
            if tok and exp > time.time() + TOKEN_MARGIN:
                return tok
            if self._view_failed:
                return ""
            r = self.fetch(SITE + VIEW_TOKEN, json_api=True)
            tok = ""
            if r.status_code == 200:
                try:
                    d = r.json()
                except ValueError:
                    d = None
                tok = d.get("access") if isinstance(d, dict) else ""
            if not isinstance(tok, str) or not tok:
                self._view_failed = True
                _warn("thingiverse.com issued no view token (HTTP %d from %s), so its API cannot be read; "
                      "using the Thing pages' og tags instead (short descriptions, one photo, no print "
                      "settings, and often no model files)." % (r.status_code, VIEW_TOKEN))
                return ""
            exp = jwt_exp(tok)
            self._view = (tok, exp if exp is not None else time.time() + 3600)
            return tok

    def api(self, path, thing_id, params=None, *, signed_in=False):
        """JSON from the website's API route, or None when it refuses or has no such Thing.

        The bearer is the signed-in user's THINGIVERSE_WEB_TOKEN when it is set,
        and otherwise the view token the site gives every visitor. signed_in marks
        the older routes (/api/things/<id>...), which answer only a signed-in user:
        without THINGIVERSE_WEB_TOKEN they are not called.
        """
        if self._api_refused:
            return None
        user = "" if self._user_refused else self.s.thingiverse_web_token
        if signed_in and not user:
            return None
        url, referer = SITE + path, thing_url(thing_id)
        if user:
            self.check_token()
            r = self.fetch(url, referer=referer, json_api=True, bearer=user, params=params)
            if r.status_code == 401 and token_refused(r):
                with self._lock:
                    if not self._user_refused:
                        self._user_refused = True
                        _warn("thingiverse.com/api refused THINGIVERSE_WEB_TOKEN (HTTP 401); using the view "
                              "token the site gives every visitor instead. Copy a fresh token for download "
                              "counts on sheets without them.")
                return self.api(path, thing_id, params, signed_in=signed_in)
        else:
            tok = self.view_token()
            r = self.fetch(url, referer=referer, json_api=True, bearer=tok, params=params)
            if r.status_code == 401 and tok and token_refused(r):
                r = self.fetch(url, referer=referer, json_api=True, bearer=self.view_token(refused=tok),
                               params=params)
            if r.status_code == 401 and token_refused(r):
                with self._lock:
                    if not self._api_refused:
                        self._api_refused = True
                        if not self._view_failed:       # view_token() has already said why
                            _warn("thingiverse.com/api refused the view token (HTTP 401); using the Thing "
                                  "pages' og tags instead (short descriptions, one photo, no print settings, "
                                  "and often no model files).")
                return None
        if r.status_code in (401, 403, 404, 410):
            return None
        if r.status_code != 200:
            raise HarvestError("HTTP %d from %s" % (r.status_code, path))
        try:
            return r.json()
        except ValueError:
            return None

    # ------------------------------------------------------------ one Thing

    def harvest(self, thing_id, need_downloads=True, zip_fallback=True) -> dict:
        """Everything the later steps need from one Thing, as a JSON-able dict.

        h["at"] is the harvest date (kept for the audit trail; it is not the
        candidate-pool date that the output's at column holds). When the file
        list is unavailable, the "download all files" zip is tried instead
        (zip_fallback) so the classifier still sees the file names and the
        slicer has the files. Photos go through the site's resizer (photo_url).
        """
        tid = str(thing_id)
        h = {"id": tid, "ok": True, "err": "", "via": "", "t": "", "by": "", "desc": "",
             "tags": [], "ti": "", "images": [], "files": [], "files_via": "", "infill": "",
             "dl": None, "zip_tried": False, "at": datetime.date.today().isoformat()}

        complete = self.api("/api/v2/things/%s/complete" % tid, tid)
        if isinstance(complete, dict) and complete.get("name"):
            self._from_complete(h, complete)
            zd = complete.get("zip_data") if isinstance(complete.get("zip_data"), dict) else {}
            gallery = zd.get("images")
            if not isinstance(gallery, list):
                gallery = self.api("/api/things/%s/images" % tid, tid, signed_in=True,
                                   params={"type": "display", "size": "large"})
            for url in image_urls(gallery if isinstance(gallery, list) else []):
                url = photo_url(url)
                if url not in h["images"]:
                    h["images"].append(url)
            files = zd.get("files")
            if not isinstance(files, list):
                files = complete.get("files")
            if not isinstance(files, list):
                files = self.api("/api/things/%s/files" % tid, tid, signed_in=True)
            h["files"] = file_entries(files if isinstance(files, list) else [])
            h["files_via"] = "api" if h["files"] else ""
        else:
            r = self.fetch(thing_url(tid))
            og = parse_thing_page(r.text) if r.status_code == 200 else {"default": True}
            if og.get("default") or not og.get("t"):
                h.update(ok=False, err=OG_ERROR, via="page")
                return h
            h.update(via="page", t=og["t"], by=og["by"], desc=strip_html(og["desc"]))
            image = og.get("image") or GEARS
            h["ti"] = thumb_url(image)
            if "/site/img/default/" not in image:
                h["images"].append(image)
            h["infill"] = find_infill(None, h["desc"])
            files = self.api("/api/things/%s/files" % tid, tid, signed_in=True)
            h["files"] = file_entries(files if isinstance(files, list) else [])
            h["files_via"] = "api" if h["files"] else ""

        h["images"] = h["images"][:MAX_IMAGES]
        if zip_fallback and not h["files"]:
            self.zip_files(h)
        if need_downloads and h["dl"] is None:
            h["dl"] = self._download_count(tid)
        return h

    def zip_files(self, h) -> list:
        """Fill h["files"] from the Thing's zip (cache/files/<id>/), once per harvest."""
        if h.get("zip_tried"):
            return h["files"]
        h["zip_tried"] = True
        got = self.download_zip(h["id"], Path(self.s.cache_dir) / "files" / h["id"])
        if got:
            h["files"] = [{"name": f["name"], "size": f["size"], "urls": [], "path": f["path"]} for f in got]
            h["files_via"] = "zip"
        return h["files"]

    def _from_complete(self, h, d):
        creator = d.get("creator") if isinstance(d.get("creator"), dict) else {}
        h["via"] = "api"
        h["t"] = " ".join(str(d.get("name") or "").split())
        h["by"] = str(creator.get("name") or "")
        desc = d.get("description_html") or d.get("description") or ""
        details = d.get("details") or ""
        summary, sections = strip_html(desc), strip_html(details)
        # details is the whole write-up (Summary, Print Settings, How I Designed This...),
        # and on the current site its Summary is the description itself
        if sections and " ".join(summary.split()) in " ".join(sections.split()):
            h["desc"] = sections
        else:
            h["desc"] = (summary + "\n" + sections).strip()
        h["tags"] = [t.get("name", "") if isinstance(t, dict) else str(t) for t in (d.get("tags") or [])][:15]
        h["infill"] = find_infill(d.get("details_parts"), sections, summary)
        default = d.get("default_image") if isinstance(d.get("default_image"), dict) else {}
        image = default.get("url") or d.get("preview_image") or d.get("thumbnail") or ""
        if not str(image).startswith("http"):
            image = ""
        h["ti"] = thumb_url(image or GEARS)
        if image and "/site/img/default/" not in image:
            h["images"].append(photo_url(image))
        for k in ("download_count", "downloads_count"):
            if isinstance(d.get(k), int):
                h["dl"] = d[k]
                break

    def _download_count(self, tid):
        """Downloads when the input sheet has none: the website's older route (a
        signed-in user's THINGIVERSE_WEB_TOKEN), then the developer API
        (THINGIVERSE_TOKEN). None when neither says."""
        thing = self.api("/api/things/%s" % tid, tid, signed_in=True)
        if isinstance(thing, dict) and isinstance(thing.get("download_count"), int):
            return thing["download_count"]
        if self.s.thingiverse_token:
            r = net.get(self.session(), "%s/things/%s" % (DEV_API, tid), delay=self.s.http_delay,
                        headers={"authorization": "Bearer " + self.s.thingiverse_token,
                                 "accept": "application/json"})
            if r.status_code == 200:
                try:
                    n = r.json().get("download_count")
                    if isinstance(n, int):
                        return n
                except ValueError:
                    pass
        return None

    # ------------------------------------------------------------ the 3-D files

    def _fetch_file(self, url, tid):
        host = urlparse(url).netloc
        if host == "api.thingiverse.com":
            if not self.s.thingiverse_token:
                return None
            r = net.get(self.session(), url, delay=self.s.http_delay, timeout=180,
                        headers={"authorization": "Bearer " + self.s.thingiverse_token})
        else:
            r = self.fetch(url, referer=thing_url(tid), timeout=180)
        if r.status_code != 200:
            return None
        data = r.content
        head = data[:200].lstrip().lower()
        if len(data) < 84 or head.startswith((b"<!doctype", b"<html")):
            return None
        return data

    def download(self, thing_id, files, dest) -> list:
        """Save the listed model files under dest; returns the paths that arrived."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in files:
            if not is_model_name(f["name"]):
                continue
            p = dest / safe_name(f["name"])
            if not (p.is_file() and p.stat().st_size > 0):
                for url in f.get("urls") or []:
                    data = self._fetch_file(url, thing_id)
                    if data:
                        p.write_bytes(data)
                        break
            if p.is_file() and p.stat().st_size > 0:
                paths.append(p)
        return paths

    def download_zip(self, thing_id, dest) -> list:
        """Every model file from the Thing's "download all files" zip, as [{name, size, path}].

        The current site builds that zip in the browser and redirects this route to
        the Files page, so it is a last resort: [] with a warning when no zip comes.
        """
        dest = Path(dest)
        r = self.fetch("%s/thing:%s/zip" % (SITE, thing_id), referer=thing_url(thing_id), timeout=300)
        if r.status_code != 200 or not r.content.startswith(b"PK"):
            _warn("thing:%s/zip served no zip (HTTP %d, %s), so its model files were not fetched"
                  % (thing_id, r.status_code, (r.headers.get("content-type") or "no type").split(";")[0]))
            return []
        out = []
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            for info in z.infolist():
                if info.is_dir() or not is_model_name(info.filename):
                    continue
                name = Path(info.filename).name
                p = dest / safe_name(name)
                p.write_bytes(z.read(info))
                out.append({"name": name, "size": info.file_size, "path": str(p)})
        return out
