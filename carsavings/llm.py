"""The OpenAI calls behind every AI step (paper: GPT-5.6 Luna for each one).

Each step is one Chat Completions call with a structured (Pydantic) answer.
Photos are downloaded here and sent inline as JPEG data URLs: Thingiverse blocks
OpenAI's own image fetcher, and the API rejects some formats outright (the Sep 2
run lost one row to invalid_image_format), so every photo is re-encoded first.
No temperature is sent; reasoning models take only their default.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import random
import sys
import threading
import time
from pathlib import Path

from . import net
from .savings import LLM_ERROR_PREFIX

MAX_SIDE = 1024
ATTEMPTS = 4
# a 400 that names one of these is about the request itself, so every row would hit it
_FATAL_PARAMS = {"model", "reasoning_effort", "response_format", "messages", "temperature"}


class LLMError(RuntimeError):
    """One call failed for good. The row records the message in err and counts $0."""


class LLMFatal(RuntimeError):
    """The key, the model or the quota is wrong. Every row would fail, so the run stops."""


def to_jpeg(data: bytes, max_side: int = MAX_SIDE) -> bytes:
    from PIL import Image, ImageOps

    im = Image.open(io.BytesIO(data))
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((max_side, max_side))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return out.getvalue()


class LLM:
    def __init__(self, settings, session_for=None):
        """session_for(url) may return a requests session for hosts that need
        cookies (Thingiverse); other photos are fetched with a plain session."""
        self.s = settings
        self.session_for = session_for
        self._client = None
        self._lock = threading.Lock()
        self._local = threading.local()
        self._failed = set()
        self.img_dir = Path(settings.cache_dir) / "img"

    def client(self):
        with self._lock:
            if self._client is None:
                if not self.s.openai_api_key:
                    raise LLMFatal("OPENAI_API_KEY is not set (copy .env.example to .env and fill it in)")
                from openai import OpenAI

                self._client = OpenAI(api_key=self.s.openai_api_key, max_retries=0, timeout=300)
        return self._client

    # ------------------------------------------------------------ photos

    def _plain_session(self):
        s = getattr(self._local, "session", None)
        if s is None:
            s = net.session(self.s.user_agent)
            self._local.session = s
        return s

    def jpeg(self, url):
        """The photo at url as JPEG bytes (cached on disk), or None if it cannot be had."""
        if not url or url in self._failed:
            return None
        self.img_dir.mkdir(parents=True, exist_ok=True)
        path = self.img_dir / (hashlib.sha1(url.encode("utf-8")).hexdigest() + ".jpg")
        if path.is_file():
            return path.read_bytes()
        session = (self.session_for(url) if self.session_for else None) or self._plain_session()
        try:
            r = net.get(session, url, delay=0.2, timeout=60)
            if r.status_code != 200 or not r.content:
                raise RuntimeError("HTTP %d" % r.status_code)
            data = to_jpeg(r.content)
        except Exception as exc:
            self._failed.add(url)
            print("warning: photo skipped (%s): %s" % (exc, url[:120]), file=sys.stderr)
            return None
        # Things that share a query share a candidate pool, so with --workers two
        # threads can fetch the same photo at once: each writes its own temporary
        # file, and a failed write costs only the cache entry, never the row.
        tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
        return data

    def image_part(self, url):
        data = self.jpeg(url)
        if data is None:
            return None
        b64 = base64.b64encode(data).decode("ascii")
        return {"type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + b64, "detail": self.s.image_detail}}

    # ------------------------------------------------------------ calls

    def parse(self, schema, system: str, parts: list):
        """One structured call. Returns the parsed Pydantic object or raises
        LLMError (this row) / LLMFatal (the whole run)."""
        import openai

        client = self.client()
        api = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
        kwargs = {"model": self.s.openai_model, "response_format": schema,
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": parts}]}
        if self.s.reasoning_effort:
            kwargs["reasoning_effort"] = self.s.reasoning_effort
        last = None
        for attempt in range(ATTEMPTS):
            try:
                resp = api(**kwargs)
            except (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError) as exc:
                raise LLMFatal(str(exc)) from exc
            except openai.RateLimitError as exc:
                if "insufficient_quota" in str(exc):
                    raise LLMFatal(str(exc)) from exc
                last = exc
            except openai.BadRequestError as exc:
                if getattr(exc, "param", None) in _FATAL_PARAMS:
                    raise LLMFatal(str(exc)) from exc
                raise LLMError(LLM_ERROR_PREFIX + str(exc)) from exc
            except Exception as exc:  # timeouts, 5xx, a cut-off or filtered answer
                last = exc
            else:
                msg = resp.choices[0].message
                if getattr(msg, "refusal", None):
                    last = RuntimeError("model refused: %s" % msg.refusal)
                elif getattr(msg, "parsed", None) is None:
                    last = RuntimeError("no structured answer")
                else:
                    return msg.parsed
            if attempt < ATTEMPTS - 1:
                time.sleep(min(60.0, 3.0 * 2 ** attempt) + random.random())
        raise LLMError(LLM_ERROR_PREFIX + str(last))
