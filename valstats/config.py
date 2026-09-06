"""Configuration loading."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "poll_interval": 1.0,
    "show_skins": True,
    "show_peak_rank": True,
    "respect_streamer_mode": True,
    "track_encounters": True,
    # ACS / HS% / K-D from recent matches. Costs extra requests on a cold cache.
    "fetch_performance": True,
    "performance_matches": 5,
    # Which queue recent form is read from, and whether to fall back to every
    # queue for players who simply have no competitive history.
    "performance_queue": "competitive",
    "performance_queue_fallback": True,
    # Print a scoreboard for the match you just played, once it is over.
    "post_match_summary": True,
    # "auto" scores players against everyone this install has ever met, once
    # enough of them are cached; "fixed" always uses the bands in perf.rating().
    "rating_calibration": "auto",
    # How long a name / MMR / match-history answer stays good for, in minutes.
    # Agent select and the match itself would otherwise ask for all of it twice.
    "player_cache_minutes": 20,
    # Minimum gap between two outbound requests, in seconds.
    "request_gap": 0.15,
    # Leave null to auto-detect from ShooterGame.log
    "region": None,
    "shard": None,
    "client_version": None,
}

# Keys whose default is null and so says nothing about the type we accept.
FREE_FORM = {"region", "shard", "client_version"}


def _type_ok(value, default):
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def validate(raw, warn=print):
    """Merge user settings over the defaults, saying so when one makes no sense.

    A silently ignored typo is the worst outcome here: "fetch_perfomance" would
    just quietly do nothing at all.
    """
    cfg = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key not in DEFAULTS:
            warn(f"[config] unknown setting {key!r} - ignored")
            continue
        default = DEFAULTS[key]
        if key in FREE_FORM:
            if value is not None and not isinstance(value, str):
                warn(f"[config] {key} should be text or null - using the default")
                continue
        elif not _type_ok(value, default):
            want = type(default).__name__
            warn(f"[config] {key} should be {want} - using the default {default!r}")
            continue
        cfg[key] = value

    if cfg["poll_interval"] < 0.2:
        warn("[config] poll_interval below 0.2s is pointless - raised to 0.2")
        cfg["poll_interval"] = 0.2
    if cfg["performance_matches"] < 1:
        warn("[config] performance_matches must be at least 1 - using 1")
        cfg["performance_matches"] = 1
    if cfg["rating_calibration"] not in ("auto", "fixed"):
        warn('[config] rating_calibration must be "auto" or "fixed" - using "auto"')
        cfg["rating_calibration"] = "auto"
    if cfg["request_gap"] < 0:
        cfg["request_gap"] = 0.0
    return cfg


def load():
    path = ROOT / "config.json"
    raw = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[config] ignoring config.json: {exc}")
            raw = {}
    if not isinstance(raw, dict):
        print("[config] config.json should hold an object - ignoring it")
        raw = {}
    return validate(raw)
