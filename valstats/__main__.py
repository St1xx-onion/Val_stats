"""Entry point: `python -m valstats [command]`."""

import sys

USAGE = """usage: python -m valstats [command]

  (no command)        watch for matches and print the live lobby
  who <name>          what the local memory knows about a player
  mates <name>        who that player keeps queueing with
  identify [who]      put Riot IDs to the players who hid behind streamer mode
  match <M12>         the Riot match id behind a local match number
  top [n]             the people you run into most often
  backfill [n]        parse your own recent matches into the cache
  agents [n]          your own agent pool, the record the pick advice uses
  calibration         what the 0-1000 score is measured against
  share [now|reset]   the shared match pool: what it is, and send what is due
  pool [sync|forget]  the downloaded pool: what is in it, and fetch it now
  update [check|now]  look on GitHub for a newer version and offer to install
"""


def _number(argv, default):
    try:
        return max(1, int(argv[0]))
    except (IndexError, ValueError):
        return default


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from .app import main as watch

        return watch() or 0

    command, rest = argv[0].lower(), argv[1:]
    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    if command in ("update", "upgrade"):
        from . import update as update_module

        return update_module.main(rest)

    if command == "pool":
        from . import pool as pool_module

        return pool_module.main(rest)

    if command == "share":
        from . import share as share_module

        return share_module.main(rest)

    if command in ("identify", "id"):
        from . import identify as identify_module

        return identify_module.identify(" ".join(rest) or None)

    from . import history

    if command == "who":
        if not rest:
            print("usage: python -m valstats who <name>")
            return 2
        return history.who(" ".join(rest))
    if command in ("mates", "with"):
        if not rest:
            print("usage: python -m valstats mates <name>")
            return 2
        return history.mates(" ".join(rest))
    if command == "match":
        if not rest:
            print("usage: python -m valstats match <M12>")
            return 2
        return history.match(rest[0])
    if command == "agents":
        return history.agents(_number(rest, 15))
    if command == "top":
        return history.top(_number(rest, 20))
    if command == "backfill":
        return history.backfill(_number(rest, 20))
    if command in ("calibration", "calib"):
        return history.calibration()

    print(f"unknown command {command!r}\n")
    print(USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
