"""Run settings, read from the environment and an optional .env file.

Every key is listed in .env.example. Secrets (the OpenAI key and the browser
cookies) only ever come from the environment; nothing here writes them out.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Cloudflare ties cf_clearance to the browser that earned it, so requests carrying
# the copied cookies should also carry that browser's user agent (BROWSER_USER_AGENT).
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# eBay prices a page in the currency of the ship-to country. These search parameters set
# the ship-to to New York (country 1, ZIP 10001), so prices come back in US dollars
# wherever the run happens. EBAY_EXTRA_PARAMS replaces them; "none" sends none.
US_SHIP_TO = "_fcid=1&_stpos=10001"


def load_dotenv(path=None) -> bool:
    """Load KEY=VALUE lines into os.environ without overriding variables already set.

    Uses python-dotenv when it is installed and a small parser otherwise, so the
    offline tools (site builder, tests) run on a bare Python.
    """
    path = Path(path) if path else ROOT / ".env"
    if not path.is_file():
        return False
    try:
        from dotenv import load_dotenv as _load
        return bool(_load(path, override=False))
    except ImportError:
        pass
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


def _env(*names, default=""):
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _number(name, default):
    raw = _env(name)
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        raise SystemExit("%s must be a number, got %r" % (name, raw)) from None


def _ebay_params():
    raw = _env("EBAY_EXTRA_PARAMS", default=US_SHIP_TO)
    return "" if raw.lower() == "none" else raw


@dataclass
class Settings:
    # AI steps (paper: OpenAI's GPT-5.6 Luna for every AI step)
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-luna"
    reasoning_effort: str = ""          # empty: the model's default
    image_detail: str = "auto"
    # Thingiverse website session (paper: cookies of a signed-in browser session)
    thingiverse_cookie: str = ""
    thingiverse_web_token: str = ""     # bearer the website's own JS sends to /api
    thingiverse_token: str = ""         # optional developer-API token: download counts, api-hosted files
    # eBay website session
    ebay_cookie: str = ""
    ebay_params: str = US_SHIP_TO       # extra query string for every eBay search
    user_agent: str = DEFAULT_UA
    http_delay: float = 1.5             # seconds between requests to one host
    # Slicing (paper: OrcaSlicer 2.4.2 driven through the orcaslicer-mcp connector)
    slicer_backend: str = "mcp"         # mcp | rest | none
    orca_mcp_command: str = "uvx orcaslicer-mcp"
    orca_api_token: str = ""
    orca_api_url: str = "http://127.0.0.1:13130"
    slice_timeout: float = 600.0
    # Arithmetic and guards
    pla_usd_per_kg: float = 25.0
    bed_mm: float = 256.0
    scale_usd_per_g: float = 100.0
    default_grams: float = 30.0
    cache_dir: Path = field(default_factory=lambda: ROOT / "cache")

    @classmethod
    def from_env(cls, env_file=None) -> "Settings":
        load_dotenv(env_file)
        return cls(
            openai_api_key=_env("OPENAI_API_KEY", "OpenAI_API_KEY"),
            openai_model=_env("OPENAI_MODEL", default="gpt-5.6-luna"),
            reasoning_effort=_env("OPENAI_REASONING_EFFORT"),
            image_detail=_env("OPENAI_IMAGE_DETAIL", default="auto"),
            thingiverse_cookie=_env("THINGIVERSE_COOKIE"),
            thingiverse_web_token=_env("THINGIVERSE_WEB_TOKEN"),
            thingiverse_token=_env("THINGIVERSE_TOKEN"),
            ebay_cookie=_env("EBAY_COOKIE"),
            ebay_params=_ebay_params(),
            user_agent=_env("BROWSER_USER_AGENT", default=DEFAULT_UA),
            http_delay=_number("HTTP_DELAY", 1.5),
            slicer_backend=_env("SLICER_BACKEND", default="mcp").lower(),
            orca_mcp_command=_env("ORCA_MCP_COMMAND", default="uvx orcaslicer-mcp"),
            orca_api_token=_env("ORCA_API_TOKEN"),
            orca_api_url=_env("ORCA_API_URL", default="http://127.0.0.1:13130").rstrip("/"),
            slice_timeout=_number("ORCA_SLICE_TIMEOUT", 600),
            pla_usd_per_kg=_number("PLA_USD_PER_KG", 25),
            bed_mm=_number("BED_MM", 256),
            scale_usd_per_g=_number("SCALE_USD_PER_G", 100),
            default_grams=_number("DEFAULT_GRAMS", 30),
            cache_dir=Path(_env("CACHE_DIR", default=str(ROOT / "cache"))),
        )

    @property
    def default_print_cost(self) -> float:
        """The flat fallback: DEFAULT_GRAMS of PLA (30 g at $25/kg = $0.75)."""
        return round(self.default_grams * self.pla_usd_per_kg / 1000.0, 3)
