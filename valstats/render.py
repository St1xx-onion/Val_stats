"""Console output: one live table per match, sized to fit the terminal."""

from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

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
    Column("agent", "Agent"),
    Column("player", "Player"),
    Column("lvl", "Lvl", "right", 90),
    Column("rank", "Rank"),
    Column("rr", "RR", "right", 30),
    Column("peak", "Peak", "left", 70),
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
)

BY_KEY = {column.key: column for column in COLUMNS}
PERF_KEYS = ("score", "acs", "kast", "hs", "kd", "dd", "n")

# Below this many matches the form numbers are dimmed: they are one hot game
# away from meaning something else entirely, and the table should say so.
THIN_SAMPLE = 3


def _tier_text(content, tier):
    info = content.tier(tier)
    return Text(info["name"], style=f"bold #{info['color']}")


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

    if row.hidden:
        # Streamer mode: the client hides these players, so we do too.
        cells["player"] = Text(HIDDEN_LABEL, style="dim italic")
        return cells

    winrate = row.winrate
    wr_style = ""
    if winrate is not None:
        wr_style = "green" if winrate >= 55 else "red" if winrate < 45 else ""

    cells["player"] = Text(row.name or "?", style="bold white" if row.is_self else "")
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


def _width(cells_per_row, keys):
    """What the rendered table would cost: content + padding + borders."""
    total = 3 * len(keys) + 1
    for key in keys:
        widths = [cell_len(BY_KEY[key].header)]
        widths += [cell_len(cells[key].plain) for cells in cells_per_row]
        total += max(widths)
    return total


def _fit(cells_per_row, keys, max_width):
    """Drop the least useful columns until the table stops being cropped."""
    keys = list(keys)
    dropped = []
    while _width(cells_per_row, keys) > max_width:
        droppable = [BY_KEY[k] for k in keys if BY_KEY[k].priority]
        if not droppable:
            break  # nothing left to give; rich crops from here
        victim = max(droppable, key=lambda column: column.priority)
        keys.remove(victim.key)
        dropped.append(victim.header)
    return keys, dropped


def _wanted(show_skins, show_perf):
    keys = []
    for column in COLUMNS:
        if column.key in PERF_KEYS and not show_perf:
            continue
        if column.key == "knife" and not show_skins:
            continue
        keys.append(column.key)
    return keys


def build_table(rows, content, title, show_skins=False, show_perf=False, own_team=None):
    # Own team first, you at the top of it, then everyone by rank descending.
    ordered = sorted(rows, key=lambda r: (r.team != own_team, r.team, not r.is_self, -r.tier))
    cells_per_row = [_row_cells(row, content, own_team, show_perf) for row in ordered]

    keys, dropped = _fit(cells_per_row, _wanted(show_skins, show_perf), console.width)
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


SUMMARY_COLUMNS = (
    ("team", "Team", "center"),
    ("agent", "Agent", "left"),
    ("player", "Player", "left"),
    ("acs", "ACS", "right"),
    ("kda", "K/D/A", "right"),
    ("kast", "KAST", "right"),
    ("hs", "HS%", "right"),
    ("dd", "DD", "right"),
)


def _summary_cells(row, own_team):
    final = row.final or {}
    side, side_style = "ALLY", "green"
    if not own_team:
        side, side_style = row.team or "-", "cyan"
    elif row.team != own_team:
        side, side_style = "ENEMY", "red"

    name = HIDDEN_LABEL if row.hidden else (row.name or "?")
    acs = final.get("acs")
    kast = final.get("kast")
    hs = final.get("hs")
    dd = final.get("dd")
    return {
        "team": Text(side, style=side_style),
        "agent": Text(row.agent),
        "player": Text(name, style="bold white" if row.is_self else ""),
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


def build_summary_table(rows, title, own_team=None, caption=None):
    """The scoreboard for the match that just ended, best ACS first."""
    ordered = sorted(
        rows,
        key=lambda r: (r.team != own_team, r.team, -((r.final or {}).get("acs") or 0)),
    )
    table = Table(
        title=title,
        title_style="bold",
        header_style="dim",
        caption=caption,
        caption_style="dim italic",
        expand=False,
    )
    for _key, header, justify in SUMMARY_COLUMNS:
        table.add_column(header, justify=justify, no_wrap=True)

    previous_team = None
    for row in ordered:
        if previous_team is not None and row.team != previous_team:
            table.add_section()
        previous_team = row.team
        cells = _summary_cells(row, own_team)
        table.add_row(*(cells[key] for key, _h, _j in SUMMARY_COLUMNS))
    return table


class MatchView:
    """One table per match, refreshed in place as the numbers arrive.

    Ranks land in a second, recent form costs a dozen more requests, and the
    lobby doubles in size when agent select turns into the match itself. All
    of that lands in the same block instead of printing another table.
    """

    def __init__(self, content):
        self.content = content
        self.live = None
        self.rows = []
        self.title = ""
        self.own_team = None
        self.show_skins = False
        self.show_perf = False
        self.status = ""

    def update(
        self,
        rows=KEEP,
        title=KEEP,
        own_team=KEEP,
        show_skins=KEEP,
        show_perf=KEEP,
        status=KEEP,
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

        renderable = self._renderable()
        if self.live is None:
            console.print()
            self.live = Live(
                renderable,
                console=console,
                refresh_per_second=4,
                vertical_overflow="visible",
            )
            self.live.start()
        else:
            self.live.update(renderable)

    def close(self, status=KEEP):
        """Stop refreshing but leave the finished table on screen."""
        if self.live is None:
            return
        if status is not KEEP:
            self.status = status
            self.live.update(self._renderable())
        self.live.stop()
        self.live = None

    def _renderable(self):
        table = build_table(
            self.rows,
            self.content,
            title=self.title,
            show_skins=self.show_skins,
            show_perf=self.show_perf,
            own_team=self.own_team,
        )
        if not self.status:
            return table
        return Group(table, Text(self.status, style="dim"))


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
