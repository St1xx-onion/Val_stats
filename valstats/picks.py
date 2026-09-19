"""What to pick, given who your team already locked and how you actually play.

Two things decide it, and they are kept apart on purpose so the line under the
table can say which one is talking:

  * what the composition is missing - roles come from Riot's own agent data,
    so a new agent lands in the right bucket without anyone editing a table;
  * how you do on that agent, weighted the same way the 0-1000 score is, which
    means one lucky match on someone does not make them a recommendation.

With an empty cache only the first half has anything to say, and the reason
column will admit that rather than dress a guess up as a number.
"""

from collections import namedtuple

from .perf import by_agent

# What the n-th agent of a role is worth to a composition, 0..60. These are
# deliberate guesses in the spirit of perf.BANDS - unlike ACS there is no
# population to calibrate them against, so they stay small, few and readable.
# Smokes are the one hole that reliably loses rounds, hence the controller.
ROLE_NEED = {
    "Controller": (60, 10, 0),
    "Initiator": (45, 25, 5),
    "Sentinel": (40, 12, 0),
    "Duelist": (35, 15, 0),
}
UNKNOWN_ROLE_NEED = (20, 10, 0)

# A perfect 0-1000 form score on an agent is worth this much next to the role
# gap above: enough to break a tie between two roles, not enough to talk you
# into a fifth duelist because you once had a good game on one.
FIT_WEIGHT = 0.05

# Your record on this map, if there is enough of it, nudges by at most this.
MAP_BONUS = 15.0
MIN_MAP_MATCHES = 3

# Nothing cached on an agent scores as the middle of the road, not as zero:
# untried is not the same as bad.
NEUTRAL_FIT = 500.0

Pick = namedtuple("Pick", "agent name role score reason")


def _clamp(value, low, high):
    return max(low, min(high, value))


def _need(role, taken_count):
    ladder = ROLE_NEED.get(role, UNKNOWN_ROLE_NEED)
    return ladder[min(taken_count, len(ladder) - 1)]


ORDINALS = {1: "2nd", 2: "3rd", 3: "4th", 4: "5th"}


def _need_reason(role, taken_count):
    if not role:
        return "role unknown"
    if taken_count == 0:
        return f"no {role.lower()} yet"
    return f"{ORDINALS.get(taken_count, 'another')} {role.lower()}"


def _fit_reason(summary):
    if not summary:
        return "nothing cached on them"
    return f"score {summary['rating']} over {summary['matches']}"


def _map_bonus(summary):
    """Your win rate on this agent on this map, as a small nudge either way."""
    if not summary or summary["matches"] < MIN_MAP_MATCHES:
        return 0.0, ""
    swing = _clamp((summary["winrate"] - 50.0) * 0.3, -MAP_BONUS, MAP_BONUS)
    return swing, f"{summary['winrate']:.0f}% here over {summary['matches']}"


def recommend(
    content, taken, own_lines, map_id=None, pool=None, limit=3, calibration=None, per_role=1
):
    """The `limit` best agents left for you, best first.

    taken:      agent uuids your team already has, yours excluded
    own_lines:  your own cached per-match lines, from Encounters.all_perf_rows
    pool:       agent uuids you may pick, or None for every playable agent
    per_role:   how many agents of one role may appear, 0 for no limit
    """
    taken_roles = {}
    for agent in taken:
        role = content.role(agent)
        taken_roles[role] = taken_roles.get(role, 0) + 1

    overall = by_agent(own_lines, calibration)
    on_map = by_agent(own_lines, calibration, map_id=map_id) if map_id else {}

    candidates = set(pool) if pool else set(content.agents)
    candidates -= {a for a in taken if a}

    picks = []
    for agent in candidates:
        role = content.role(agent)
        need = _need(role, taken_roles.get(role, 0))

        mine = overall.get(agent)
        fit = (mine["rating"] if mine else NEUTRAL_FIT) * FIT_WEIGHT
        bonus, map_note = _map_bonus(on_map.get(agent))

        reason = [_need_reason(role, taken_roles.get(role, 0)), _fit_reason(mine)]
        if map_note:
            reason.append(map_note)
        picks.append(
            Pick(
                agent=agent,
                name=content.agent(agent),
                role=role,
                score=need + fit + bonus,
                reason=", ".join(reason),
            )
        )

    # Ties broken by name so the same lobby never reshuffles between redraws.
    picks.sort(key=lambda p: (-p.score, p.name))
    return _spread(picks, limit, per_role)


def _spread(picks, limit, per_role):
    """Keep the list to `per_role` agents of each role, best of them first.

    Without this an empty cache answers a missing controller with the three
    highest-sorting controllers, which is one suggestion printed three times.
    Capping it per role turns the list back into three separate answers: which
    hole to fill, and who you are best on for it.
    """
    if not per_role:
        return picks[:limit]
    seen = {}
    out = []
    for pick in picks:
        # An unrecognised role is its own bucket rather than one shared one.
        bucket = pick.role or f"?{pick.agent}"
        if seen.get(bucket, 0) >= per_role:
            continue
        seen[bucket] = seen.get(bucket, 0) + 1
        out.append(pick)
        if len(out) >= limit:
            break
    return out


def taken_agents(rows):
    """Agent uuids your team has locked or is hovering, yours left out."""
    return [r.agent_id for r in rows if r.agent_id and not r.is_self]
