"""Static game content from valorant-api.com (agents, rank tiers, acts, skins).

Cached on disk for a day so a session of matches costs zero extra requests.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_TTL = 24 * 60 * 60
BASE = "https://valorant-api.com/v1"

MELEE_UUID = "2f59173c-4bed-b6c3-2191-dea9b58be9c7"
SOCKET_SKIN = "bcef87d6-209b-46c6-8b19-fbe40bd95abc"

UNRANKED = {"name": "Unranked", "color": "808080"}

# Keep rank labels narrow so the table fits a normal terminal.
ABBREV = {
    "IRON": "Iron",
    "BRONZE": "Bronze",
    "SILVER": "Silver",
    "GOLD": "Gold",
    "PLATINUM": "Plat",
    "DIAMOND": "Dia",
    "ASCENDANT": "Asc",
    "IMMORTAL": "Imm",
    "RADIANT": "Radiant",
}


def _tier_label(tier):
    """"PLATINUM 3" -> "Plat 3". Tiers 1-2 are placeholders Riot never uses."""
    raw = (tier.get("tierName") or "").strip().upper()
    if not raw or raw.startswith("UNUSED") or raw == "UNRANKED":
        return UNRANKED["name"]
    division, _, number = raw.partition(" ")
    short = ABBREV.get(division, division.title())
    return f"{short} {number}".strip()


def _get(endpoint, params=None):
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / (endpoint.replace("/", "_") + ".json")
    if cache.is_file() and time.time() - cache.stat().st_mtime < CACHE_TTL:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    resp = requests.get(f"{BASE}/{endpoint}", params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()["data"]
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


class Content:
    def __init__(self):
        self.agents = {}
        self.tiers = {}
        self.skins = {}
        self.current_act = None

    def load(self, want_skins=True):
        self._load_agents()
        self._load_tiers()
        self._load_acts()
        if want_skins:
            self._load_skins()
        return self

    def _load_agents(self):
        for agent in _get("agents", {"isPlayableCharacter": "true"}):
            self.agents[agent["uuid"].lower()] = agent["displayName"]

    def _load_tiers(self):
        episodes = _get("competitivetiers")
        # The last episode carries the tier table currently in use.
        for tier in episodes[-1]["tiers"]:
            self.tiers[tier["tier"]] = {
                "name": _tier_label(tier),
                "color": (tier.get("color") or "808080ff")[:6],
            }

    def _load_acts(self):
        now = datetime.now(timezone.utc)
        for season in _get("seasons"):
            if season.get("type") != "EAresSeasonType::Act":
                continue
            try:
                start = datetime.fromisoformat(season["startTime"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(season["endTime"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if start <= now <= end:
                self.current_act = season["uuid"]
                return

    def _load_skins(self):
        for skin in _get("weapons/skins"):
            self.skins[skin["uuid"].lower()] = skin["displayName"]

    # ------------------------------------------------------------- accessors

    def agent(self, uuid):
        if not uuid:
            return "-"
        return self.agents.get(uuid.lower(), "?")

    def tier(self, number):
        return self.tiers.get(number or 0, UNRANKED)

    def skin_name(self, loadout_items, weapon_uuid=MELEE_UUID):
        """Resolve a weapon skin from a core-game loadout Items blob."""
        if not loadout_items:
            return "-"
        item = loadout_items.get(weapon_uuid) or loadout_items.get(weapon_uuid.upper())
        if not item:
            return "-"
        socket = (item.get("Sockets") or {}).get(SOCKET_SKIN)
        skin_id = ((socket or {}).get("Item") or {}).get("ID")
        if not skin_id:
            return "-"
        name = self.skins.get(skin_id.lower(), "?")
        # "Prime//2.0 Karambit" reads better as just the skin line.
        return name.replace(" Melee", "").strip()
