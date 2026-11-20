"""The breachlab command."""

import argparse
import json
import os
import re
import sys
import time
import traceback
import urllib.parse

from breachlab import __version__
from breachlab import config
from breachlab.client import (
    ApiError,
    Client,
    ConfigError,
    DEFAULT_TIMEOUT,
    NetworkError,
    NotLoggedIn,
    error_from,
    seconds,
)
from breachlab.output import Style, clean, pairs, table, use_color

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_AUTH = 3
EXIT_RATE_LIMITED = 4
EXIT_NETWORK = 5
EXIT_INTERRUPTED = 130

DOCS_AUTH = "https://breachlab.org/docs/api/authentication"
ACCOUNT_PAGE = "https://breachlab.org/dashboard/account"
FLAG_MIN = 4
FLAG_MAX = 128
COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
DIFFICULTY_STYLE = {"easy": "green", "medium": "yellow", "hard": "red", "insane": "red"}
STATUS_STYLE = {"solved": "green", "available": None, "locked": "dim"}

sleep = time.sleep


class Failure(Exception):
    """A command failed for a reason that is already worded for the user."""

    def __init__(self, message, exit_code, code=None, doc_url=None, extra=None):
        Exception.__init__(self, message)
        self.message = message
        self.exit_code = exit_code
        self.code = code
        self.doc_url = doc_url
        self.extra = extra or {}


class Context:
    def __init__(self, args):
        self.args = args
        self.json = bool(getattr(args, "json", False))
        self.debug = bool(getattr(args, "debug", False))
        self.out = sys.stdout
        self.err = sys.stderr
        self.style = Style(use_color(self.out) and not self.json)
        self.err_style = Style(use_color(self.err))
        self.token_source = None

    def say(self, text=""):
        print(text, file=self.out)

    def note(self, text=""):
        print(text, file=self.err)

    def emit(self, value):
        """Print a value as JSON on stdout."""
        print(json.dumps(value, indent=2, ensure_ascii=False), file=self.out)

    def _debug_line(self, text):
        print(self.err_style.paint("debug: " + text, "dim"), file=self.err)

    def client(self, need_token=True):
        base = os.environ.get(config.ENV_API) or config.DEFAULT_API
        token = None
        if need_token:
            token, self.token_source = config.load_token()
            if not token:
                raise NotLoggedIn()
        return Client(
            base,
            token=token,
            timeout=self.args.timeout,
            debug=self._debug_line if self.debug else None,
            notify=self.note,
            auto_wait=not self.args.no_wait,
        )


# -- commands ------------------------------------------------------------------


def cmd_login(ctx):
    client = ctx.client(need_token=False)
    scope = "read" if ctx.args.read_only else "read submit"
    start = client.call("POST", "/api/v1/auth/device", body={"scope": scope}, auth=False)

    device_code = start.get("device_code") if isinstance(start, dict) else None
    user_code = start.get("user_code") if isinstance(start, dict) else None
    if not isinstance(device_code, str) or not isinstance(user_code, str):
        raise Failure("The server started the login in an unexpected way.", EXIT_REFUSED,
                      code="bad_response")
    client.add_secret(device_code)
    url = start.get("verification_uri_complete") or start.get("verification_uri")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise Failure("The server did not say where to approve the login.", EXIT_REFUSED,
                      code="bad_response")
    expires_in = _positive_int(start.get("expires_in"), 600)
    interval = _positive_int(start.get("interval"), 5)

    # With --json the instructions go to stderr so stdout stays machine readable.
    tell = ctx.note if ctx.json else ctx.say
    style = ctx.err_style if ctx.json else ctx.style
    tell("Open this page in a browser, sign in and approve the request:")
    tell()
    tell("  " + style.paint(clean(url), "cyan"))
    tell()
    tell("Code: " + style.paint(clean(user_code), "bold"))
    tell("The code expires in %s. Waiting for approval. Press Ctrl+C to cancel."
         % _duration(expires_in))

    granted = _poll_for_token(client, device_code, interval, expires_in)
    token = granted.get("access_token")
    if not config.valid_token(token):
        raise Failure("The server returned a token in an unexpected format.", EXIT_REFUSED,
                      code="bad_response")
    client.add_secret(token)
    path = config.save_token(token)
    granted_scope = granted.get("scope") if isinstance(granted.get("scope"), str) else scope

    username = None
    try:
        me = Client(client.base_url, token=token, timeout=ctx.args.timeout,
                    debug=ctx._debug_line if ctx.debug else None).call("GET", "/api/v2/me")
        if isinstance(me, dict) and isinstance(me.get("username"), str):
            username = me["username"]
    except (ApiError, NetworkError):
        pass

    env_set = bool(os.environ.get(config.ENV_TOKEN, "").strip())
    if ctx.json:
        ctx.emit({
            "logged_in": True,
            "username": username,
            "scope": granted_scope,
            "token_path": path,
        })
    else:
        who = " as " + style.paint(clean(username), "bold") if username else ""
        ctx.say()
        ctx.say("Logged in%s. Token saved to %s." % (who, config.display_path(path)))
        if "submit" not in granted_scope.split():
            ctx.say("This token can read but cannot submit flags.")
        ctx.say("Next: breachlab status")
    if env_set:
        ctx.note("Note: %s is set and takes priority over the saved token." % config.ENV_TOKEN)
    return EXIT_OK


def _poll_for_token(client, device_code, interval, expires_in):
    elapsed = 0
    failures = 0
    while True:
        # One second over the interval, so network jitter cannot make a poll
        # arrive early and earn a slow_down.
        wait = interval + 1
        if elapsed + wait > expires_in:
            raise Failure('The code expired before it was approved. Run "breachlab login" again.',
                          EXIT_AUTH, code="expired_token")
        sleep(wait)
        elapsed += wait
        try:
            status, headers, payload = client.send(
                "POST", "/api/v1/auth/token", body={"device_code": device_code}, auth=False)
        except NetworkError:
            failures += 1
            if failures >= 3:
                raise
            continue
        failures = 0
        if status == 200 and isinstance(payload, dict) and "access_token" in payload:
            return payload
        error = payload.get("error") if isinstance(payload, dict) else None
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise Failure("The request was denied in the browser. Nothing was saved.",
                          EXIT_AUTH, code="access_denied")
        if error == "expired_token":
            raise Failure('The code expired or was already used. Run "breachlab login" again.',
                          EXIT_AUTH, code="expired_token")
        failure = error_from(status, headers, payload)
        if status == 429:
            pause = failure.retry_after or interval
            if elapsed + pause > expires_in:
                raise failure
            sleep(pause)
            elapsed += pause
            continue
        raise failure


def cmd_logout(ctx):
    path = config.token_path()
    removed = config.delete_token()
    env_set = bool(os.environ.get(config.ENV_TOKEN, "").strip())
    if ctx.json:
        ctx.emit({"logged_out": removed, "token_path": path})
    elif removed:
        ctx.say("Logged out. The token was removed from %s." % config.display_path(path))
        ctx.say("It stays valid on the server until you revoke it at %s" % ACCOUNT_PAGE)
    else:
        ctx.say("You were not logged in. There is no saved token.")
    if env_set:
        ctx.note("Note: %s is still set, so commands will keep using that token."
                 % config.ENV_TOKEN)
    return EXIT_OK


def cmd_whoami(ctx):
    client = ctx.client()
    me = client.call("GET", "/api/v2/me")
    if ctx.json:
        ctx.emit(me)
        return EXIT_OK
    access = me.get("access") if isinstance(me.get("access"), dict) else {}
    if access.get("subscription_active"):
        subscription = "active"
    elif access.get("paywall_enabled") is False:
        subscription = "inactive, the paywall is off"
    else:
        subscription = "inactive"
    rows = [
        ("User", ctx.style.paint(clean(me.get("username")), "bold")),
        ("Points", str(me.get("points", 0))),
        ("Solved", str(me.get("solved", 0))),
        ("Subscription", subscription),
    ]
    if me.get("is_agent"):
        rows.append(("Account type", "AI agent"))
    if ctx.token_source == "env":
        rows.append(("Token", "from %s" % config.ENV_TOKEN))
    else:
        rows.append(("Token", config.display_path(config.token_path())))
    ctx.say(pairs(rows, ctx.style))
    return EXIT_OK


def cmd_tracks(ctx):
    client = ctx.client()
    tracks, more = client.fetch_list("/api/v2/tracks")
    if ctx.json:
        ctx.emit({"object": "list", "data": tracks, "has_more": more})
        return EXIT_OK
    if not tracks:
        ctx.say("No tracks are live yet.")
        return EXIT_OK
    rows = []
    for track in tracks:
        mix = track.get("difficulty_mix") if isinstance(track.get("difficulty_mix"), dict) else {}
        access = clean(track.get("access"))
        rows.append([
            clean(track.get("slug")),
            clean(track.get("name")),
            clean(track.get("status")),
            (access, "dim" if access == "locked" else None),
            str(track.get("level_count", 0)),
            str(mix.get("easy", 0)),
            str(mix.get("medium", 0)),
            str(mix.get("hard", 0)),
            str(mix.get("insane", 0)),
        ])
    headers = ["TRACK", "NAME", "STATUS", "ACCESS", "LEVELS", "EASY", "MEDIUM", "HARD", "INSANE"]
    ctx.say(table(headers, rows, ctx.style, right=(4, 5, 6, 7, 8)))
    return EXIT_OK


def cmd_levels(ctx):
    client = ctx.client()
    slug = ctx.args.track.strip().lower()
    try:
        levels, more = client.fetch_list("/api/v2/tracks/%s/levels" % _path_part(slug))
    except ApiError as exc:
        if exc.code == "not_found":
            exc.detail = 'There is no live track named "%s". Run "breachlab tracks" to list them.' \
                % clean(slug)
        raise

    if ctx.args.level is not None:
        match = [lv for lv in levels if lv.get("idx") == ctx.args.level]
        if not match:
            raise Failure('Track "%s" has no level %d. Run "breachlab levels %s" to list them.'
                          % (clean(slug), ctx.args.level, clean(slug)), EXIT_REFUSED,
                          code="not_found")
        level = match[0]
        if ctx.json:
            ctx.emit(level)
            return EXIT_OK
        _print_level(ctx, level)
        return EXIT_OK

    if ctx.json:
        ctx.emit({"object": "list", "data": levels, "has_more": more})
        return EXIT_OK
    if not levels:
        ctx.say("This track has no levels yet.")
        return EXIT_OK
    rows = []
    for level in levels:
        difficulty = clean(level.get("difficulty")) or "none"
        status = clean(level.get("status"))
        access = clean(level.get("access"))
        rows.append([
            str(level.get("idx", "")),
            clean(level.get("title")),
            (difficulty, DIFFICULTY_STYLE.get(difficulty)),
            str(level.get("points", 0)),
            (status, STATUS_STYLE.get(status)),
            (access, "dim" if access == "locked" else None),
        ])
    ctx.say(table(["LEVEL", "TITLE", "DIFFICULTY", "POINTS", "STATUS", "ACCESS"], rows, ctx.style,
                  right=(0, 3)))
    return EXIT_OK


def _print_level(ctx, level):
    title = "%s level %s: %s" % (clean(level.get("track")), level.get("idx"),
                                 clean(level.get("title")))
    ctx.say(ctx.style.paint(title, "bold"))
    ctx.say(pairs([
        ("Difficulty", clean(level.get("difficulty")) or "none"),
        ("Points", "%s, first blood bonus %s" % (level.get("points", 0),
                                                  level.get("first_blood_bonus", 0))),
        ("Status", clean(level.get("status"))),
        ("Access", clean(level.get("access"))),
    ], ctx.style))
    ctx.say()
    description = level.get("description")
    if description:
        ctx.say(clean(description, multiline=True).strip())
    elif level.get("access") == "locked":
        ctx.say("The briefing is hidden because this level is not open to your account.")
    else:
        ctx.say("This level has no briefing.")


def cmd_status(ctx):
    client = ctx.client()
    me = client.call("GET", "/api/v2/me")
    tracks, _ = client.fetch_list("/api/v2/tracks")
    report = []
    for track in tracks:
        slug = track.get("slug")
        if not isinstance(slug, str):
            continue
        levels, _ = client.fetch_list("/api/v2/tracks/%s/levels" % _path_part(slug))
        solved = [lv for lv in levels if lv.get("status") == "solved"]
        open_levels = sorted((lv for lv in levels if lv.get("status") == "available"),
                             key=lambda lv: lv.get("idx", 0))
        report.append({
            "slug": slug,
            "name": track.get("name"),
            "access": track.get("access"),
            "levels": len(levels),
            "solved": len(solved),
            "points": sum(lv.get("points", 0) for lv in solved
                          if isinstance(lv.get("points"), int)),
            "completed": bool(levels) and len(solved) == len(levels),
            "next_level": open_levels[0] if open_levels else None,
        })

    if ctx.json:
        ctx.emit({"object": "status", "account": me, "tracks": report})
        return EXIT_OK

    ctx.say("%s  %s points  %s solved" % (ctx.style.paint(clean(me.get("username")), "bold"),
                                          me.get("points", 0), me.get("solved", 0)))
    if not report:
        ctx.say("No tracks are live yet.")
        return EXIT_OK
    ctx.say()
    rows = []
    for item in report:
        nxt = item["next_level"]
        if item["levels"] == 0:
            upcoming = ("no levels yet", "dim")
        elif item["completed"]:
            upcoming = ("complete", "green")
        elif nxt is None and item["access"] == "locked":
            upcoming = ("not open to your account", "dim")
        elif nxt is None:
            upcoming = ("locked", "dim")
        else:
            text = "%s %s" % (nxt.get("idx"), clean(nxt.get("title")))
            if nxt.get("access") == "locked":
                upcoming = (text + ", not open to your account", "dim")
            else:
                upcoming = text
        access = clean(item["access"])
        rows.append([
            clean(item["slug"]),
            "%d/%d" % (item["solved"], item["levels"]),
            str(item["points"]),
            (access, "dim" if access == "locked" else None),
            upcoming,
        ])
    ctx.say(table(["TRACK", "SOLVED", "POINTS", "ACCESS", "NEXT LEVEL"], rows, ctx.style,
                  right=(1, 2)))
    return EXIT_OK


def cmd_submit(ctx):
    flag = _read_flag(ctx)
    client = ctx.client()
    # A wrong flag earns a growing cooldown, so a submission is never retried
    # behind the user's back unless they asked for it with --wait.
    result = client.call("POST", "/api/v2/submit", body={"flag": flag}, wait=ctx.args.wait)
    verdict = result.get("result") if isinstance(result, dict) else None
    if verdict not in ("correct", "incorrect"):
        raise Failure("The server sent a verdict in an unexpected shape.", EXIT_REFUSED,
                      code="bad_response")
    if ctx.json:
        ctx.emit(result)
        return EXIT_OK if verdict == "correct" else EXIT_REFUSED

    if verdict == "incorrect":
        ctx.say("%s The API does not say whether the flag is wrong or its level is not open to you."
                % ctx.style.paint("Incorrect.", "red"))
        return EXIT_REFUSED

    good = ctx.style.paint("Correct.", "green")
    if result.get("test") is True:
        ctx.say("%s That was the test flag, so nothing was recorded. Your token can submit." % good)
        return EXIT_OK
    line = "%s %s level %s solved, %s points awarded." % (
        good, clean(result.get("track")), result.get("level_idx"), result.get("points_awarded", 0))
    if result.get("first_blood"):
        line += " First blood."
    if result.get("track_completed"):
        line += " Track completed."
    ctx.say(line)
    return EXIT_OK


def _read_flag(ctx):
    raw = ctx.args.flag
    if sys.stdin is None and raw in (None, "-"):
        raise Failure("No flag given and standard input is closed.", EXIT_USAGE)
    if raw is None and sys.stdin.isatty():
        try:
            print("Flag: ", end="", file=ctx.err, flush=True)
            raw = sys.stdin.readline()
        except EOFError:
            raw = ""
        flag = raw.strip()
    elif raw is None or raw == "-":
        data = sys.stdin.read(65536)
        flag = data.strip()
        if "\n" in flag or "\r" in flag:
            raise Failure("Expected one flag on standard input, but got several lines.", EXIT_USAGE)
    else:
        flag = raw
    if not flag:
        raise Failure("No flag given. Pass it as an argument, or pipe it in: "
                      "cat flag | breachlab submit -", EXIT_USAGE)
    if not FLAG_MIN <= len(flag) <= FLAG_MAX:
        raise Failure("A flag is %d to %d characters long. This one has %d."
                      % (FLAG_MIN, FLAG_MAX, len(flag)), EXIT_USAGE)
    return flag


def cmd_leaderboard(ctx):
    args = ctx.args
    query = {}
    if args.board != "humans":
        query["board"] = args.board
    if args.period != "all":
        query["period"] = args.period
    if args.country:
        if not COUNTRY_RE.match(args.country):
            raise Failure("A country is a two letter code such as us or de.", EXIT_USAGE)
        query["country"] = args.country.lower()
    if args.board == "agents" and (args.period != "all" or args.country):
        raise Failure("The agents board supports only the all time period and no country filter.",
                      EXIT_USAGE)
    if args.country and args.period != "all":
        raise Failure("A country filter works only with the all time period.", EXIT_USAGE)

    client = ctx.client()
    entries, more = client.fetch_list("/api/v2/leaderboard", query=query,
                                      max_items=None if args.all else args.limit)
    if ctx.json:
        ctx.emit({"object": "list", "data": entries, "has_more": more})
        return EXIT_OK
    if not entries:
        ctx.say("This board is empty.")
        return EXIT_OK
    rows = []
    if args.board == "agents":
        for entry in entries:
            rows.append([
                str(entry.get("rank", "")),
                clean(entry.get("agent_name")),
                clean(entry.get("agent_model")) or "undisclosed",
                clean(entry.get("username")),
                str(entry.get("points", 0)),
                str(entry.get("solved", 0)),
            ])
        text = table(["RANK", "AGENT", "MODEL", "OPERATOR", "POINTS", "SOLVED"], rows, ctx.style,
                     right=(0, 4, 5))
    else:
        for entry in entries:
            rows.append([
                str(entry.get("rank", "")),
                clean(entry.get("username")),
                str(entry.get("points", 0)),
                str(entry.get("solved", 0)),
                clean(entry.get("country")),
                ("hall of fame", "yellow") if entry.get("is_hall_of_fame") else "",
            ])
        text = table(["RANK", "USER", "POINTS", "SOLVED", "COUNTRY", "NOTE"], rows, ctx.style,
                     right=(0, 2, 3))
    ctx.say(text)
    if more:
        ctx.note("More entries exist. Use --limit N or --all to see them.")
    return EXIT_OK


def cmd_solves(ctx):
    client = ctx.client()
    solves, more = client.fetch_list("/api/v2/me/solves",
                                     max_items=None if ctx.args.all else ctx.args.limit)
    if ctx.json:
        ctx.emit({"object": "list", "data": solves, "has_more": more})
        return EXIT_OK
    if not solves:
        ctx.say("No solves yet. Run \"breachlab status\" to see where to start.")
        return EXIT_OK
    rows = []
    for solve in solves:
        rows.append([
            _when(solve.get("solved_at")),
            clean(solve.get("track")),
            str(solve.get("level_idx", "")),
            clean(solve.get("title")),
            str(solve.get("points", 0)),
        ])
    ctx.say(table(["SOLVED AT UTC", "TRACK", "LEVEL", "TITLE", "POINTS"], rows, ctx.style,
                  right=(2, 4)))
    if more:
        ctx.note("Older solves exist. Use --limit N or --all to see them.")
    return EXIT_OK


# -- helpers -------------------------------------------------------------------


def _path_part(value):
    return urllib.parse.quote(value, safe="")


def _positive_int(value, default):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _duration(total):
    if total >= 90:
        minutes = int(round(total / 60.0))
        return "1 minute" if minutes == 1 else "%d minutes" % minutes
    return seconds(total)


def _when(value):
    """Shorten an ISO timestamp such as 2026-11-06T09:30:00Z to minutes."""
    text = clean(value)
    match = re.match(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})", text)
    return "%s %s" % (match.group(1), match.group(2)) if match else text


def _limit(text):
    try:
        number = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number")
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _level_idx(text):
    try:
        number = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a level number such as 0 or 12")
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _timeout(text):
    try:
        number = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of seconds")
    if not 0 < number <= 600:
        raise argparse.ArgumentTypeError("must be between 0 and 600 seconds")
    return number


# -- argument parsing ----------------------------------------------------------

EPILOG = """\
first run:
  breachlab login                 approve the request in your browser
  breachlab status                your progress and the next level per track
  breachlab submit 'BL{...}'      submit a flag
  cat flag | breachlab submit -   submit a flag from standard input

exit codes:
  0 ok   1 wrong flag or refused by the API   2 usage   3 not authenticated
  4 rate limited   5 the API could not be reached

environment:
  BREACHLAB_TOKEN   use this token instead of the saved one
  BREACHLAB_API     API base URL, https only except for localhost
  NO_COLOR          turn colour off

docs: https://breachlab.org/docs/api
"""


def build_parser():
    def add_global(parser, top):
        # The same flags are accepted before and after the command name. On a
        # subcommand they default to SUPPRESS so they never overwrite a value
        # given before the command.
        def default(value):
            return value if top else argparse.SUPPRESS
        group = parser.add_argument_group("global options")
        group.add_argument("--json", action="store_true", default=default(False),
                           help="print JSON for scripts")
        group.add_argument("--debug", action="store_true", default=default(False),
                           help="show requests and full errors, with the token redacted")
        group.add_argument("--timeout", type=_timeout, metavar="SECONDS",
                           default=default(DEFAULT_TIMEOUT),
                           help="seconds to wait for the API, default %g" % DEFAULT_TIMEOUT)
        group.add_argument("--no-wait", action="store_true", default=default(False),
                           help="when rate limited, fail at once and do not wait and retry")

    parser = argparse.ArgumentParser(
        prog="breachlab",
        description="Submit flags and check your BreachLab progress from the terminal.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="breachlab %s" % __version__)
    add_global(parser, top=True)
    sub = parser.add_subparsers(dest="command", metavar="command")

    def command(name, func, summary, **kwargs):
        child = sub.add_parser(name, help=summary, description=summary[0].upper() + summary[1:] + ".",
                               **kwargs)
        child.set_defaults(func=func)
        add_global(child, top=False)
        return child

    login = command("login", cmd_login, "log in by approving a code in your browser")
    login.add_argument("--read-only", action="store_true",
                       help="ask for a token that can read but cannot submit flags")

    command("logout", cmd_logout, "remove the saved token from this machine")
    command("whoami", cmd_whoami, "show the account the token belongs to")
    command("tracks", cmd_tracks, "list the live tracks")

    levels = command("levels", cmd_levels, "list the levels of a track, or show one briefing")
    levels.add_argument("track", help="track slug, for example ghost")
    levels.add_argument("level", nargs="?", type=_level_idx,
                        help="level number, to show that level's briefing")

    command("status", cmd_status, "show your progress and the next level per track")

    submit = command("submit", cmd_submit, "submit a flag")
    submit.add_argument("flag", nargs="?",
                        help="the flag. Use - or leave it out to read it from standard input")
    submit.add_argument("--wait", action="store_true",
                        help="when rate limited for up to a minute, wait and retry")

    board = command("leaderboard", cmd_leaderboard, "show the leaderboard")
    board.add_argument("--board", choices=("humans", "agents"), default="humans",
                       help="which board, default humans")
    board.add_argument("--period", choices=("all", "weekly", "active"), default="all",
                       help="all time, this week, or most active in the last 24 hours")
    board.add_argument("--country", metavar="CC", help="two letter country code")
    board.add_argument("--limit", type=_limit, default=20, metavar="N",
                       help="how many entries to show, default 20")
    board.add_argument("--all", action="store_true", help="show every entry")

    solves = command("solves", cmd_solves, "list your solves, newest first")
    solves.add_argument("--limit", type=_limit, default=20, metavar="N",
                        help="how many solves to show, default 20")
    solves.add_argument("--all", action="store_true", help="show every solve")
    return parser


# -- error reporting -----------------------------------------------------------


def _report(ctx, message, exit_code, code=None, doc_url=None, extra=None, from_api=False):
    """Print one error line on stderr, and a JSON error on stdout with --json.

    The code is printed only when the API issued it. Codes this client makes
    up for local failures appear in the JSON output alone.
    """
    line = message.rstrip()
    if code and from_api:
        line += " Code: %s." % code
    if doc_url:
        line += " Docs: %s" % doc_url
    print("%s %s" % (ctx.err_style.paint("breachlab:", "red"), clean(line)), file=ctx.err)
    if ctx.json:
        error = {"code": code or "error", "message": message, "exit_code": exit_code}
        if doc_url:
            error["doc_url"] = doc_url
        error.update(extra or {})
        ctx.emit({"error": error})
    return exit_code


def _report_api_error(ctx, exc):
    extra = {"status": exc.status}
    if exc.request_id:
        extra["request_id"] = exc.request_id
    message = exc.detail
    exit_code = EXIT_REFUSED
    doc_url = exc.doc_url

    if exc.status == 401 or exc.code == "unauthorized":
        exit_code = EXIT_AUTH
        if ctx.token_source == "env":
            message = ("The token in %s was rejected. It may be revoked or expired."
                       % config.ENV_TOKEN)
        else:
            message = ('Your saved token was rejected. It may be revoked or expired. '
                       'Run "breachlab login" to get a new one.')
    elif exc.code == "insufficient_scope":
        exit_code = EXIT_AUTH
        message = ('Your token does not carry the scope this command needs. '
                   'Run "breachlab login" to get one that can read and submit.')
    elif exc.status == 429:
        exit_code = EXIT_RATE_LIMITED
        if exc.code == "cooldown":
            message = "Slow down after wrong answers."
        else:
            message = "Rate limited."
        if exc.retry_after is not None:
            at = time.strftime("%H:%M:%S", time.localtime(time.time() + exc.retry_after))
            message += " Retry in %s, at %s." % (seconds(exc.retry_after), at)
            extra["retry_after_seconds"] = exc.retry_after
        else:
            message += " The API did not say how long to wait."
    elif exc.code == "already_solved":
        message = "You already solved that level, so it cannot be scored again."
    elif exc.code == "email_unverified":
        message = "Verify your account's email address on breachlab.org before submitting flags."

    if ctx.debug and exc.request_id:
        ctx._debug_line("request id %s" % exc.request_id)
    return _report(ctx, message, exit_code, code=exc.code, doc_url=doc_url, extra=extra,
                   from_api=True)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context(args)
    if not getattr(args, "func", None):
        parser.print_help(ctx.out)
        return EXIT_OK
    try:
        return args.func(ctx)
    except Failure as exc:
        return _report(ctx, exc.message, exc.exit_code, code=exc.code, doc_url=exc.doc_url,
                       extra=exc.extra)
    except NotLoggedIn:
        return _report(
            ctx, 'You are not logged in. Run "breachlab login" and approve the request in your browser.',
            EXIT_AUTH, code="not_logged_in", doc_url=DOCS_AUTH)
    except config.TokenError as exc:
        return _report(ctx, str(exc), EXIT_AUTH, code="bad_token")
    except ConfigError as exc:
        return _report(ctx, str(exc), EXIT_USAGE, code="bad_config")
    except ApiError as exc:
        return _report_api_error(ctx, exc)
    except NetworkError as exc:
        if ctx.debug:
            traceback.print_exc(file=ctx.err)
        return _report(ctx, str(exc), EXIT_NETWORK, code="network_error")
    except KeyboardInterrupt:
        print("", file=ctx.err)
        return _report(ctx, "Cancelled.", EXIT_INTERRUPTED, code="cancelled")
    except Exception as exc:  # the last line of defence: never a bare stack trace
        if ctx.debug:
            traceback.print_exc(file=ctx.err)
        return _report(
            ctx, "Unexpected error: %s. Run again with --debug for details." % exc.__class__.__name__,
            EXIT_REFUSED, code="internal")


def run():
    try:
        code = main()
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader went away, as in "breachlab leaderboard --all | head".
        # Point stdout at /dev/null so the interpreter's exit flush is quiet.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        code = EXIT_OK
    sys.exit(code)


if __name__ == "__main__":
    run()
