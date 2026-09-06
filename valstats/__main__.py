"""Entry point: `python -m valstats [command]`."""

import sys

USAGE = """usage: python -m valstats [command]

  (no command)        watch for matches and print the live lobby
  who <name>          what the local memory knows about a player
  top [n]             the people you run into most often
  backfill [n]        parse your own recent matches into the cache
  calibration         what the 0-1000 score is measured against
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

        watch()
        return 0

    command, rest = argv[0].lower(), argv[1:]
    if command in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    from . import history

    if command == "who":
        if not rest:
            print("usage: python -m valstats who <name>")
            return 2
        return history.who(" ".join(rest))
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
