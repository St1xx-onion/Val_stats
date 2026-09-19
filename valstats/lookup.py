"""One player, looked up by name, out of the local memory alone.

    python -m valstats player [name]

`who` answers a name you can already spell. This is for the other case: you
remember the first two letters, or the shape of it, or you are squinting at a
Riot ID full of lookalike characters. Type what you have and the list narrows
as you go, forgiving a slip or two; Enter opens whoever you landed on, with
every match you have shared with them, down to the minute.

Nothing here touches the network. The game can be closed and the machine
offline: encounters.db already holds all of it.
"""

import sys
from datetime import datetime, timezone

from rich.live import Live
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import render
from .content import Content
from .db import Encounters
from .history import _label, _link, _self_puuid
from .perf import aggregate, load_calibration, summarise

# How many names the completer offers at once. More than this and the list
# stops being something you can read between two keystrokes.
MAX_SUGGESTIONS = 8

# How wrong a name may be and still be offered. Two edits is a fumbled key and
# a doubled letter; three starts offering strangers.
MAX_TYPOS = 2

# Below this many characters only the beginning of a name is matched: at one
# or two letters every name in the memory is within two edits of every other.
MIN_FUZZY = 3

DASH = Text("-", style="dim")


# ------------------------------------------------------------------ matching


def distance(a, b, limit=MAX_TYPOS):
    """Levenshtein distance, given up on as soon as it passes `limit`.

    Bounded on purpose: this runs over every name in the memory on every
    keystroke, and the only thing ever asked of it is "close enough or not".
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, start=1):
        current = [i]
        for j, right in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right))
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _rank(name, query):
    """How well one remembered name answers what has been typed, or None.

    Lower is better, and the order is the order you would want the list in:
    what you typed exactly, then what starts with it, then what contains it,
    then what you nearly typed - at the front of the name first, because that
    is where a half-finished Riot ID goes wrong, then anywhere in it.
    """
    short = name.split("#")[0]
    if query in (name, short):
        return 0
    if short.startswith(query) or name.startswith(query):
        return 1
    if query in name:
        return 2
    if len(query) < MIN_FUZZY:
        return None
    # Compare against only as much of the name as has been typed: a slip in
    # "st1x" should still find St1xx-onion, which as a whole is nothing like it.
    near = distance(short[: len(query)], query)
    if near <= MAX_TYPOS:
        return 3 + near
    whole = distance(short, query)
    if whole <= MAX_TYPOS:
        return 6 + whole
    return None


def suggest(players, query, limit=MAX_SUGGESTIONS):
    """The names worth offering for what has been typed so far, best first.

    An empty query is not a failure to match - it is how every lookup starts,
    and the people you meet most are the best guess available.
    """
    query = (query or "").strip().lower()
    named = [player_row for player_row in players if player_row["name"]]
    if not query:
        return sorted(named, key=lambda row: -(row["times"] or 0))[:limit]
    scored = []
    for player_row in named:
        name = player_row["name"].lower()
        rank = _rank(name, query)
        if rank is None:
            continue
        scored.append((rank, -(player_row["times"] or 0), name, player_row))
    scored.sort(key=lambda row: row[:3])
    return [row[3] for row in scored[:limit]]


# ------------------------------------------------------------------- display


def _when(ts, seconds=False):
    """A stored UTC timestamp as local wall-clock time, which is what you saw."""
    if not ts:
        return "?"
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return ts[:16]
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    shape = "%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M"
    return moment.astimezone().strftime(shape)


def _name(text):
    """Riot IDs can contain brackets, which rich would read as markup."""
    return escape(text or "[hidden]")


def _num(value, shape="{:.0f}", suffix=""):
    return DASH if value is None else Text(f"{shape.format(value)}{suffix}")


def _kda(line):
    return f"{line['kills']}/{line['deaths']}/{line['assists']}"


def _who(row, own_puuid):
    """What to call one line of a scoreboard.

    Your own name is not in the encounter log - you are not somebody you ran
    into - so the one row that would otherwise show a bare puuid is the one row
    that needs no name at all.
    """
    if row["name"]:
        return _name(row["name"])
    if own_puuid and row["puuid"] == own_puuid:
        return "you"
    return f"{row['puuid'][:8]}..."


class _Lines:
    """The cached scoreboard of a match, read once however often it is asked for."""

    def __init__(self, db):
        self.db = db
        self._by_match = {}

    def get(self, match_id):
        if match_id not in self._by_match:
            self._by_match[match_id] = self.db.match_lines(match_id)
        return self._by_match[match_id]

    def line(self, match_id, puuid):
        for line in self.get(match_id):
            if line["puuid"] == puuid:
                return line
        return None


def _side(line, own):
    """ALLY / ENEMY where both sides are known, the raw side where they are not."""
    if not line or not line.get("team"):
        return "", ""
    if not own or not own.get("team"):
        return line["team"], "cyan"
    return ("ALLY", "green") if line["team"] == own["team"] else ("ENEMY", "red")


def _outcome(line):
    if not line or not line.get("rounds"):
        return DASH
    return Text("WON", style="green") if line.get("won") else Text("LOST", style="red")


def _plain_table():
    return Table(header_style="dim", box=None, pad_edge=False, padding=(0, 1))


def _fitted(spec, cells_per_row, sections=()):
    """A table holding only the columns this window has room for.

    The same rule the live lobby table follows, and for the same reason: a
    scoreboard cropped down the middle is unreadable, while one that gave up
    KAST is merely shorter. Priorities are per table - see render.Column - and
    zero means the column stays whatever happens.
    """
    by_key = {column.key: column for column in spec}
    keys, dropped = render._fit(
        cells_per_row, [column.key for column in spec], render.console.width, by_key
    )
    table = _plain_table()
    for key in keys:
        column = by_key[key]
        table.add_column(column.header, justify=column.justify, no_wrap=True)
    for place, cells in enumerate(cells_per_row):
        if place in sections:
            table.add_section()
        table.add_row(*(cells[key] for key in keys))
    if dropped:
        table.caption = "window too narrow, hidden: " + ", ".join(
            header for header in reversed(dropped) if header
        )
        table.caption_style = "dim italic"
    return table


MATCH_COLUMNS = (
    render.Column("place", "#", "right"),
    render.Column("match", "Match", "left", 15),
    render.Column("when", "When"),
    render.Column("map", "Map"),
    render.Column("queue", "Queue", "left", 70),
    render.Column("side", "Side", "center", 20),
    render.Column("agent", "Agent", "left", 25),
    render.Column("rank", "Rank", "left", 50),
    render.Column("kda", "K/D/A", "right"),
    render.Column("acs", "ACS", "right"),
    render.Column("hs", "HS%", "right", 55),
    render.Column("kast", "KAST", "right", 60),
    render.Column("dd", "DD", "right", 30),
    render.Column("result", "", "left"),
)

BOARD_COLUMNS = (
    render.Column("side", "Side", "center"),
    render.Column("player", "Player"),
    render.Column("rank", "Rank", "left", 20),
    render.Column("agent", "Agent", "left", 10),
    render.Column("kda", "K/D/A", "right"),
    render.Column("acs", "ACS", "right"),
    render.Column("hs", "HS%", "right", 50),
    render.Column("kast", "KAST", "right", 55),
    render.Column("dd", "DD", "right", 40),
    render.Column("result", "", "left"),
)


def _matches_table(db, content, entry, lines, own_puuid):
    """Every match you shared with one player, newest first, numbered to open."""
    met = db.met_in(entry["puuid"])
    if not met:
        return None, []
    cells_per_row = []
    for place, met_row in enumerate(met, start=1):
        match_id = met_row["match_id"]
        theirs = lines.line(match_id, entry["puuid"])
        mine = lines.line(match_id, own_puuid) if own_puuid else None
        stats = summarise([theirs]) if theirs and theirs["rounds"] else None
        side, side_style = _side(theirs, mine)
        cells_per_row.append(
            {
                "place": Text(str(place), style="dim"),
                "match": Text(_label(met_row["num"]), style="dim"),
                "when": Text(_when(met_row["ts"])),
                "map": Text(content.map_name(met_row["map_id"] or "") or "?"),
                "queue": Text(met_row["queue"] or "?", style="dim"),
                "side": Text(side, style=side_style) if side else DASH,
                "agent": Text(content.agent((theirs or {}).get("agent") or "") or "-"),
                "rank": (
                    Text(content.tier(theirs["tier"])["name"])
                    if theirs and theirs.get("tier")
                    else DASH
                ),
                "kda": Text(_kda(theirs)) if theirs else DASH,
                "acs": _num(stats and stats["acs"]),
                "hs": _num(stats and stats["hs"], suffix="%"),
                "kast": _num(stats and stats["kast"], suffix="%"),
                "dd": _num(stats and stats["dd"], "{:+.0f}"),
                "result": _outcome(theirs),
            }
        )
    return _fitted(MATCH_COLUMNS, cells_per_row), met


def _card(db, content, entry, lines, own_puuid, calibration):
    """One player: who they are to this install, and every match you shared."""
    render.console.print()
    render.console.print(
        f"[bold]{_name(entry['name'])}[/bold]  "
        f"[dim]met {entry['times']}x, first {_when(entry['first_seen'])}, "
        f"last {_when(entry['last_seen'])}[/dim]"
    )
    render.console.print(f"  [dim]puuid[/dim] {entry['puuid']}")
    summary = aggregate(db.all_perf_rows(entry["puuid"]), calibration)
    if summary:
        render.console.print(
            f"  [dim]over {summary['matches']} cached matches:[/dim] "
            f"score {summary['rating']}  ACS {summary['acs']:.0f}  "
            f"K/D {summary['kd']:.2f}  WR {summary['winrate']:.0f}%"
        )
    table, met = _matches_table(db, content, entry, lines, own_puuid)
    render.console.print()
    if table is None:
        render.warn(
            "we have met them, but not in a match this memory can name - "
            "run.bat backfill fills those in"
        )
        return []
    render.console.print(table)
    return met


def _scoreboard(db, content, match_id, lines, highlight=None, own_puuid=None):
    """The whole match as the local cache remembers it: both sides, all ten."""
    rows = lines.get(match_id)
    info = db.match_info(match_id) or {
        "num": None,
        "match_id": match_id,
        "ts": None,
        "map_id": "",
        "queue": "",
    }
    if not rows:
        render.warn(
            f"{_label(info['num'])} is in the encounter log but was never parsed - "
            "run.bat backfill downloads the ones still missing"
        )
        return 1

    mine = next((row for row in rows if row["puuid"] == own_puuid), None)
    where = content.map_name(info["map_id"] or "") or "?"
    verdict = ""
    if mine and mine.get("rounds"):
        verdict = "  WON" if mine.get("won") else "  LOST"
    render.console.print()
    render.console.print(
        f"[bold]{_label(info['num'])}  {_when(info['ts'], seconds=True)}[/bold]  "
        f"[dim]{where}  {info['queue'] or '?'}  {rows[0]['rounds']} rounds{verdict}[/dim]"
    )
    render.console.print(f"  [dim]match id[/dim] {info['match_id']}")

    def sort_key(row):
        stats = summarise([row]) if row["rounds"] else None
        same_side = bool(mine) and row.get("team") == mine.get("team")
        return (not same_side, row.get("team") or "", -((stats or {}).get("acs") or 0))

    cells_per_row, sections, previous = [], set(), None
    for row in sorted(rows, key=sort_key):
        if previous is not None and row.get("team") != previous:
            sections.add(len(cells_per_row))
        previous = row.get("team")
        stats = summarise([row]) if row["rounds"] else None
        side, side_style = _side(row, mine)
        style = ""
        if row["puuid"] == own_puuid:
            style = "bold white"
        elif row["puuid"] == highlight:
            style = "bold cyan"
        cells_per_row.append(
            {
                "side": Text(side, style=side_style) if side else DASH,
                "player": Text(_who(row, own_puuid), style=style),
                "rank": Text(content.tier(row["tier"])["name"]) if row.get("tier") else DASH,
                "agent": Text(content.agent(row.get("agent") or "") or "-"),
                "kda": Text(_kda(row)),
                "acs": _num(stats and stats["acs"]),
                "hs": _num(stats and stats["hs"], suffix="%"),
                "kast": _num(stats and stats["kast"], suffix="%"),
                "dd": _num(stats and stats["dd"], "{:+.0f}"),
                "result": _outcome(row),
            }
        )
    render.console.print()
    render.console.print(_fitted(BOARD_COLUMNS, cells_per_row, sections))
    nameless = sum(1 for row in rows if not row["name"] and row["puuid"] != own_puuid)
    if nameless:
        render.console.print(
            f"  [dim]{nameless} of {len(rows)} players have no name here - they were "
            "hidden when you met and have not been named since[/dim]"
        )
    return 0


# -------------------------------------------------------------------- typing


def _read_key():
    """One keypress: a name for the special keys, the character itself otherwise.

    Windows only, which is where this program runs; anywhere else the caller
    falls back to a plain prompt. Key names are longer than one character, so
    the caller can tell them apart from what was typed.
    """
    import msvcrt

    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):  # an arrow or a function key, in two parts
        return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
    if key == "\r":
        return "enter"
    if key == "\x08":
        return "back"
    if key == "\t":
        return "tab"
    if key == "\x1b":
        return "esc"
    if key in ("\x03", "\x04"):  # ctrl-c, ctrl-d: msvcrt hands these over as text
        return "quit"
    return key if key.isprintable() else ""


def _suggestions(typed, found, chosen):
    """The prompt line and the list under it, as one thing to redraw."""
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
    for _ in range(4):
        table.add_column(no_wrap=True)
    table.add_row(
        Text("name:", style="dim"),
        Text(typed or "", style="bold") + Text("_", style="dim"),
        Text(""),
        Text("enter opens   tab completes   esc quits", style="dim"),
    )
    if not found:
        table.add_row(Text(""), Text("nothing like that in the local memory", style="dim italic"))
    for place, player_row in enumerate(found):
        here = place == chosen
        table.add_row(
            Text(">" if here else "", style="cyan"),
            Text(_name(player_row["name"]), style="bold cyan" if here else ""),
            Text(f"met {player_row['times']}x", style="dim"),
            Text(f"last {_when(player_row['last_seen'])}", style="dim"),
        )
    return table


def _choose(players):
    """Pick a player by typing at it. None when the user is done."""
    typed, chosen = "", 0
    found = suggest(players, typed)
    with Live(
        _suggestions(typed, found, chosen),
        console=render.console,
        transient=True,
        refresh_per_second=20,
    ) as live:
        while True:
            key = _read_key()
            if key in ("esc", "quit"):
                return None
            if key == "enter":
                if found:
                    return found[chosen]
                continue
            if key == "up":
                chosen = max(0, chosen - 1)
            elif key == "down":
                chosen = min(len(found) - 1, chosen + 1) if found else 0
            elif key == "tab":
                if found:
                    typed = found[chosen]["name"]
                    found, chosen = suggest(players, typed), 0
            elif key == "back":
                typed = typed[:-1]
                found, chosen = suggest(players, typed), 0
            elif len(key) == 1:
                typed += key
                found, chosen = suggest(players, typed), 0
            else:
                continue
            live.update(_suggestions(typed, found, chosen))


def _ask(prompt):
    """One line of input; None when there is nobody there to answer.

    A blank line and a closed stdin are not the same thing, and telling them
    apart is what keeps `player <name> | less` from opening the same match
    forever: blank means "the default, please", end of input means "stop".
    """
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        render.console.print()
        return None


def _pick_number(count, what):
    """A number in 1..count, or None for "never mind".

    Blank means back, and nothing means blank: a typo asks again rather than
    dropping you out of the player you were looking at, and end of input ends
    the loop instead of answering it forever.
    """
    while count > 0:
        answer = _ask(f"{what} 1-{count} (blank to go back): ")
        if not answer:
            return None
        try:
            number = int(answer.lstrip("Mm#"))
        except ValueError:
            number = 0
        if 1 <= number <= count:
            return number
        render.warn(f"{answer!r} is not one of 1-{count}")
    return None


def _choose_plainly(players):
    """The same choice without live typing, for a console that cannot do it."""
    while True:
        typed = _ask("name (blank to quit): ")
        if not typed:  # None from a closed stdin, "" from a blank line: both stop
            return None
        found = suggest(players, typed)
        if not found:
            render.warn(f"nobody like {typed!r} in the local memory")
            continue
        if len(found) == 1:
            return found[0]
        for place, player_row in enumerate(found, start=1):
            render.console.print(
                f"  [dim]{place}[/dim] {_name(player_row['name'])} "
                f"[dim]met {player_row['times']}x[/dim]"
            )
        picked = _pick_number(len(found), "which one")
        if picked:
            return found[picked - 1]


def _interactive():
    """Whether the live completer can run here at all."""
    if sys.platform != "win32":
        return False
    try:
        return sys.stdin.isatty()
    except ValueError:  # a closed stdin, which is not a console either
        return False


# ------------------------------------------------------------------ commands


def player(query=None):
    """Find a player by name, then open the matches you shared with them."""
    db = Encounters()
    try:
        _link(db)
        people = db.named_players()
        if not people:
            render.warn(
                "no named players in the local memory yet - play a match with this "
                "running, or fill it in from your own history with run.bat backfill"
            )
            return 1
        content = _content()
        own_puuid = _self_puuid(db)
        calibration = load_calibration(db, "auto")
        lines = _Lines(db)
        render.info(f"{len(people)} named players in the local memory")

        asked = False
        while True:
            if query and not asked:
                entry = _resolve(people, query)
                if entry is None:
                    return 1
            elif _interactive():
                entry = _choose(people)
            else:
                entry = _choose_plainly(people)
            asked = True
            if entry is None:
                return 0
            met = _card(db, content, entry, lines, own_puuid, calibration)
            while met:
                place = _pick_number(len(met), "open match")
                if not place:
                    break
                _scoreboard(
                    db,
                    content,
                    met[place - 1]["match_id"],
                    lines,
                    highlight=entry["puuid"],
                    own_puuid=own_puuid,
                )
            if query:
                return 0  # a name on the command line asks one question, not many
    finally:
        db.close()


def _resolve(people, query):
    """The player a name typed on the command line meant, if it meant just one."""
    found = suggest(people, query)
    if not found:
        render.warn(f"nobody like {query!r} in the local memory")
        return None
    if len(found) > 1 and (found[0]["name"] or "").lower() != query.strip().lower():
        render.console.print()
        render.console.print(
            f"[dim]{len(found)} players look like[/dim] {escape(query)}[dim]:[/dim]"
        )
        for player_row in found:
            render.console.print(
                f"  {_name(player_row['name'])} [dim]met {player_row['times']}x, "
                f"last {_when(player_row['last_seen'])}[/dim]"
            )
        render.console.print()
        render.info("run it without a name to pick one by typing at the list")
        return None
    return found[0]


def _content():
    """Agent and rank names, or plain ids when there is no cache and no network."""
    try:
        return Content().load(want_skins=False)
    except Exception as exc:  # noqa: BLE001 - being offline is a state, not a fault
        render.warn(f"no cached game content and no way to fetch it ({exc}) - showing raw ids")
        return _RawContent()


class _RawContent:
    """Stand-in for Content: says what it knows, which is the id it was handed."""

    current_act = None

    @staticmethod
    def agent(uuid):
        return (uuid or "")[:8] or "-"

    @staticmethod
    def map_name(map_id):
        return (map_id or "").rsplit("/", 1)[-1] or ""

    @staticmethod
    def tier(number):
        return {"name": f"tier {number}", "color": "808080"}
