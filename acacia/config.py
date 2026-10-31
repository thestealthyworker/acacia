"""Environment, versioned policy weights, thresholds and the shared seed helper.

Weights and thresholds live here only; never hard-code them elsewhere. Treat every dict
here as read-only (copy before changing).
"""

from __future__ import annotations

import hashlib
import os
import random
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env", override=False)


def _env(name: str, default: str) -> str:
    """Env value, treating an empty string as unset."""
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _resolve_dir(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (ROOT_DIR / path).resolve()


# --- environment -----------------------------------------------------------------------
SEED_SALT: str = _env("ACACIA_SEED_SALT", "acacia-2026")
DATA_DIR: Path = _resolve_dir(_env("ACACIA_DATA_DIR", "./data"))
AGENT_MODE: str = _env("ACACIA_AGENT_MODE", "rule")
MODEL: str = _env("ACACIA_MODEL", "claude-haiku-5-5")
try:
    LLM_TIMEOUT_S: float = float(_env("ACACIA_LLM_TIMEOUT_S", "20"))
except ValueError:
    LLM_TIMEOUT_S = 20.0
RUNS_DIR: Path = ROOT_DIR / "runs"
RECORDINGS_DIR: Path = ROOT_DIR / "recordings"

# --- scoring policy (HANDOVER 7) ---------------------------------------------------------
POLICY_VERSION = "policy-0.1"
POLICY: dict[str, float] = {
    "throughput": 0.35,
    "connections": 0.30,
    "fuel": 0.15,
    "turnaround": 0.20,
}

# --- impact policy (HANDOVER 18.4) -------------------------------------------------------
IMPACT_POLICY_VERSION = "impact-0.1"
IMPACT_POLICY: dict[str, float] = {
    "knock_on_min": 1.0,
    "pax_connections_at_risk": 3.0,
    "late_night_legs": 60.0,
    "flights_affected": 10.0,
}

HUBS: tuple[str, ...] = (
    "ATL", "DFW", "ORD", "DEN", "IAH", "CLT", "JFK",
    "SFO", "LAX", "PHX", "MSP", "DTW", "EWR", "SEA",
)

# --- thresholds ----------------------------------------------------------------------------
MAX_TURNS = 3
MIN_SEPARATION = 3            # minutes between runway movements
MCT = 35                      # minimum connection time, minutes
WEATHER_THRESHOLD = 15        # WEATHER_DELAY > 15 qualifies
LATE_AIRCRAFT_THRESHOLD = 20  # LATE_AIRCRAFT_DELAY > 20 qualifies
DEFAULT_TAXI_IN = 8
QUALIFY_WINDOW_START = 60     # 01:00, earliest landing must be >= this (PLAN 2.A.3)
QUALIFY_WINDOW_END = 1290     # 21:30, earliest landing must be <= this
MINUTES_PER_DAY = 1440
DAY_END = 1439                # last minute of the day; injected intervals are clamped to it
GATE_MIN_FREE = 30            # world.gate_free_from minimum free run (FABLE_ADDENDUM 4.6)
CONNECTION_WINDOW = (35, 120) # minutes, HANDOVER 18.1
CONNECTION_FANOUT = 5
PROPAGATION_MAX_DEPTH = 8
LATE_NIGHT_FROM = 23 * 60     # 23:00
CLUSTER_WINDOW_MIN = 60
TIER0_FUEL_MARGIN = 10        # land_by - first <= 10 -> tier 0


def seed_key(*parts: object) -> str:
    """Build a seed key "{SEED_SALT}|part1|part2|..."."""
    return "|".join([SEED_SALT, *(str(p) for p in parts)])


def seeded_rng(key: str) -> random.Random:
    """Deterministic RNG: random.Random(int(sha256(key).hexdigest()[:16], 16)).

    The key must start with SEED_SALT so changing the salt changes every simulated value.
    """
    if not isinstance(key, str) or not key.startswith(SEED_SALT):
        raise ValueError(f"seed key must start with SEED_SALT {SEED_SALT!r}: {key!r}")
    return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))


def min_turn(distance: int | float) -> int:
    """Minimum turnaround in minutes: 60 if distance > 2000, 45 if > 1000, else 35."""
    if distance > 2000:
        return 60
    if distance > 1000:
        return 45
    return 35
