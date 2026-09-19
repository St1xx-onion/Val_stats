"""Who wins - and how much of that is actually knowable.

Everything else in this program reports a fact: this match paid that much RR,
this player has gone afk eleven times. A win probability is not a fact, it is a
model, and the honest way to print one is to be explicit about all three parts
of it - what goes in, what turns that into odds, and how wide the answer is.

  what goes in    one number per player, in RR, called their true rating:

                      rank + hidden-MMR pull + form

                  The first is where they are standing. The second is where the
                  ranked system is walking them, which is the system's own
                  correction to the first and is already measured in RR - see
                  mmr.py. The third is the only term that needs a conversion,
                  and that conversion is *measured*, never assumed: the slope
                  of rank against the 0-1000 score over every cached player who
                  has both. Where the cache cannot measure it, or measures
                  something too weak to trust, the form term is dropped
                  entirely rather than guessed at.

  the scale       how much a difference in true rating is worth in odds. One
                  number, fitted by maximum likelihood against cached matches
                  whose outcome and whose two sides' ranks are both known.
                  Until there are enough of those it is a stated prior - one
                  division of team-average difference is about 60/40 - and the
                  report says which of the two it used, every time.

  the width       a probability off five players and thirty matches each is not
                  a three-digit number. Each player's true rating carries an
                  error: the width of their own hidden-MMR band where there is
                  one, and otherwise what mmr.py measured about how far a
                  typical player sits from their own rank. Those add up into an
                  error on the difference, and that comes out as a range of
                  probabilities rather than one.

Two things this deliberately does not do. It does not fold in the streak, the
sitting or the flags: those are real, they are printed two sections above, and
nobody has measured what a four-loss streak is worth in RR - putting a number
on it here would be inventing the most interesting part of the answer. And it
does not know anything at all about the game about to be played: comps, map,
who is on voice, whether anybody is actually trying. A 55% here means the
numbers lean that way, not that the match is decided.

Arithmetic over rows already cached. No requests.
"""

import math
from collections import namedtuple

from . import mmr as mmr_module
from . import perf as perf_module

# The prior scale, in RR of side-average difference per one logit of odds.
#
# Anchored on the one statement about this that can be made without a fit: a
# division of difference between the two sides' average rank is worth roughly
# 60/40. logit(0.6) is 0.405, so one division - 100 RR - buys 0.405 of a logit
# and the scale is 100 / 0.405. It is a prior and is labelled as one on screen
# until fit() replaces it with a number measured on this machine's own matches.
DEFAULT_SCALE = 100.0 / math.log(0.6 / 0.4)

# How wrong a rank alone is about a player's strength, as a standard deviation
# in RR. Not invented here either: mmr.py's own sample put the spread of drifts
# at 2.87 RR a match, which over CONVERGENCE_MATCHES is how far the typical
# player sits from the rank they are standing on.
RANK_ONLY_SIGMA = 2.87 * mmr_module.CONVERGENCE_MATCHES

# And how wrong the lobby average is about a player with no rank at all. A
# division and a half: wide enough that a side leaning on one of these says so
# through the width of its answer.
IMPUTED_SIGMA = 150.0

# In between the two: a rank this machine saw the player at some evening in the
# past, out of rank_snapshots. Worse than a rank read today, because they have
# been playing since, and much better than the lobby average, because it is an
# observation about *them* rather than about the people standing near them.
REMEMBERED_SIGMA = 100.0

# rr:     their strength in RR
# sigma:  how wrong it might be, one standard deviation
# source: "read" from a rank in hand, "remembered" from a rank we saw before,
#         "lobby" from the middle of the players around them. The report
#         prints the counts, because "four of five read" and "two read and
#         three guessed" are not the same claim and must not look alike.
Reading = namedtuple("Reading", "rr sigma source")

# Players a side needs a real reading on before the odds are printed at all:
# half of it, rounded up. See needed().
def needed(count):
    """How many of a side have to be placed before it can be weighed.

    Two, or one on a side of one or two. Not half, and not three.

    Both of the earlier rules were wrong in the same direction - they refused
    to answer where an answer was available - and each was wrong for its own
    reason. A flat three was written looking at a five-a-side lobby and made
    every smaller one unanswerable, since two players cannot produce three
    readings however good the cache is. Half fixed that and still threw away
    the commonest case there is: two of the opposition placed and three not,
    which is most lobbies on a young cache.

    What makes that safe to answer is that the doubt is already carried
    properly. A player taken from the lobby average carries IMPUTED_SIGMA, and
    three of them widen the band until it says so out loud; the imputation
    itself pulls the two sides *together*, so what it costs is a claimed edge
    rather than a false one. Refusing on top of that was refusing twice.

    One placed player is still nothing: at that point four fifths of the side
    is an assumption about the people standing next to them.
    """
    count = int(count or 0)
    return 1 if count <= 2 else 2

# Cached matches needed before the scale is fitted rather than assumed, and how
# many ranked players a side of one of those needs before it can vote.
MIN_FIT_MATCHES = 60
MIN_FIT_RANKED = 3

# The grid the fit searches, in RR. Wide enough that hitting an end means the
# data had no opinion rather than that the answer was clipped.
FIT_LOW, FIT_HIGH, FIT_STEPS = 40.0, 2000.0, 240

# Log-likelihood units inside the maximum that count as "could also be this".
# 1.92 is the usual 95% for one fitted parameter.
FIT_INTERVAL = 1.92

# A fit whose interval is wider than this many times its own answer is not an
# answer. Better the stated prior than a measured number nobody can lean on.
FIT_MAX_LOOSENESS = 2.0

# Odds inside this of even are printed as too close to call. The number is
# still shown - hiding it would be worse - but it is not dressed up as a read.
TOO_CLOSE = 0.55

# rr:         the side's mean true rating
# error:      standard error of that mean, in RR
# known:      players whose rank was in hand
# remembered: players taken from a rank seen on some earlier evening
# imputed:    players who had no rank anywhere and were taken as the lobby
#             average - the only one of the three that is an assumption
# count:      players on the side
Side = namedtuple("Side", "rr error known remembered imputed count")

# ours/theirs: the two probabilities, summing to one
# low/high:    the range for `theirs` across the error on the difference
# delta:       their true rating minus ours, in RR
# error:       the error on that difference
# scale:       the scale used
# measured:    the Scale it was measured as, or None when the prior was used
Chance = namedtuple("Chance", "ours theirs low high delta error scale measured")

# rr:      the fitted scale
# matches: how many cached matches it was fitted on
# span:    (low, high) of the likelihood interval
Scale = namedtuple("Scale", "rr matches span")

# slope:       RR per point of the 0-1000 score
# players:     how many cached players it was measured on
# correlation: how strongly the two actually move together, -1..1
# middle:      the median score of that population - the form term is a
#              difference from it, so that a lobby of ordinary players comes
#              out at its rank instead of several divisions off it
Form = namedtuple("Form", "slope players correlation middle")

# Below this correlation the slope is a line drawn through a cloud. The form
# term is dropped rather than applied at a weight nothing supports.
MIN_FORM_CORRELATION = 0.10
MIN_FORM_PLAYERS = 60


def logistic(value):
    if value < -60:
        return 0.0
    if value > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-value))


# ------------------------------------------------------------------ strengths


def true_rating(tier, rr, band, rating=None, form=None, mean_rating=None, remembered=False):
    """One player's strength in RR, with its error, or None.

    None means there is no rank anywhere - not in hand and not in the cache -
    which is not a small gap: rank is the only term here that is a measurement
    of strength rather than a correction to one. The caller decides what to do
    about it; see lobby().

    `remembered` says the rank came out of rank_snapshots rather than from a
    lookup today. Same arithmetic, wider error, and the source is carried on
    the reading so the report can count the three kinds separately.
    """
    if not tier:
        return None
    value = float(tier * mmr_module.RR_PER_DIVISION + (rr or 0))
    # A band's spread is a 95% half-width; the error wanted here is one sigma.
    sigma = (band.spread / mmr_module.CONFIDENCE) if band is not None else RANK_ONLY_SIGMA
    if remembered:
        # They have been playing since we saw that rank, and we do not know
        # which way. A band read off today's RR history does not cover that.
        sigma = max(sigma, REMEMBERED_SIGMA)
    if band is not None:
        value += band.gap
    if form and rating and mean_rating is not None:
        value += form.slope * (rating - mean_rating)
    return Reading(value, sigma, "remembered" if remembered else "read")


def lobby(readings):
    """[Reading or None] -> the same list with the gaps filled from the middle.

    A player with no rank anywhere is taken as the middle of the lobby rather
    than dropped, because dropping them silently changes what a side average
    is an average of - four placed players and one unknown is a different
    claim from four players. The filled ones carry IMPUTED_SIGMA and say
    "lobby" for a source, so a side leaning on them answers with a visibly
    wider range and says how many of it were guesses.
    """
    known = [entry for entry in readings if entry]
    if not known:
        return [None] * len(readings)
    middle = sum(entry.rr for entry in known) / len(known)
    return [entry or Reading(middle, IMPUTED_SIGMA, "lobby") for entry in readings]


def side(filled):
    """One side's mean true rating, its error, and what it was made of."""
    values = [entry for entry in filled if entry]
    if not values:
        return Side(None, None, 0, 0, 0, len(filled))
    count = len(values)
    mean = sum(entry.rr for entry in values) / count
    # The error on a mean of independent estimates, each with its own sigma.
    error = math.sqrt(sum(entry.sigma**2 for entry in values)) / count
    return Side(
        mean,
        error,
        sum(1 for entry in values if entry.source == "read"),
        sum(1 for entry in values if entry.source == "remembered"),
        sum(1 for entry in values if entry.source == "lobby"),
        len(filled),
    )


def placed(view):
    """Players on a side who were placed rather than guessed at."""
    return (view.known + view.remembered) if view else 0


# ----------------------------------------------------------------------- odds


def chance(ours, theirs, measured=None):
    """The two sides' chances, as a range, or None when it cannot be said.

    Refuses on the same principle as everything else here: below needed() real
    readings a side is not a side, and a number computed anyway would be the
    most confident-looking thing in the report resting on the least.
    """
    if not ours or not theirs or ours.rr is None or theirs.rr is None:
        return None
    if placed(ours) < needed(ours.count) or placed(theirs) < needed(theirs.count):
        return None
    scale = measured.rr if measured else DEFAULT_SCALE
    delta = theirs.rr - ours.rr
    error = math.sqrt((ours.error or 0) ** 2 + (theirs.error or 0) ** 2)
    them = logistic(delta / scale)
    reach = mmr_module.CONFIDENCE * error
    return Chance(
        ours=1.0 - them,
        theirs=them,
        low=logistic((delta - reach) / scale),
        high=logistic((delta + reach) / scale),
        delta=delta,
        error=error,
        scale=scale,
        measured=measured,
    )


def too_close(odds):
    """True when the answer is a coin toss wearing two digits."""
    return odds is not None and max(odds.ours, odds.theirs) < TOO_CLOSE


# ---------------------------------------------------------------- calibration


def fit(samples):
    """The scale, fitted on cached matches, or None.

    `samples` is [(difference in RR, did the stronger-numbered side win)] - one
    row per match, the difference signed the same way every time. The fit is a
    one-parameter maximum likelihood over a grid, which is enough for one
    parameter and needs nothing outside the standard library.

    Three refusals, and they matter more than the fit does. Too few matches is
    the obvious one. The maximum landing on an end of the grid is the data
    saying it has no opinion - which is exactly what a season of evenly matched
    lobbies looks like, and it must not come out as "40 RR is a certainty".
    And an interval wider than FIT_MAX_LOOSENESS times the answer is a measured
    number nobody can lean on; the stated prior is the better of the two.
    """
    rows = [(float(delta), bool(won)) for delta, won in samples or () if delta is not None]
    if len(rows) < MIN_FIT_MATCHES:
        return None
    grid = [
        FIT_LOW * (FIT_HIGH / FIT_LOW) ** (step / (FIT_STEPS - 1)) for step in range(FIT_STEPS)
    ]
    scored = [(_likelihood(rows, value), value) for value in grid]
    best, where = max(scored)
    if where in (grid[0], grid[-1]):
        return None
    inside = [value for score, value in scored if score >= best - FIT_INTERVAL]
    span = (min(inside), max(inside))
    if span[0] == grid[0] or span[1] == grid[-1]:
        return None
    if (span[1] - span[0]) > FIT_MAX_LOOSENESS * where:
        return None
    return Scale(where, len(rows), span)


def _likelihood(rows, scale):
    total = 0.0
    for delta, won in rows:
        chance_of_win = logistic(delta / scale)
        # Clamped so one impossible-looking match cannot take the whole
        # likelihood to negative infinity and decide the fit by itself.
        chance_of_win = min(max(chance_of_win, 1e-6), 1 - 1e-6)
        total += math.log(chance_of_win if won else 1 - chance_of_win)
    return total


def form_fit(pairs):
    """RR per point of the 0-1000 score, measured, or None.

    An ordinary least-squares slope of rank on score over cached players who
    have both. The correlation comes back with it and is the point: a slope
    through a cloud is still a slope, and the only thing that can say whether
    it deserves to move anybody's estimate is how tightly the cloud holds.

    A caveat worth keeping in view: the slope is measured against rank alone,
    while true_rating() applies it on top of rank *and* the hidden-MMR pull.
    The two corrections are not independent - both are reaching for the same
    underlying strength - so the form term double-counts a little. It is small
    by measurement rather than by assumption, which is the only reason it is
    tolerable at all.
    """
    rows = [(float(rating), float(position)) for rating, position in pairs or () if rating]
    if len(rows) < MIN_FORM_PLAYERS:
        return None
    mean_x = sum(x for x, _ in rows) / len(rows)
    mean_y = sum(y for _, y in rows) / len(rows)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in rows)
    sxx = sum((x - mean_x) ** 2 for x, _ in rows)
    syy = sum((y - mean_y) ** 2 for _, y in rows)
    if not sxx or not syy:
        return None
    correlation = sxy / math.sqrt(sxx * syy)
    if abs(correlation) < MIN_FORM_CORRELATION:
        return None
    scores = sorted(x for x, _ in rows)
    return Form(sxy / sxx, len(rows), correlation, scores[len(scores) // 2])


# ------------------------------------------------------- loading from a cache


def load_scale(db):
    """The fitted scale for this install, rebuilt as the cache grows, or None.

    Same shape as perf.load_calibration and mmr.load_calibration, including the
    tenth-of-the-population rule for a stored answer going stale. None is not a
    failure: it is "not enough cached matches carry both sides' ranks and a
    result yet", and the report prints the prior and says so.

    That pool grows on its own from here. Every match the deep sweep downloads
    now keeps the rank each player was at - free, out of a payload already in
    hand - so a cache that cannot answer this week can next month.
    """
    if db is None:
        return None
    samples = db.side_strengths(MIN_FIT_RANKED)
    if len(samples) < MIN_FIT_MATCHES:
        return None
    stored = _stored_scale(db)
    if stored and stored.matches and len(samples) < stored.matches * 1.1:
        return stored
    fresh = fit(samples)
    if fresh:
        db.remember("odds_scale", f"{fresh.rr:.2f}")
        db.remember("odds_scale_matches", str(fresh.matches))
        db.remember("odds_scale_span", f"{fresh.span[0]:.2f}/{fresh.span[1]:.2f}")
    return fresh


def _stored_scale(db):
    try:
        value = float(db.setting("odds_scale") or 0)
        matches = int(db.setting("odds_scale_matches") or 0)
        low, high = (db.setting("odds_scale_span") or "0/0").split("/")
        span = (float(low), float(high))
    except (TypeError, ValueError):
        return None
    if not value or not matches:
        return None
    return Scale(value, matches, span)


def load_form(db, calibration=None):
    """The measured RR-per-rating-point for this install, or None.

    Two queries and some arithmetic over every cached player who has both a
    rank on record and enough matches to be scored. Not stored between runs:
    unlike the scale it is cheap, and unlike the scale it moves with the
    calibration it was computed under.
    """
    if db is None:
        return None
    positions = db.ranked_positions()
    if len(positions) < MIN_FORM_PLAYERS:
        return None
    pairs = []
    for summary in db.population():
        position = positions.get(summary["puuid"])
        if position is None:
            continue
        score = perf_module.rating(summary, calibration)
        if score:
            pairs.append((score, position))
    return form_fit(pairs)
