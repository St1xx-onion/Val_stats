"""The rank the system thinks you are, read out of what it pays you.

Riot does not publish MMR. What it does publish, once per ranked match, is how
much RR the match was worth - and that number carries the answer, because the
ranked system is a servo: it is always pulling the visible rank towards the
hidden one, and the pull shows up as an asymmetry between wins and losses.

    +24 on a win, -11 on a loss   the system wants you higher
    +20 on a win, -20 on a loss   the system has you where it wants you
    +16 on a win, -25 on a loss   the system wants you lower

So the estimate is one subtraction: take the mean RR a win pays, take the mean
a loss costs, and look at the midpoint. At equilibrium the midpoint is zero.
Anywhere else, it is the drift per match - the rate at which the rank is being
walked towards wherever the system thinks the player belongs.

Turning a drift into a distance needs one constant: how long the walk takes.
A division is 100 RR, and at an even win rate the system closes a division's
worth of gap in about CONVERGENCE_MATCHES matches, so a drift of `a` RR per
match stands for a gap of `a * CONVERGENCE_MATCHES`. That constant is the one
thing here Riot has not told anybody - and it is the one thing here this
machine can measure for itself, out of the histories it has already cached:
see calibrate() at the bottom.

What this module will not do is print a single rank. The RR a match pays is
noisy - round difference and personal performance move it by a few points
either way - so a handful of matches cannot place anybody exactly, and a
number like "Gold 2, 34 RR of hidden rating" would be a lie told to three
digits. The noise is measurable, though, so the answer comes out as a band:
the estimate plus or minus the error the sample actually carries. Six matches
give a band about a fifth of a division wide, twenty-five give one half that,
and a band that changes width with the evidence is the honest shape for this
answer.

Four things it does that a plain mean of the deltas does not:

  recovers    a promotion or demotion is dropped only when the delta really
              was clipped, which is a question the payload answers rather than
              one to guess at - see skip_reason. Climbing players used to lose
              their most informative matches to that rule.
  weights     recent matches count for more, because the gap this measures is
              the gap *now* and the servo has been closing it all sample long.
  floors      a sample that happens to agree with itself does not get a band
              of width zero. One match carries about MATCH_NOISE RR of noise
              whatever six of them happened to land on.
  refuses     above Immortal the 0-100 ladder stops describing anything, so no
              rank is named - but the pull is still measured and still
              printed, because which way the system is pushing somebody does
              not depend on the ladder at all.

Everything here is arithmetic on numbers already fetched. No requests.
"""

import math
from collections import namedtuple
from datetime import datetime

# RR in one division, for every rank the 0-100 ladder applies to.
RR_PER_DIVISION = 100

# The first Immortal tier. From here up RR accumulates past 100 against a
# regional leaderboard instead of resetting each division, so the arithmetic
# below - which is all "position = tier * 100 + rr" - stops describing
# anything. Those players get a pull with no rank on it rather than a wrong
# rank: see estimate().
IMMORTAL = 24

# Iron 1. Below this there is no rank to be under.
LOWEST = 3

# Matches the system takes to close a one-division gap at an even win rate.
#
# This is the one number Riot does not publish, and it is a calibration rather
# than a fact. It is not a guess either: measured over 36 players, the spread
# of drifts has a standard deviation of 2.87 RR, which at this constant puts
# the typical player a little over half a division from their own rank - and
# that agrees with the other thing the same sample says, which is that just
# under half of everyone sits inside +-1.5 RR of dead level.
#
# It is also no longer only a prior. calibrate() measures it against the RR
# histories this install has cached, by the one route the servo model allows:
# a player whose rank has moved has had their gap closed by exactly that much,
# so their drift must have fallen by that much over the constant. Where the
# cache is deep enough to answer, the measured number is used instead of this
# one, and the report says which it used.
CONVERGENCE_MATCHES = 20

# Wins and losses needed on each side before a mean means anything. Three is
# not many, but the band widens on its own when the sample is thin, so this
# only has to keep out the cases where there is no mean to take at all.
MIN_SIDE = 3

# An RR change further from zero than this did not come from an ordinary win
# or loss - placement runs, act resets and rank corrections all land here.
OUTLIER_RR = 50

# How wide the printed band is, in standard errors. Two is the usual 95%.
CONFIDENCE = 1.96

# What one match's RR is worth of noise, in RR, before any sample is looked
# at. Round difference and the performance bonus move a payout by a few points
# either way, so a run of wins that all paid exactly the same is a small
# sample agreeing with itself - not proof that the payout is fixed. This is
# the prior that stops that run from producing a band of width zero.
MATCH_NOISE = 3.0

# How much that prior is worth, in matches. Same shape as perf._shrink: a thin
# sample is pulled towards the prior, a thick one overrules it. Four is about
# a third of a readable sample, which leaves a real spread visible while
# keeping a zero one from being believed.
PRIOR_MATCHES = 4.0

# Matches over which a match's weight halves, newest first. The drift measured
# over a long window is the average gap over that window, and the servo has
# been closing that gap the whole time; weighting the recent end more is what
# makes the answer describe now rather than a fortnight ago. Only used when
# the entries carry times - see _ordered.
HALF_LIFE_MATCHES = 12.0

# Wins and losses needed on each side of each half before the two halves are
# compared for a trend. Lower than MIN_SIDE on purpose: the trend is a word,
# not a number, and it is allowed to rest on less than the estimate does.
MIN_TREND_SIDE = 2

# gain:      mean RR a win paid, recency-weighted
# loss:      mean RR a loss cost, negative, recency-weighted
# drift:     the midpoint of those two - RR per match of pull, the whole signal
# error:     standard error of that midpoint, in RR
# wins/losses: how many of each survived the filters
# skipped:   {reason: count} for everything that did not, so a report can say
#            why a player with twenty matches was read from nine
# recovered: promotions and demotions kept because the payload proved their
#            delta was not clipped
# trend:     (older drift, newer drift) over the two halves of the sample, or
#            None when there is not enough on both sides of both halves
Reading = namedtuple("Reading", "gain loss drift error wins losses skipped recovered trend")

# low/high:  (tier, rr) at each end of the band, or None when it is not placed
# gap:       RR between the visible rank and the middle of the band, signed
# spread:    half the band's width in RR - the error, not the gap
# matches:   how many matches the reading rests on
# placed:    False above Immortal, where there is a pull but no ladder to put
#            it on. low and high are None exactly then.
Band = namedtuple("Band", "low high gap spread matches placed")

# matches:  the measured convergence constant
# players:  how many cached histories voted on it
# spread:   half the middle of the vote, in matches - how much they disagreed
Calibration = namedtuple("Calibration", "matches players spread")

# Sanity rails for a measured constant. A convergence outside these is not a
# player converging, it is two halves of a sample disagreeing about something
# else, and the median is better off without it.
MIN_CONVERGENCE = 5.0
MAX_CONVERGENCE = 60.0

# What one player's history has to carry before it can vote on the constant.
MIN_CALIBRATION_MATCHES = 16
MIN_CALIBRATION_PLAYERS = 8

# The two halves have to disagree about the drift by at least this much, in
# RR per match, before their difference is worth dividing by. Below it the
# quotient is noise over noise.
MIN_DRIFT_CHANGE = 1.0


def earned(update):
    """The RR this match paid for being the rank it was played at.

    The performance bonus comes off: it is paid for how the player did, not
    for where the system thinks they belong, and it is the second thing that
    would otherwise read as the pull this module is trying to measure.
    """
    return (update.get("RankedRatingEarned") or 0) - (
        update.get("RankedRatingPerformanceBonus") or 0
    )


def _position(tier, rr):
    return (tier or 0) * RR_PER_DIVISION + (rr or 0)


def _clipped(update):
    """True when a tier change cost the match its real delta.

    A promotion carries the overflow into the new division and a demotion is
    caught by a floor, and only the second of those loses information - but
    rather than take a view on which of Riot's rules applied, this asks the
    payload: the rank before and the rank after are both in it, so the ground
    actually covered is a subtraction. Where that agrees with what the match
    says it paid, nothing was clipped and the match is ordinary evidence.

    Worth the care, because the matches it recovers are the ones a climbing
    player's estimate most needs. Dropping every promotion means dropping the
    wins of everybody the system is pushing up, which biases what is left
    downwards exactly when the answer matters most.
    """
    moved = _position(
        update.get("TierAfterUpdate"), update.get("RankedRatingAfterUpdate")
    ) - _position(update.get("TierBeforeUpdate"), update.get("RankedRatingBeforeUpdate"))
    # One RR of slack: the payload rounds, and a rank-up has been seen to land
    # a point either side of the arithmetic.
    return abs(moved - earned(update)) > 1


def skip_reason(update):
    """Why this match cannot be read as an ordinary win or loss, or "".

    Every one of these is a flag Riot sets itself. Before they were in the
    payload the only way to spot a distorted match was to call its delta
    strange and drop it, which drops real evidence along with the rest.
    """
    if update.get("IsPlacementMatch"):
        return "placement"
    if update.get("WasDerankProtected"):
        # The loss was held at the tier floor, so it cost less than it says.
        return "derank protection"
    if update.get("AFKPenalty"):
        return "afk penalty"
    if update.get("RRPenalty"):
        # The party rank-gap penalty: the win paid less because of who they
        # queued with, not because of where the system thinks they belong.
        return "party penalty"
    if update.get("RankedRatingRefundApplied"):
        return "rr refunded"
    if update.get("NewMapIncentiveRRForgiven"):
        return "new map bonus"
    if update.get("TierBeforeUpdate") != update.get("TierAfterUpdate") and _clipped(update):
        # Promotion and demotion *can* clip the delta against the division
        # boundary. Only the ones that demonstrably did are dropped.
        return "changed tier"
    if abs(earned(update)) > OUTLIER_RR:
        return "outlier"
    if not earned(update):
        # Zero pays nothing and tells us nothing, and its sign - which is how
        # everything here tells a win from a loss - does not exist.
        return "no movement"
    return ""


def recovered(update):
    """True for a tier change this module decided to keep."""
    return (
        update.get("TierBeforeUpdate") != update.get("TierAfterUpdate")
        and not _clipped(update)
        and not skip_reason(update)
    )


def when(update):
    """When this match was played, in epoch seconds, or None.

    Two spellings reach here. Riot's own payload carries MatchStartTime in
    milliseconds; the copy the database hands back carries StoredAt, which is
    the same instant already turned into ISO text. Neither is guaranteed, and
    a sample where some entries have a time and some do not is not orderable,
    so the caller checks for None rather than filling one in.
    """
    raw = update.get("MatchStartTime")
    if raw:
        try:
            return float(raw) / 1000.0
        except (TypeError, ValueError):
            pass
    text = update.get("StoredAt")
    if text:
        try:
            return datetime.fromisoformat(str(text)).timestamp()
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    return None


def _ordered(entries):
    """(entries newest first, weights) - or the sample as given, unweighted.

    Weighting needs an order, and the only order worth having is the one the
    matches were actually played in. Where every entry carries a time, that
    order is known and each match is worth half as much as the one
    HALF_LIFE_MATCHES newer than it. Where any entry does not, or where they
    all landed on the same instant, nothing is assumed: every match counts
    once, which is what this module did before it weighted anything.

    Weights are normalised inside each side by the caller, never across the
    two - a weighted midpoint whose halves were weighted differently would
    start to move with the win rate, and being independent of the win rate is
    the whole reason the midpoint is the thing measured.
    """
    stamps = [when(update) for update, _ in entries]
    if not entries or any(stamp is None for stamp in stamps) or len(set(stamps)) < 2:
        return entries, [1.0] * len(entries)
    paired = sorted(zip(entries, stamps), key=lambda pair: pair[1], reverse=True)
    ordered = [entry for entry, _ in paired]
    weights = [0.5 ** (index / HALF_LIFE_MATCHES) for index in range(len(ordered))]
    return ordered, weights


def read(updates):
    """One player's RR history as a drift, or None when it cannot be read."""
    entries, skipped, kept = [], {}, 0
    for update in updates or ():
        reason = skip_reason(update)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        if recovered(update):
            kept += 1
        entries.append((update, earned(update)))

    entries, weights = _ordered(entries)
    gains = [(value, weight) for (_, value), weight in zip(entries, weights) if value > 0]
    losses = [(value, weight) for (_, value), weight in zip(entries, weights) if value < 0]
    if len(gains) < MIN_SIDE or len(losses) < MIN_SIDE:
        return None
    gain, loss = _wmean(gains), _wmean(losses)
    return Reading(
        gain,
        loss,
        (gain + loss) / 2,
        _error(gains, losses),
        len(gains),
        len(losses),
        skipped,
        kept,
        _trend(entries),
    )


def _trend(entries):
    """The drift over the older half and over the newer one, or None.

    A gap that is closing and a gap that is holding steady read identically in
    one number, and they are not the same player: the first has nearly
    arrived, the second is being carried somewhere. Both halves have to hold
    enough wins and losses of their own, which is why this is usually None on
    a twenty-match sample and usually there on a thirty-match one.

    Entries arrive newest first, so the second half of the list is the older
    half of the evening.
    """
    if len(entries) < 4 * MIN_TREND_SIDE:
        return None
    middle = len(entries) // 2
    halves = []
    for chunk in (entries[middle:], entries[:middle]):
        gains = [(value, 1.0) for _, value in chunk if value > 0]
        losses = [(value, 1.0) for _, value in chunk if value < 0]
        if len(gains) < MIN_TREND_SIDE or len(losses) < MIN_TREND_SIDE:
            return None
        halves.append((_wmean(gains) + _wmean(losses)) / 2)
    return tuple(halves)


def shortfall(updates):
    """Why read() could not answer, said in the words of the data.

    "Not enough ranked matches" is true of almost nobody who has the route
    open; what is usually true is something more specific and more useful -
    eight wins and two losses, so there is no loss side to measure, or five
    matches of which four were played in a stack and carry a party penalty
    that hides the pull this measures. Both of those are worth printing,
    because both of them are facts about the player rather than about us.
    """
    if not updates:
        return "no ranked matches on record"
    gains = losses = 0
    skipped = {}
    for update in updates:
        reason = skip_reason(update)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        elif earned(update) > 0:
            gains += 1
        else:
            losses += 1
    short = []
    if gains < MIN_SIDE:
        short.append(f"{gains} readable {'win' if gains == 1 else 'wins'}")
    if losses < MIN_SIDE:
        short.append(f"{losses} readable {'loss' if losses == 1 else 'losses'}")
    note = f"only {' and '.join(short)}" if short else "not enough readable matches"
    note += f" out of {len(updates)} ranked"
    if skipped:
        dropped = ", ".join(f"{count} {why}" for why, count in sorted(skipped.items()))
        note += f" - {dropped}"
    return note


def _wmean(pairs):
    total = sum(weight for _, weight in pairs)
    if not total:
        return 0.0
    return sum(value * weight for value, weight in pairs) / total


def _error(gains, losses):
    """Standard error of the midpoint of the two means.

    Each mean carries the error of its own sample, and halving their sum
    halves the error with it - which is why this is half the root of the sum
    rather than the root of the sum.
    """
    return 0.5 * math.sqrt(_sem(gains) ** 2 + _sem(losses) ** 2)


def _sem(pairs):
    """Standard error of one weighted mean, with the noise prior mixed in.

    The sample's own spread is the honest input, and on a thick sample it is
    the only one that matters. On a thin one it is not trustworthy in the
    direction that hurts: three wins that all paid +24 have a spread of zero,
    and believing that prints a band of width zero - a hidden rank quoted to
    the RR off three matches. MATCH_NOISE is what a single match is known to
    carry whatever those three did, and PRIOR_MATCHES is how many matches that
    knowledge is worth. The same shrink perf.py uses on a thin form sample,
    for the same reason.
    """
    if not pairs:
        return MATCH_NOISE
    total = sum(weight for _, weight in pairs)
    square = sum(weight * weight for _, weight in pairs)
    # Effective sample size: how many equally weighted matches this weighted
    # sample is worth. Ten matches weighted down to almost nothing are not ten.
    count = (total * total / square) if square else 0.0
    if count <= 1:
        return MATCH_NOISE
    mean = _wmean(pairs)
    spread = sum(weight * (value - mean) ** 2 for value, weight in pairs) / total
    # ...corrected the way an unbiased variance is, so a two-match sample does
    # not report the spread of the two points it happens to hold.
    spread *= count / (count - 1)
    blended = (count * spread + PRIOR_MATCHES * MATCH_NOISE**2) / (count + PRIOR_MATCHES)
    return math.sqrt(blended / count)


def convergence(calibration=None):
    """The constant to use: the measured one where there is one."""
    if calibration is None:
        return float(CONVERGENCE_MATCHES)
    matches = getattr(calibration, "matches", calibration)
    try:
        matches = float(matches)
    except (TypeError, ValueError):
        return float(CONVERGENCE_MATCHES)
    if not MIN_CONVERGENCE <= matches <= MAX_CONVERGENCE:
        return float(CONVERGENCE_MATCHES)
    return matches


def estimate(tier, rr, reading, calibration=None):
    """Where the system seems to place a player, as a band, or None.

    None has two meanings and the caller is expected to say which: there was
    no reading, or there is no rank to measure from. Both are "we do not
    know", and neither is "level".

    Above Immortal the answer is a band that is not *placed*: the pull is
    measured exactly as it is for everybody else - it is an asymmetry between
    two payouts and owes nothing to the ladder - but there is no division
    arithmetic to hang a rank on, so low and high come back None and only the
    gap is printed. A smaller answer than the one below Immortal, and a much
    larger one than the blank those players used to get.
    """
    if reading is None or not tier or tier < LOWEST:
        return None
    steps = convergence(calibration)
    gap = reading.drift * steps
    spread = CONFIDENCE * reading.error * steps
    matches = reading.wins + reading.losses
    if tier >= IMMORTAL:
        return Band(None, None, gap, spread, matches, False)
    here = tier * RR_PER_DIVISION + rr
    return Band(_place(here + gap - spread), _place(here + gap + spread), gap, spread, matches, True)


def place(position):
    """An absolute RR position back into (tier, rr), clamped to the ladder.

    Public because a mean rank is an RR position too: a side whose five
    players average 1,347 is standing at the same place on the ladder as one
    player with that number, and naming it needs this same arithmetic.
    """
    return _place(position)


def _place(position):
    """An absolute RR position back into (tier, rr), clamped to the ladder."""
    floor = LOWEST * RR_PER_DIVISION
    ceiling = (IMMORTAL - 1) * RR_PER_DIVISION + RR_PER_DIVISION - 1
    position = max(floor, min(position, ceiling))
    tier = int(position // RR_PER_DIVISION)
    return tier, int(round(position - tier * RR_PER_DIVISION))


def mark(band):
    """The arrow beside the band: how far off the visible rank it sits.

    The thresholds are in RR rather than in divisions because the interesting
    question is not which side of a boundary the estimate landed on - that is
    an accident of where in their division the player happens to be standing -
    but how much ground lies between where they are and where they are headed.
    """
    if band is None:
        return ""
    if abs(band.gap) <= 25:
        return "="
    if band.gap > 0:
        return "^^" if band.gap > RR_PER_DIVISION else "^"
    return "vv" if band.gap < -RR_PER_DIVISION else "v"


def sure(band):
    """True when the band is narrow enough to be read as a rank.

    A wider one still says which way the pull goes, which is the useful half
    of the answer - it just should not be read as a placement. An unplaced
    band is never sure: there is no rank on it to be sure about.
    """
    return band is not None and band.placed and band.spread <= RR_PER_DIVISION / 2


def describe(band, content):
    """The band as text: "Gold 1 - Gold 2", or one rank when the ends agree.

    Above Immortal there is no rank to name and the distance is printed
    instead - "+38 RR" means the system is pulling that far above wherever
    they are standing, which is the whole of what is knowable there.
    """
    if band is None:
        return ""
    if not band.placed:
        return f"{band.gap:+.0f} RR"
    low = content.tier(band.low[0])["name"]
    high = content.tier(band.high[0])["name"]
    return low if low == high else f"{low} - {high}"


def settling(reading):
    """"closing" / "widening" / "steady" for the two halves, or "".

    Read against the size of the gap, not its sign: a player being pushed up
    and one being pulled down are both settling when the pull weakens.
    """
    if reading is None or not reading.trend:
        return ""
    older, newer = reading.trend
    if abs(newer) < abs(older) - MIN_DRIFT_CHANGE:
        return "closing"
    if abs(newer) > abs(older) + MIN_DRIFT_CHANGE:
        return "widening"
    return "steady"


def explain(reading, band, updates=None, calibration=None):
    """The long form, for the report that has room for it."""
    if reading is None:
        return (
            f"no reading - {shortfall(updates)}"
            if updates
            else "not enough ranked matches to read the RR pattern"
        )
    paid = f"wins pay {reading.gain:+.1f}, losses cost {reading.loss:+.1f}"
    if band is None:
        return f"{paid} - but there is no rank on record to measure that against"
    note = (
        "the system is pushing them up"
        if band.gap > 25
        else "the system is pulling them down"
        if band.gap < -25
        else "the system has them at their rank"
    )
    text = (
        f"{paid}, drift {reading.drift:+.1f} RR per match over {band.matches} "
        f"matches - {note} ({band.gap:+.0f} RR, give or take {band.spread:.0f})"
    )
    if not band.placed:
        text += " - no rank named: above Immortal the 0-100 ladder stops applying"
    tail = []
    trend = settling(reading)
    if trend:
        older, newer = reading.trend
        tail.append(f"drift {older:+.1f} then {newer:+.1f}, {trend}")
    if reading.recovered:
        tail.append(f"{reading.recovered} kept through a rank change")
    if calibration is not None and getattr(calibration, "players", 0):
        tail.append(
            f"convergence {convergence(calibration):.0f} matches, "
            f"measured on {calibration.players} cached histories"
        )
    if tail:
        # Dashes rather than brackets: this string is printed through rich,
        # and rich reads square brackets as markup - the whole tail vanished
        # into a style tag nobody had defined, silently, which is the worst
        # way for a line to go missing.
        text += " - " + "; ".join(tail)
    return text


# -------------------------------------------------------------- calibration


def calibrate(histories):
    """CONVERGENCE_MATCHES measured against cached histories, or None.

    The servo model says the drift is the gap divided by the constant. It also
    says what happens as a player climbs: every RR of ground they cover is an
    RR the gap loses, so their drift has to fall by that much over the
    constant. Rearranged, that is the constant:

        C = (ground covered) / (drift before - drift after)

    Both halves of that are in the cache already. One player's RR history,
    split down the middle, gives a drift for each half; the ranks those
    matches were played at give the ground covered between them. Nothing here
    asks Riot anything, and nothing here needs a single extra request.

    It refuses more often than it answers, and every refusal is one of three
    things: a history too short to halve, two halves whose drifts agree (the
    quotient would be noise over noise), or a quotient outside the rails -
    which usually means the player got better and the hidden rating moved
    while this was assuming it stood still. The median of what survives is the
    answer, and the count of voters is printed with it, so a number resting on
    nine players can be read as one.
    """
    found = sorted(votes(histories))
    if len(found) < MIN_CALIBRATION_PLAYERS:
        return None
    return Calibration(_median(found), len(found), _half_spread(found))


def votes(histories):
    """Every usable vote on the constant, unsorted.

    calibrate() is this plus a median. It is separate because "how many of the
    cached histories could actually answer" is a different number from "how
    many are deep enough to try", and a command that reports on the cache has
    to be able to say both - the gap between them is most of the story.
    """
    return [value for value in (_one(updates) for updates in histories or ()) if value]


def _one(updates):
    """One player's vote on the constant, or None."""
    entries = []
    for update in updates or ():
        if skip_reason(update):
            continue
        tier = update.get("TierBeforeUpdate") or 0
        if tier < LOWEST or tier >= IMMORTAL:
            # The ladder arithmetic is what the ground covered is measured in.
            continue
        stamp = when(update)
        if stamp is None:
            continue
        entries.append(
            (stamp, earned(update), _position(tier, update.get("RankedRatingBeforeUpdate")))
        )
    if len(entries) < MIN_CALIBRATION_MATCHES:
        return None
    entries.sort(key=lambda entry: entry[0])
    middle = len(entries) // 2
    older, newer = entries[:middle], entries[middle:]
    first, second = _half_drift(older), _half_drift(newer)
    if first is None or second is None:
        return None
    change = first - second
    if abs(change) < MIN_DRIFT_CHANGE:
        return None
    ground = _mean([entry[2] for entry in newer]) - _mean([entry[2] for entry in older])
    value = ground / change
    if not MIN_CONVERGENCE <= value <= MAX_CONVERGENCE:
        return None
    return value


def _half_drift(entries):
    """The plain, unweighted drift over one half, or None.

    Unweighted on purpose: the two halves are being compared with each other,
    and a recency weight inside each of them would tilt both towards their own
    newer end, which is the very difference being measured.
    """
    gains = [value for _, value, _ in entries if value > 0]
    losses = [value for _, value, _ in entries if value < 0]
    if len(gains) < MIN_SIDE or len(losses) < MIN_SIDE:
        return None
    return (_mean(gains) + _mean(losses)) / 2


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _median(values):
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _half_spread(values):
    """Half the gap between the quartiles - how much the voters disagreed."""
    if len(values) < 4:
        return 0.0
    low = values[len(values) // 4]
    high = values[(3 * len(values)) // 4]
    return (high - low) / 2


def load_calibration(db, mode="auto"):
    """The measured constant for this install, rebuilt when the cache grows.

    Mirrors perf.load_calibration, including the tenth-of-the-population rule
    for when a stored answer has gone stale: this is arithmetic over every
    cached RR history, which is cheap but not free, and the answer moves
    slowly by construction.

    `mode` is "auto" to measure, "fixed" to stay on the constant, or a number
    to pin it by hand - a setting for somebody who has measured their own.
    """
    if db is None or mode in (None, "fixed"):
        return None
    if mode != "auto":
        try:
            pinned = float(mode)
        except (TypeError, ValueError):
            return None
        if not MIN_CONVERGENCE <= pinned <= MAX_CONVERGENCE:
            return None
        return Calibration(pinned, 0, 0.0)

    histories = db.rr_histories(MIN_CALIBRATION_MATCHES)
    if len(histories) < MIN_CALIBRATION_PLAYERS:
        return None
    stored = _stored(db)
    if stored and stored[1] and len(histories) < stored[1] * 1.1:
        return stored[0]
    fresh = calibrate(histories.values())
    if fresh:
        db.remember("mmr_convergence", f"{fresh.matches:.3f}")
        db.remember("mmr_convergence_players", str(fresh.players))
        db.remember("mmr_convergence_spread", f"{fresh.spread:.3f}")
        db.remember("mmr_convergence_pool", str(len(histories)))
    return fresh


def _stored(db):
    """(Calibration, the pool it was built from) as last written, or None."""
    try:
        matches = float(db.setting("mmr_convergence") or 0)
        players = int(db.setting("mmr_convergence_players") or 0)
        spread = float(db.setting("mmr_convergence_spread") or 0)
        pool = int(db.setting("mmr_convergence_pool") or 0)
    except (TypeError, ValueError):
        return None
    if not matches or not players:
        return None
    return Calibration(matches, players, spread), pool
