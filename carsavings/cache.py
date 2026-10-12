"""Per-stage JSON cache: cache/<stage>/<thing id>.json.

Every stage writes its result here before the next one runs, so an interrupted
run resumes where it stopped and a single stage can be redone with
--refresh <stage> without paying for the others again.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

STAGES = ("harvest", "classify", "search", "verify", "slice")


def digest(*parts) -> str:
    """A short, filename-safe key for stages keyed by content (a query, a file set)."""
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


class Cache:
    def __init__(self, root):
        self.root = Path(root)

    def path(self, stage: str, key: str) -> Path:
        return self.root / stage / ("%s.json" % key)

    def get(self, stage: str, key: str):
        p = self.path(stage, key)
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def put(self, stage: str, key: str, value) -> None:
        p = self.path(stage, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name("%s.%d.%d.tmp" % (p.name, os.getpid(), threading.get_ident()))
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def drop(self, stage: str, key: str) -> None:
        p = self.path(stage, key)
        if p.is_file():
            p.unlink()
