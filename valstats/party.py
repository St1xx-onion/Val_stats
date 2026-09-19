"""Who queued together, worked out from matches already in the local cache.

Riot does not tell you which of the five enemies came in as a duo: PartyID is
served for your own party and nobody else's. But a party leaves a trace anyway.
People who queue together keep landing in the same matches on the same side,
and those matches are already sitting in the cache - they were downloaded for
the form numbers. So this costs no requests at all, just a query.

Three rules keep it from inventing parties out of coincidence:

  * only same-side matches count. Meeting as opponents says nothing: that is
    what matchmaking does to everyone.
  * one shared match is a coincidence, especially in a small region where the
    same faces come round again. Two is the default threshold.
  * two matches an hour apart are not two pieces of evidence. Queueing together
    is one decision, made once and then repeated by pressing Play again, so a
    night of five games back to back is closer to one observation than to five.
    What counts is the number of separate occasions - sessions - and a pair
    that came back on another evening is a far stronger claim than a pair
    matchmaking happened to join up twice before dinner.

That last rule is what the match times are for, and it cuts both ways. A long
run inside a single session is not chance either: matchmaking will put two
strangers on the same side twice in an evening, but not four times. So a pair
is called a party when it either came back on a second occasion or stayed
together through a run longer than coincidence explains. Anything that clears
the threshold without clearing one of those two bars is still reported - it is
real evidence - but marked as thin, in the table and in the line under it.

A group is then the connected component of those pairs. That can over-reach -
A duoed with B on Monday and B with C on Tuesday, and neither pairing involved
the third - so a component whose every pair is not linked is reported as
tentative rather than silently presented as a three-stack.

Nothing here reads a name, and nothing here goes out to the network.
"""

from collections import namedtuple
from datetime import datetime, timezone
from itertools import combinations

# Letters handed out in the table. More groups than this in one lobby is not
# a thing that happens; if it ever does, the rest simply go unlabelled.
LABELS = "ABCDEFGH"

# Shared same-side matches before two players count as queueing together.
MIN_SHARED = 2

# A break longer than this ends a play session, in hours. Three is comfortably
# past a coffee break and comfortably short of "came back the next evening",
# which is the distinction the number exists to draw.
SESSION_GAP_HOURS = 3.0

# Same-side matches inside one session that stop reading as matchmaking. Kept
# deliberately short of a full evening, and deliberately conservative: by the
# fourth repeat a party is much the simpler explanation, but three still
# happens to strangers in a thin queue.
RUN_IS_A_PARTY = 4

# matches:  shared same-side matches, the raw count
# sessions: separate occasions those fall into, 0 when no match time is known
# last:     the most recent shared match we have a time for, ISO, or ""
# strong:   came back on a second occasion, or ran longer than chance explains
# confirmed: Riot's own party id says these two queued together. Not inferred
#            from anything - see db.pool_parties. A confirmed pair skips every
#            threshold below, because the thresholds exist to guess at exactly
#            the thing this knows.
Bond = namedtuple("Bond", "matches sessions last strong confirmed")

# label:    "A", "B", ... as shown in the table
# members:  puuids, sorted, always two or more
# shared:   the weakest link in the group - the fewest matches any pair shares
# sessions: the same for occasions; 0 when the cache holds no times at all
# solid:    every pair inside is linked, not just enough of them to connect
# strong:   every pair inside cleared the bar above, not merely the threshold
Group = namedtuple("Group", "label team members shared sessions solid strong confirmed")

_Raw = namedtuple("_Raw", "team members shared sessions solid strong confirmed")


def weigh(times, session_gap=SESSION_GAP_HOURS, confirmed=0):
    """One pair's shared matches read as evidence: how many, on how many days.

    `times` holds one entry per shared same-side match - the match start where
    the cache recorded one, None where it did not. Sessions come back as 0 when
    not a single time is known, which is the honest answer and the one that
    makes every caller here fall back on the raw count, exactly as this module
    did before it had the times to do better.
    """
    stamps = sorted(stamp for stamp in (_parse(value) for value in times) if stamp)
    matches = len(times)
    sessions = _sessions(stamps, session_gap)
    last = stamps[-1].isoformat(timespec="seconds") if stamps else ""
    strong = confirmed or sessions >= 2 or matches >= RUN_IS_A_PARTY
    return Bond(matches, sessions, last, strong, confirmed)


def _sessions(stamps, gap_hours):
    """How many separate sittings a sorted list of match times falls into."""
    if not stamps:
        return 0
    gap = max(0.0, gap_hours) * 3600
    count = 1
    for earlier, later in zip(stamps, stamps[1:]):
        if (later - earlier).total_seconds() > gap:
            count += 1
    return count


def _parse(value):
    """One stored match time as an aware datetime, or None if it is unusable."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _times(value):
    """Evidence for one pair as a list of match times.

    Callers hand over what the cache gave them, which is a list of times. A
    bare count is accepted too and read as that many matches at unknown times:
    it is what the query returned before match times were joined in, and it
    degrades to the old count-only behaviour rather than to a crash.
    """
    if isinstance(value, int):
        return [None] * max(0, value)
    return list(value or ())


def _pairs_in(members):
    """Every pair inside one known roster, sorted, or nothing for a solo queue."""
    unique = sorted({m for m in (members or ()) if m})
    return list(combinations(unique, 2)) if len(unique) > 1 else []


def _known(pair, confirmed):
    """1 when this pair is confirmed with no timestamps behind it, else 0.

    Your own party arrives as a fact with no history attached - Riot says who
    is in it right now, not when they last queued - so it needs a way to count
    as confirmed without pretending to a match count it does not have.
    """
    return 1 if pair in confirmed else 0


def detect(
    rows,
    evidence,
    min_shared=MIN_SHARED,
    own_team=None,
    session_gap=SESSION_GAP_HOURS,
    confirmed=None,
    roster=None,
):
    """The parties in one lobby, most confident first.

    rows:      PlayerRow objects for the whole lobby
    evidence:  {(a, b): [match times]}, from Encounters.party_timeline
    confirmed: {(a, b): [match times]} where Riot's own party id matched, from
               a pooled match. Evidence of a different kind entirely: it is not
               a co-occurrence that has to be argued from, it is the answer.
    roster:    puuids of your own party, from client.own_party. The same kind
               of answer, about the one party Riot will discuss, read live.
    """
    if not evidence and not confirmed and not roster:
        return []

    confirmed = dict(confirmed or {})
    # Your own party, which Riot names outright. Folded in as confirmed pairs
    # so that everything downstream - the letter, the status line, the tests -
    # treats "Riot told us" and "the pool told us" the same way, because they
    # are the same claim. The difference is only where it was read.
    for pair in _pairs_in(roster):
        confirmed.setdefault(pair, [])

    bonds = {
        pair: weigh(_times(value), session_gap, len(confirmed.get(pair) or ()) or _known(pair, confirmed))
        for pair, value in evidence.items()
    }
    # A pair Riot's records put in one party is a party whether or not it also
    # cleared the co-occurrence threshold, so it is added rather than filtered.
    for pair, times in confirmed.items():
        if pair not in bonds:
            bonds[pair] = weigh(_times(times), session_gap, len(times or ()) or 1)

    by_team = {}
    for row in rows:
        if row.puuid and row.team:
            by_team.setdefault(row.team, []).append(row.puuid)

    found = []
    for team, members in sorted(by_team.items()):
        found.extend(_groups_in(team, members, bonds, max(1, min_shared)))

    # Your own side first, then the groups worth believing, then bigger ones,
    # then by puuid - so a redraw of the same lobby hands out the same letters.
    found.sort(
        key=lambda g: (
            own_team is not None and g.team != own_team,
            not g.strong,
            g.team,
            -len(g.members),
            g.members,
        )
    )
    return [
        Group(LABELS[index] if index < len(LABELS) else "*", *raw)
        for index, raw in enumerate(found)
    ]


def _groups_in(team, members, bonds, min_shared):
    """A _Raw for each clump of players on one side."""
    linked = {}
    for pair in combinations(sorted(set(members)), 2):
        bond = bonds.get(pair)
        if bond and (bond.confirmed or bond.matches >= min_shared):
            linked.setdefault(pair[0], set()).add(pair[1])
            linked.setdefault(pair[1], set()).add(pair[0])

    out = []
    for component in _components(linked):
        inside = [bonds.get(pair, Bond(0, 0, "", False, 0)) for pair in combinations(component, 2)]
        # One pair with no time on any of its matches makes the whole group's
        # session count unknowable, and 0 is how this module says "unknown".
        known = [bond.sessions for bond in inside if bond.sessions]
        out.append(
            _Raw(
                team,
                component,
                min(bond.matches for bond in inside),
                min(known) if len(known) == len(inside) else 0,
                all(bond.confirmed or bond.matches >= min_shared for bond in inside),
                all(bond.strong for bond in inside),
                all(bond.confirmed for bond in inside),
            )
        )
    return out


def _components(linked):
    """Connected components of an adjacency map, each returned sorted."""
    seen = set()
    out = []
    for start in sorted(linked):
        if start in seen:
            continue
        stack, group = [start], []
        seen.add(start)
        while stack:
            node = stack.pop()
            group.append(node)
            for neighbour in linked[node] - seen:
                seen.add(neighbour)
                stack.append(neighbour)
        out.append(sorted(group))
    return out


def apply(rows, groups):
    """Stamp each row with its group letter, blanking the ones with no group.

    A group the module is not confident about - thin evidence, or a component
    held together by a chain rather than by every pair - is stamped "A?" and
    drawn faintly. The letter still tells you who is with whom; the mark says
    not to build a ban on it.
    """
    labels = {
        # "A*" is Riot's own party id agreeing; "A" is a confident inference;
        # "A?" is real evidence that is not enough to build a ban on.
        puuid: group.label
        + ("*" if group.confirmed else "" if group.strong and group.solid else "?")
        for group in groups
        for puuid in group.members
    }
    for row in rows:
        row.party = labels.get(row.puuid, "")
    return rows


def describe(groups, samples=None):
    """The line under the table: what was found, and what it rests on.

    "3 of 5" reads very differently from "3 of 40", so the denominator is worth
    printing: a pair can only share as many matches as the thinner of the two
    has cached. The session count is worth printing for the same reason - four
    matches over three evenings and four in one sitting are not the same claim.
    Returns "" when there is nothing to say, so a caller can drop the line
    entirely rather than print a heading over an empty list.
    """
    if not groups:
        return ""
    parts = []
    for group in groups:
        if group.confirmed and not group.shared:
            # Confirmed with no shared matches behind it is your own party,
            # read from Riot a moment ago. A match count would be noise: the
            # claim does not rest on one, and printing "0 shared" under a
            # certainty reads as the opposite of what it means.
            parts.append(f"{group.label}: {len(group.members)} players")
            continue
        note = f"{group.label}: {len(group.members)} players, {group.shared}"
        cap = _cap(group, samples)
        note += f" of {cap} shared" if cap else " shared"
        note += _occasions(group)
        if group.confirmed:
            # Nothing to hedge: Riot's record of a pooled match says these
            # people queued together, so the line stops guessing out loud.
            parts.append(f"{note} (confirmed)")
            continue
        doubts = []
        if not group.solid:
            doubts.append("partly")
        if not group.strong:
            doubts.append("thin")
        parts.append(f"{note} ({', '.join(doubts)})" if doubts else note)
    # "Likely" is right for an inference and wrong for a fact. When every group
    # on screen came from Riot rather than from arithmetic, say so.
    heading = "queued together" if all(g.confirmed for g in groups) else "likely queued together"
    return f"{heading} - " + ", ".join(parts)


def _occasions(group):
    """How the shared matches were spread out, when the cache knows."""
    if not group.sessions:
        return ""
    if group.sessions == 1:
        return " in one sitting"
    return f" over {group.sessions} sessions"


def _cap(group, samples):
    """The most matches the group's weakest pair could possibly have shared."""
    if not samples:
        return 0
    known = [samples.get(puuid, 0) for puuid in group.members]
    return min(known) if all(known) else 0
