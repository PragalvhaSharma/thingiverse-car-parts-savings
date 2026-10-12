#!/usr/bin/env python3
"""Check every setting before a run: Python packages, OpenAI, the Thingiverse
session, eBay, the DuckDuckGo fallback and the slicer.

    python script/doctor.py
    python script/doctor.py --skip-slicer

Each check prints PASS, WARN (the run works, with a fallback) or FAIL (the run
would stop or produce nothing), with what to do. Secrets are never printed: the
cookies and tokens are reported as present or missing, and any message that
quotes one has it replaced by [redacted]. Exit code 1 when anything failed.
"""

import argparse
import datetime
import importlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from carsavings.config import ROOT, Settings  # noqa: E402

TEST_THING = "3013735"          # the paper's worked example (Sep 2: repair, a $19.99 listing)
TEST_QUERY = "Toyota Prius headlight wire opening cap"


def cookie_values(cookie):
    """The cookie values to hide. A value that is a domain (.thingiverse.com) is left out,
    or it would blank the site out of every URL in a message."""
    out = []
    for part in (cookie or "").split(";"):
        if "=" in part:
            value = part.split("=", 1)[1].strip()
            if not re.fullmatch(r"\.[a-z0-9.-]+|true|false|deleted", value):
                out.append(value)
    return out


def cookie_names(cookie):
    return [part.split("=", 1)[0].strip() for part in (cookie or "").split(";") if "=" in part]


class Report:
    def __init__(self, settings):
        s = settings
        secrets = [s.openai_api_key, s.thingiverse_cookie, s.thingiverse_web_token, s.thingiverse_token,
                   s.ebay_cookie, s.orca_api_token]
        secrets += cookie_values(s.thingiverse_cookie) + cookie_values(s.ebay_cookie)
        self.secrets = sorted({x for x in secrets if x and len(x) >= 6}, key=len, reverse=True)
        self.fails = 0
        self.warns = 0

    def clean(self, text) -> str:
        text = str(text)
        for x in self.secrets:
            text = text.replace(x, "[redacted]")
        text = re.sub(r"sk-[A-Za-z0-9_\-]{6,}", "sk-[redacted]", text)
        text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}", r"\1[redacted]", text)
        return text

    def say(self, kind, what, detail=""):
        self.fails += kind == "FAIL"
        self.warns += kind == "WARN"
        print("%-4s  %-20s %s" % (kind, what, self.clean(detail)), flush=True)


def when(epoch) -> str:
    return datetime.datetime.fromtimestamp(epoch).isoformat(" ", "minutes")


def check_packages(rep, s):
    need = [("requests", "requests"), ("bs4", "beautifulsoup4"), ("openai", "openai"),
            ("pydantic", "pydantic"), ("PIL", "Pillow"), ("trimesh", "trimesh"), ("numpy", "numpy")]
    if s.slicer_backend == "mcp":
        need.append(("mcp", "mcp"))
    missing = []
    for mod, pkg in need:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(pkg)
    if missing:
        rep.say("FAIL", "python packages", "missing %s: pip install -r requirements.txt" % ", ".join(missing))
    else:
        rep.say("PASS", "python packages", "all installed (Python %d.%d)" % sys.version_info[:2])
    try:
        import curl_cffi
        rep.say("PASS", "web client", "curl_cffi %s: Thingiverse and eBay are read with Chrome's TLS "
                "handshake" % getattr(curl_cffi, "__version__", "?"))
    except ImportError:
        rep.say("WARN", "web client", "curl_cffi is not installed: Cloudflare (Thingiverse) and eBay challenge "
                "Python's own HTTP client even with valid cookies; pip install -r requirements.txt")


def check_settings(rep, s, env_file):
    path = env_file or os.path.join(str(ROOT), ".env")
    if os.path.isfile(path):
        rep.say("PASS", "settings", "%s found" % path)
    else:
        rep.say("WARN", "settings", "no %s; reading the environment only (cp .env.example .env)" % path)
    rep.say("PASS", "arithmetic", "PLA $%g/kg, default print %g g = $%.2f, bed %g mm, scale guard $%g/g"
            % (s.pla_usd_per_kg, s.default_grams, s.default_print_cost, s.bed_mm, s.scale_usd_per_g))


def check_openai(rep, s):
    if not s.openai_api_key:
        rep.say("FAIL", "OpenAI", "OPENAI_API_KEY is not set")
        return
    from openai import OpenAI

    try:
        model = OpenAI(api_key=s.openai_api_key, max_retries=1, timeout=60).models.retrieve(s.openai_model)
        rep.say("PASS", "OpenAI", "key works; %s is available%s"
                % (model.id, " (reasoning effort %s)" % s.reasoning_effort if s.reasoning_effort else ""))
    except Exception as exc:
        rep.say("FAIL", "OpenAI", "%s: %s (check the key and OPENAI_MODEL=%s)"
                % (type(exc).__name__, exc, s.openai_model))


def check_thingiverse(rep, s):
    from carsavings.schema import thing_url
    from carsavings.thingiverse import (Thingiverse, ThingiverseBlocked, file_entries, image_urls,
                                        is_model_name, jwt_exp, parse_thing_page)

    names = cookie_names(s.thingiverse_cookie)
    if not names:
        rep.say("WARN", "Thingiverse cookie", "THINGIVERSE_COOKIE is empty; Cloudflare usually challenges "
                "requests without a browser session")
    elif "cf_clearance" in names:
        rep.say("PASS", "Thingiverse cookie", "%d cookies, cf_clearance present" % len(names))
    else:
        rep.say("WARN", "Thingiverse cookie", "%d cookies but no cf_clearance; copy the whole Cookie header "
                "from a request a thingiverse.com tab made" % len(names))

    tok = s.thingiverse_web_token
    if not tok:
        rep.say("PASS", "Thingiverse token", "THINGIVERSE_WEB_TOKEN not set, which is fine: Things are read "
                "with the view token the site gives every visitor. A signed-in token only adds download "
                "counts for sheets without a Downloads column")
    else:
        exp = jwt_exp(tok)
        if exp is None:
            rep.say("PASS", "Thingiverse token", "set (not a JWT, so its expiry is unknown)")
        elif exp < time.time():
            rep.say("FAIL", "Thingiverse token", "expired at %s; copy a fresh one or empty it" % when(exp))
        elif exp < time.time() + 6 * 3600:
            rep.say("WARN", "Thingiverse token", "expires at %s; refresh it before a long run" % when(exp))
        else:
            rep.say("PASS", "Thingiverse token", "valid until %s" % when(exp))

    tv = Thingiverse(s)
    try:
        r = tv.fetch(thing_url(TEST_THING))
    except ThingiverseBlocked as exc:
        rep.say("FAIL", "Thingiverse page", str(exc))
        return
    except Exception as exc:
        rep.say("FAIL", "Thingiverse page", "%s: %s" % (type(exc).__name__, exc))
        return
    og = parse_thing_page(r.text) if r.status_code == 200 else {"default": True}
    if r.status_code == 200 and not og.get("default"):
        rep.say("PASS", "Thingiverse page", "thing:%s reads %r by %s" % (TEST_THING, og["t"], og["by"]))
    else:
        rep.say("FAIL", "Thingiverse page", "thing:%s came back %s (HTTP %d): the session is not reaching "
                "Thing pages" % (TEST_THING, "with the default og tags" if r.status_code == 200
                                 else "without its page", r.status_code))
    try:
        d = tv.api("/api/v2/things/%s/complete" % TEST_THING, TEST_THING)
        zd = d.get("zip_data") if isinstance(d, dict) and isinstance(d.get("zip_data"), dict) else {}
        models = [f for f in file_entries(zd.get("files")) if is_model_name(f["name"])]
        got = None
        for url in (models[0]["urls"] if models else []):
            got = tv._fetch_file(url, TEST_THING)
            if got:
                break
    except ThingiverseBlocked as exc:
        rep.say("FAIL", "Thingiverse API", str(exc))
        return
    except Exception as exc:
        rep.say("WARN", "Thingiverse API", "%s: %s" % (type(exc).__name__, exc))
        return
    if not (isinstance(d, dict) and d.get("name")):
        rep.say("WARN", "Thingiverse API", "the website API refused the visitor's view token: fallback mode "
                "(the Thing pages' og tags: short descriptions, one photo, no print settings, and usually no "
                "model files, so prints are costed at the default)")
    elif got:
        rep.say("PASS", "Thingiverse API", "thing:%s lists %d model files and %d photos; %s downloaded (%d KB)"
                % (TEST_THING, len(models), len(image_urls(zd.get("images"))), models[0]["name"],
                   len(got) // 1024))
    elif models:
        rep.say("WARN", "Thingiverse API", "thing:%s lists %d model files, but %s did not download, so "
                "prints would be costed at the default" % (TEST_THING, len(models), models[0]["name"]))
    else:
        rep.say("WARN", "Thingiverse API", "thing:%s answers but lists no model files" % TEST_THING)

    if tok:
        try:
            d = tv.api("/api/things/%s" % TEST_THING, TEST_THING, signed_in=True)
        except Exception as exc:
            d = None
            print("      (%s: %s)" % (type(exc).__name__, rep.clean(exc)))
        if isinstance(d, dict) and isinstance(d.get("download_count"), int):
            rep.say("PASS", "Download counts", "THINGIVERSE_WEB_TOKEN works (thing:%s has %d downloads)"
                    % (TEST_THING, d["download_count"]))
        else:
            rep.say("WARN", "Download counts", "THINGIVERSE_WEB_TOKEN was refused: download counts come only "
                    "from the sheet%s" % (" and THINGIVERSE_TOKEN" if s.thingiverse_token else ""))

    if s.thingiverse_token:
        from carsavings import net

        try:
            r = net.get(tv.session(), "https://api.thingiverse.com/things/%s" % TEST_THING, timeout=30,
                        headers={"authorization": "Bearer " + s.thingiverse_token,
                                 "accept": "application/json"})
            if r.status_code == 200:
                rep.say("PASS", "Thingiverse dev API", "token works (download counts available)")
            else:
                rep.say("WARN", "Thingiverse dev API", "HTTP %d with THINGIVERSE_TOKEN" % r.status_code)
        except Exception as exc:
            rep.say("WARN", "Thingiverse dev API", "%s: %s" % (type(exc).__name__, exc))


def check_ebay(rep, s):
    from carsavings.ebay import Ebay, EbayBlocked

    if not s.ebay_cookie:
        rep.say("WARN", "eBay cookie", "EBAY_COOKIE is empty; searches go out without a browser session, "
                "which eBay may challenge")
    try:
        listings = Ebay(s).search(TEST_QUERY)
    except EbayBlocked as exc:
        rep.say("FAIL", "eBay search", str(exc))
        return
    except Exception as exc:
        rep.say("FAIL", "eBay search", "%s: %s" % (type(exc).__name__, exc))
        return
    usd = [x for x in listings if x.get("p")]
    if not listings:
        rep.say("WARN", "eBay search", "a result page, but no listings for %r" % TEST_QUERY)
    elif not usd:
        rep.say("FAIL", "eBay search", "%d listings but none in US dollars: leave EBAY_EXTRA_PARAMS empty "
                "(it then asks eBay to ship to a US ZIP code), or set eBay's ship-to to a US ZIP in that "
                "browser and copy the cookies again" % len(listings))
    else:
        rep.say("PASS", "eBay search", "%d listings, %d in US dollars (cheapest $%.2f)"
                % (len(listings), len(usd), min(x["p"] for x in usd)))


def check_web(rep, s):
    from carsavings.websearch import WebSearch

    try:
        res = WebSearch(s).search(TEST_QUERY)
    except Exception as exc:
        res = None
        print("      (%s)" % rep.clean(exc))
    if res is None:
        rep.say("WARN", "DuckDuckGo", "unreachable: rows with no eBay listing stay unmatched and are "
                "retried on the next run")
    else:
        rep.say("PASS", "DuckDuckGo", "fallback answers (%d priced results)" % len(res))


def check_slicer(rep, s):
    from carsavings.slicing import SlicerUnavailable, open_slicer

    if s.slicer_backend == "none":
        rep.say("WARN", "slicer", "SLICER_BACKEND=none: every print is costed from its geometry (estimator)")
        return
    try:
        slicer = open_slicer(s)
    except SlicerUnavailable as exc:
        rep.say("FAIL", "slicer", str(exc))
        return
    except Exception as exc:
        rep.say("FAIL", "slicer", "%s: %s" % (type(exc).__name__, exc))
        return
    try:
        status = slicer.call("get_status")
        rep.say("PASS", "slicer", "OrcaSlicer answers through %s; infill default %s; status %s"
                % (s.slicer_backend, slicer.baseline or "?", json.dumps(status)[:100]))
    finally:
        slicer.clear = lambda: None          # leave whatever is on the plate alone
        slicer.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-slicer", action="store_true", help="do not start the slicer")
    ap.add_argument("--env-file", help="settings file (default: .env in the repo)")
    a = ap.parse_args(argv)

    s = Settings.from_env(a.env_file)
    rep = Report(s)
    checks = [("packages", lambda: check_packages(rep, s)),
              ("settings", lambda: check_settings(rep, s, a.env_file)),
              ("OpenAI", lambda: check_openai(rep, s)),
              ("Thingiverse", lambda: check_thingiverse(rep, s)),
              ("eBay", lambda: check_ebay(rep, s)),
              ("DuckDuckGo", lambda: check_web(rep, s))]
    if not a.skip_slicer:
        checks.append(("slicer", lambda: check_slicer(rep, s)))
    for name, check in checks:
        try:
            check()
        except Exception as exc:
            rep.say("FAIL", name, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d failed, %d warnings" % (rep.fails, rep.warns))
    return 1 if rep.fails else 0


if __name__ == "__main__":
    sys.exit(main())
