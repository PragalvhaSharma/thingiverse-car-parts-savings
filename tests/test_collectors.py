"""The eBay, DuckDuckGo and Thingiverse parsers, on hand-written pages.

Live pages need the browser cookies in .env, so these tests use synthetic HTML
in the shapes the sites serve; script/doctor.py checks the live sessions.
"""

import base64
import contextlib
import io
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs

from helpers import has

from carsavings import ebay, net, thingiverse, websearch
from carsavings.config import Settings

S_ITEM_PAGE = """
<ul class="srp-results srp-list">
<li class="s-item s-item__pl-on-bottom">
  <div class="s-item__image-section"><img src="https://ir.ebaystatic.com/rs/v/placeholder.png"></div>
  <a class="s-item__link" href="https://ebay.com/itm/123456789012?itmmeta=x"><div class="s-item__title"><span>Shop on eBay</span></div></a>
  <span class="s-item__price">$20.00</span>
</li>
<li class="s-item">
  <div class="s-item__image-wrapper"><img src="https://i.ebayimg.com/thumbs/images/g/AbCdEf123/s-l140.jpg"></div>
  <a class="s-item__link" href="https://www.ebay.com/itm/Prius-Cap/234567890123?hash=item1"><div class="s-item__title"><span role="heading"><span class="LIGHT_HIGHLIGHT">New Listing</span>Toyota Prius Headlight Wire Cap<span class="clipped">Opens in a new window or tab</span></span></div></a>
  <span class="s-item__price">$12.99 to $15.99</span>
  <span class="s-item__shipping s-item__logisticsCost">+$4.50 shipping</span>
</li>
<li class="s-item">
  <img data-defer-load="https://i.ebayimg.com/images/g/XyZ789/s-l225.webp">
  <a href="https://www.ebay.com/itm/345678901234"><div class="s-item__title">Prius cap priced in Canadian dollars</div></a>
  <span class="s-item__price">C $18.50</span>
  <span class="s-item__shipping">Free shipping</span>
</li>
<li class="s-item">
  <a href="https://www.ebay.com/itm/234567890123"><div class="s-item__title">The same listing again</div></a>
  <span class="s-item__price">$1.00</span>
</li>
<li class="srp-river-answer srp-river-answer--REWRITE_START"><span>Results matching fewer words</span></li>
<li class="s-item">
  <a href="https://www.ebay.com/itm/456789012345"><div class="s-item__title">After the divider</div></a>
  <span class="s-item__price">$2.00</span>
</li>
</ul>
"""

S_CARD_PAGE = """
<ul class="srp-results srp-list">
<li class="s-card s-card--horizontal" data-listingid="567890123456">
  <div class="s-card__media-wrapper"><img class="s-card__image" src="https://i.ebayimg.com/images/g/QwErTy/s-l140.webp"></div>
  <a class="su-link" href="https://www.ebay.com/itm/567890123456?_skw=x"><div role="heading" class="s-card__title"><span>Sun Visor Clip for Kia EV6</span><span>Opens in a new window or tab</span></div></a>
  <div class="s-card__attribute-row"><span class="s-card__price">$9.49</span></div>
  <div class="s-card__attribute-row"><span>+$3.99 delivery</span></div>
</li>
<li class="s-card" data-listingid="678901234567">
  <div class="s-card__title"><span>Listing without a link</span></div>
  <span class="s-card__price">$5.00</span>
</li>
</ul>
"""


def fake_response(status=200, text="", url="https://www.ebay.com/sch/i.html"):
    return SimpleNamespace(status_code=status, text=text, url=url, headers={})


class EbayParser(unittest.TestCase):
    def test_s_item_layout(self):
        got = ebay.parse_results(S_ITEM_PAGE)
        self.assertEqual(got, [
            {"p": 12.99, "s": 4.5, "t": "New Listing Toyota Prius Headlight Wire Cap",
             "u": "https://www.ebay.com/itm/234567890123",
             "im": "https://i.ebayimg.com/images/g/AbCdEf123/s-l500.webp"},
            {"p": None, "s": 0.0, "t": "Prius cap priced in Canadian dollars",
             "u": "https://www.ebay.com/itm/345678901234",
             "im": "https://i.ebayimg.com/images/g/XyZ789/s-l500.webp"},
        ])

    def test_s_card_layout(self):
        got = ebay.parse_results(S_CARD_PAGE)
        self.assertEqual(got, [
            {"p": 9.49, "s": 3.99, "t": "Sun Visor Clip for Kia EV6",
             "u": "https://www.ebay.com/itm/567890123456",
             "im": "https://i.ebayimg.com/images/g/QwErTy/s-l500.webp"},
            {"p": 5.0, "s": 0.0, "t": "Listing without a link",
             "u": "https://www.ebay.com/itm/678901234567", "im": ""},
        ])

    def test_prices_and_shipping(self):
        self.assertEqual(ebay.parse_price("US $1,043.28"), 1043.28)
        self.assertIsNone(ebay.parse_price("AU $20.00"))
        self.assertIsNone(ebay.parse_price("EUR 12,00"))
        self.assertIsNone(ebay.parse_price("$0.00"))
        self.assertEqual(ebay.parse_shipping("+$14.13 delivery"), 14.13)
        self.assertEqual(ebay.parse_shipping("Free delivery in 2-4 days"), 0.0)
        self.assertEqual(ebay.parse_shipping("Located in United States"), 0.0)

    def test_pool_is_the_first_ten_usd_listings_cheapest_first(self):
        listings = [{"p": None, "s": 0, "t": "foreign", "u": "u0", "im": ""}]
        listings += [{"p": float(20 - i), "s": 0, "t": "t%d" % i, "u": "u%d" % i, "im": ""} for i in range(1, 13)]
        pool = ebay.make_pool(listings)
        self.assertEqual([c["i"] for c in pool], list(range(1, 11)))
        self.assertEqual([c["p"] for c in pool], [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0])
        self.assertNotIn("u11", [c["u"] for c in pool])

    def test_search_url(self):
        self.assertEqual(ebay.search_url("prius cap", "&_fcid=1"),
                         "https://www.ebay.com/sch/i.html?_nkw=prius+cap&_sop=12&_fcid=1")

    def _ebay(self, response):
        e = ebay.Ebay(Settings(http_delay=0))
        e.session = lambda: None
        e.check_waits = (0, 0)
        return e, mock.patch.object(net, "get", lambda *a, **k: response)

    def test_blocked_sessions_stop_the_run(self):
        cases = [
            fake_response(403, "<html>Access denied</html>"),
            fake_response(200, "<html>x</html>", url="https://www.ebay.com/splashui/challenge?ap=1"),
            fake_response(200, "<title>Pardon Our Interruption...</title>"),
            fake_response(200, "<html><body>a sign-in wall, no results</body></html>"),
            fake_response(200, "<ul class='srp-results'>" + "".join(
                "<li class='s-item'><a href='https://www.ebay.com/itm/%d'><div class='s-item__title'>t%d</div></a>"
                "<span class='s-item__price'>C $%d.00</span></li>" % (100000000 + i, i, i + 5) for i in range(4))
                + "</ul>"),
        ]
        for resp in cases:
            e, patch = self._ebay(resp)
            with patch, self.assertRaises(ebay.EbayBlocked):
                e.search("prius cap")

    def test_empty_result_page_and_server_errors(self):
        e, patch = self._ebay(fake_response(200, "<div class='srp-save-null-search'>No exact matches found</div>"))
        with patch:
            self.assertEqual(e.search("nothing like this"), [])
        e, patch = self._ebay(fake_response(503, "busy"))
        with patch, self.assertRaises(ebay.SearchError):
            e.search("prius cap")

    def test_a_bot_check_is_retried_on_a_fresh_session(self):
        e = ebay.Ebay(Settings(http_delay=0))
        e.check_waits = (0, 0)
        replies = [fake_response(200, "<title>Pardon Our Interruption...</title>"), fake_response(200, S_ITEM_PAGE)]
        made, used = [], []

        def new_session(*a, **k):
            made.append(SimpleNamespace())
            return made[-1]

        def get(session, url, **kw):
            used.append(session)
            return replies.pop(0)

        with mock.patch.object(net, "session", new_session), mock.patch.object(net, "get", get):
            listings = e.search("prius cap")
        self.assertTrue(listings)
        self.assertEqual(len(made), 2)
        self.assertIsNot(used[0], used[1])


DDG_PAGE = """
<div class="result results_links web-result">
  <h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.example.com%2Fcap%3Fa%3D1&amp;rut=abc">Prius headlight cap - $33.00</a></h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Genuine Toyota cap. Ships fast.</a>
</div>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://shop.example.ca/x">Cap CA $40.00</a></h2>
  <a class="result__snippet" href="https://shop.example.ca/x">Price C$40.00 only</a>
</div>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://parts.example.com/y">OEM wire cap</a></h2>
  <div class="result__snippet">Buy now for 19.5 USD with free shipping</div>
</div>
<div class="result">
  <h2 class="result__title"><a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=x">Sponsored $1.00</a></h2>
</div>
"""


class DuckDuckGo(unittest.TestCase):
    def test_parse_results(self):
        got = websearch.parse_results(DDG_PAGE)
        self.assertEqual([(x["p"], x["u"]) for x in got],
                         [(33.0, "https://www.example.com/cap?a=1"), (19.5, "https://parts.example.com/y")])
        self.assertEqual(got[0]["snip"], "Genuine Toyota cap. Ships fast.")
        self.assertEqual((got[0]["s"], got[0]["im"]), (0.0, ""))

    def test_pool_is_cheapest_first(self):
        pool = websearch.make_pool(websearch.parse_results(DDG_PAGE))
        self.assertEqual([(c["i"], c["p"]) for c in pool], [(1, 19.5), (2, 33.0)])

    def test_decode_url(self):
        self.assertEqual(websearch.decode_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fb%20c&rut=1"),
                         "https://a.com/b c")
        self.assertEqual(websearch.decode_url("https://a.com/x"), "https://a.com/x")

    def test_usd_price(self):
        self.assertEqual(websearch.usd_price("only $1,299.00 today"), 1299.0)
        self.assertEqual(websearch.usd_price("AU$ 20 or C $30"), 0.0)

    def test_unreachable_is_none_and_no_results_is_empty(self):
        w = websearch.WebSearch(Settings(http_delay=0))
        w.fetch = lambda q: (200, "<div class='no-results'>No results.</div>")
        self.assertEqual(w.search("q"), [])
        err = io.StringIO()
        w.fetch = lambda q: (202, "anomaly")
        with contextlib.redirect_stderr(err):
            self.assertIsNone(w.search("q"))
        self.assertIn("DuckDuckGo fallback unavailable (HTTP 202)", err.getvalue())
        w.fetch = lambda q: (200, DDG_PAGE)
        self.assertEqual(len(w.search("q")), 2)

    # DuckDuckGo answers the GET form of the URL with a 202 bot check, so the query
    # has to go in a POST body, as its own search form sends it.
    def test_query_is_posted_through_curl(self):
        w = websearch.WebSearch(Settings(http_delay=0, user_agent="UA"))
        sent = []

        def run(cmd, **kw):
            sent.append(cmd)
            return SimpleNamespace(returncode=0, stdout=(DDG_PAGE + "\n200").encode(), stderr=b"")

        with mock.patch.object(websearch.net, "pace", lambda *a: None), \
                mock.patch.object(websearch.shutil, "which", lambda name: "/usr/bin/curl"), \
                mock.patch.object(websearch.subprocess, "run", run):
            self.assertEqual(len(w.search("Prius cap")), 2)
        cmd = sent[0]
        self.assertEqual(cmd[-1], websearch.DDG)
        form = parse_qs(cmd[cmd.index("--data") + 1])
        self.assertEqual((form["q"], form["kl"]), (["Prius cap buy price"], ["us-en"]))

    @unittest.skipUnless(has("requests"), "needs requests")
    def test_query_is_posted_without_curl(self):
        w = websearch.WebSearch(Settings(http_delay=0))
        posted = []
        w.session = lambda: SimpleNamespace(
            post=lambda url, **kw: posted.append((url, kw["data"])) or fake_response(200, DDG_PAGE))
        with mock.patch.object(websearch.net, "pace", lambda *a: None), \
                mock.patch.object(websearch.shutil, "which", lambda name: None):
            self.assertEqual(len(w.search("Prius cap")), 2)
        self.assertEqual(posted[0][0], websearch.DDG)
        self.assertEqual(posted[0][1]["q"], "Prius cap buy price")


@unittest.skipUnless(has("requests") or has("curl_cffi"), "needs requests or curl_cffi")
class BrowserSession(unittest.TestCase):
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/154.0.0.0 Safari/537.36")

    @staticmethod
    def cookie_sent(s, url):
        import urllib.request

        req = urllib.request.Request(url)
        getattr(s.cookies, "jar", s.cookies).add_cookie_header(req)
        return req.get_header("Cookie") or ""

    # the copied cookies belong to one site; model files and photos often come from other hosts
    def test_site_cookies_go_only_to_that_site(self):
        s = net.session(self.UA, "PHPSESSID=abc; cf_clearance=xyz-1.2", domain="thingiverse.com")
        self.assertNotIn("cookie", {k.lower() for k in s.headers})
        sent = self.cookie_sent(s, "https://www.thingiverse.com/thing:3013735")
        self.assertIn("cf_clearance=xyz-1.2", sent)
        self.assertIn("PHPSESSID=abc", sent)
        self.assertEqual(self.cookie_sent(s, "https://files.example-cdn.com/cap.stl"), "")
        self.assertIn('v="154"', s.headers["sec-ch-ua"])


def page(**meta):
    tags = "".join('<meta property="%s" content="%s">' % (k.replace("_", ":"), v) for k, v in meta.items())
    return "<html><head>%s<title>ignored</title></head></html>" % tags


class ThingiversePage(unittest.TestCase):
    def test_og_tags(self):
        got = thingiverse.parse_thing_page(page(
            og_title="Sun Visor Clip by Some by Maker42",
            og_image="https://resize.thingiverse.com/?url=https://cdn.thingiverse.com/assets/a/b.png&w=628&h=472",
            og_description="A clip &amp; a spare."))
        self.assertEqual((got["default"], got["t"], got["by"]), (False, "Sun Visor Clip by Some", "Maker42"))
        self.assertEqual(got["desc"], "A clip & a spare.")
        self.assertTrue(got["image"].endswith("w=628&h=472"))

    def test_default_og_tags_mean_no_thing(self):
        got = thingiverse.parse_thing_page(page(og_title="Thingiverse - The community for Open Hardware"))
        self.assertTrue(got["default"])
        self.assertTrue(thingiverse.parse_thing_page("")["default"])

    def test_title_tag_when_og_title_is_missing(self):
        got = thingiverse.parse_thing_page("<title>Door handle by Maker - Thingiverse</title>")
        self.assertEqual((got["t"], got["by"]), ("Door handle", "Maker"))

    def test_thumbnails(self):
        cdn = "https://cdn.thingiverse.com/assets/c4/99/a.png"
        self.assertEqual(thingiverse.thumb_url(cdn), thingiverse.THUMB % cdn)
        for direct in ("https://cdn.thingiverse.com/assets/c4/99/a.webp",
                       "https://cdn.thingiverse.com/assets/65/29/Capture.GIF",
                       thingiverse.GEARS,
                       "https://resize.thingiverse.com/?url=%s&w=628&h=472&fit=contain&cbg=white&n=-1" % cdn):
            self.assertEqual(thingiverse.thumb_url(direct), direct)
        self.assertEqual(thingiverse.thumb_url(""), "")

    def test_jwt_exp(self):
        payload = base64.urlsafe_b64encode(json.dumps({"exp": 1790000000}).encode()).decode().rstrip("=")
        self.assertEqual(thingiverse.jwt_exp("e30.%s.sig" % payload), 1790000000.0)
        self.assertIsNone(thingiverse.jwt_exp("opaque-token"))

    def test_file_entries(self):
        got = thingiverse.file_entries([
            {"name": "cap.stl", "size": "2048", "direct_url": "https://cdn.thingiverse.com/assets/x/cap.stl",
             "public_url": "https://www.thingiverse.com/download:1", "url": "https://api.thingiverse.com/files/1"},
            {"name": "", "size": 1},
            "not a file",
        ])
        self.assertEqual(got, [{"name": "cap.stl", "size": 2048,
                                "urls": ["https://cdn.thingiverse.com/assets/x/cap.stl",
                                         "https://www.thingiverse.com/download:1"]}])

    def test_image_urls_prefer_the_large_display_size(self):
        got = thingiverse.image_urls([
            {"sizes": [{"type": "thumb", "size": "large", "url": "https://cdn/t.jpg"},
                       {"type": "display", "size": "large", "url": "https://cdn/d.jpg"}]},
            {"url": "https://cdn.thingiverse.com/site/img/default/Gears.webp"},
            "https://cdn/d.jpg",
            "https://cdn/e.jpg",
        ])
        self.assertEqual(got, ["https://cdn/d.jpg", "https://cdn/e.jpg"])

    def test_find_infill(self):
        parts = [{"name": "print settings", "data": [{"supports": "No"}, {"infill": "20%"}]}]
        self.assertEqual(thingiverse.find_infill(parts), "20%")
        for field, want in (("15-20%", "20%"), ("20", "20%"), ("3 walls, 20%", "20%"), (25, "25%"),
                            ("12.5 %", "12.5%"), ("150%", ""), ("2020", ""), ("Doesn't Matter", "")):
            with self.subTest(field=field):
                self.assertEqual(thingiverse.find_infill([{"data": [{"infill": field}]}]), want)
        for text, want in (
                ("Printed in PETG. Infill: 15 %", "15%"),
                ("I also used 15% infill I think", "15%"),
                ("Printed at 20 % gyroid infill.", "20%"),
                ("Scale: 100%\nInfill: 25%", "25%"),
                ("Print Settings\nInfill:\n30%", "30%"),       # a heading, its value below
                ("Infill: 15-20%", "20%"),                      # a range, read at its top
                ("infill 15% to 20 %", "20%"),
                ("Scale 100% Infill 30%", "30%"),
                ("Infill: whatever you like\n100% scale", ""),
                ("Infill: any. Scale 100%", ""),
                ("Use any infill, scale 100%", ""),
                ("no settings here", "")):
            with self.subTest(text=text):
                self.assertEqual(thingiverse.find_infill(None, text), want)
        for page in ("<h3>Infill</h3><p>30%</p>", "&lt;h3&gt;Infill&lt;/h3&gt;&lt;p&gt;30%&lt;/p&gt;"):
            with self.subTest(page=page):
                self.assertEqual(thingiverse.find_infill(None, thingiverse.strip_html(page)), "30%")

    def test_photo_url(self):
        cdn = "https://cdn.thingiverse.com/assets/c4/99/a.png"
        self.assertEqual(thingiverse.photo_url(cdn), "https://img.thingiverse.com/cdn-cgi/image/"
                         "fit=scale-down,width=1024,height=1024/" + cdn)
        for same in ("https://cdn.thingiverse.com/assets/65/29/Capture.GIF", thingiverse.GEARS,
                     "https://example.com/a.jpg"):
            self.assertEqual(thingiverse.photo_url(same), same)

    def test_strip_html_reads_twice_escaped_sections(self):
        got = thingiverse.strip_html("<div><h1>Print Settings</h1>&lt;p&gt;&lt;strong&gt;Infill: &lt;/strong&gt;"
                                     " &lt;div&gt;&lt;p&gt;20%&lt;/p&gt;&lt;/div&gt;&lt;/p&gt; A &amp;amp; B</div>")
        self.assertEqual(got, "Print Settings\nInfill: 20%\nA & B")


class FakeReply:
    def __init__(self, status, data=None, text=None):
        self.status_code, self._data, self.headers = status, data, {}
        self.text = text if text is not None else json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        if self._data is None:
            raise ValueError("not JSON")
        return self._data


def jwt(exp):
    return "e30.%s.sig" % base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")


CDN = "https://cdn.thingiverse.com/assets/"
COMPLETE = {   # the shape of /api/v2/things/<id>/complete (Oct 2026), trimmed
    "name": "Sun Visor  Clip", "creator": {"name": "Maker42"},
    "description_html": "<div><p>Clip for a 1998 Mazda 121.</p></div>",
    "details": ("<div><h1>Summary</h1><div><p>Clip for a 1998 Mazda 121.</p></div><h1>Print Settings</h1>"
                "&lt;p&gt;&lt;strong&gt;Infill: &lt;/strong&gt; 20%&lt;/p&gt;</div>"),
    "details_parts": [{"type": "settings", "name": "Print Settings", "data": [{"supports": "No", "infill": "20%"}]}],
    "tags": [{"name": "mazda"}, {"name": "visor"}],
    "default_image": {"url": CDN + "aa/clip.jpg"},
    "zip_data": {"files": [{"name": "clip.stl", "url": CDN + "bb/clip.stl"},
                           {"name": "clip.step", "url": CDN + "cc/clip.step"}],
                 "images": [{"name": "clip.jpg", "url": CDN + "aa/clip.jpg"},
                            {"name": "fit.gif", "url": CDN + "dd/fit.gif"}]},
}


class FakeSite(thingiverse.Thingiverse):
    """thingiverse.com as a visitor sees it: a view token, then the v2 routes.
    The older /api/things routes refuse every bearer but a signed-in user's."""

    def __init__(self, tokens, refuse=()):
        super().__init__(Settings(http_delay=0))
        self.tokens = list(tokens)          # issued in turn by /api/v2/auth/view
        self.refuse = set(refuse)           # bearers the API calls expired
        self.complete = COMPLETE
        self.calls = []

    def fetch(self, url, *, bearer="", **kw):
        path = url.replace(thingiverse.SITE, "")
        self.calls.append((path, bearer))
        if path == thingiverse.VIEW_TOKEN:
            return FakeReply(200, {"access": self.tokens.pop(0)})
        if path.startswith("/api/things/"):
            return FakeReply(401, {"error": "Unauthorized", "type": "INVALID_ACCESS_TOKEN"})
        if bearer in self.refuse:
            return FakeReply(401, {"error": "Unauthorized", "type": "EXPIRED_ACCESS_TOKEN"})
        if path == "/api/v2/things/5019865/complete" and bearer:
            return FakeReply(200, self.complete)
        return FakeReply(404, {"error": "Not Found"})


class ThingiverseSite(unittest.TestCase):
    def test_a_visitor_gets_the_write_up_files_and_photos(self):
        tv = FakeSite([jwt(time.time() + 86400)])
        h = tv.harvest("5019865")
        self.assertEqual((h["ok"], h["via"], h["files_via"], h["t"], h["by"], h["tags"], h["infill"]),
                         (True, "api", "api", "Sun Visor Clip", "Maker42", ["mazda", "visor"], "20%"))
        self.assertEqual(h["files"], [{"name": "clip.stl", "size": 0, "urls": [CDN + "bb/clip.stl"]},
                                      {"name": "clip.step", "size": 0, "urls": [CDN + "cc/clip.step"]}])
        self.assertEqual(h["images"], [thingiverse.PHOTO % (CDN + "aa/clip.jpg"), CDN + "dd/fit.gif"])
        self.assertEqual(h["ti"], thingiverse.THUMB % (CDN + "aa/clip.jpg"))
        self.assertEqual(h["desc"], "Summary\nClip for a 1998 Mazda 121.\nPrint Settings\nInfill: 20%")
        self.assertIsNone(h["dl"])          # the count needs a signed-in user's token
        paths = [p for p, _ in tv.calls]
        self.assertEqual(paths.count(thingiverse.VIEW_TOKEN), 1)
        self.assertFalse([p for p in paths if p.startswith("/api/things/")])

    def test_without_a_settings_block_the_infill_comes_from_the_write_up(self):
        tv = FakeSite([jwt(time.time() + 86400)])
        tv.complete = dict(COMPLETE, details_parts=[],
                           details="<div><h1>Print Settings</h1><h3>Infill</h3><p>30%</p></div>")
        self.assertEqual(tv.harvest("5019865", need_downloads=False)["infill"], "30%")

    def test_a_refused_view_token_is_replaced(self):
        old, new = jwt(time.time() + 86400), jwt(time.time() + 90000)
        tv = FakeSite([old, new], refuse={old})
        self.assertEqual(tv.harvest("5019865", need_downloads=False)["via"], "api")
        self.assertEqual([b for p, b in tv.calls if p.endswith("/complete")], [old, new])

    def test_a_view_token_near_expiry_is_replaced_before_use(self):
        soon, fresh = jwt(time.time() + 60), jwt(time.time() + 86400)
        tv = FakeSite([soon, fresh])
        self.assertEqual([tv.view_token(), tv.view_token(), tv.view_token()], [soon, fresh, fresh])

    def test_without_a_view_token_the_page_is_read_and_one_warning_given(self):
        class Closed(FakeSite):
            def fetch(self, url, **kw):
                self.calls.append((url.replace(thingiverse.SITE, ""), kw.get("bearer", "")))
                if url.endswith(thingiverse.VIEW_TOKEN):
                    return FakeReply(403, text="<html>no</html>")
                if "/api/" in url:
                    return FakeReply(401, {"type": "NO_TOKEN_PROVIDED"})
                return FakeReply(200, text=page(og_title="Sun Visor Clip by Maker42",
                                                og_image=CDN + "aa/clip.jpg"))

        tv, err = Closed([]), io.StringIO()
        with contextlib.redirect_stderr(err):
            got = [tv.harvest("5019865", need_downloads=False, zip_fallback=False) for _ in range(2)]
        self.assertEqual([(h["via"], h["t"], h["by"]) for h in got], [("page", "Sun Visor Clip", "Maker42")] * 2)
        self.assertEqual(err.getvalue().count("warning:"), 1)
        self.assertEqual([p for p, _ in tv.calls].count(thingiverse.VIEW_TOKEN), 1)

    def test_token_refused(self):
        self.assertTrue(thingiverse.token_refused(FakeReply(401, {"type": "INVALID_ACCESS_TOKEN"})))
        self.assertTrue(thingiverse.token_refused(FakeReply(401, {"error": "Unauthorized"})))
        self.assertTrue(thingiverse.token_refused(FakeReply(401, text="<html>Unauthorized</html>")))
        self.assertFalse(thingiverse.token_refused(FakeReply(401, {"type": "THING_NOT_PUBLIC"})))


if __name__ == "__main__":
    unittest.main()
