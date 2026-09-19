"""Checking GitHub for a newer version, and installing it on request.

    python -m valstats update           check, and offer to install
    python -m valstats update check     check and say, install nothing
    python -m valstats update now       install without asking

The program is handed to people as a zip, and a zip does not come back for
fixes. So it asks: once at startup, at most once a day, with a y/n, and never
while a match is on.

What an update replaces, and what it must never touch
-----------------------------------------------------
Only code. The list is KEEP below, and it is the important part of this file:

    config.json     your settings, including anybody's API keys
    encounters.db   every match and every player this install has ever cached -
                    months of work, and not recoverable from anywhere
    cache/          the content dumps
    .venv/          the interpreter this is running on

Replacing a database with a fresh one because a version number moved is the
single worst thing an updater can do, so the rule here is inverted from the
usual one: nothing is deleted, ever. Files are copied over, and a file the new
version does not have is left alone rather than removed.

Getting it wrong anyway
-----------------------
A half-written update is a program that does not start, on somebody else's
machine, with no way to ask them to run a command. So the install is staged:
everything is downloaded and unpacked and checked first, the current files are
copied to a backup folder, and only then is anything overwritten. If a copy
fails part way through, the backup goes back.

The update is applied and the program says to restart. It does not restart
itself: the code it is running is in memory and already stale, and relaunching
from inside a half-swapped process is how an updater eats its own feet.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import __version__, render

ROOT = Path(__file__).resolve().parent.parent

# What an update is allowed to write. Same shape as tools/make_release.py, and
# for the same reason: an allowlist is the only kind of list that is safe when
# the thing being listed arrives over the network.
REPLACE = (
    "run.bat",
    "player.bat",
    "requirements.txt",
    "config.example.json",
    "README.md",
    "valstats/*.py",
    "tests/*.py",
    "server/*",
    "tools/*.py",
)

# Never written, never deleted, not even looked at. See the docstring.
KEEP = ("config.json", "encounters.db", "cache", ".venv", "errors.log", "out.txt")

# How often to look, in hours. Once a day is plenty for a program somebody runs
# to watch a match: an update that lands this morning can wait until tomorrow.
CHECK_EVERY_HOURS = 24.0

TIMEOUT = 30.0

# A release archive bigger than this is not this project.
MAX_BYTES = 32 * 1024 * 1024


class Failed(Exception):
    """The update could not be applied, and nothing was changed."""


def parts(version):
    """"1.2.3" -> (1, 2, 3), tolerating a leading v and trailing noise."""
    text = str(version or "").strip().lstrip("vV")
    out = []
    for piece in text.split(".")[:3]:
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def newer(candidate, current=__version__):
    """Is `candidate` a version worth offering?"""
    return parts(candidate) > parts(current)


def api(repo, path):
    """One GitHub API call. Returns None rather than raising for a 404."""
    url = f"https://api.github.com/repos/{repo}{path}"
    response = requests.get(
        url,
        timeout=TIMEOUT,
        headers={"accept": "application/vnd.github+json", "user-agent": "valstats-update"},
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def latest(repo):
    """{version, url, notes, published} for the newest release, or None.

    Releases first, because a release is the maintainer saying "this one is
    ready" - which is the whole point of asking somebody else to install it.
    A repository with no releases yet falls back to the head of the default
    branch, so the feature works before the first tag is cut.
    """
    release = api(repo, "/releases/latest")
    if release and release.get("tag_name"):
        return {
            "version": release["tag_name"],
            "url": release.get("zipball_url") or "",
            "notes": (release.get("body") or "").strip(),
            "published": (release.get("published_at") or "")[:10],
            "kind": "release",
        }

    head = api(repo, "/commits/HEAD")
    if not head or not head.get("sha"):
        return None
    commit = (head.get("commit") or {}).get("message") or ""
    return {
        "version": head["sha"][:7],
        "url": f"https://api.github.com/repos/{repo}/zipball/{head['sha']}",
        "notes": commit.splitlines()[0] if commit else "",
        "published": (((head.get("commit") or {}).get("author") or {}).get("date") or "")[:10],
        "kind": "commit",
    }


def due(db, hours=CHECK_EVERY_HOURS):
    """Is it time to look again?"""
    last = db.setting("update_checked_at") if db else None
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if not when.tzinfo:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() >= max(0.0, hours) * 3600


def available(config, db=None, force=False):
    """The newer version on GitHub, or None. Never raises, never blocks long.

    A machine with no network, a rate-limited API, a repository that has been
    renamed - all of them mean "no update today", not "stop the program".
    """
    repo = (config.get("update_repo") or "").strip()
    if not repo or not config.get("check_updates", True):
        return None
    if not force and not due(db, config.get("update_check_hours", CHECK_EVERY_HOURS)):
        return None
    try:
        found = latest(repo)
    except (requests.RequestException, ValueError):
        return None
    if db:
        db.remember("update_checked_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if not found:
        return None
    if found["kind"] == "release" and not newer(found["version"]):
        return None
    if found["kind"] == "commit" and found["version"] == (db.setting("update_seen") if db else ""):
        # No releases to compare against, so "new" means "not the one we
        # already offered and were turned down on".
        return None
    return found


def fetch(url):
    """Download the archive into memory and hand back its ZipFile."""
    response = requests.get(url, timeout=TIMEOUT, headers={"user-agent": "valstats-update"})
    response.raise_for_status()
    if len(response.content) > MAX_BYTES:
        raise Failed(f"the download is {len(response.content) / 1e6:.0f} MB - that is not this")
    try:
        return zipfile.ZipFile(io.BytesIO(response.content))
    except zipfile.BadZipFile as exc:
        raise Failed(f"the download is not a zip: {exc}") from exc


def unpack(archive, into):
    """Extract a GitHub zipball, dropping its single top-level folder.

    GitHub wraps everything in "owner-repo-sha/", which nobody wants on disk.
    Paths are checked on the way out: an entry that climbs out of the target
    directory is refused rather than written, because an archive is a thing
    that arrives from elsewhere.
    """
    root = into.resolve()
    names = [name for name in archive.namelist() if not name.endswith("/")]
    if not names:
        raise Failed("the archive is empty")
    prefix = ""
    first = names[0].split("/")[0]
    if all(name.startswith(first + "/") for name in names):
        prefix = first + "/"

    for name in names:
        relative = name[len(prefix):]
        if not relative:
            continue
        target = (root / relative).resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise Failed(f"the archive tries to write outside itself: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(name) as source, open(target, "wb") as sink:
            shutil.copyfileobj(source, sink)
    return root


def plan(staged):
    """Which files in the unpacked copy this is allowed to install."""
    import fnmatch

    out = []
    for pattern in REPLACE:
        if "/" in pattern:
            folder, glob = pattern.rsplit("/", 1)
            base = staged / folder
            if not base.is_dir():
                continue
            for path in sorted(base.iterdir()):
                if path.is_file() and fnmatch.fnmatch(path.name, glob):
                    out.append(path.relative_to(staged))
        else:
            path = staged / pattern
            if path.is_file():
                out.append(path.relative_to(staged))
    return [item for item in out if not _protected(item)]


def _protected(relative):
    """Is this one of the paths an update must never write?"""
    head = relative.parts[0]
    return head in KEEP or relative.as_posix() in KEEP


def sane(staged):
    """Does the unpacked copy actually look like this program?

    Cheap, and the difference between installing a bad build and refusing to.
    """
    must = ("valstats/__init__.py", "valstats/app.py", "run.bat", "requirements.txt")
    missing = [name for name in must if not (staged / name).is_file()]
    if missing:
        raise Failed(f"the download is missing {', '.join(missing)}")
    return True


def install(found, root=ROOT, say=render.info):
    """Apply an update. Returns the backup folder, or raises Failed.

    Nothing is written until everything is downloaded, unpacked and checked.
    """
    if not found or not found.get("url"):
        raise Failed("nothing to install")

    with tempfile.TemporaryDirectory(prefix="valstats-update-") as scratch:
        staged = unpack(fetch(found["url"]), Path(scratch) / "new")
        sane(staged)
        wanted = plan(staged)
        if not wanted:
            raise Failed("the download had nothing installable in it")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = root / ".backup" / stamp
        backup.mkdir(parents=True, exist_ok=True)

        done = []
        try:
            for relative in wanted:
                target = root / relative
                if target.exists():
                    kept = backup / relative
                    kept.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, kept)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(staged / relative, target)
                done.append(relative)
        except OSError as exc:
            _roll_back(root, backup, done, say)
            raise Failed(f"could not write {relative}: {exc}") from exc

        say(f"updated {len(done)} files - the previous copy is in .backup/{stamp}")
        return backup


def _roll_back(root, backup, done, say):
    """Put back what was already overwritten, as far as that is possible."""
    say("the update failed part way - putting the previous files back")
    for relative in done:
        kept = backup / relative
        if kept.is_file():
            try:
                shutil.copy2(kept, root / relative)
            except OSError:
                say(f"could not restore {relative} - it is in .backup")


def offer(config, db, found, ask=input, say=render.info):
    """Ask, once, whether to install. Returns True if it was applied.

    Answering no is remembered, so the same version is not offered again on
    every single start - it is offered again when a newer one appears.
    """
    if not found:
        return False

    say("")
    say(f"a newer version is on GitHub: {found['version']}" + (
        f" ({found['published']})" if found.get("published") else ""
    ))
    if found.get("notes"):
        for line in found["notes"].splitlines()[:6]:
            say(f"    {line}")
    say(f"you are running {__version__}")
    say("your config.json, encounters.db and cache are never touched by an update")

    try:
        answer = (ask("install it now? [y/N] ") or "").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    if answer not in ("y", "yes", "д", "да"):
        if db:
            db.remember("update_seen", found["version"])
        say("left alone - run.bat update when you want it")
        return False

    try:
        install(found, say=say)
    except (Failed, requests.RequestException, OSError) as exc:
        render.warn(f"the update did not go through: {exc}")
        render.warn("nothing was changed")
        return False

    if db:
        db.remember("update_seen", found["version"])
    say("")
    say("done - close this window and start run.bat again")
    return True


def at_startup(config, db, ask=input, say=render.info):
    """The check the program makes when it starts. Silent when there is nothing.

    Deliberately not on a timer and not between matches: an update swaps the
    code out from under a running process, and the only moment that is safe is
    before anything has happened.
    """
    if not config.get("check_updates", True):
        return False
    try:
        found = available(config, db)
    except Exception:  # noqa: BLE001 - an update check may never stop a start
        return False
    if not found:
        return False
    if config.get("auto_update"):
        say(f"installing {found['version']} from GitHub...")
        try:
            install(found, say=say)
        except (Failed, requests.RequestException, OSError) as exc:
            render.warn(f"the update did not go through: {exc}; nothing was changed")
            return False
        if db:
            db.remember("update_seen", found["version"])
        say("updated - close this window and start run.bat again")
        return True
    return offer(config, db, found, ask=ask, say=say)


def main(argv=None):
    """`python -m valstats update [check|now]`."""
    from .config import load as load_config
    from .db import Encounters

    argv = list(argv or ())
    command = argv[0].lower() if argv else "ask"
    config = load_config()
    repo = (config.get("update_repo") or "").strip()
    if not repo:
        print("update_repo is not set in config.json - nothing to check against")
        print('  for example: "update_repo": "your-name/valorant-stats"')
        return 1

    db = Encounters(True)
    try:
        print(f"installed {__version__}, checking {repo} ...")
        found = available(config, db, force=True)
        if not found:
            print("you are on the latest version")
            return 0

        if command == "check":
            print(f"available: {found['version']} ({found.get('published') or 'undated'})")
            if found.get("notes"):
                print(f"  {found['notes'].splitlines()[0]}")
            print("run  run.bat update  to install it")
            return 0
        if command == "now":
            install(found, say=print)
            db.remember("update_seen", found["version"])
            print("done - start run.bat again")
            return 0
        return 0 if offer(config, db, found, say=print) else 0
    except Failed as exc:
        print(f"the update did not go through: {exc}")
        print("nothing was changed")
        return 1
    except requests.RequestException as exc:
        print(f"could not reach GitHub: {exc}")
        return 1
    finally:
        if db.conn:
            db.conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
