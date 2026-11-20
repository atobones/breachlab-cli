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

    def calls(self):
        return [(r["method"], r["path"]) for r in self.state.requests]

    def old_server(self, missing_status=404):
        """Make the stub the API as it was before the newer endpoints."""
        self.state.new_endpoints = False
        self.state.missing_status = missing_status


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

    def test_scope_option_passes_the_scopes_through(self):
        self.state.allowed_scopes = ["read", "submit", "play"]
        result = self.run_cli("login", "--scope", "read,submit", "play")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(self.state.requests[0]["body"]), {"scope": "read submit play"})
        self.assertIn("Granted scopes: read, submit, play.", result.out)
        self.assertNotIn("cannot submit", result.out)

    def test_scope_option_is_not_checked_against_a_list(self):
        self.state.allowed_scopes = ["read", "labs:beta_2"]
        result = self.run_cli("login", "--json", "--scope", "labs:beta_2 read read")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(json.loads(self.state.requests[0]["body"]), {"scope": "labs:beta_2 read"})
        self.assertEqual(result.json()["scope"], "read labs:beta_2")

    def test_scope_without_submit_says_so(self):
        self.state.allowed_scopes = ["read", "submit", "play"]
        result = self.run_cli("login", "--scope", "read,play")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("Granted scopes: read, play.", result.out)
        self.assertIn("cannot submit", result.out)

    def test_scope_the_server_does_not_grant(self):
        result = self.run_cli("login", "--scope", "read,play")
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertIn("The server refused the requested scope. Allowed scopes: read, submit.",
                      result.err)
        self.assertIn("Code: invalid_scope.", result.err)
        self.assertIn("Docs: https://breachlab.org/docs/api/errors/invalid_scope", result.err)
        self.assertEqual(len(result.err.strip().splitlines()), 1)
        self.assertFalse(os.path.exists(self.token_file))
        self.assertEqual(self.paths(), ["/api/v1/auth/device"])

    def test_scope_refused_by_an_old_server_has_no_doc_link(self):
        self.old_server()
        result = self.run_cli("login", "--json", "--scope", "play")
        self.assertEqual(result.code, 1)
        self.assertIn("Code: invalid_scope.", result.err)
        self.assertNotIn("Docs:", result.err)
        self.assertNotIn("doc_url", result.json()["error"])

    def test_scope_usage_errors_send_nothing(self):
        for argv in (["login", "--scope", "read;rm"], ["login", "--scope", "re\x1bad"],
                     ["login", "--scope", ","], ["login", "--scope", "x" * 33],
                     ["login", "--scope"], ["login", "--read-only", "--scope", "read"]):
            result = self.run_cli(*argv)
            self.assertEqual(result.code, 2, argv)
            self.assertNotIn("\x1b", result.err)
        self.assertEqual(self.state.requests, [])

    def test_denied_shows_the_servers_reason_and_doc_link(self):
        self.state.poll_script = ["access_denied"]
        result = self.run_cli("login")
        self.assertEqual(result.code, 3)
        self.assertIn("maximum number of active tokens. Nothing was saved.", result.err)
        self.assertIn("Docs: https://breachlab.org/docs/api/errors/access_denied", result.err)
        self.assertEqual(len(result.err.strip().splitlines()), 1)
        as_json = self.run_cli("login", "--json")
        self.assertEqual(as_json.json()["error"]["doc_url"],
                         "https://breachlab.org/docs/api/errors/expired_token")

    def test_poll_errors_from_an_old_server(self):
        self.old_server()
        self.state.poll_script = ["access_denied"]
        denied = self.run_cli("login")
        self.assertEqual(denied.code, 3)
        self.assertIn("denied in the browser. Nothing was saved.", denied.err)
        self.assertNotIn("Docs:", denied.err)
        self.state.poll_script = ["expired_token"]
        expired = self.run_cli("login")
        self.assertEqual(expired.code, 3)
        self.assertNotIn("Docs:", expired.err)

    def test_expired_code_shows_the_doc_link(self):
        self.state.poll_script = ["expired_token"]
        result = self.run_cli("login")
        self.assertEqual(result.code, 3)
        self.assertIn("Docs: https://breachlab.org/docs/api/errors/expired_token", result.err)

    def test_unknown_poll_error_is_reported_with_its_description(self):
        self.state.poll_script = ["server_busy"]
        result = self.run_cli("login", "--json")
        self.assertEqual(result.code, 1)
        self.assertIn("The server refused the login. The request failed.", result.err)
        self.assertEqual(result.json()["error"]["code"], "server_busy")
        self.assertFalse(os.path.exists(self.token_file))

    def test_doc_link_that_is_not_https_is_not_printed(self):
        self.state.device_doc_base = "http://evil.example/\x1b[31m"
        self.state.poll_script = ["access_denied"]
        denied = self.run_cli("login")
        self.assertEqual(denied.code, 3)
        self.assertNotIn("Docs:", denied.err)
        self.assertNotIn("evil.example", denied.err)
        refused = self.run_cli("login", "--scope", "nope")
        self.assertEqual(refused.code, 1)
        self.assertNotIn("evil.example", refused.err)
        self.assertNotIn("\x1b", denied.err + refused.err)


class LogoutTests(CliCase):
    BY_HAND = "Revoke it by hand at https://breachlab.org/dashboard/account"

    def test_revokes_on_the_server_then_removes_the_file(self):
        self.login()
        result = self.run_cli("logout")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("Logged out. The token was revoked on the server and removed from", result.out)
        self.assertEqual(result.err, "")
        self.assertFalse(os.path.exists(self.token_file))
        self.assertEqual(self.calls(), [("DELETE", "/api/v2/token")])
        request = self.state.requests[0]
        self.assertEqual(request["headers"]["authorization"], "Bearer " + TOKEN)
        self.assertEqual(request["body"], b"")
        self.assertEqual(request["query"], {})
        self.assertNotIn(TOKEN, result.out + result.err)
        # The token is dead on the server, not only forgotten here.
        after = self.run_cli("whoami", env=self.env(BREACHLAB_TOKEN=TOKEN))
        self.assertEqual(after.code, 3)

    def test_second_logout_sends_nothing(self):
        self.login()
        self.run_cli("logout")
        again = self.run_cli("logout")
        self.assertEqual(again.code, 0)
        self.assertIn("not logged in", again.out)
        self.assertEqual(len(self.state.requests), 1)

    def test_old_server_still_removes_the_file_and_says_how_to_revoke(self):
        for missing_status in (404, 405):
            self.login()
            self.old_server(missing_status)
            result = self.run_cli("logout")
            self.assertEqual(result.code, 0, result.err)
            self.assertIn("Logged out. The token was removed from", result.out)
            self.assertNotIn("revoked", result.out)
            self.assertIn("This server cannot revoke tokens yet, so the token may still be valid. "
                          + self.BY_HAND, result.err)
            self.assertFalse(os.path.exists(self.token_file))
        self.assertEqual(self.calls(), [("DELETE", "/api/v2/token")] * 2)
        self.assertFalse(self.state.revoked)

    def test_unreachable_server_still_removes_the_file(self):
        self.login()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = self.run_cli("logout", env=self.env(BREACHLAB_API="http://127.0.0.1:%d" % port))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("Logged out. The token was removed from", result.out)
        self.assertIn("The server could not be reached, so the token may still be valid. "
                      + self.BY_HAND, result.err)
        self.assertNotIn("Traceback", result.err)
        self.assertFalse(os.path.exists(self.token_file))

    def test_token_the_server_no_longer_knows_counts_as_revoked(self):
        self.login("blp_" + "x" * 43)
        result = self.run_cli("logout")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("had already revoked the token or let it expire", result.out)
        self.assertEqual(result.err, "")
        self.assertFalse(os.path.exists(self.token_file))

    def test_server_error_is_not_waited_out_and_the_file_still_goes(self):
        self.login()
        self.state.rate_limit_next = 5
        result = self.run_cli("logout")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(result.sleeps, [])
        self.assertEqual(len(self.state.requests), 1)
        self.assertIn("The server did not revoke the token, so the token may still be valid. "
                      + self.BY_HAND, result.err)
        self.assertFalse(os.path.exists(self.token_file))

    def test_redirect_is_not_followed(self):
        self.login()
        self.state.redirect_next = self.server.url + "/api/v2/token?hop=1"
        result = self.run_cli("logout")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(self.state.requests), 1)
        self.assertIn("may still be valid", result.err)
        self.assertFalse(self.state.revoked)
        self.assertFalse(os.path.exists(self.token_file))

    def test_refused_base_url_sends_nothing_and_the_file_still_goes(self):
        self.login()
        result = self.run_cli("logout", env=self.env(BREACHLAB_API="http://example.com"))
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("may still be valid", result.err)
        self.assertEqual(self.state.requests, [])
        self.assertFalse(os.path.exists(self.token_file))

    def test_saved_token_is_revoked_and_the_environment_token_is_left_alone(self):
        self.login()
        other = "blp_" + "y" * 43
        result = self.run_cli("logout", env=self.env(BREACHLAB_TOKEN=other))
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.state.requests[0]["headers"]["authorization"], "Bearer " + TOKEN)
        self.assertEqual(len(self.state.requests), 1)
        self.assertIn("BREACHLAB_TOKEN is still set", result.err)
        self.assertNotIn(other, result.out + result.err)

    def test_environment_token_alone_is_never_revoked(self):
        result = self.run_cli("logout", env=self.env(BREACHLAB_TOKEN=TOKEN))
        self.assertEqual(result.code, 0)
        self.assertIn("not logged in", result.out)
        self.assertIn("Logout does not revoke it", result.err)
        self.assertEqual(self.state.requests, [])
        self.assertFalse(self.state.revoked)

    def test_damaged_token_file_is_removed_without_a_request(self):
        os.makedirs(os.path.dirname(self.token_file))
        with open(self.token_file, "w") as handle:
            handle.write("not a token\n")
        result = self.run_cli("logout")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("damaged token file", result.out)
        self.assertIn("nothing was revoked", result.err)
        self.assertNotIn("not a token", result.out + result.err)
        self.assertEqual(self.state.requests, [])
        self.assertFalse(os.path.exists(self.token_file))

    def test_json(self):
        self.login()
        data = self.run_cli("logout", "--json").json()
        self.assertEqual(data, {"logged_out": True, "revoked": True, "revocation": "revoked",
                                "token_path": self.token_file})
        self.assertEqual(self.run_cli("--json", "logout").json(),
                         {"logged_out": False, "revoked": False, "token_path": self.token_file})

    def test_json_on_an_old_server(self):
        self.login()
        self.old_server()
        result = self.run_cli("logout", "--json")
        self.assertEqual(result.json(), {
            "logged_out": True, "revoked": False, "revocation": "unsupported",
            "token_path": self.token_file,
            "revoke_url": "https://breachlab.org/dashboard/account"})
        self.assertIn("may still be valid", result.err)

    def test_debug_redacts_the_token(self):
        self.login()
        result = self.run_cli("--debug", "logout")
        self.assertEqual(result.code, 0)
        self.assertIn("debug: > DELETE", result.err)
        self.assertIn("Authorization: Bearer ***", result.err)
        self.assertNotIn(TOKEN, result.out + result.err)


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
        self.assertRegex(result.out, r"Rank\s+42 on the humans board")
        self.assertRegex(result.out, r"Scopes\s+read, submit")
        self.assertRegex(result.out, r"Expires\s+never")
        self.assertNotIn("cannot submit", result.out)
        self.assertEqual(self.calls(), [("GET", "/api/v2/me"), ("GET", "/api/v2/token")])

    def test_whoami_json_carries_the_token_object(self):
        data = self.run_cli("whoami", "--json").json()
        token = data.pop("token")
        self.assertEqual(data, self.state.account)
        self.assertEqual(token, {"object": "token", "scopes": ["read", "submit"],
                                 "created_at": "2026-11-19T20:40:12.000Z", "expires_at": None,
                                 "is_agent": False})

    def test_whoami_says_when_the_token_cannot_submit(self):
        self.state.scopes = ["read", "play"]
        self.state.token_expires_at = "2027-02-17T20:40:12.000Z"
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 0, result.err)
        self.assertRegex(result.out, r"Scopes\s+read, play")
        self.assertRegex(result.out, r"Expires\s+2027-02-17 20:40 UTC")
        self.assertEqual(result.out.splitlines()[-1],
                         'This token cannot submit flags. Run "breachlab login" to get one that can.')

    def test_whoami_unranked_and_agent_board(self):
        self.state.account["leaderboard"] = None
        self.assertNotIn("Rank", self.run_cli("whoami").out)
        self.state.account["leaderboard"] = {"board": "agents", "rank": 3, "points": 900}
        self.assertRegex(self.run_cli("whoami").out, r"Rank\s+3 on the agents board")
        self.assertIn("rank 3 on the agents board", self.run_cli("status").out.splitlines()[0])
        for odd in ({"board": "humans", "rank": "1\x1b[2J"}, {"board": "humans", "rank": True},
                    {"board": "humans", "rank": 0}, "first", ["humans", 1]):
            self.state.account["leaderboard"] = odd
            result = self.run_cli("whoami")
            self.assertEqual(result.code, 0, result.err)
            self.assertNotIn("Rank", result.out)
        self.state.account["leaderboard"] = {"board": "te\x1bams", "rank": 9}
        self.assertRegex(self.run_cli("whoami").out, r"Rank\s+9\n")

    def test_whoami_on_an_old_server(self):
        for missing_status in (404, 405):
            self.old_server(missing_status)
            result = self.run_cli("whoami")
            self.assertEqual(result.code, 0, result.err)
            self.assertEqual(result.err, "")
            self.assertRegex(result.out, r"User\s+operator_01")
            for absent in ("Scopes", "Expires", "Rank", "cannot submit"):
                self.assertNotIn(absent, result.out)
            data = self.run_cli("whoami", "--json").json()
            self.assertIsNone(data["token"])
            self.assertNotIn("leaderboard", data)

    def test_whoami_token_scopes_are_neutralised(self):
        self.state.scopes = ["read", "sub\x1b[31mmit"]
        result = self.run_cli("whoami")
        self.assertNotIn("\x1b", result.out)
        self.assertIn("read, sub?[31mmit", result.out)

    def test_whoami_does_not_hide_other_token_errors(self):
        self.state.token_failure = 500
        result = self.run_cli("whoami")
        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertIn("Code: internal_error.", result.err)

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
        # One request, to the level itself. The list is not paged.
        self.assertEqual(self.calls(), [("GET", "/api/v2/tracks/ghost/levels/2")])
        self.assertEqual(self.state.requests[0]["query"], {})
        locked = self.run_cli("levels", "phantom", "0")
        self.assertIn("briefing is hidden", locked.out)
        self.assertEqual(self.run_cli("levels", "ghost", "2", "--json").json(),
                         self.state.levels["ghost"][2])

    def test_one_level_that_does_not_exist(self):
        missing = self.run_cli("levels", "ghost", "99")
        self.assertEqual(missing.code, 1)
        self.assertIn('Track "ghost" has no level 99.', missing.err)
        self.assertEqual(self.paths(), ["/api/v2/tracks/ghost/levels/99",
                                        "/api/v2/tracks/ghost/levels"])
        unknown = self.run_cli("levels", "nope", "0")
        self.assertEqual(unknown.code, 1)
        self.assertIn('no live track named "nope"', unknown.err)
        self.assertIn("Code: not_found.", unknown.err)

    def test_one_level_on_an_old_server_pages_the_list(self):
        new = self.run_cli("levels", "ghost", "2")
        as_json = self.run_cli("levels", "ghost", "2", "--json").json()
        for missing_status in (404, 405):
            self.state.requests[:] = []
            self.old_server(missing_status)
            old = self.run_cli("levels", "ghost", "2")
            self.assertEqual(old.code, 0, old.err)
            self.assertEqual(old.out, new.out)
            self.assertEqual(old.err, "")
            self.assertEqual(self.paths(), ["/api/v2/tracks/ghost/levels/2",
                                            "/api/v2/tracks/ghost/levels"])
            self.assertEqual(self.run_cli("levels", "ghost", "2", "--json").json(), as_json)
            self.assertIn("briefing is hidden", self.run_cli("levels", "phantom", "0").out)
            missing = self.run_cli("levels", "ghost", "99")
            self.assertEqual(missing.code, 1)
            self.assertIn('Track "ghost" has no level 99.', missing.err)
            self.assertIn('no live track named "nope"', self.run_cli("levels", "nope", "0").err)

    def test_one_level_deep_in_a_long_track_on_an_old_server(self):
        self.state.levels["ghost"] = stub_server.make_levels("ghost", 230, 7)
        self.old_server()
        result = self.run_cli("levels", "ghost", "215")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("ghost level 215: Ghost Level 215", result.out)
        self.assertEqual(len(self.state.requests), 4)

    def test_one_level_other_errors_do_not_fall_back(self):
        self.state.rate_limit_next = 1
        result = self.run_cli("levels", "ghost", "2", "--no-wait")
        self.assertEqual(result.code, 4)
        self.assertEqual(len(self.state.requests), 1)

    def test_one_level_answer_for_another_level_is_refused(self):
        ok = stub_server.Handler._ok
        with mock.patch.object(stub_server.Handler, "_ok",
                               lambda handler, body: ok(handler, dict(body, idx=3))):
            result = self.run_cli("levels", "ghost", "2")
        self.assertEqual(result.code, 1)
        self.assertIn("unexpected shape", result.err)

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
        self.assertEqual(lines[0], "operator_01  1200 points  12 solved  rank 42 on the humans board")
        self.assertEqual(lines[2].split(), ["TRACK", "NAME", "SOLVED", "ACCESS", "NEXT", "LEVEL"])
        self.assertEqual(lines[3].split(), ["ghost", "Ghost", "2/5", "free", "2"])
        self.assertEqual(lines[4].split(), ["phantom", "Phantom", "0/3", "locked", "not", "open",
                                            "to", "your", "account"])
        # The account and one call for every track's progress, whatever their number.
        self.assertEqual(self.calls(), [("GET", "/api/v2/me"), ("GET", "/api/v2/me/progress")])

    def test_status_on_an_old_server_uses_tracks_and_levels(self):
        new = self.run_cli("status")
        new_json = self.run_cli("status", "--json").json()
        for missing_status in (404, 405):
            self.state.requests[:] = []
            self.old_server(missing_status)
            old = self.run_cli("status")
            self.assertEqual(old.code, 0, old.err)
            self.assertEqual(old.err, "")
            self.assertEqual(self.paths(), [
                "/api/v2/me", "/api/v2/me/progress", "/api/v2/tracks",
                "/api/v2/tracks/ghost/levels", "/api/v2/tracks/phantom/levels"])
            lines = old.out.splitlines()
            self.assertEqual(lines[0], "operator_01  1200 points  12 solved")
            # The table is the same one a current server produces.
            self.assertEqual(lines[1:], new.out.splitlines()[1:])
            old_json = self.run_cli("status", "--json").json()
            self.assertEqual(old_json["tracks"], new_json["tracks"])
            self.assertNotIn("leaderboard", old_json["account"])

    def test_status_json_and_completed_track(self):
        self.state.levels["ghost"] = stub_server.make_levels("ghost", 5, 5)
        data = self.run_cli("status", "--json").json()
        self.assertEqual(data["object"], "status")
        ghost, phantom = data["tracks"]
        self.assertEqual(ghost, {"object": "track_progress", "track": "ghost", "name": "Ghost",
                                 "access": "free", "level_count": 5, "solved": 5,
                                 "next_level_idx": None})
        self.assertEqual((phantom["solved"], phantom["level_count"]), (0, 3))
        self.assertEqual(data["account"]["username"], "operator_01")
        self.assertIn("complete", self.run_cli("status").out)
        self.old_server()
        self.assertEqual(self.run_cli("status", "--json").json()["tracks"], data["tracks"])
        self.assertIn("complete", self.run_cli("status").out)

    def test_status_next_level_wording(self):
        self.state.levels["phantom"][0]["status"] = "available"
        self.state.levels["ghost"] = []
        self.state.tracks.append({"object": "track", "slug": "specter", "name": "Spec\x1bter",
                                  "status": "live", "level_count": 2, "access": "paid",
                                  "difficulty_mix": {}})
        self.state.levels["specter"] = stub_server.make_levels("specter", 2, 0)
        for level in self.state.levels["specter"]:
            level["status"] = "locked"
        for old in (False, True):
            if old:
                self.old_server()
            result = self.run_cli("status")
            self.assertEqual(result.code, 0, result.err)
            lines = result.out.splitlines()
            self.assertTrue(lines[3].endswith("0/0  free    no levels yet"), lines[3])
            self.assertTrue(lines[4].endswith("0/3  locked  0, not open to your account"), lines[4])
            self.assertTrue(lines[5].endswith("0/2  paid    locked"), lines[5])
            self.assertIn("Spec?ter", lines[5])
            self.assertNotIn("\x1b", result.out)

    def test_status_other_errors_do_not_fall_back(self):
        self.state.rate_limit_next = 2
        result = self.run_cli("status", "--no-wait")
        self.assertEqual(result.code, 4)
        self.assertEqual(self.paths(), ["/api/v2/me"])

    def test_status_survives_odd_progress_values(self):
        self.state.tracks[0]["name"] = None
        self.state.levels["ghost"][0]["status"] = None
        with mock.patch.object(stub_server.Handler, "_progress", lambda handler: [
                {"object": "track_progress", "track": "ghost", "name": None, "access": None,
                 "level_count": "5", "solved": -2, "next_level_idx": True}, "junk"]):
            result = self.run_cli("status")
        self.assertEqual(result.code, 0, result.err)
        self.assertIn("ghost", result.out)

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
