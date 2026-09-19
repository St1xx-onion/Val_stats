"""How a player behaved in a match, as the match record itself reports it.

The form numbers say how well somebody plays. They say nothing at all about
whether the game will be worth playing, and that is a different question with
a different answer sitting in the same payload, untouched until now.

Riot records it in two places and they are worth keeping apart:

  behaviorFactors   one block per player per match: how many rounds they were
                    away from the keyboard, how many they spent standing in
                    spawn, how much damage they put into their own team, how
                    much they did to themselves. These are the numbers the
                    conduct system itself acts on.

  playerStats       one block per player per *round*, carrying wasAfk,
                    wasPenalized and stayedInSpawn. Coarser, but it says which
                    rounds, so "went afk for the last three" reads differently
                    from "was afk for three scattered rounds of a long game".

Neither is a report and neither is a ban. This is the record of what happened
in matches this machine downloaded anyway, and the strongest thing it can
honestly say is "in these thirty matches, this account was away for eleven
rounds and put 900 damage into its own team". That is a fact about the games,
not a verdict about the person, and the report that prints it says so.

Two other things ride along in the same payload because they answer questions
the rest of the program was guessing at:

  sessionPlaytimeMinutes  how long that account had been playing when the match
                          started. A measured session length, where everything
                          else here has to infer sessions from the gaps between
                          match times.

  partyRRPenalties        the RR penalty applied to each party in the match,
                          keyed by party id. Non-zero means that party queued
                          with a rank gap wide enough for the system to charge
                          them for it - which is a party the system is naming
                          outright, not one inferred from co-occurrence.

Nothing here reads a name, and nothing here goes to the network.
"""

from collections import namedtuple

# Friendly fire below this is a stray Molly, not a grudge, and naming every
# one of them would bury the matches that are actually worth looking at.
FF_WORTH_NAMING = 100

# when:  ISO start of the match, or "" when nothing recorded one
# queue: the queue it was played in
# what:  everything notable about that match, already worded
Incident = namedtuple("Incident", "when queue match_id what")

FIELDS = (
    "afk_rounds",
    "spawn_rounds",
    "penalised_rounds",
    "ff_damage",
    "ff_taken",
    "self_damage",
    "session_minutes",
    "account_level",
    "party_size",
    "party_penalty",
    "rounds",
)

BLANK = {field: 0 for field in FIELDS}


def extract(details):
    """match_id, {puuid: conduct} for one match-details payload."""
    match_id = (details.get("matchInfo") or {}).get("matchId") or ""
    penalties = (details.get("matchInfo") or {}).get("partyRRPenalties") or {}

    sizes = {}
    for player in details.get("players") or []:
        party_id = player.get("partyId") or ""
        if party_id:
            sizes[party_id] = sizes.get(party_id, 0) + 1

    per_player = {}
    for player in details.get("players") or []:
        subject = player.get("subject")
        if not subject:
            continue
        factors = player.get("behaviorFactors") or {}
        party_id = player.get("partyId") or ""
        row = dict(BLANK)
        row.update(
            afk_rounds=_int(factors.get("afkRounds")),
            spawn_rounds=_int(factors.get("stayedInSpawnRounds")),
            ff_damage=_int(factors.get("friendlyFireOutgoing")),
            ff_taken=_int(factors.get("friendlyFireIncoming")),
            self_damage=_int(factors.get("selfDamage")),
            session_minutes=_int(player.get("sessionPlaytimeMinutes")),
            account_level=_int(player.get("accountLevel")),
            party_size=sizes.get(party_id, 0),
            # Riot stores this per party; a player carries their own party's.
            party_penalty=_hundredths(penalties.get(party_id)),
            rounds=_int((player.get("stats") or {}).get("roundsPlayed")),
        )
        per_player[subject] = row

    # The per-round flags, which behaviorFactors does not cover: a round can be
    # penalised without counting as afk, and the count is what the report reads.
    for round_result in details.get("roundResults") or []:
        for stat in round_result.get("playerStats") or []:
            row = per_player.get(stat.get("subject"))
            if row is None:
                continue
            if stat.get("wasPenalized"):
                row["penalised_rounds"] += 1

    return match_id, per_player


def _int(value):
    """Riot mixes ints and floats in this block; the report wants whole units."""
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _hundredths(value):
    """A penalty fraction as whole hundredths, so the column stays an integer.

    0.25 is stored as 25, which reads as "a quarter of the RR was withheld"
    and survives a database round-trip without carrying a float's baggage.
    """
    try:
        return int(round(float(value or 0) * 100))
    except (TypeError, ValueError):
        return 0


def summarise(rows):
    """Totals over a player's cached conduct rows, or None when there are none.

    Rounds are summed alongside, because eleven afk rounds out of 600 and
    eleven out of 40 are not the same claim and a total on its own cannot tell
    them apart.
    """
    rows = [row for row in rows or () if row]
    if not rows:
        return None
    sessions = [row["session_minutes"] for row in rows if row.get("session_minutes")]
    total = {
        "matches": len(rows),
        "rounds": sum(row.get("rounds") or 0 for row in rows),
        "afk_rounds": sum(row.get("afk_rounds") or 0 for row in rows),
        "spawn_rounds": sum(row.get("spawn_rounds") or 0 for row in rows),
        "penalised_rounds": sum(row.get("penalised_rounds") or 0 for row in rows),
        "ff_damage": sum(row.get("ff_damage") or 0 for row in rows),
        "ff_taken": sum(row.get("ff_taken") or 0 for row in rows),
        "self_damage": sum(row.get("self_damage") or 0 for row in rows),
        "afk_matches": sum(1 for row in rows if row.get("afk_rounds")),
        "penalised_matches": sum(1 for row in rows if row.get("penalised_rounds")),
        "stacked_matches": sum(1 for row in rows if (row.get("party_size") or 0) > 1),
        "penalised_parties": sum(1 for row in rows if row.get("party_penalty")),
        "level": max((row.get("account_level") or 0) for row in rows),
    }
    # The longest session seen is the useful one next to the average: it is
    # what "plays until four in the morning sometimes" looks like in a number.
    total["session_avg"] = (sum(sessions) / len(sessions)) if sessions else None
    total["session_max"] = max(sessions) if sessions else None
    return total


def incidents(rows, penalties=None):
    """One entry per match where something happened, newest first.

    The totals say a player was afk for eleven rounds; they cannot say whether
    that was one bad evening in March or a habit. This can, because every row
    it reads is dated - so the report can stop summarising and name the
    matches. `penalties` is {match id: RR docked}, from the ranked history,
    which is the other half of the same story: what it cost them.
    """
    out = []
    for row in rows or ():
        notes = []
        if row.get("afk_rounds"):
            notes.append(f"afk for {_count(row['afk_rounds'], 'round')}")
        if row.get("penalised_rounds"):
            notes.append(f"penalised in {_count(row['penalised_rounds'], 'round')}")
        if row.get("spawn_rounds"):
            notes.append(f"{_count(row['spawn_rounds'], 'round')} in spawn")
        if (row.get("ff_damage") or 0) >= FF_WORTH_NAMING:
            notes.append(f"{row['ff_damage']} damage to their own team")
        if not notes:
            continue
        docked = (penalties or {}).get(row.get("match_id")) or 0
        if docked:
            notes.append(f"-{docked} RR")
        out.append(
            Incident(
                when=row.get("started_at") or "",
                queue=row.get("queue") or "",
                match_id=row.get("match_id") or "",
                what=", ".join(notes),
            )
        )
    return out


def flags(total):
    """Short warnings worth putting on screen, worst first, or an empty list.

    Deliberately few and deliberately dull. Every one of these is a count from
    the match record with a threshold on it, not a judgement: the report prints
    the number next to the flag so the reader can disagree with the threshold.
    """
    if not total or not total["rounds"]:
        return []
    out = []
    afk_rate = 100.0 * total["afk_rounds"] / total["rounds"]
    if afk_rate >= 2.0:
        out.append(
            f"afk in {total['afk_rounds']} of {total['rounds']} rounds "
            f"({afk_rate:.1f}%) across {_count(total['afk_matches'], 'match', 'matches')}"
        )
    if total["penalised_rounds"]:
        out.append(
            f"penalised in {_count(total['penalised_rounds'], 'round')} across "
            f"{_count(total['penalised_matches'], 'match', 'matches')}"
        )
    if total["ff_damage"] >= 500:
        out.append(f"{total['ff_damage']} damage dealt to their own team")
    if total["spawn_rounds"] >= 5:
        out.append(f"stayed in spawn for {_count(total['spawn_rounds'], 'round')}")
    return out


def _count(number, singular, plural=""):
    """"1 round" / "4 rounds" - a report that cannot count reads as careless."""
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"
