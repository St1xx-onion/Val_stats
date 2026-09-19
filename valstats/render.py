"""Console output: one live table per match, sized to fit the terminal."""

import sys
from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from . import mmr as mmr_module
from . import progress


def _utf8_stdout():
    """A Riot ID can be written in any script there is.

    On a console still running an ANSI code page, printing one takes the whole
    process down with a UnicodeEncodeError - and the next lobby with that
    player in it does it again. Ask for UTF-8, and settle for replacement
    characters if even that is refused.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass


_utf8_stdout()
console = Console()

HIDDEN_LABEL = "[hidden]"
DASH = Text("-", style="dim")

# Sentinel for MatchView.update: "leave this field alone".
KEEP = object()


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    justify: str = "left"
    # 0 never gets dropped; otherwise the highest number goes first when the
    # window is too narrow. Losing a whole column beats cropping every one.
    priority: int = 0


COLUMNS = (
    Column("team", "Team", "center"),
    Column("party", "Pty", "center", 20),
    Column("agent", "Agent"),
    Column("player", "Player"),
    Column("lvl", "Lvl", "right", 90),
    Column("rank", "Rank"),
    Column("rr", "RR", "right", 30),
    Column("peak", "Peak", "left", 70),
    # Where the ranked system looks to be walking them, from what their
    # matches pay - see mmr.py. Nearly the last thing to go when the window is
    # narrow, and deliberately: a rank says where somebody is standing, this
    # says which way they are being carried, and of the two the second is the
    # one no other column repeats. The deep report's own overview has held it
    # at that rank all along; this used to drop it fourth, ahead of the peak
    # and the damage delta, which put the newest column and the least
    # replaceable one in the same sentence when they are not the same thing.
    Column("mmr", "Hidden MMR", "left", 22),
    Column("wg", "W/G", "right", 80),
    Column("wr", "WR", "right", 40),
    Column("score", "Score", "right", 35),
    Column("acs", "ACS", "right"),
    Column("kast", "KAST", "right", 60),
    Column("hs", "HS%", "right", 45),
    Column("kd", "K/D", "right"),
    Column("dd", "DD", "right", 65),
    Column("n", "N", "right", 55),
    Column("knife", "Knife", "left", 100),
    Column("seen", "Seen", "right", 50),
    # How far they have moved since you last met them. Dropped early when the
    # window is narrow: it is the most interesting column and the least
    # necessary one, which is exactly what a low priority number means here.
    Column("prog", "+/-", "center", 25),
)

BY_KEY = {column.key: column for column in COLUMNS}
PERF_KEYS = ("score", "acs", "kast", "hs", "kd", "dd", "n")

# Below this many matches the form numbers are dimmed: they are one hot game
# away from meaning something else entirely, and the table should say so.
THIN_SAMPLE = 3


def _name_text(row):
    """The Player cell for somebody we can name.

    A name out of the local memory or an old leaderboard dump is marked and
    dimmed, the same way a remembered rank is and for the same reason: it says
    who this puuid was the last time anything wrote it down, which is a
    weaker claim than the one the name service makes about the rest of the
    table. See identity.Named.
    """
    if row.is_self:
        return Text(row.name or "?", style="bold white")
    if row.name and row.name_when:
        return Text(f"~{row.name}", style="dim")
    return Text(row.name or "?")


def _tier_text(content, tier, remembered=False):
    info = content.tier(tier)
    if remembered:
        # Not bold, and marked: this is the last rank we saw them at, which is
        # a different claim from the one every other rank in the table makes.
        return Text(f"~{info['name']}", style=f"dim #{info['color']}")
    return Text(info["name"], style=f"bold #{info['color']}")


def _unranked_text(content):
    """"Unranked" - we looked at their matches and there was no rank in them.

    A different cell from the dash beside it, and the difference is the point:
    a dash is "nobody has told us", this is "their own match records say they
    have never been ranked". Quiet rather than bold, because it is a fact
    about an absence and should not draw the eye the way a Radiant does.
    """
    info = content.tier(0)
    return Text(info["name"], style=f"dim #{info['color']}")


def _peak_text(content, row):
    """The act peak, or a dash where there is none to show.

    The word `hidden` means one thing only: the player set IsActRankBadgeHidden
    and the table honours that. Someone whose peak was simply never looked up
    gets a dash instead - a case the live table does not have - and the column
    is left out altogether when show_peak_rank is off. See _wanted.
    """
    if row.peak_hidden:
        return Text("hidden", style="dim italic")
    if content is None or not row.peak_tier:
        return DASH
    return _tier_text(content, row.peak_tier)


# -------------------------------------------------------------------- cells


def _perf_cells(row):
    """Form numbers, coloured against rough competitive averages."""
    if row.acs is None:
        return {key: DASH for key in PERF_KEYS}
    rating_style = "bold green" if row.rating >= 600 else "red" if row.rating < 400 else "bold"
    acs_style = "green" if row.acs >= 240 else "red" if row.acs < 170 else ""
    kast_style = "" if row.kast is None else "green" if row.kast >= 72 else ""
    hs_style = "" if row.hs is None else "green" if row.hs >= 25 else ""
    kd_style = "green" if row.kd >= 1.15 else "red" if row.kd < 0.85 else ""
    dd_style = "" if row.dd is None else "green" if row.dd >= 20 else "red" if row.dd < -20 else ""
    cells = {
        "score": Text(str(row.rating), style=rating_style),
        "acs": Text(f"{row.acs:.0f}", style=acs_style),
        "kast": DASH if row.kast is None else Text(f"{row.kast:.0f}%", style=kast_style),
        "hs": DASH if row.hs is None else Text(f"{row.hs:.0f}%", style=hs_style),
        "kd": Text(f"{row.kd:.2f}", style=kd_style),
        "dd": DASH if row.dd is None else Text(f"{row.dd:+.0f}", style=dd_style),
        "n": Text(str(row.perf_matches or "-")),
    }
    if row.perf_matches < THIN_SAMPLE:
        # Too few matches to colour-code honestly - show them, but quietly.
        cells = {key: Text(cell.plain, style="dim italic") for key, cell in cells.items()}
    return cells


def _progress_cell(row):
    """Rank movement since the last meeting: "+2" green, "-1" red, "=" quiet.

    Drawn faintly when the earlier reading is months old - the movement is
    still real, but "up two divisions since March" is a weaker claim about who
    is in front of you than the same movement since last week.
    """
    got = getattr(row, "progress", None)
    text = progress.mark(got) if got else ""
    if not text:
        return DASH
    if text == "=":
        return Text("=", style="dim")
    style = "bold green" if got.tiers > 0 else "bold red"
    return Text(text, style=f"dim {style}" if progress.stale(got) else style)


def _mmr_cell(row, content=None):
    """The hidden-rating band, coloured by which way it sits from their rank.

    Green above, red below, quiet when the system has them where they are. A
    band too wide to read as a placement is dimmed rather than dropped: which
    way the pull goes is the useful half of the answer and survives a thin
    sample, while the rank at either end does not. Above Immortal the cell
    reads "+38 RR" instead of a rank and is dimmed for the same reason - there
    is a pull to print and no ladder to name it on.
    """
    band = getattr(row, "mmr_band", None)
    if band is None:
        return DASH
    style = "green" if band.gap > 25 else "red" if band.gap < -25 else "dim"
    if not mmr_module.sure(band):
        style = "dim" if style == "dim" else f"dim {style}"
    text = f"{row.mmr_text} {mmr_module.mark(band)}".strip()
    return Text(text, style=style)


def _row_cells(row, content, own_team, show_perf):
    cells = {column.key: DASH for column in COLUMNS}

    side, side_style = "ALLY", "green"
    if not own_team:
        side, side_style = row.team or "-", "cyan"
    elif row.team != own_team:
        side, side_style = "ENEMY", "red"
    cells["team"] = Text(side, style=side_style)
    cells["agent"] = Text(row.agent)
    cells["seen"] = Text(str(row.seen_before), style="yellow bold") if row.seen_before else DASH
    cells["prog"] = _progress_cell(row)
    cells["mmr"] = _mmr_cell(row)
    if row.party:
        # "A?" is party.apply saying the group is real but thin - drawn faintly
        # so a glance at the table sorts the duos from the maybes.
        tentative = row.party.endswith("?")
        cells["party"] = Text(row.party, style="dim magenta" if tentative else "bold magenta")

    if row.hidden:
        # Streamer mode: the client hides the *name*, so we do too, and that
        # is the whole of what it hides here. Everything still printed on this
        # row was read out of matches these players have already finished -
        # the rank out of the `competitiveTier` in the record, the form
        # columns out of the same records the full report reads - and none of
        # it came from a lookup we declined to make about them. See
        # app._rank_hidden and app._fill_hidden_performance.
        #
        # What stays blank stays blank: RR, the act peak, W/G and the account
        # level live only in the MMR lookup, which is precisely the request
        # Incognito closes and this program does not make.
        cells["player"] = Text(HIDDEN_LABEL, style="dim italic")
        if row.tier:
            cells["rank"] = _tier_text(content, row.tier, remembered=bool(row.tier_seen))
        elif row.unranked:
            cells["rank"] = _unranked_text(content)
        if show_perf:
            cells.update(_perf_cells(row))
        return cells

    winrate = row.winrate
    wr_style = ""
    if winrate is not None:
        wr_style = "green" if winrate >= 55 else "red" if winrate < 45 else ""

    cells["player"] = _name_text(row)
    cells["lvl"] = Text(str(row.level or "-"))
    cells["rank"] = _tier_text(content, row.tier)
    cells["rr"] = Text(str(row.rr) if row.tier else "-")
    cells["peak"] = (
        Text("hidden", style="dim italic")
        if row.peak_hidden
        else _tier_text(content, row.peak_tier)
    )
    cells["wg"] = Text(f"{row.wins}/{row.games}" if row.games else "-")
    cells["wr"] = DASH if winrate is None else Text(f"{winrate:.0f}%", style=wr_style)
    cells["knife"] = Text(row.knife or "-", style="dim")
    if show_perf:
        cells.update(_perf_cells(row))
    return cells


# ------------------------------------------------------------------- layout


def _width(cells_per_row, keys, by_key=BY_KEY):
    """What the rendered table would cost: content + padding + borders."""
    total = 3 * len(keys) + 1
    for key in keys:
        widths = [cell_len(by_key[key].header)]
        widths += [cell_len(cells[key].plain) for cells in cells_per_row]
        total += max(widths)
    return total


def _fit(cells_per_row, keys, max_width, by_key=BY_KEY):
    """Drop the least useful columns until the table stops being cropped."""
    keys = list(keys)
    dropped = []
    while _width(cells_per_row, keys, by_key) > max_width:
        droppable = [by_key[k] for k in keys if by_key[k].priority]
        if not droppable:
            break  # nothing left to give; rich crops from here
        victim = max(droppable, key=lambda column: column.priority)
        keys.remove(victim.key)
        dropped.append(victim.header)
    return keys, dropped


def fit(cells_per_row, columns, max_width=None):
    """The same column-dropping the live table does, for any other table.

    The deep report builds tables this module knows nothing about and has the
    same problem to solve - a terminal narrower than the columns deserve - so
    the rule lives in one place rather than being written twice with two
    different ideas of which column goes first. Takes Column objects and the
    cells keyed by their keys, and gives back the keys that fit.
    """
    by_key = {column.key: column for column in columns}
    keys = [column.key for column in columns]
    return _fit(cells_per_row, keys, max_width or console.width, by_key)


def _wanted(
    show_skins, show_perf, show_party=False, show_peak=True, show_prog=False, show_mmr=False
):
    keys = []
    for column in COLUMNS:
        if column.key in PERF_KEYS and not show_perf:
            continue
        if column.key == "knife" and not show_skins:
            continue
        # A column of dashes is worse than no column: most lobbies are solo.
        if column.key == "prog" and not show_prog:
            continue
        if column.key == "party" and not show_party:
            continue
        # And a column of "hidden" is worse still - that word is the player's
        # own IsActRankBadgeHidden, not a setting of yours. With show_peak_rank
        # off the peak is not shown at all rather than shown as withheld.
        if column.key == "peak" and not show_peak:
            continue
        # Nobody in the lobby had enough ranked history to read, so the column
        # would be ten dashes wide and say nothing.
        if column.key == "mmr" and not show_mmr:
            continue
        keys.append(column.key)
    return keys


def build_table(
    rows, content, title, show_skins=False, show_perf=False, own_team=None, show_peak=True
):
    # Own team first, you at the top of it, then everyone by rank descending.
    ordered = sorted(rows, key=lambda r: (r.team != own_team, r.team, not r.is_self, -r.tier))
    cells_per_row = [_row_cells(row, content, own_team, show_perf) for row in ordered]

    show_party = any(row.party for row in rows)
    show_prog = any(progress.anything(getattr(row, "progress", None)) for row in rows)
    show_mmr = any(getattr(row, "mmr_band", None) for row in rows)
    keys, dropped = _fit(
        cells_per_row,
        _wanted(show_skins, show_perf, show_party, show_peak, show_prog, show_mmr),
        console.width,
    )
    caption = None
    if dropped:
        # Reversed so the caption reads in the order the columns would return.
        caption = "window too narrow, hidden: " + ", ".join(reversed(dropped))

    table = Table(
        title=title,
        title_style="bold",
        header_style="dim",
        caption=caption,
        caption_style="dim italic",
        expand=False,
    )
    for key in keys:
        column = BY_KEY[key]
        table.add_column(column.header, justify=column.justify, no_wrap=True)

    previous_team = None
    for row, cells in zip(ordered, cells_per_row):
        if previous_team is not None and row.team != previous_team:
            table.add_section()
        previous_team = row.team
        table.add_row(*(cells[key] for key in keys))
    return table


ROLE_STYLES = {
    "Controller": "cyan",
    "Duelist": "red",
    "Initiator": "yellow",
    "Sentinel": "green",
}


def build_picks(picks, heading="Suggested picks"):
    """The short list under the lobby, best first.

    Deliberately unboxed and dim: it is advice sitting under the table, not a
    second table competing with it.
    """
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
    table.add_column(justify="right", style="dim")
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(style="dim")
    for place, pick in enumerate(picks, start=1):
        table.add_row(
            f"{place}",
            Text(pick.name, style="bold"),
            Text(pick.role or "-", style=ROLE_STYLES.get(pick.role, "dim")),
            Text(pick.reason),
        )
    return Group(Text(heading, style="bold dim"), table)


# Only what the post-match lookup adds carries a priority: everything else here
# was on the scoreboard before them and stays whatever the window does, exactly
# as it did.
SUMMARY_COLUMNS = (
    Column("team", "Team", "center"),
    Column("agent", "Agent"),
    Column("player", "Player"),
    Column("rank", "Rank"),
    Column("rr", "RR", "right", 30),
    Column("peak", "Peak", "left", 70),
    # The same band the live table carries, kept on the scoreboard because this
    # is the one table that is read after the match rather than during it -
    # which is when "where is this heading" is a question anybody has time for.
    # Costs nothing for the lobby that was just played: the bands were read
    # while the table was live, and only the players who hid need asking.
    Column("mmr", "Hidden MMR", "left", 35),
    Column("acs", "ACS", "right"),
    Column("kda", "K/D/A", "right"),
    Column("kast", "KAST", "right"),
    Column("hs", "HS%", "right"),
    Column("dd", "DD", "right"),
)

SUMMARY_BY_KEY = {column.key: column for column in SUMMARY_COLUMNS}

# Columns the scoreboard leaves out when nothing filled them: with the
# post-match lookup turned off there is no RR and no peak for anyone who hid,
# and a column of dashes says less than no column at all. The band goes the
# same way - a lobby where nobody had enough ranked history to read is a lobby
# where that column is ten dashes wide.
SUMMARY_OPTIONAL = ("rr", "peak", "mmr")


def _summary_cells(row, own_team, content=None):
    final = row.final or {}
    side, side_style = "ALLY", "green"
    if not own_team:
        side, side_style = row.team or "-", "cyan"
    elif row.team != own_team:
        side, side_style = "ENEMY", "red"

    name = HIDDEN_LABEL if row.hidden else (row.name or "?")
    # A tilde says the same thing here as it does above and in the Rank column:
    # this name was not read for this match, it was remembered from another.
    if not row.hidden and row.name and row.name_when:
        name = f"~{name}"
    # Cyan says "this one was [hidden] in the table above": the caption under
    # the table says which source put a name to them.
    name_style = "bold white" if row.is_self else "cyan" if row.revealed else ""
    acs = final.get("acs")
    kast = final.get("kast")
    hs = final.get("hs")
    dd = final.get("dd")
    return {
        "team": Text(side, style=side_style),
        "agent": Text(row.agent),
        "player": Text(name, style=name_style),
        "rank": _tier_text(content, row.tier) if content is not None and row.tier else DASH,
        "rr": Text(str(row.rr)) if row.tier and row.rr else DASH,
        "peak": _peak_text(content, row),
        "mmr": _mmr_cell(row),
        "acs": DASH if acs is None else Text(f"{acs:.0f}"),
        "kda": Text(
            f"{final.get('kills', 0)}/{final.get('deaths', 0)}/{final.get('assists', 0)}"
        )
        if final
        else DASH,
        "kast": DASH if kast is None else Text(f"{kast:.0f}%"),
        "hs": DASH if hs is None else Text(f"{hs:.0f}%"),
        "dd": DASH if dd is None else Text(f"{dd:+.0f}"),
    }


def build_summary_table(
    rows, title, own_team=None, caption=None, content=None, show_peak=True
):
    """The scoreboard for the match that just ended, best ACS first."""
    ordered = sorted(
        rows,
        key=lambda r: (r.team != own_team, r.team, -((r.final or {}).get("acs") or 0)),
    )
    cells_per_row = [_summary_cells(row, own_team, content) for row in ordered]
    # A rank and a peak nobody has are not worth a column of dashes, and in a
    # narrow window they are the two the scoreboard can afford to lose.
    wanted = [
        column.key
        for column in SUMMARY_COLUMNS
        if (column.key != "peak" or show_peak)
        and (
            column.key not in SUMMARY_OPTIONAL
            or any(cells[column.key] is not DASH for cells in cells_per_row)
        )
    ]
    keys, dropped = _fit(cells_per_row, wanted, console.width, SUMMARY_BY_KEY)
    if dropped:
        note = "window too narrow, hidden: " + ", ".join(reversed(dropped))
        caption = f"{caption}   {note}" if caption else note

    table = Table(
        title=title,
        title_style="bold",
        header_style="dim",
        caption=caption,
        caption_style="dim italic",
        expand=False,
    )
    for key in keys:
        column = SUMMARY_BY_KEY[key]
        table.add_column(column.header, justify=column.justify, no_wrap=True)

    previous_team = None
    for row, cells in zip(ordered, cells_per_row):
        if previous_team is not None and row.team != previous_team:
            table.add_section()
        previous_team = row.team
        table.add_row(*(cells[key] for key in keys))
    return table


def _start_live(renderable):
    """Begin a live block, surviving one that was left running.

    A crash halfway through a redraw can leave rich holding a display that was
    never stopped, and depending on the version that either takes the console
    over or refuses the next block outright - either way, every later table in
    the session was broken until the program was restarted. Clearing it and
    trying once more is cheap; if even that fails the caller prints the table
    instead of refreshing it in place.
    """
    for attempt in range(2):
        live = Live(
            renderable,
            console=console,
            refresh_per_second=4,
            vertical_overflow="visible",
        )
        try:
            live.start()
            return live
        except Exception:  # noqa: BLE001 - whatever it was, try to clear it once
            clear = getattr(console, "clear_live", None)
            if attempt or not clear:
                return None
            try:
                clear()
            except Exception:  # noqa: BLE001 - nothing left to clear is fine too
                return None
    return None


class MatchView:
    """One table per match, refreshed in place as the numbers arrive.

    Ranks land in a second, recent form costs a dozen more requests, and the
    lobby doubles in size when agent select turns into the match itself. All
    of that lands in the same block instead of printing another table.
    """

    def __init__(self, content, show_peak=True):
        self.content = content
        # Config, not a per-lobby fact, so it is settled once here rather than
        # passed in with every redraw.
        self.show_peak = show_peak
        self.live = None
        # Set when a live block could not be started at all: the table is then
        # printed once at the end instead of being refreshed in place.
        self.degraded = False
        self.rows = []
        self.title = ""
        self.own_team = None
        self.show_skins = False
        self.show_perf = False
        self.status = ""
        self.picks = None
        self.picks_heading = ""
        # The two sides' chances, as lines of Text. Its own slot rather than
        # more status, because status is one line that every stage overwrites
        # and this has to survive the stages that come after it.
        self.odds = None

    def update(
        self,
        rows=KEEP,
        title=KEEP,
        own_team=KEEP,
        show_skins=KEEP,
        show_perf=KEEP,
        status=KEEP,
        picks=KEEP,
        picks_heading=KEEP,
        odds=KEEP,
    ):
        if rows is not KEEP:
            self.rows = rows
        if title is not KEEP:
            self.title = title
        if own_team is not KEEP:
            self.own_team = own_team
        if show_skins is not KEEP:
            self.show_skins = show_skins
        if show_perf is not KEEP:
            self.show_perf = show_perf
        if status is not KEEP:
            self.status = status
        if picks is not KEEP:
            self.picks = picks
        if picks_heading is not KEEP:
            self.picks_heading = picks_heading
        if odds is not KEEP:
            self.odds = odds

        if self.degraded:
            return  # no live block available; close() prints the finished table
        renderable = self._renderable()
        if self.live is not None:
            self.live.update(renderable)
            return
        console.print()
        self.live = _start_live(renderable)
        self.degraded = self.live is None

    def close(self, status=KEEP):
        """Stop refreshing but leave the finished table on screen.

        Also the tidy-up on every error path, so it must not raise: a broken
        screen is a nuisance, a broken screen that ends the process is a lost
        match.
        """
        live, self.live = self.live, None
        degraded, self.degraded = self.degraded, False
        if status is not KEEP:
            self.status = status
        if live is None:
            if degraded and self.rows:
                console.print(self._renderable())
            return
        try:
            live.update(self._renderable())
            live.stop()
        except Exception as exc:  # noqa: BLE001 - closing a screen is never the story
            console.print(f"[yellow]could not close the live table: {exc}[/yellow]")

    def _renderable(self):
        table = build_table(
            self.rows,
            self.content,
            title=self.title,
            show_skins=self.show_skins,
            show_perf=self.show_perf,
            own_team=self.own_team,
            show_peak=self.show_peak,
        )
        parts = [table]
        if self.odds:
            parts.append(Text(""))
            parts.extend(self.odds)
        if self.status:
            parts.append(Text(self.status, style="dim"))
        if self.picks:
            parts.append(Text(""))
            parts.append(build_picks(self.picks, self.picks_heading or "Suggested picks"))
        return table if len(parts) == 1 else Group(*parts)


# -------------------------------------------------------------------- prose


def banner(client, content):
    act = "act unknown" if not content.current_act else f"act {content.current_act[:8]}"
    console.print(
        f"[dim]region[/dim] {client.region}/{client.shard}   "
        f"[dim]build[/dim] {client.client_version}   [dim]{act}[/dim]"
    )


_last_info = None


def info(message, repeat=False):
    """Status chatter. A line identical to the previous one is swallowed."""
    global _last_info
    if not repeat and message == _last_info:
        return
    _last_info = message
    console.print(f"[dim]{message}[/dim]")


def warn(message):
    global _last_info
    _last_info = None
    console.print(f"[yellow]{message}[/yellow]")


def error(message):
    global _last_info
    _last_info = None
    console.print(f"[red]{message}[/red]")
