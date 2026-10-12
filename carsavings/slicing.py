"""Paper step 4: the print cost, from a slice of the Thing's own files.

Each Thing's files are sliced in OrcaSlicer 2.4.2, driven through the open-source
orcaslicer-mcp connector (SLICER_BACKEND=mcp, the paper's setup) or straight
through the slicer's Remote API, which is what the connector itself calls
(SLICER_BACKEND=rest). Slicing uses the infill recommended on the Thing page and
otherwise the slicer's standard settings. The plastic mass the slicer reports is
priced at $25 per kg of PLA (savings.print_cost).

One printable unit is costed:
  * the files are the ones the classifier picked as one unit (all model files
    when none of its picks exist);
  * the paper slices the Thing's STL files, so a pick saved as 3MF or OBJ with an
    STL of the same name beside it is sliced from that STL (a designer's 3MF can
    be a whole plate: Kia EV6 knob, 7186295);
  * congruent copies of one piece plated in the same file collapse to one copy;
  * a picked file that is a congruent duplicate of another picked file is dropped.

When a file cannot be sliced, its mass comes from its geometry instead (the
estimator: a 1.2 mm shell plus infill, at PLA's 1.24 g/cm3). ms records the
source: orcaslicer (every file sliced), mixed (some sliced, some estimated),
estimator (none sliced), default (no usable file: the flat 30 g = $0.75).

The longest side of any piece (mm, from an oriented bounding box) feeds the
oversize guard in savings.finalize.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import shlex
import threading
import time
from pathlib import Path

from .thingiverse import is_model_name, safe_name

PLA_DENSITY = 1.24        # g/cm3
SHELL_CM = 0.12           # walls plus top and bottom layers, in cm
DEFAULT_INFILL = 0.15     # when the Thing page recommends none
MAX_BODIES = 500          # more loose pieces than this are costed as one body
JOB_TIMEOUT = 120         # seconds to wait for auto-orient / arrange
REQUIRED_TOOLS = ("get_status", "list_objects", "delete_object", "load_model", "auto_orient",
                  "arrange_plate", "get_job_status", "get_config", "set_config", "slice_and_wait")
NO_SLICER_HINT = ("Start OrcaSlicer (the 2.4.2 build with the Remote API, see README) with "
                  "Preferences > Remote API enabled and its token in ORCA_API_TOKEN, or set "
                  "SLICER_BACKEND=none to cost every print from its geometry for a quick test.")


class SlicerUnavailable(RuntimeError):
    """OrcaSlicer or the connector cannot be reached. The run stops; caches keep the progress."""


class SliceFailed(RuntimeError):
    """This one file did not slice; its mass falls back to the estimator."""


# ---------------------------------------------------------------- geometry

def infill_fraction(infill) -> float:
    try:
        value = float(str(infill).strip().rstrip("%")) / 100.0
    except ValueError:
        return DEFAULT_INFILL
    return value if 0.0 <= value <= 1.0 else DEFAULT_INFILL


def estimate_grams(volume_mm3, area_mm2, infill="", watertight=True) -> float:
    """Shell plus infill at PLA density. Volume is only trusted on closed bodies."""
    area = abs(float(area_mm2)) / 100.0          # cm2
    volume = abs(float(volume_mm3)) / 1000.0     # cm3
    shell = area * SHELL_CM
    if not watertight:
        return PLA_DENSITY * shell
    fill = infill_fraction(infill) if infill else DEFAULT_INFILL
    return PLA_DENSITY * (min(shell, volume) + max(volume - shell, 0.0) * fill)


def load_mesh(path):
    import trimesh

    mesh = trimesh.load(str(path), force="mesh")
    if mesh is None or not hasattr(mesh, "faces") or len(mesh.faces) == 0:
        raise ValueError("no triangles")
    return mesh


def analyze(path, infill="") -> dict:
    """Pieces of one model file, with congruent copies grouped.

    Pieces are compared by volume, surface area and sorted extents (to 0.1),
    which catches the usual case of one piece plated several times.
    """
    import numpy as np

    mesh = load_mesh(path)
    bodies = list(mesh.split(only_watertight=False)) or [mesh]
    if len(bodies) > MAX_BODIES:
        bodies = [mesh]
    groups = {}
    # An open piece has zero volume, and trimesh divides by it for a centre of
    # mass that is never used here.
    with np.errstate(divide="ignore", invalid="ignore"):
        for i, b in enumerate(bodies):
            sig = (round(abs(float(b.volume)), 1), round(float(b.area), 1),
                   tuple(sorted(round(float(e), 1) for e in b.extents)))
            groups.setdefault(sig, []).append(i)
    reps = [bodies[idx[0]] for idx in groups.values()]
    longest, grams = 0.0, 0.0
    for b in reps:
        try:
            ext = b.bounding_box_oriented.primitive.extents
        except Exception:
            ext = b.extents
        longest = max(longest, float(max(ext)))
        grams += estimate_grams(b.volume, b.area, infill, bool(b.is_watertight))
    return {"pieces": len(bodies), "unique": len(reps), "dup": len(reps) < len(bodies),
            "sig": sorted(groups), "mm": round(longest, 1), "est": grams, "reps": reps}


def export_unit(info, dest) -> Path:
    """Write the unique pieces, in their original positions, to one STL."""
    import trimesh

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    trimesh.util.concatenate(info["reps"]).export(str(dest))
    return dest


# ---------------------------------------------------------------- the slicer

def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def job_idle(status) -> bool:
    if not isinstance(status, dict) or status.get("error"):
        return True
    if not any(k in status for k in ("idle", "state", "status", "running", "busy")):
        return True
    if status.get("idle") is True:
        return True
    for k in ("state", "status"):
        v = status.get(k)
        if isinstance(v, str) and v.lower() in ("idle", "done", "finished", "complete", "completed"):
            return True
    return any(status.get(k) is False for k in ("running", "busy"))


class Slicer:
    """One file per slice: clear the plate, load, auto-orient, arrange, set infill, slice."""

    def __init__(self, settings):
        self.s = settings
        self.baseline = ""
        self.current = ""

    def call(self, name, args=None, wait=120.0) -> dict:
        raise NotImplementedError

    def open(self):
        pass

    def start(self):
        status = self.call("get_status")
        if status.get("error"):
            raise SlicerUnavailable("OrcaSlicer did not answer: %s. %s" % (status["error"], NO_SLICER_HINT))
        cfg = self.call("get_config", {"keys": ["sparse_infill_density"]})
        self.baseline = self.current = str((cfg.get("config") or {}).get("sparse_infill_density") or "")

    def wait_idle(self, what):
        deadline = time.monotonic() + JOB_TIMEOUT
        while time.monotonic() < deadline:
            if job_idle(self.call("get_job_status")):
                return
            time.sleep(0.5)
        raise SliceFailed("%s did not finish in %d s" % (what, JOB_TIMEOUT))

    def run_job(self, name):
        # OrcaSlicer refuses a plate job while another one runs (409 job_running),
        # so let whatever is still running on the plate finish first.
        self.wait_idle("the plate job before " + name)
        r = self.call(name)
        if "job_running" in str(r.get("error") or ""):
            self.wait_idle("the plate job before " + name)
            r = self.call(name)
        if r.get("error"):
            raise SliceFailed("%s: %s" % (name, r["error"]))
        self.wait_idle(name)

    def clear(self):
        listed = self.call("list_objects")
        for obj in listed.get("objects") or []:
            if isinstance(obj, dict) and obj.get("id") is not None:
                self.call("delete_object", {"object_id": int(obj["id"])})

    def set_infill(self, value):
        value = value or self.baseline
        if not value or value == self.current:
            return
        r = self.call("set_config", {"changes": {"sparse_infill_density": value}})
        if r.get("error") or r.get("errors"):
            raise SliceFailed("infill %s refused: %s" % (value, r.get("error") or r.get("errors")))
        self.current = value

    def slice_file(self, path, infill=""):
        """(grams, seconds) for one file, or SliceFailed."""
        self.clear()
        r = self.call("load_model", {"path": str(Path(path).resolve())}, wait=180)
        if r.get("error"):
            raise SliceFailed("load: %s" % r["error"])
        self.run_job("auto_orient")
        self.run_job("arrange_plate")
        self.set_infill(infill)
        r = self.call("slice_and_wait", {"timeout": int(self.s.slice_timeout)}, wait=self.s.slice_timeout + 60)
        if r.get("error"):
            raise SliceFailed(str(r["error"]))
        if r.get("state") != "done":
            raise SliceFailed("slice ended %s: %s" % (r.get("state"), r.get("message") or ""))
        stats = r.get("stats") or {}
        grams = _num(stats.get("filament_used_g"))
        if not grams or grams <= 0:
            raise SliceFailed("the slicer reported no filament")
        return grams, _num(stats.get("estimated_time_seconds")) or 0.0

    def close(self):
        try:
            if self.baseline and self.current != self.baseline:
                self.call("set_config", {"changes": {"sparse_infill_density": self.baseline}})
            self.clear()
        except Exception:
            pass


class RestSlicer(Slicer):
    """The Remote API routes the connector wraps (header X-Api-Token)."""

    def open(self):
        import requests

        self.http = requests.Session()
        self.http.headers["X-Api-Token"] = self.s.orca_api_token
        self.base = self.s.orca_api_url.rstrip("/") + "/api/v1"

    def _req(self, method, path, wait=30.0, **kw) -> dict:
        import requests

        try:
            r = self.http.request(method, self.base + path, timeout=wait, **kw)
        except requests.RequestException as exc:
            return {"error": "OrcaSlicer not reachable at %s: %s" % (self.s.orca_api_url, exc)}
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400:
            body = body if isinstance(body, dict) else {}
            return {"error": str(body.get("error") or body.get("message") or "HTTP %d" % r.status_code),
                    "errors": body.get("errors"), "status": r.status_code}
        return body if isinstance(body, dict) else {"result": body}

    def call(self, name, args=None, wait=120.0) -> dict:
        a = args or {}
        if name == "get_status":
            return self._req("GET", "/status")
        if name == "list_objects":
            return self._req("GET", "/objects")
        if name == "delete_object":
            return self._req("DELETE", "/objects/%d" % int(a["object_id"]))
        if name == "load_model":
            return self._req("POST", "/model", wait=max(wait, 130.0), json={"path": a["path"]})
        if name == "auto_orient":
            return self._req("POST", "/orient")
        if name == "arrange_plate":
            return self._req("POST", "/arrange")
        if name == "get_job_status":
            return self._req("GET", "/jobs/status")
        if name == "get_config":
            r = self._req("GET", "/config")
            if r.get("error"):
                return r
            cfg = r.get("config") or {}
            keys = a.get("keys")
            return {"config": {k: cfg[k] for k in keys if k in cfg} if keys else cfg}
        if name == "set_config":
            return self._req("PUT", "/config", json=a["changes"])
        if name == "slice_and_wait":
            return self._slice_and_wait(int(a.get("timeout", 300)))
        raise ValueError("unknown slicer call %s" % name)

    def _slice_and_wait(self, timeout) -> dict:
        r = self._req("POST", "/slice")
        if r.get("error") and "slice_not_started" in json.dumps(r):
            time.sleep(1.5)
            r = self._req("POST", "/slice")
        if r.get("error"):
            return r
        deadline = time.monotonic() + timeout
        st = self._req("GET", "/slice/status")
        while not st.get("error") and st.get("state") not in ("done", "error", "idle") \
                and time.monotonic() < deadline:
            time.sleep(1.0)
            st = self._req("GET", "/slice/status")
        return st


def _result_field(res, snake, camel):
    """A tool-result field under its MCP SDK 2.x name (snake_case) or its 1.x name (camelCase)."""
    value = getattr(res, snake, None)
    return value if value is not None else getattr(res, camel, None)


def tool_json(res) -> dict:
    """The JSON a connector tool returned (structured content or its text block)."""
    sc = _result_field(res, "structured_content", "structuredContent")
    out = None
    if isinstance(sc, dict) and sc:
        inner = sc["result"] if set(sc) == {"result"} else sc
        out = inner if isinstance(inner, dict) else {"result": inner}
    else:
        for block in getattr(res, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    value = json.loads(text)
                except ValueError:
                    value = {"text": text}
                out = value if isinstance(value, dict) else {"result": value}
                break
    out = out or {}
    if _result_field(res, "is_error", "isError") and "error" not in out:
        out = dict(out, error=out.get("text") or "tool error")
    return out


class McpSlicer(Slicer):
    """The paper's setup: OrcaSlicer driven through the orcaslicer-mcp connector.

    The connector runs as a stdio MCP server (ORCA_MCP_COMMAND, by default
    "uvx orcaslicer-mcp"). One session serves the whole run from a background
    event loop, and calls are made one at a time.
    """

    def open(self):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, name="orcaslicer-mcp", daemon=True).start()
        self._queue = None
        self.tool_names = set()
        ready = concurrent.futures.Future()
        self._task = asyncio.run_coroutine_threadsafe(self._serve(ready), self._loop)
        try:
            ready.result(timeout=300)
        except concurrent.futures.TimeoutError:
            raise SlicerUnavailable("%r did not start within 5 minutes. %s"
                                    % (self.s.orca_mcp_command, NO_SLICER_HINT)) from None
        except Exception as exc:
            raise SlicerUnavailable("could not start %r: %s. %s"
                                    % (self.s.orca_mcp_command, exc, NO_SLICER_HINT)) from exc
        missing = [t for t in REQUIRED_TOOLS if t not in self.tool_names]
        if missing:
            raise SlicerUnavailable("the connector does not offer %s; update orcaslicer-mcp" % ", ".join(missing))

    async def _serve(self, ready):
        # Everything sits inside the try so that any failure (no mcp package, an
        # empty ORCA_MCP_COMMAND, an unwritable cache) reaches open() at once
        # instead of after its five-minute wait.
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            argv = shlex.split(self.s.orca_mcp_command)
            if not argv:
                raise SlicerUnavailable("ORCA_MCP_COMMAND is empty")
            env = dict(os.environ, ORCA_API_TOKEN=self.s.orca_api_token, ORCA_API_URL=self.s.orca_api_url)
            params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)
            self._queue = asyncio.Queue()
            Path(self.s.cache_dir).mkdir(parents=True, exist_ok=True)
            with open(Path(self.s.cache_dir) / "orcaslicer-mcp.log", "a", encoding="utf-8") as log:
                async with stdio_client(params, errlog=log) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        self.tool_names = {t.name for t in listed.tools}
                        ready.set_result(True)
                        while True:
                            item = await self._queue.get()
                            if item is None:
                                break
                            name, args, wait, fut = item
                            try:
                                res = await asyncio.wait_for(session.call_tool(name, args), wait)
                            except asyncio.CancelledError:
                                fut.set_exception(RuntimeError("cancelled"))
                                raise
                            except Exception as exc:
                                fut.set_exception(exc)
                            else:
                                fut.set_result(res)
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(repr(exc)))
            raise

    def call(self, name, args=None, wait=120.0) -> dict:
        if self._task.done():
            exc = self._task.exception() if not self._task.cancelled() else None
            raise SlicerUnavailable("the orcaslicer-mcp connector stopped (%s); see cache/orcaslicer-mcp.log"
                                    % (exc or "closed"))
        fut = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (name, dict(args or {}), float(wait), fut))
        try:
            res = fut.result(timeout=float(wait) + 30)
        except concurrent.futures.TimeoutError:
            return {"error": "%s timed out after %d s" % (name, wait)}
        except Exception as exc:
            return {"error": "%s failed: %s" % (name, exc)}
        return tool_json(res)

    def close(self):
        super().close()
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._task.result(timeout=15)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def open_slicer(settings):
    """The configured slicer, started and checked; None for SLICER_BACKEND=none."""
    backend = (settings.slicer_backend or "mcp").lower()
    if backend == "none":
        return None
    if backend not in ("mcp", "rest"):
        raise SlicerUnavailable("SLICER_BACKEND must be mcp, rest or none (got %r)" % backend)
    if not settings.orca_api_token:
        raise SlicerUnavailable("ORCA_API_TOKEN is not set. " + NO_SLICER_HINT)
    slicer = McpSlicer(settings) if backend == "mcp" else RestSlicer(settings)
    slicer.open()
    slicer.start()
    return slicer


# ---------------------------------------------------------------- one Thing

def model_entries(h) -> list:
    return [f for f in h.get("files") or [] if is_model_name(f.get("name", ""))]


def pick_entries(h, chosen) -> list:
    files = model_entries(h)
    chosen = set(chosen or ())
    picked = [f for f in files if f["name"] in chosen] or files
    stl = {}
    for f in files:
        name = Path(f["name"])
        if name.suffix.lower() == ".stl":
            stl.setdefault(name.stem.lower(), f)
    out = []
    for f in picked:
        name = Path(f["name"])
        if name.suffix.lower() != ".stl":
            f = stl.get(name.stem.lower(), f)
        if f not in out:
            out.append(f)
    return out


def local_files(tv, h, entries, dest) -> list:
    """[{name, path}] for the entries, downloading what is not on disk yet."""
    dest = Path(dest)
    missing = [f for f in entries if not (f.get("path") and Path(f["path"]).is_file())]
    if missing and tv is not None:
        tv.download(h["id"], missing, dest)
    out = []
    for f in entries:
        p = Path(f["path"]) if f.get("path") else dest / safe_name(f["name"])
        if p.is_file() and p.stat().st_size > 0:
            out.append({"name": f["name"], "path": p})
    return out


def cost_unit(files, *, slicer, infill, work_dir) -> dict:
    """Slice (or estimate) the files that make up one printable unit."""
    work_dir = Path(work_dir)
    grams, seconds, longest = 0.0, 0.0, None
    sliced, estimated, used, notes, seen = 0, 0, [], [], set()
    for f in files:
        info = None
        try:
            info = analyze(f["path"], infill)
        except Exception as exc:
            notes.append("%s: geometry unreadable (%s)" % (f["name"], exc))
        if info is not None:
            sig = json.dumps(info["sig"])
            if sig in seen:
                notes.append("%s: duplicate of another file, dropped" % f["name"])
                continue
            seen.add(sig)
            longest = max(longest or 0.0, info["mm"])
        target = Path(f["path"])
        if info is not None and (info["dup"] or target.suffix.lower() == ".3mf"):
            # one copy of each piece, and 3MF projects as bare geometry so their
            # embedded print settings cannot override the slicer's own
            target = export_unit(info, work_dir / (safe_name(Path(f["name"]).stem) + ".unit.stl"))
        g = None
        if slicer is not None:
            try:
                g, secs = slicer.slice_file(target, infill)
                seconds += secs
            except SliceFailed as exc:
                notes.append("%s: %s" % (f["name"], exc))
        if g is not None:
            sliced += 1
        elif info is not None and info["est"] > 0:
            g = info["est"]
            estimated += 1
        else:
            continue
        grams += g
        used.append(f["name"])
    if not used or grams <= 0:
        ms = "default"
    elif estimated == 0:
        ms = "orcaslicer"
    elif sliced == 0:
        ms = "estimator"
    else:
        ms = "mixed"
    return {"ms": ms, "g": round(grams, 2) if ms != "default" else None,
            "hrs": round(seconds / 3600.0, 3) if sliced else None,
            "mm": longest, "files": used, "notes": notes}
