"""The breachlab command."""

import argparse
import json
import os
import re
import sys
import time
import traceback

from breachlab import __version__
from breachlab import config
from breachlab.client import (
    BreachLab,
    CLI_USER_AGENT,
    DEFAULT_SCOPE,
    DEFAULT_TIMEOUT,
    endpoint_missing,
    seconds,
)
from breachlab.errors import (
    ApiError,
    ConfigError,
    DeviceFlowError,
    NetworkError,
    NotLoggedIn,
    UnexpectedResponse,
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
# Which scopes exist is the server's business. The client only refuses names
# that could not be a scope on any server.
SCOPE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
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

    def client(self, need_token=True, token=None, quiet=False):
        """The SDK client every command talks through.

        The command decides where the token comes from and never lets the
        client look for one, and it repeats nothing after a failure, so what
        it prints stays the same from run to run.
        """
        if need_token and token is None:
            token, self.token_source = config.load_token()
            if not token:
                raise NotLoggedIn()
        return BreachLab(
            token=token,
            base_url=os.environ.get(config.ENV_API) or config.DEFAULT_API,
            timeout=self.args.timeout,
            user_agent=CLI_USER_AGENT,
            debug=self._debug_line if self.debug else None,
            notify=None if quiet else self.note,
            wait_on_rate_limit=True if quiet else not self.args.no_wait,
            retry_errors=False,
            auto_token=False,
        )


# -- commands ------------------------------------------------------------------


def cmd_login(ctx):
    client = ctx.client(need_token=False)
    scope = _requested_scope(ctx.args)
    try:
        start = client.start_device_login(scope)
    except UnexpectedResponse as exc:
        raise Failure(exc.detail, EXIT_REFUSED, code="bad_response")
    except ApiError as exc:
        if exc.code == "invalid_scope":
            exc.detail = "The server refused the requested scope. %s." % exc.detail.rstrip(". ")
        raise

    device_code = start["device_code"]
    user_code = start["user_code"]
    url = start.get("verification_uri_complete") or start.get("verification_uri")
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
        me = ctx.client(token=token, quiet=True).me()
        if isinstance(me.get("username"), str):
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
        if ctx.args.scope or set(granted_scope.split()) != set(scope.split()):
            ctx.say("Granted scopes: %s." % (clean(", ".join(granted_scope.split())) or "none"))
        if "submit" not in granted_scope.split():
            ctx.say("This token can read but cannot submit flags.")
        ctx.say("Next: breachlab status")
    if env_set:
        ctx.note("Note: %s is set and takes priority over the saved token." % config.ENV_TOKEN)
    return EXIT_OK


def _requested_scope(args):
    """The scope string to ask for: space separated names, as the API expects."""
    if args.read_only:
        return "read"
    if not args.scope:
        return DEFAULT_SCOPE
    names = []
    for chunk in args.scope:
        for name in re.split(r"[,\s]+", chunk):
            if not name:
                continue
            if not SCOPE_RE.match(name):
                raise Failure('"%s" is not a scope name. A scope name uses letters, digits, '
                              'underscores, dots, colons and hyphens only.' % name, EXIT_USAGE)
            if name not in names:
                names.append(name)
    if not names:
        raise Failure("No scope given. Example: breachlab login --scope read,submit", EXIT_USAGE)
    return " ".join(names)


def _poll_for_token(client, device_code, interval, expires_in):
    """Wait for the approval, wording every way it can end for the terminal."""
    try:
        return client.poll_device_login(device_code, interval, expires_in)
    except DeviceFlowError as exc:
        if exc.expired_locally:
            raise Failure('The code expired before it was approved. Run "breachlab login" again.',
                          EXIT_AUTH, code="expired_token")
        if exc.code == "access_denied":
            raise Failure("%s Nothing was saved."
                          % (exc.description or "The request was denied in the browser."),
                          EXIT_AUTH, code="access_denied", doc_url=exc.doc_url)
        if exc.code == "expired_token":
            raise Failure('The code expired or was already used. Run "breachlab login" again.',
                          EXIT_AUTH, code="expired_token", doc_url=exc.doc_url)
        raise Failure("The server refused the login. %s"
                      % (exc.description or "It gave no reason."),
                      EXIT_REFUSED, code=exc.code, doc_url=exc.doc_url)


def cmd_logout(ctx):
    path = config.token_path()
    shown = config.display_path(path)
    env_set = bool(os.environ.get(config.ENV_TOKEN, "").strip())
    try:
        token = config.load_saved_token()
        damaged = False
    except config.TokenError:
        token, damaged = None, True

    # Revoke on the server first, while the token is still at hand. Whatever
    # the server says, the local file goes away afterwards.
    outcome = _revoke(ctx, token) if token else None
    removed = config.delete_token()

    by_hand = "Revoke it by hand at %s" % ACCOUNT_PAGE
    warning = None
    if outcome == "revoked":
        text = "Logged out. The token was revoked on the server and removed from %s." % shown
    elif outcome == "already_invalid":
        text = ("Logged out. The server had already revoked the token or let it expire. "
                "It was removed from %s." % shown)
    elif outcome is not None:
        text = "Logged out. The token was removed from %s." % shown
        if outcome == "unsupported":
            reason = "This server cannot revoke tokens yet"
        elif outcome == "unreachable":
            reason = "The server could not be reached"
        else:
            reason = "The server did not revoke the token"
        warning = "%s, so the token may still be valid. %s" % (reason, by_hand)
    elif damaged and removed:
        text = "Logged out. The damaged token file %s was removed." % shown
        warning = ("Its token could not be read, so nothing was revoked. "
                   "If that token may still be valid, revoke it by hand at %s" % ACCOUNT_PAGE)
    elif removed:
        text = "Logged out. The empty token file %s was removed." % shown
    else:
        text = "You were not logged in. There is no saved token."

    if ctx.json:
        result = {"logged_out": removed, "revoked": outcome == "revoked", "token_path": path}
        if outcome is not None:
            result["revocation"] = outcome
        if warning:
            result["revoke_url"] = ACCOUNT_PAGE
        ctx.emit(result)
    else:
        ctx.say(text)
    if warning:
        ctx.note(warning)
    if env_set:
        ctx.note("Note: %s is still set, so commands will keep using that token. "
                 "Logout does not revoke it." % config.ENV_TOKEN)
    return EXIT_OK


def _revoke(ctx, token):
    """Ask the server to revoke the saved token. Return what came of it.

    "revoked", "already_invalid" when the server no longer knows the token,
    "unsupported" when the server has no such endpoint, "unreachable", or
    "failed" for anything else. It never raises, so logout always finishes.
    """
    try:
        client = ctx.client(token=token)
        # Logging out should not sit through a rate limit, so no waiting.
        answer = client.revoke_token(wait=False)
    except ApiError as exc:
        if exc.status == 401:
            return "already_invalid"
        if endpoint_missing(exc):
            return "unsupported"
        return "failed"
    except NetworkError:
        return "unreachable"
    except ConfigError:
        return "failed"
    if answer.get("revoked") is True:
        return "revoked"
    return "failed"


def cmd_whoami(ctx):
    client = ctx.client()
    me = client.me()
    token = _token_details(client)
    if ctx.json:
        # The account object, plus the token object under "token". That key is
        # null on a server that cannot describe the token yet.
        ctx.emit(dict(me, token=token))
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
    rank = _rank_text(me)
    if rank:
        rows.append(("Rank", rank))
    if me.get("is_agent"):
        rows.append(("Account type", "AI agent"))
    if ctx.token_source == "env":
        rows.append(("Token", "from %s" % config.ENV_TOKEN))
    else:
        rows.append(("Token", config.display_path(config.token_path())))
    scopes = None
    if token is not None:
        scopes = [s for s in token.get("scopes") or [] if isinstance(s, str)] \
            if isinstance(token.get("scopes"), list) else []
        rows.append(("Scopes", clean(", ".join(scopes)) or "none"))
        expires = token.get("expires_at")
        rows.append(("Expires", "%s UTC" % _when(expires) if isinstance(expires, str) and expires
                     else "never"))
    ctx.say(pairs(rows, ctx.style))
    if scopes is not None and "submit" not in scopes:
        ctx.say()
        ctx.say('This token cannot submit flags. Run "breachlab login" to get one that can.')
    return EXIT_OK


def _token_details(client):
    """The calling token as the API describes it, or None on an older server."""
    try:
        return client.token_info()
    except UnexpectedResponse:
        return None
    except ApiError as exc:
        if endpoint_missing(exc):
            return None
        raise


def _rank_text(me):
    """The account's leaderboard place, or None when it is not ranked or unknown."""
    place = me.get("leaderboard")
    if not isinstance(place, dict):
        return None
    rank = _count(place.get("rank"))
    if not rank:
        return None
    if place.get("board") in ("humans", "agents"):
        return "%d on the %s board" % (rank, place["board"])
    return str(rank)


def cmd_tracks(ctx):
    client = ctx.client()
    tracks, more = _all(client.tracks())
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

    if ctx.args.level is not None:
        level = _one_level(client, slug, ctx.args.level)
        if ctx.json:
            ctx.emit(level)
            return EXIT_OK
        _print_level(ctx, level)
        return EXIT_OK

    levels, more = _levels_of(client, slug)
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


def _levels_of(client, slug):
    try:
        return _all(client.levels(slug))
    except ApiError as exc:
        if exc.code == "not_found":
            exc.detail = 'There is no live track named "%s". Run "breachlab tracks" to list them.' \
                % clean(slug)
        raise


def _one_level(client, slug, idx):
    """One level from its own endpoint, or from the level list on an older server.

    A 404 also falls through to the list. That costs one more request for a
    level that does not exist, and in return the error can say whether the
    track or the level is the unknown one.
    """
    try:
        return client.level(slug, idx)
    except ApiError as exc:
        if not endpoint_missing(exc):
            raise

    levels, _ = _levels_of(client, slug)
    for level in levels:
        if isinstance(level, dict) and level.get("idx") == idx:
            return level
    raise Failure('Track "%s" has no level %d. Run "breachlab levels %s" to list them.'
                  % (clean(slug), idx, clean(slug)), EXIT_REFUSED, code="not_found")


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
    me = client.me()
    report = _progress(client)

    if ctx.json:
        ctx.emit({"object": "status", "account": me, "tracks": report})
        return EXIT_OK

    headline = "%s  %s points  %s solved" % (ctx.style.paint(clean(me.get("username")), "bold"),
                                             me.get("points", 0), me.get("solved", 0))
    rank = _rank_text(me)
    if rank:
        headline += "  rank " + rank
    ctx.say(headline)
    if not report:
        ctx.say("No tracks are live yet.")
        return EXIT_OK
    ctx.say()
    rows = []
    for item in report:
        total = _count(item.get("level_count"))
        solved = _count(item.get("solved"))
        nxt = item.get("next_level_idx")
        if not isinstance(nxt, int) or isinstance(nxt, bool):
            nxt = None
        locked = item.get("access") == "locked"
        if total == 0:
            upcoming = ("no levels yet", "dim")
        elif solved >= total:
            upcoming = ("complete", "green")
        elif nxt is None and locked:
            upcoming = ("not open to your account", "dim")
        elif nxt is None:
            upcoming = ("locked", "dim")
        elif locked:
            upcoming = ("%d, not open to your account" % nxt, "dim")
        else:
            upcoming = str(nxt)
        access = clean(item.get("access"))
        rows.append([
            clean(item.get("track")),
            clean(item.get("name")),
            "%d/%d" % (solved, total),
            (access, "dim" if locked else None),
            upcoming,
        ])
    ctx.say(table(["TRACK", "NAME", "SOLVED", "ACCESS", "NEXT LEVEL"], rows, ctx.style, right=(2,)))
    return EXIT_OK


def _progress(client):
    """One track_progress object per live track.

    A current server answers this in one call. An older one is asked for its
    tracks and then for the levels of each, and the same objects are built
    here, so the output does not depend on the server's age.
    """
    try:
        entries = client.progress().all()
    except ApiError as exc:
        if not endpoint_missing(exc):
            raise
    else:
        return [entry for entry in entries if isinstance(entry, dict)]

    report = []
    for track in client.tracks():
        slug = track.get("slug") if isinstance(track, dict) else None
        if not isinstance(slug, str):
            continue
        levels = [lv for lv in client.levels(slug) if isinstance(lv, dict)]
        open_idx = [lv["idx"] for lv in levels if lv.get("status") == "available"
                    and isinstance(lv.get("idx"), int) and not isinstance(lv.get("idx"), bool)]
        report.append({
            "object": "track_progress",
            "track": slug,
            "name": track.get("name"),
            "access": track.get("access"),
            "level_count": len(levels),
            "solved": sum(1 for lv in levels if lv.get("status") == "solved"),
            "next_level_idx": min(open_idx) if open_idx else None,
        })
    return report


def cmd_submit(ctx):
    flag = _read_flag(ctx)
    client = ctx.client()
    # A wrong flag earns a growing cooldown, so a submission is never retried
    # behind the user's back unless they asked for it with --wait.
    try:
        result = client.submit(flag, wait=ctx.args.wait)
    except UnexpectedResponse as exc:
        raise Failure(exc.detail, EXIT_REFUSED, code="bad_response")
    verdict = result["result"]
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
    if args.country and not COUNTRY_RE.match(args.country):
        raise Failure("A country is a two letter code such as us or de.", EXIT_USAGE)
    if args.board == "agents" and (args.period != "all" or args.country):
        raise Failure("The agents board supports only the all time period and no country filter.",
                      EXIT_USAGE)
    if args.country and args.period != "all":
        raise Failure("A country filter works only with the all time period.", EXIT_USAGE)

    client = ctx.client()
    entries, more = _all(client.leaderboard(
        board=args.board, period=args.period, country=args.country,
        max_items=None if args.all else args.limit))
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
    solves, more = _all(client.solves(max_items=None if ctx.args.all else ctx.args.limit))
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


def _all(paginator):
    """Every item of a list, and whether the API had more past the last one."""
    items = paginator.all()
    return items, bool(paginator.has_more)


def _count(value):
    """A count from the API as a whole number that is never negative."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


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
    scopes = login.add_mutually_exclusive_group()
    scopes.add_argument("--read-only", action="store_true",
                        help="ask for a token that can read but cannot submit flags")
    scopes.add_argument("--scope", nargs="+", metavar="SCOPE",
                        help="ask for exactly these scopes, separated by commas or spaces, "
                             "for example read,submit,play. The default is read,submit")

    command("logout", cmd_logout, "revoke the saved token and remove it from this machine")
    command("whoami", cmd_whoami, "show the account and what the token may do")
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
