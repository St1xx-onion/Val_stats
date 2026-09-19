"""What has changed about a player since the last time you met them.

The database already knows you have met somebody 4 times. That is the `Seen`
column, and it is a number that says almost nothing: four meetings with a
Silver who is still Silver and four with a Silver who is now Ascendant are the
same 4. The interesting part was always the difference, and it has been sitting
in rank_snapshots the whole time unused.

Two kinds of progress, and they are kept apart because they are known in very
different ways:

  rank    read straight out of rank_snapshots, which has recorded a tier and RR
          every time this program has seen anybody since it was written. It is
          exact, it needs no network, and it is available for a player who is
          hiding behind Incognito right now - a rank is not a name.

  form    ACS, K/D, KAST, the 0-1000 rating. These are computed from the last
          N matches, so "what they were in June" is not recoverable today: the
          matches that made up that number have scrolled out of the window. The
          only way to have it is to have written it down at the time, which is
          what form_snapshots is - one row per encounter, taken as the live
          table fills in.

So rank progress works on the first encounter after this module exists, and
form progress works on the second. That asymmetry is real and the table says
which it is showing rather than pretending both arrived together.

Nothing here goes to the network, and nothing here reads a name.
"""

from collections import namedtuple
from datetime import datetime, timezone

# tiers:   tier now minus tier then, in divisions. 0 when level, None when we
#          cannot say - no snapshot then, or no rank now.
# rr:      the same for RR, only meaningful inside one tier
# since:   when the earlier reading was taken, ISO, or ""
# meets:   how many separate matches we have seen them in before this one
# rating:  0-1000 now minus 0-1000 then, or None
# acs/kd:  the same for those two, or None
Progress = namedtuple("Progress", "tiers rr since meets rating acs kd")

BLANK = Progress(None, None, "", 0, None, None, None)

# A reading older than this is still shown, but it is worth knowing that a
# player's rank a year ago says little about them now. In days.
STALE_DAYS = 120


def between(then, now):
    """Rank movement between two {tier, rr} readings, in divisions and RR.

    Returns (None, None) unless both readings carry a real tier: an unranked
    player and a player we simply did not look up are both tier 0 here, and
    inventing a fall from Gold to nothing out of that would be a lie.
    """
    if not then or not now:
        return None, None
    old_tier = then.get("tier") or 0
    new_tier = now.get("tier") or 0
    if not old_tier or not new_tier:
        return None, None
    return new_tier - old_tier, (now.get("rr") or 0) - (then.get("rr") or 0)


def weigh(rank_then, rank_now, form_then, form_now, meets=0):
    """Everything that changed, as one Progress.

    Both halves are optional and independent: a player we have a rank history
    for but no form snapshot gets the rank half and None for the rest, which is
    exactly the state every player is in the first time this runs.
    """
    tiers, rr = between(rank_then, rank_now)
    since = (rank_then or {}).get("ts") or (form_then or {}).get("ts") or ""

    rating = acs = kd = None
    if form_then and form_now:
        rating = _gap(form_now.get("rating"), form_then.get("rating"))
        acs = _gap(form_now.get("acs"), form_then.get("acs"))
        kd = _gap(form_now.get("kd"), form_then.get("kd"))

    return Progress(tiers, rr, since, meets, rating, acs, kd)


def _gap(now, then):
    """now - then, when both are real numbers. None otherwise."""
    if now is None or then is None:
        return None
    try:
        return float(now) - float(then)
    except (TypeError, ValueError):
        return None


def anything(progress):
    """Is there any movement worth drawing a column for?"""
    if not progress:
        return False
    return progress.tiers is not None or progress.rating is not None


def mark(progress):
    """The short cell for the live table: "+2", "-1", "=" or "".

    Divisions, not RR. RR moves every single match and would make the column
    noise; a division is the thing you would actually remark on.
    """
    if not progress or progress.tiers is None:
        return ""
    if progress.tiers > 0:
        return f"+{progress.tiers}"
    if progress.tiers < 0:
        return str(progress.tiers)
    return "="


def stale(progress, days=STALE_DAYS):
    """Is the earlier reading old enough that the comparison is weak?"""
    if not progress or not progress.since:
        return False
    try:
        when = datetime.fromisoformat(progress.since)
    except ValueError:
        return False
    if not when.tzinfo:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).days > days


def describe(progress, content=None, rank_then=None, rank_now=None):
    """The long form, for `who` and for the line under the table.

    "Gold 2 -> Platinum 1, +2 since 2026-03-04, rating +85 over 3 meetings"
    """
    if not anything(progress):
        return ""
    parts = []

    if progress.tiers is not None:
        if content and rank_then and rank_now:
            was = content.tier(rank_then.get("tier") or 0)
            now = content.tier(rank_now.get("tier") or 0)
            if progress.tiers:
                parts.append(f"{was} -> {now}")
            else:
                parts.append(f"still {now}")
        elif progress.tiers:
            parts.append(f"{progress.tiers:+d} divisions")
        else:
            parts.append("same rank")

    if progress.rating is not None and abs(progress.rating) >= 1:
        parts.append(f"rating {progress.rating:+.0f}")
    if progress.acs is not None and abs(progress.acs) >= 1:
        parts.append(f"ACS {progress.acs:+.0f}")
    if progress.kd is not None and abs(progress.kd) >= 0.01:
        parts.append(f"K/D {progress.kd:+.2f}")

    note = ", ".join(parts)
    if progress.since:
        note += f" since {progress.since[:10]}"
    if progress.meets:
        plural = "s" if progress.meets != 1 else ""
        note += f", over {progress.meets} earlier meeting{plural}"
    return note


def attach(rows, rank_before, form_before, meets=None):
    """Stamp each row with its Progress, from what the database remembered.

    rank_before: {puuid: {tier, rr, ts}} as of before this match
    form_before: {puuid: {rating, acs, kd, ts}} from the last encounter
    """
    meets = meets or {}
    for row in rows:
        if row.is_self or not row.puuid:
            continue
        now = {"tier": row.tier, "rr": row.rr}
        form_now = None
        if row.rating is not None:
            form_now = {"rating": row.rating, "acs": row.acs, "kd": row.kd}
        row.progress = weigh(
            rank_before.get(row.puuid),
            now,
            form_before.get(row.puuid),
            form_now,
            meets.get(row.puuid, 0),
        )
    return rows
