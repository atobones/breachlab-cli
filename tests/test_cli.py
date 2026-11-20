import contextlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

from breachlab import __version__, cli, config  # noqa: E402
from breachlab.client import ConfigError, validate_base_url  # noqa: E402
from breachlab.output import Style, clean, use_color  # noqa: E402
import stub_server  # noqa: E402
from stub_server import DEVICE_CODE, GOOD_FLAG, SOLVED_FLAG, TEST_FLAG, TOKEN, USER_CODE  # noqa: E402


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class Result:
    def __init__(self, code, out, err, sleeps):
        self.code = code
        self.out = out
        self.err = err
        self.sleeps = sleeps

    def json(self):
        return json.loads(self.out)


class CliCase(unittest.TestCase):
    def setUp(self):
        self.server = stub_server.StubServer().start()
        self.state = self.server.state
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.xdg = os.path.join(self.home, "xdg")
        self.token_file = os.path.join(self.xdg, "breachlab", "token")

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()
        # Two rules hold for every request any test made.
        for request in self.state.requests:
            self.assertNotIn("blp_", request["raw_path"], "a token reached a URL")
            self.assertEqual(request["headers"].get("user-agent"), "breachlab-cli/" + __version__)

    def env(self, **extra):
        env = {"HOME": self.home, "XDG_CONFIG_HOME": self.xdg, "BREACHLAB_API": self.server.url}
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def login(self, token=TOKEN):
        with mock.patch.dict(os.environ, self.env(), clear=True):
            config.save_token(token)

    def run_cli(self, *argv, stdin=None, env=None, out=None, err=None):
        out = out if out is not None else io.StringIO()
        err = err if err is not None else io.StringIO()
        sleeps = []
        stdin_stream = stdin if hasattr(stdin, "read") else io.StringIO(stdin or "")
        with mock.patch.dict(os.environ, env if env is not None else self.env(), clear=True), \
                mock.patch("breachlab.cli.sleep", sleeps.append), \
                mock.patch("breachlab.client.sleep", sleeps.append), \
                mock.patch.object(sys, "stdin", stdin_stream), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return Result(code, out.getvalue(), err.getvalue(), sleeps)

    def paths(self, method=None):
        return [r["path"] for r in self.state.requests if method in (None, r["method"])]


class LoginTests(CliCase):
    def test_device_flow_stores_the_token(self):
        result = self.run_cli("login")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn(USER_CODE, result.out)
        self.assertIn("/activate?code=" + USER_CODE, result.out)
        self.assertIn("Logged in as operator_01.", result.out)
        self.assertNotIn(TOKEN, result.out + result.err)
        self.assertNotIn(DEVICE_CODE, result.out + result.err)

        with open(self.token_file) as handle:
            self.assertEqual(handle.read(), TOKEN + "\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.token_file)).st_mode), 0o700)
        self.assertEqual(os.listdir(os.path.dirname(self.token_file)), ["token"])

        start = self.state.requests[0]
        self.assertEqual((start["method"], start["path"]), ("POST", "/api/v1/auth/device"))
        self.assertEqual(json.loads(start["body"]), {"scope": "read submit"})
        self.assertNotIn("authorization", start["headers"])
        polls = [r for r in self.state.requests if r["path"] == "/api/v1/auth/token"]
        self.assertEqual(len(polls), 2)
        self.assertEqual(json.loads(polls[0]["body"]), {"device_code": DEVICE_CODE})
        # One second over the server's interval of 5, before every poll.
        self.assertEqual(result.sleeps, [6, 6])

    def test_slow_down_lengthens_the_interval(self):
        self.state.poll_script = ["slow_down", "authorization_pending", "approved"]
        result = self.run_cli("login")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.sleeps, [6, 11, 11])

    def test_denied(self):
        self.state.poll_script = ["access_denied"]
        result = self.run_cli("login")
        self.assertEqual(result.code, 3)
        self.assertIn("denied", result.err)
        self.assertFalse(os.path.exists(self.token_file))

    def test_expired_code(self):
        self.state.poll_script = ["expired_token"]
        result = self.run_cli("login")
        self.assertEqual(result.code, 3)
        self.assertIn('Run "breachlab login" again', result.err)

    def test_never_approved_stops_at_expiry(self):
        self.state.poll_script = ["authorization_pending"] * 500
        result = self.run_cli("login")
        self.assertEqual(result.code, 3)
        self.assertLessEqual(sum(result.sleeps), 600)
        self.assertIn("expired", result.err)

    def test_json_keeps_stdout_clean_and_hides_the_token(self):
        result = self.run_cli("login", "--json")
        self.assertEqual(result.code, 0, result.err)
        data = result.json()
        self.assertEqual(data["logged_in"], True)
        self.assertEqual(data["username"], "operator_01")
        self.assertEqual(data["scope"], "read submit")
        self.assertIn(USER_CODE, result.err)
        self.assertNotIn(TOKEN, result.out + result.err)

    def test_read_only_scope(self):
        result = self.run_cli("login", "--read-only")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(self.state.requests[0]["body"]), {"scope": "read"})
        self.assertIn("cannot submit", result.out)

    def test_debug_redacts_token_and_device_code(self):
        result = self.run_cli("--debug", "login")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("debug: > POST", result.err)
        self.assertIn('"access_token": "***"', result.err)
        self.assertNotIn(TOKEN, result.out + result.err)
        self.assertNotIn(DEVICE_CODE, result.out + result.err)

    def test_logout(self):
        self.login()
        result = self.run_cli("logout")
        self.assertEqual(result.code, 0)
        self.assertIn("Logged out.", result.out)
        self.assertFalse(os.path.exists(self.token_file))
        again = self.run_cli("logout")
        self.assertEqual(again.code, 0)
        self.assertIn("not logged in", again.out)
        self.assertEqual(self.state.requests, [])


class AuthTests(CliCase):
    def test_no_token_fails_before_any_request(self):
        for command in (["tracks"], ["whoami"], ["status"], ["solves"], ["leaderboard"],
                        ["levels", "ghost"], ["submit", GOOD_FLAG]):
            result = self.run_cli(*command)
            self.assertEqual(result.code, 3, command)
            self.assertEqual(result.out, "")
            self.assertIn('Run "breachlab login"', result.err)
            self.assertEqual(len(result.err.strip().splitlines()), 1)
        self.assertEqual(self.state.requests, [])

    def test_rejected_token_shows_code_and_doc_url(self):
        self.login("blp_" + "x" * 43)
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 3)
        self.assertEqual(result.out, "")
        self.assertIn("Code: unauthorized.", result.err)
        self.assertIn("Docs: https://breachlab.org/docs/api/errors/unauthorized", result.err)
        self.assertIn('Run "breachlab login"', result.err)
        self.assertEqual(len(result.err.strip().splitlines()), 1)
        self.assertNotIn("Traceback", result.err)

    def test_rejected_token_json_error(self):
        self.login("blp_" + "x" * 43)
        result = self.run_cli("--json", "whoami")
        self.assertEqual(result.code, 3)
        error = result.json()["error"]
        self.assertEqual(error["code"], "unauthorized")
        self.assertEqual(error["status"], 401)
        self.assertEqual(error["exit_code"], 3)
        self.assertEqual(error["doc_url"], "https://breachlab.org/docs/api/errors/unauthorized")
        self.assertRegex(error["request_id"], r"^req_[a-z0-9]{24}$")

    def test_env_token_overrides_the_file(self):
        self.login("blp_" + "x" * 43)
        result = self.run_cli("whoami", env=self.env(BREACHLAB_TOKEN=TOKEN))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("operator_01", result.out)
        self.assertIn("from BREACHLAB_TOKEN", result.out)
        self.assertNotIn(TOKEN, result.out + result.err)

    def test_env_token_that_is_not_a_token(self):
        result = self.run_cli("whoami", env=self.env(BREACHLAB_TOKEN="hunter2\r\nX-Evil: 1"))
        self.assertEqual(result.code, 3)
        self.assertNotIn("hunter2", result.err)
        self.assertEqual(self.state.requests, [])

    def test_damaged_token_file(self):
        os.makedirs(os.path.dirname(self.token_file))
        with open(self.token_file, "w") as handle:
            handle.write("not a token\n")
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 3)
        self.assertIn("damaged", result.err)

    def test_loose_permissions_are_tightened(self):
        self.login()
        os.chmod(self.token_file, 0o644)
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)

    def test_default_config_dir_without_xdg(self):
        env = self.env(XDG_CONFIG_HOME=None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(config.token_path(),
                             os.path.join(self.home, ".config", "breachlab", "token"))
            config.save_token(TOKEN)
        result = self.run_cli("whoami", env=env)
        self.assertEqual(result.code, 0, result.err)

    def test_insufficient_scope(self):
        self.login()
        self.state.scopes = ["read"]
        result = self.run_cli("submit", GOOD_FLAG)
        self.assertEqual(result.code, 3)
        self.assertIn("Code: insufficient_scope.", result.err)

    def test_debug_redacts_the_bearer_token(self):
        self.login()
        result = self.run_cli("whoami", "--debug")
        self.assertEqual(result.code, 0)
        self.assertIn("Authorization: Bearer ***", result.err)
        self.assertNotIn(TOKEN, result.out + result.err)


class ReadTests(CliCase):
    def setUp(self):
        CliCase.setUp(self)
        self.login()

    def test_whoami(self):
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 0, result.err)
        self.assertRegex(result.out, r"User\s+operator_01")
        self.assertRegex(result.out, r"Points\s+1200")
        self.assertRegex(result.out, r"Subscription\s+inactive")
        self.assertEqual(self.run_cli("whoami", "--json").json(), self.state.account)

    def test_tracks(self):
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 0, result.err)
        lines = result.out.splitlines()
        self.assertEqual(lines[0].split(), ["TRACK", "NAME", "STATUS", "ACCESS", "LEVELS", "EASY",
                                            "MEDIUM", "HARD", "INSANE"])
        self.assertEqual(lines[1].split(), ["ghost", "Ghost", "live", "free", "5", "2", "1", "1", "1"])
        self.assertEqual(lines[2].split()[:4], ["phantom", "Phantom", "live", "locked"])
        self.assertNotIn("\x1b", result.out)
        data = self.run_cli("tracks", "--json").json()
        self.assertEqual(data["data"], self.state.tracks)

    def test_levels(self):
        result = self.run_cli("levels", "ghost")
        self.assertEqual(result.code, 0, result.err)
        lines = result.out.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[1].split(), ["0", "Ghost", "Level", "0", "easy", "100", "solved", "free"])
        self.assertIn("available", lines[3])
        self.assertEqual(self.state.requests[-1]["query"], {"limit": "100"})

    def test_levels_follow_cursors(self):
        self.state.levels["ghost"] = stub_server.make_levels("ghost", 230, 7)
        result = self.run_cli("levels", "ghost", "--json")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(result.json()["data"]), 230)
        queries = [r["query"] for r in self.state.requests]
        self.assertEqual(len(queries), 3)
        self.assertNotIn("starting_after", queries[0])
        self.assertIn("starting_after", queries[1])
        self.assertNotEqual(queries[1]["starting_after"], queries[2]["starting_after"])

    def test_one_level_briefing(self):
        result = self.run_cli("levels", "ghost", "2")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("ghost level 2: Ghost Level 2", result.out)
        self.assertIn("Briefing for ghost level 2.", result.out)
        locked = self.run_cli("levels", "phantom", "0")
        self.assertIn("briefing is hidden", locked.out)
        missing = self.run_cli("levels", "ghost", "99")
        self.assertEqual(missing.code, 1)

    def test_unknown_track(self):
        result = self.run_cli("levels", "nope")
        self.assertEqual(result.code, 1)
        self.assertIn('no live track named "nope"', result.err)
        self.assertIn("Code: not_found.", result.err)
        self.assertIn("Docs: https://breachlab.org/docs/api/errors/not_found", result.err)

    def test_status(self):
        result = self.run_cli("status")
        self.assertEqual(result.code, 0, result.err)
        lines = result.out.splitlines()
        self.assertIn("operator_01  1200 points  12 solved", lines[0])
        self.assertEqual(lines[2].split(), ["TRACK", "SOLVED", "POINTS", "ACCESS", "NEXT", "LEVEL"])
        self.assertEqual(lines[3].split(), ["ghost", "2/5", "210", "free", "2", "Ghost", "Level", "2"])
        self.assertEqual(lines[4].split(), ["phantom", "0/3", "0", "locked", "not", "open", "to", "your", "account"])

    def test_status_json_and_completed_track(self):
        self.state.levels["ghost"] = stub_server.make_levels("ghost", 5, 5)
        data = self.run_cli("status", "--json").json()
        ghost, phantom = data["tracks"]
        self.assertEqual((ghost["solved"], ghost["levels"], ghost["completed"]), (5, 5, True))
        self.assertIsNone(ghost["next_level"])
        self.assertEqual(phantom["completed"], False)
        self.assertEqual(data["account"]["username"], "operator_01")
        self.assertIn("complete", self.run_cli("status").out)

    def test_leaderboard_default_is_one_page_of_twenty(self):
        result = self.run_cli("leaderboard")
        self.assertEqual(result.code, 0, result.err)
        lines = result.out.splitlines()
        self.assertEqual(len(lines), 21)
        self.assertEqual(lines[1].split(), ["1", "player_000", "9000", "90", "us", "hall", "of", "fame"])
        self.assertEqual(len(self.state.requests), 1)
        self.assertEqual(self.state.requests[0]["query"], {"limit": "20"})
        self.assertIn("More entries exist", result.err)

    def test_leaderboard_limit_spans_pages(self):
        result = self.run_cli("leaderboard", "--limit", "150")
        self.assertEqual(len(result.out.splitlines()), 151)
        queries = [r["query"] for r in self.state.requests]
        self.assertEqual([q["limit"] for q in queries], ["100", "50"])
        self.assertNotIn("starting_after", queries[0])
        self.assertIn("starting_after", queries[1])

    def test_leaderboard_all_walks_every_page(self):
        result = self.run_cli("leaderboard", "--all", "--json")
        data = result.json()
        self.assertEqual(len(data["data"]), 250)
        self.assertEqual(data["has_more"], False)
        self.assertEqual([e["rank"] for e in data["data"]], list(range(1, 251)))
        self.assertEqual(len(self.state.requests), 3)

    def test_leaderboard_filters(self):
        result = self.run_cli("leaderboard", "--country", "US", "--limit", "3")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.state.requests[-1]["query"], {"country": "us", "limit": "3"})
        agents = self.run_cli("leaderboard", "--board", "agents")
        self.assertEqual(agents.code, 0, agents.err)
        self.assertIn("Agent 004", agents.out)
        self.assertIn("undisclosed", agents.out)
        before = len(self.state.requests)
        bad = self.run_cli("leaderboard", "--board", "agents", "--period", "weekly")
        self.assertEqual(bad.code, 2)
        self.assertEqual(self.run_cli("leaderboard", "--country", "usa").code, 2)
        self.assertEqual(len(self.state.requests), before)

    def test_solves(self):
        result = self.run_cli("solves")
        self.assertEqual(result.code, 0, result.err)
        lines = result.out.splitlines()
        self.assertEqual(len(lines), 21)
        self.assertEqual(lines[1].split(), ["2026-11-01", "09:30", "ghost", "44", "Ghost", "Level",
                                            "44", "100"])
        everything = self.run_cli("solves", "--all", "--json").json()
        self.assertEqual(len(everything["data"]), 45)

    def test_control_characters_from_the_api_are_neutralised(self):
        self.state.tracks[0]["name"] = "Gh\x1b[31most\x07"
        result = self.run_cli("tracks")
        self.assertNotIn("\x1b", result.out)
        self.assertNotIn("\x07", result.out)
        self.assertIn("Gh?[31most?", result.out)


class SubmitTests(CliCase):
    def setUp(self):
        CliCase.setUp(self)
        self.login()

    def submitted(self):
        return [json.loads(r["body"]) for r in self.state.requests if r["path"] == "/api/v2/submit"]

    def test_correct(self):
        result = self.run_cli("submit", GOOD_FLAG)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.out.strip(),
                         "Correct. ghost level 2 solved, 120 points awarded. First blood.")
        self.assertEqual(self.submitted(), [{"flag": GOOD_FLAG}])

    def test_test_flag(self):
        result = self.run_cli("submit", TEST_FLAG)
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("test flag", result.out)

    def test_wrong_flag(self):
        result = self.run_cli("submit", "BL{nope}")
        self.assertEqual(result.code, 1)
        self.assertTrue(result.out.startswith("Incorrect."))
        self.assertEqual(result.err, "")

    def test_json_verdicts(self):
        good = self.run_cli("submit", GOOD_FLAG, "--json")
        self.assertEqual(good.code, 0)
        self.assertEqual(good.json()["points_awarded"], 120)
        bad = self.run_cli("--json", "submit", "BL{nope}")
        self.assertEqual(bad.code, 1)
        self.assertEqual(bad.json(), {"result": "incorrect"})

    def test_flag_from_stdin_dash(self):
        result = self.run_cli("submit", "-", stdin=GOOD_FLAG + "\n")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.submitted(), [{"flag": GOOD_FLAG}])

    def test_flag_from_pipe_without_dash(self):
        result = self.run_cli("submit", stdin="  " + GOOD_FLAG + "\r\n")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.submitted(), [{"flag": GOOD_FLAG}])

    def test_flag_prompt_on_a_terminal(self):
        result = self.run_cli("submit", stdin=FakeTTY(GOOD_FLAG + "\n"))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("Flag: ", result.err)
        self.assertEqual(self.submitted(), [{"flag": GOOD_FLAG}])

    def test_bad_input_is_a_usage_error_and_sends_nothing(self):
        for argv, stdin in ((["submit", "abc"], ""), (["submit", "x" * 129], ""),
                            (["submit", "-"], ""), (["submit", "-"], "one\ntwo\n")):
            result = self.run_cli(*argv, stdin=stdin)
            self.assertEqual(result.code, 2, argv)
        self.assertEqual(self.submitted(), [])

    def test_already_solved(self):
        result = self.run_cli("submit", SOLVED_FLAG)
        self.assertEqual(result.code, 1)
        self.assertIn("already solved", result.err)
        self.assertIn("Code: already_solved.", result.err)

    def test_email_unverified(self):
        self.state.email_verified = False
        result = self.run_cli("submit", GOOD_FLAG)
        self.assertEqual(result.code, 1)
        self.assertIn("Code: email_unverified.", result.err)

    def test_cooldown_is_reported_and_not_retried(self):
        self.state.submit_cooldown = 30
        result = self.run_cli("submit", "BL{nope}")
        self.assertEqual(result.code, 4)
        self.assertRegex(result.err, r"Retry in 30 seconds, at \d\d:\d\d:\d\d\.")
        self.assertIn("Code: cooldown.", result.err)
        self.assertEqual(result.sleeps, [])
        self.assertEqual(len(self.submitted()), 1)
        data = self.run_cli("submit", "BL{nope}", "--json").json()
        self.assertEqual(data["error"]["retry_after_seconds"], 30)

    def test_cooldown_without_a_known_wait(self):
        self.state.submit_cooldown = 0
        result = self.run_cli("submit", "BL{nope}")
        self.assertEqual(result.code, 4)
        self.assertIn("did not say how long", result.err)

    def test_wait_flag_retries_after_retry_after(self):
        self.state.rate_limit_next = 1
        result = self.run_cli("submit", GOOD_FLAG, "--wait")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.sleeps, [7])
        self.assertEqual(len(self.submitted()), 2)


class RateLimitTests(CliCase):
    def setUp(self):
        CliCase.setUp(self)
        self.login()

    def test_read_waits_for_retry_after_then_succeeds(self):
        self.state.rate_limit_next = 1
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.sleeps, [7])
        self.assertIn("Rate limited. Retrying in 7 seconds.", result.err)
        self.assertIn("ghost", result.out)

    def test_no_wait_fails_with_the_retry_time(self):
        self.state.rate_limit_next = 1
        result = self.run_cli("tracks", "--no-wait")
        self.assertEqual(result.code, 4)
        self.assertEqual(result.sleeps, [])
        self.assertRegex(result.err, r"Rate limited\. Retry in 7 seconds, at \d\d:\d\d:\d\d\.")
        self.assertIn("Code: rate_limited.", result.err)
        self.assertIn("Docs: https://breachlab.org/docs/api/errors/rate_limited", result.err)

    def test_long_wait_is_not_slept_through(self):
        self.state.rate_limit_next = 1
        self.state.retry_after = 900
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 4)
        self.assertEqual(result.sleeps, [])
        self.assertIn("Retry in 900 seconds", result.err)

    def test_gives_up_after_two_waits(self):
        self.state.rate_limit_next = 10
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 4)
        self.assertEqual(result.sleeps, [7, 7])
        self.assertEqual(len(self.state.requests), 3)

    def test_wait_is_read_from_the_body_when_the_header_is_missing(self):
        self.state.rate_limit_next = 1
        self.state.send_retry_after = False
        self.state.retry_in_body = True
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.sleeps, [7])

    def test_unknown_wait_is_never_guessed(self):
        self.state.rate_limit_next = 1
        self.state.send_retry_after = False
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 4)
        self.assertEqual(result.sleeps, [])
        self.assertIn("did not say how long", result.err)


class RobustnessTests(CliCase):
    def test_http_base_url_is_refused_unless_local(self):
        self.login()
        result = self.run_cli("tracks", env=self.env(BREACHLAB_API="http://example.com"))
        self.assertEqual(result.code, 2)
        self.assertIn("not https", result.err)
        self.assertEqual(self.state.requests, [])

    def test_base_url_rules(self):
        self.assertEqual(validate_base_url("https://breachlab.org/"), "https://breachlab.org")
        self.assertEqual(validate_base_url("http://localhost:3000"), "http://localhost:3000")
        self.assertEqual(validate_base_url("http://127.0.0.1:3000"), "http://127.0.0.1:3000")
        self.assertEqual(validate_base_url("http://[::1]:3000"), "http://[::1]:3000")
        for bad in ("http://breachlab.org", "http://localhost.evil.com", "ftp://breachlab.org",
                    "breachlab.org", "", "https://user:pw@breachlab.org",
                    "https://breachlab.org/?token=1", "http://127.0.0.1.evil.com"):
            with self.assertRaises(ConfigError, msg=bad):
                validate_base_url(bad)

    def test_unreachable_api(self):
        self.login()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = self.run_cli("tracks", env=self.env(BREACHLAB_API="http://127.0.0.1:%d" % port))
        self.assertEqual(result.code, 5)
        self.assertIn("Could not reach the API", result.err)
        self.assertNotIn("Traceback", result.err)
        self.assertEqual(len(result.err.strip().splitlines()), 1)

    def test_timeout(self):
        self.login()
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        try:
            url = "http://127.0.0.1:%d" % silent.getsockname()[1]
            result = self.run_cli("--timeout", "0.3", "tracks", env=self.env(BREACHLAB_API=url))
        finally:
            silent.close()
        self.assertEqual(result.code, 5)
        self.assertIn("did not answer within 0.3 seconds", result.err)

    def test_redirects_are_not_followed(self):
        self.login()
        self.state.redirect_next = self.server.url + "/api/v2/tracks?hop=1"
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 1)
        self.assertIn("redirect", result.err)
        self.assertEqual(len(self.state.requests), 1)

    def test_edge_block_without_a_problem_document(self):
        self.login()
        self.state.edge_block = True
        result = self.run_cli("tracks")
        self.assertEqual(result.code, 1)
        self.assertIn("blocked before it reached the API", result.err)
        self.assertIn("Code: http_403.", result.err)

    def test_unexpected_error_has_no_stack_trace(self):
        self.login()
        with mock.patch("breachlab.cli.table", side_effect=RuntimeError("boom")):
            result = self.run_cli("tracks")
            loud = self.run_cli("tracks", "--debug")
        self.assertEqual(result.code, 1)
        self.assertNotIn("Traceback", result.err)
        self.assertIn("Run again with --debug", result.err)
        self.assertIn("Traceback", loud.err)

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.run_cli("frobnicate").code, 2)
        self.assertEqual(self.run_cli("levels").code, 2)
        self.assertEqual(self.run_cli("leaderboard", "--limit", "0").code, 2)

    def test_no_command_prints_help(self):
        result = self.run_cli()
        self.assertEqual(result.code, 0)
        self.assertIn("breachlab login", result.out)


class ColourTests(CliCase):
    def test_colour_only_on_a_terminal_without_no_color(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(use_color(io.StringIO()))
            self.assertTrue(use_color(FakeTTY()))
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            self.assertFalse(use_color(FakeTTY()))
        self.assertEqual(Style(False).paint("x", "red"), "x")
        self.assertEqual(Style(True).paint("x", "red"), "\x1b[31mx\x1b[0m")
        self.assertEqual(clean("a\x1b[2Jb\u202e"), "a?[2Jb?")

    def test_end_to_end(self):
        self.login()
        coloured = self.run_cli("levels", "ghost", out=FakeTTY())
        self.assertIn("\x1b[32m", coloured.out)
        plain = self.run_cli("levels", "ghost", out=FakeTTY(), env=self.env(NO_COLOR="1"))
        self.assertNotIn("\x1b", plain.out)
        as_json = self.run_cli("levels", "ghost", "--json", out=FakeTTY())
        self.assertNotIn("\x1b", as_json.out)


class EntryPointTests(CliCase):
    def run_module(self, *argv):
        env = self.env(PYTHONPATH=SRC)
        return subprocess.run([sys.executable, "-m", "breachlab"] + list(argv), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=60)

    def test_version(self):
        done = self.run_module("--version")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "breachlab " + __version__)

    def test_not_logged_in(self):
        done = self.run_module("tracks")
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout, "")
        self.assertIn('Run "breachlab login"', done.stderr)

    def test_piped_flag(self):
        self.login()
        env = self.env(PYTHONPATH=SRC)
        done = subprocess.run([sys.executable, "-m", "breachlab", "submit", "-"], env=env,
                              input=GOOD_FLAG + "\n", stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Correct.", done.stdout)


if __name__ == "__main__":
    unittest.main()
