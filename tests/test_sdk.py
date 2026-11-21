"""Tests of the Python SDK: `from breachlab import BreachLab`.

Most tests talk to a scripted server on 127.0.0.1 that answers with whatever
the test queued and records what the client sent. The pagination and login
tests use the fuller stub of the API from stub_server.py.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
sys.path.insert(0, HERE)

import breachlab  # noqa: E402
from breachlab import (  # noqa: E402
    AlreadySolvedError,
    ApiError,
    AuthenticationError,
    BreachLab,
    BreachLabError,
    ConfigError,
    ConflictError,
    DeviceFlowError,
    ForbiddenError,
    NetworkError,
    NotFoundError,
    NotLoggedIn,
    Page,
    Paginator,
    PaywallError,
    RateLimitError,
    RedirectError,
    Run,
    ScopeError,
    ServerError,
    TokenError,
    UnexpectedResponse,
    ValidationError,
    config,
)
from breachlab import client as client_module  # noqa: E402
import stub_server  # noqa: E402

TOKEN = "blp_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe9Dc8Ba7"
RUN_ID = "0b6f3c1e-4f4e-4c55-9a0e-6f6f2f9a1c11"
DOCS = "https://breachlab.org/docs/api/errors/"
TITLES = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
          409: "Conflict", 413: "Payload Too Large", 422: "Unprocessable Entity",
          429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"}


def problem(status, code, headers=None, **extra):
    """A queued answer in the problem+json shape of API v2."""
    body = {"type": DOCS + code, "title": TITLES.get(status, "Error"), "status": status,
            "code": code, "detail": "Detail of %s." % code, "doc_url": DOCS + code,
            "request_id": "req_4f9k2m7q1x8c3v6b0n5t2w8z"}
    body.update(extra)
    return status, body, dict(headers or {}, **{"Content-Type": "application/problem+json"})


def run_data(**changes):
    run = {"object": "run", "id": RUN_ID, "track": "ghost", "model": None, "harness": None,
           "harness_version": None, "state": "open", "reported_status": None,
           "created_at": "2026-11-09T12:00:00.000Z", "finished_at": None, "step_count": 0,
           "steps_truncated": False, "last_seq": None, "tokens_used": None, "cost_usd": None,
           "result": {"solved_levels": [], "points": 0}}
    run.update(changes)
    return run


def batch_result(count):
    return {"object": "step_batch", "accepted": count, "duplicates": 0, "dropped": 0,
            "truncated": 0, "step_count": count, "steps_truncated": False}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _handle(self):
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        script = self.server.script
        script.requests.append({
            "method": self.command, "path": parts.path, "query": dict(parse_qsl(parts.query)),
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": json.loads(raw.decode("utf-8")) if raw else None,
        })
        status, body, headers = script.replies.pop(0) if script.replies else (
            599, {"error": "nothing queued"}, {})
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        if "Content-Type" not in headers:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = _handle


class Script:
    """A server that answers with what was queued, in order."""

    def __init__(self):
        self.replies = []
        self.requests = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.script = self
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self._httpd.server_address[1]

    def queue(self, body, status=200, headers=None):
        self.replies.append((status, body, headers or {}))

    def queue_raw(self, reply):
        self.replies.append(reply)

    @property
    def last(self):
        return self.requests[-1]

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class SdkCase(unittest.TestCase):
    def setUp(self):
        self.script = Script()
        self.addCleanup(self.script.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.xdg = os.path.join(self.tmp.name, "xdg")
        env = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "XDG_CONFIG_HOME": self.xdg},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.sleeps = []
        naps = mock.patch("breachlab.client.sleep", self.sleeps.append)
        naps.start()
        self.addCleanup(naps.stop)
        self.client = BreachLab(TOKEN, self.script.url)

    def tearDown(self):
        for request in self.script.requests:
            self.assertTrue(request["headers"].get("user-agent", "").startswith("breachlab-"))
            self.assertNotIn("blp_", json.dumps(request["query"]))


# -- construction ----------------------------------------------------------------


class ConstructionTests(SdkCase):
    def test_public_names(self):
        for name in breachlab.__all__:
            self.assertTrue(hasattr(breachlab, name), name)
        self.assertEqual(breachlab.__version__, "0.3.0")

    def test_py_typed_marker_is_in_the_package(self):
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(breachlab.__file__),
                                                    "py.typed")))

    def test_token_argument_wins(self):
        with mock.patch.dict(os.environ, {"BREACHLAB_TOKEN": "blp_" + "e" * 20}):
            client = BreachLab(TOKEN, self.script.url)
        self.assertEqual(client.token, TOKEN)
        self.assertEqual(client.token_source, "argument")

    def test_token_from_environment(self):
        with mock.patch.dict(os.environ, {"BREACHLAB_TOKEN": TOKEN}):
            client = BreachLab(base_url=self.script.url)
        self.assertEqual((client.token, client.token_source), (TOKEN, "env"))

    def test_token_from_the_saved_file(self):
        config.save_token(TOKEN)
        client = BreachLab(base_url=self.script.url)
        self.assertEqual((client.token, client.token_source), (TOKEN, "file"))

    def test_environment_wins_over_the_saved_file(self):
        config.save_token("blp_" + "f" * 20)
        with mock.patch.dict(os.environ, {"BREACHLAB_TOKEN": TOKEN}):
            self.assertEqual(BreachLab(base_url=self.script.url).token, TOKEN)

    def test_auto_token_off_ignores_environment_and_file(self):
        config.save_token(TOKEN)
        with mock.patch.dict(os.environ, {"BREACHLAB_TOKEN": TOKEN}):
            self.assertIsNone(BreachLab(base_url=self.script.url, auto_token=False).token)

    def test_a_malformed_token_is_refused(self):
        for bad in ("nope", "blp_short", "blp_" + "a" * 20 + "\r\nX-Evil: 1", ""):
            with self.assertRaises(TokenError):
                BreachLab(bad, self.script.url)

    def test_no_token_raises_not_logged_in_and_sends_nothing(self):
        client = BreachLab(base_url=self.script.url)
        with self.assertRaises(NotLoggedIn):
            client.me()
        self.assertEqual(self.script.requests, [])

    def test_base_url_defaults(self):
        self.assertEqual(BreachLab(TOKEN).base_url, "https://breachlab.org")
        with mock.patch.dict(os.environ, {"BREACHLAB_API": "https://staging.example.org/"}):
            self.assertEqual(BreachLab(TOKEN).base_url, "https://staging.example.org")

    def test_default_user_agent_is_not_the_urllib_one(self):
        self.script.queue({"object": "account"})
        self.client.me()
        agent = self.script.last["headers"]["user-agent"]
        self.assertEqual(agent, "breachlab-python/" + breachlab.__version__)
        self.assertNotIn("urllib", agent.lower())

    def test_custom_user_agent(self):
        client = BreachLab(TOKEN, self.script.url, user_agent="breachlab-test-harness/1.0")
        self.script.queue({"object": "account"})
        client.me()
        self.assertEqual(self.script.last["headers"]["user-agent"], "breachlab-test-harness/1.0")

    def test_bad_user_agent_is_refused(self):
        for bad in ("", "  ", "a\r\nX-Evil: 1"):
            with self.assertRaises(ConfigError):
                BreachLab(TOKEN, self.script.url, user_agent=bad)

    def test_timeout_becomes_a_network_error(self):
        client = BreachLab(TOKEN, self.script.url, timeout=0.05, retry_errors=False)
        with mock.patch.object(client._opener, "open", side_effect=TimeoutError()):
            with self.assertRaises(NetworkError) as caught:
                client.me()
        self.assertIn("0.05 seconds", str(caught.exception))

    def test_every_error_is_a_breachlab_error(self):
        for cls in (ApiError, NetworkError, ConfigError, TokenError, NotLoggedIn, DeviceFlowError):
            self.assertTrue(issubclass(cls, BreachLabError))


# -- the https guard and the redirect guard -------------------------------------------


class TransportGuardTests(SdkCase):
    def test_plain_http_is_refused_for_any_host_but_this_machine(self):
        for url in ("http://breachlab.org", "http://example.com:8080", "http://10.0.0.5",
                    "http://localhost.example.com", "http://127.0.0.1.example.com"):
            with self.assertRaises(ConfigError, msg=url):
                BreachLab(TOKEN, url)

    def test_plain_http_is_allowed_for_localhost(self):
        for url in ("http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"):
            self.assertEqual(BreachLab(TOKEN, url).base_url, url)

    def test_other_bad_base_urls(self):
        for url in ("ftp://breachlab.org", "breachlab.org", "https://user:pw@breachlab.org",
                    "https://breachlab.org/?x=1", "https://breachlab.org/#frag", "https://"):
            with self.assertRaises(ConfigError, msg=url):
                BreachLab(TOKEN, url)

    def test_the_guard_also_covers_the_environment(self):
        with mock.patch.dict(os.environ, {"BREACHLAB_API": "http://example.com"}):
            with self.assertRaises(ConfigError):
                BreachLab(TOKEN)

    def test_a_path_cannot_change_the_host(self):
        with self.assertRaises(ConfigError):
            self.client.call("GET", "@example.com/api/v2/me")
        self.assertEqual(self.script.requests, [])

    def test_path_parts_are_quoted(self):
        self.script.queue_raw(problem(404, "not_found"))
        with self.assertRaises(NotFoundError):
            self.client.user("../../me?x=1")
        self.assertEqual(self.script.last["path"], "/api/v2/users/..%2F..%2Fme%3Fx%3D1")

    def test_a_redirect_to_another_host_is_not_followed(self):
        other = Script()
        self.addCleanup(other.stop)
        self.script.queue(b"", 302, {"Location": other.url + "/api/v2/me"})
        with self.assertRaises(RedirectError) as caught:
            self.client.me()
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(other.requests, [], "the token followed a redirect to another host")
        self.assertEqual(len(self.script.requests), 1)

    def test_no_redirect_status_is_followed(self):
        for status in (301, 303, 307, 308):
            self.script.queue(b"", status, {"Location": self.script.url + "/api/v2/tracks"})
            with self.assertRaises(RedirectError):
                self.client.me()
        self.assertEqual([r["path"] for r in self.script.requests], ["/api/v2/me"] * 4)

    def test_a_redirect_of_a_post_is_not_followed(self):
        other = Script()
        self.addCleanup(other.stop)
        self.script.queue(b"", 307, {"Location": other.url + "/api/v2/submit"})
        with self.assertRaises(RedirectError):
            self.client.submit("BL{some_flag}")
        self.assertEqual(other.requests, [])


# -- every method ------------------------------------------------------------------


class MethodTests(SdkCase):
    def sent(self, method, path, query=None, body=None):
        last = self.script.last
        self.assertEqual((last["method"], last["path"]), (method, path))
        self.assertEqual(last["query"], query or {})
        self.assertEqual(last["body"], body)
        self.assertEqual(last["headers"]["authorization"], "Bearer " + TOKEN)
        self.assertEqual(last["headers"]["accept"], "application/json")

    def one_page(self, items):
        self.script.queue({"object": "list", "data": items, "has_more": False,
                           "next_cursor": None, "prev_cursor": None})

    def test_me(self):
        account = {"object": "account", "username": "operator_01", "points": 1200}
        self.script.queue(account)
        self.assertEqual(self.client.me(), account)
        self.sent("GET", "/api/v2/me")

    def test_progress(self):
        entry = {"object": "track_progress", "track": "ghost", "solved": 2, "next_level_idx": 2}
        self.one_page([entry])
        self.assertEqual(self.client.progress().all(), [entry])
        self.sent("GET", "/api/v2/me/progress", {"limit": "100"})

    def test_solves(self):
        self.one_page([{"object": "solve", "track": "ghost", "level_idx": 1}])
        self.assertEqual(len(self.client.solves(max_items=5).all()), 1)
        self.sent("GET", "/api/v2/me/solves", {"limit": "5"})

    def test_tracks(self):
        self.one_page([{"object": "track", "slug": "ghost"}])
        self.assertEqual([t["slug"] for t in self.client.tracks()], ["ghost"])
        self.sent("GET", "/api/v2/tracks", {"limit": "100"})

    def test_levels(self):
        self.one_page([{"object": "level", "track": "ghost", "idx": 0}])
        self.assertEqual(self.client.levels("ghost").all()[0]["idx"], 0)
        self.sent("GET", "/api/v2/tracks/ghost/levels", {"limit": "100"})

    def test_level(self):
        level = {"object": "level", "track": "ghost", "idx": 3, "title": "Welcome"}
        self.script.queue(level)
        self.assertEqual(self.client.level("ghost", 3), level)
        self.sent("GET", "/api/v2/tracks/ghost/levels/3")

    def test_level_checks_the_index_it_got(self):
        self.script.queue({"object": "level", "track": "ghost", "idx": 4})
        with self.assertRaises(UnexpectedResponse):
            self.client.level("ghost", 3)

    def test_level_index_must_be_a_whole_number(self):
        for bad in (-1, "3", 1.5, True):
            with self.assertRaises(ValueError):
                self.client.level("ghost", bad)
        self.assertEqual(self.script.requests, [])

    def test_level_stats(self):
        stats = {"object": "level_stats", "track": "ghost", "idx": 3,
                 "humans": {"solved": 812, "first_blood": None, "time_between_solves": None},
                 "agents": None, "computed_at": None}
        self.script.queue(stats)
        self.assertEqual(self.client.level_stats("ghost", 3), stats)
        self.sent("GET", "/api/v2/tracks/ghost/levels/3/stats")

    def test_credentials(self):
        creds = {"object": "level_credentials", "track": "ghost", "idx": 3, "kind": "ssh",
                 "host": "203.0.113.7", "port": 2224, "username": "ghost3",
                 "command": "ssh ghost3@203.0.113.7 -p 2224", "url": None,
                 "password": "per-player-password", "password_source": "issued",
                 "source_level_idx": None, "account_id": None, "access_header": None}
        self.script.queue(creds)
        self.assertEqual(self.client.credentials("ghost", 3), creds)
        self.sent("GET", "/api/v2/tracks/ghost/levels/3/credentials")

    def test_create_play_token(self):
        token = {"object": "box_token", "token": "box-token-value",
                 "expires_at": "2026-11-27T12:00:00.000Z"}
        self.script.queue(token, 201)
        self.assertEqual(self.client.create_play_token(), token)
        self.sent("POST", "/api/v2/play/token")
        self.assertEqual(self.script.last["headers"]["content-length"], "0")
        self.assertNotIn("idempotency-key", self.script.last["headers"])

    def test_submit_correct(self):
        verdict = {"result": "correct", "track": "ghost", "level_idx": 2, "points_awarded": 120,
                   "first_blood": False, "track_completed": False}
        self.script.queue(verdict)
        self.assertEqual(self.client.submit("BL{a_flag}"), verdict)
        self.sent("POST", "/api/v2/submit", body={"flag": "BL{a_flag}"})

    def test_submit_incorrect_is_an_answer_not_an_error(self):
        self.script.queue({"result": "incorrect"})
        self.assertEqual(self.client.submit("BL{wrong}")["result"], "incorrect")

    def test_submit_with_run_id(self):
        self.script.queue({"result": "incorrect"})
        self.client.submit("BL{wrong}", run_id=RUN_ID)
        self.sent("POST", "/api/v2/submit", body={"flag": "BL{wrong}", "run_id": RUN_ID})

    def test_submit_rejects_an_unknown_verdict(self):
        self.script.queue({"result": "maybe"})
        with self.assertRaises(UnexpectedResponse):
            self.client.submit("BL{a_flag}")

    def test_leaderboard_default(self):
        self.one_page([{"object": "leaderboard_entry", "rank": 1}])
        self.assertEqual(self.client.leaderboard(max_items=10).all()[0]["rank"], 1)
        self.sent("GET", "/api/v2/leaderboard", {"limit": "10"})

    def test_leaderboard_filters(self):
        self.one_page([])
        self.client.leaderboard(period="weekly").all()
        self.sent("GET", "/api/v2/leaderboard", {"period": "weekly", "limit": "100"})
        self.one_page([])
        self.client.leaderboard(country="US").all()
        self.sent("GET", "/api/v2/leaderboard", {"country": "us", "limit": "100"})
        self.one_page([])
        self.client.leaderboard(board="agents").all()
        self.sent("GET", "/api/v2/leaderboard", {"board": "agents", "limit": "100"})

    def test_model_leaderboard(self):
        entry = {"object": "model_leaderboard_entry", "rank": 1, "model": "other/test-model",
                 "agents": 3, "points": 1200, "solved": 12, "username": "agent_runner",
                 "agent_name": "Agent 004", "agent_number": 4, "owner": "operator_01"}
        self.one_page([entry])
        self.assertEqual(self.client.model_leaderboard().all(), [entry])
        self.sent("GET", "/api/v2/leaderboard/models", {"limit": "100"})

    def test_user(self):
        self.script.queue({"object": "user", "username": "operator_01"})
        self.assertEqual(self.client.user("operator_01")["username"], "operator_01")
        self.sent("GET", "/api/v2/users/operator_01")

    def test_token_info(self):
        token = {"object": "token", "scopes": ["read", "submit"],
                 "created_at": "2026-11-20T09:30:00.000Z", "expires_at": None, "is_agent": False}
        self.script.queue(token)
        self.assertEqual(self.client.token_info(), token)
        self.sent("GET", "/api/v2/token")

    def test_revoke_token(self):
        self.script.queue({"object": "token", "revoked": True})
        self.assertEqual(self.client.revoke_token()["revoked"], True)
        self.sent("DELETE", "/api/v2/token")
        self.assertIsNone(self.client.token, "a revoked token is not kept")

    def test_create_run(self):
        self.script.queue(run_data(harness="my-harness"), 201)
        run = self.client.create_run("ghost", harness="my-harness", harness_version="1.2.0",
                                     idempotency_key="create-1")
        self.assertEqual(run["id"], RUN_ID)
        self.sent("POST", "/api/v2/runs",
                  body={"track": "ghost", "harness": "my-harness", "harness_version": "1.2.0"})
        self.assertEqual(self.script.last["headers"]["idempotency-key"], "create-1")

    def test_create_run_sends_no_model_unless_given(self):
        self.script.queue(run_data(), 201)
        self.client.create_run("ghost")
        self.assertEqual(self.script.last["body"], {"track": "ghost"})
        self.assertNotIn("idempotency-key", self.script.last["headers"])
        self.script.queue(run_data(model="other/test-model"), 201)
        self.client.create_run("ghost", model="other/test-model")
        self.assertEqual(self.script.last["body"], {"track": "ghost", "model": "other/test-model"})

    def test_get_run(self):
        self.script.queue(run_data())
        self.assertEqual(self.client.get_run(RUN_ID)["state"], "open")
        self.sent("GET", "/api/v2/runs/" + RUN_ID)

    def test_runs_with_state(self):
        self.one_page([run_data()])
        self.assertEqual(len(self.client.runs(state="open").all()), 1)
        self.sent("GET", "/api/v2/runs", {"state": "open", "limit": "100"})
        self.one_page([])
        self.client.runs().all()
        self.sent("GET", "/api/v2/runs", {"limit": "100"})

    def test_append_steps_with_a_given_key(self):
        steps = [{"seq": 1, "ts": "2026-11-09T12:00:00.000Z", "type": "thought", "content": "hi"}]
        self.script.queue(batch_result(1))
        self.assertEqual(self.client.append_steps(RUN_ID, steps, "steps-key-1")["accepted"], 1)
        self.sent("POST", "/api/v2/runs/%s/steps" % RUN_ID, body={"steps": steps})
        self.assertEqual(self.script.last["headers"]["idempotency-key"], "steps-key-1")

    def test_append_steps_derives_a_stable_key(self):
        steps = [{"seq": 1, "ts": "2026-11-09T12:00:00.000Z", "type": "note", "content": "a"}]
        other = [{"seq": 2, "ts": "2026-11-09T12:00:01.000Z", "type": "note", "content": "b"}]
        for batch in (steps, steps, other):
            self.script.queue(batch_result(1))
            self.client.append_steps(RUN_ID, batch)
        keys = [r["headers"]["idempotency-key"] for r in self.script.requests]
        self.assertEqual(keys[0], keys[1], "the same batch must carry the same key")
        self.assertNotEqual(keys[0], keys[2])
        self.assertRegex(keys[0], r"^[\x21-\x7e]{1,255}$")

    def test_finish_run(self):
        self.script.queue(run_data(state="finished", reported_status="solved"))
        run = self.client.finish_run(RUN_ID, "solved", tokens_used=120000, cost_usd=0.4200004,
                                     idempotency_key="finish-1")
        self.assertEqual(run["state"], "finished")
        self.sent("POST", "/api/v2/runs/%s/finish" % RUN_ID,
                  body={"status": "solved", "tokens_used": 120000, "cost_usd": 0.42})
        self.assertEqual(self.script.last["headers"]["idempotency-key"], "finish-1")

    def test_a_bad_idempotency_key_is_refused_before_sending(self):
        for bad in ("", "has space", "x" * 256, "line\nbreak"):
            with self.assertRaises(ValueError):
                self.client.finish_run(RUN_ID, "solved", idempotency_key=bad)
        self.assertEqual(self.script.requests, [])

    def test_an_answer_that_is_not_json(self):
        self.script.queue(b"<html>hello</html>", 200, {"Content-Type": "text/html"})
        with self.assertRaises(ApiError) as caught:
            self.client.me()
        self.assertEqual(caught.exception.code, "bad_response")

    def test_an_answer_of_the_wrong_shape(self):
        self.script.queue(["not", "an", "object"])
        with self.assertRaises(UnexpectedResponse):
            self.client.me()


# -- error mapping -----------------------------------------------------------------


class ErrorTests(SdkCase):
    CASES = [
        (401, "unauthorized", AuthenticationError),
        (403, "insufficient_scope", ScopeError),
        (403, "subscription_required", PaywallError),
        (403, "prerequisite_required", ForbiddenError),
        (403, "level_locked", ForbiddenError),
        (403, "email_unverified", ForbiddenError),
        (404, "not_found", NotFoundError),
        (404, "board_unavailable", NotFoundError),
        (409, "already_solved", AlreadySolvedError),
        (409, "run_closed", ConflictError),
        (409, "not_api_playable", ConflictError),
        (409, "idempotency_in_progress", ConflictError),
        (400, "invalid_request", ValidationError),
        (400, "invalid_parameter", ValidationError),
        (400, "invalid_cursor", ValidationError),
        (413, "payload_too_large", ValidationError),
        (422, "invalid_format", ValidationError),
        (422, "idempotency_key_reused", ValidationError),
        (429, "rate_limited", RateLimitError),
        (429, "cooldown", RateLimitError),
        (429, "run_limit_reached", RateLimitError),
        (500, "internal_error", ServerError),
        (503, "agents_disabled", ServerError),
        (503, "play_disabled", ServerError),
        (503, "credentials_unavailable", ServerError),
    ]

    def setUp(self):
        SdkCase.setUp(self)
        self.client = BreachLab(TOKEN, self.script.url, max_retries=0)

    def test_every_code_maps_to_its_class(self):
        for status, code, cls in self.CASES:
            self.script.queue_raw(problem(status, code))
            with self.assertRaises(cls, msg=code) as caught:
                self.client.submit("BL{a_flag}")
            exc = caught.exception
            self.assertIs(type(exc), cls, code)
            self.assertIsInstance(exc, ApiError)
            self.assertEqual((exc.status, exc.code), (status, code))
            self.assertEqual(exc.type, DOCS + code)
            self.assertEqual(exc.title, TITLES[status])
            self.assertEqual(exc.detail, "Detail of %s." % code)
            self.assertEqual(str(exc), exc.detail)
            self.assertEqual(exc.doc_url, DOCS + code)
            self.assertEqual(exc.request_id, "req_4f9k2m7q1x8c3v6b0n5t2w8z")
            self.assertEqual(exc.body["code"], code)

    def test_paywall_is_a_kind_of_forbidden_and_scope_is_not(self):
        self.assertTrue(issubclass(PaywallError, ForbiddenError))
        self.assertFalse(issubclass(ScopeError, ForbiddenError))
        self.assertTrue(issubclass(AlreadySolvedError, ConflictError))

    def test_already_solved_carries_the_earlier_solve(self):
        self.script.queue_raw(problem(409, "already_solved", track="ghost", level_idx=3))
        with self.assertRaises(AlreadySolvedError) as caught:
            self.client.submit("BL{a_flag}")
        self.assertEqual((caught.exception.track, caught.exception.level_idx), ("ghost", 3))

    def test_already_solved_without_the_earlier_solve(self):
        self.script.queue_raw(problem(409, "already_solved"))
        with self.assertRaises(AlreadySolvedError) as caught:
            self.client.submit("BL{a_flag}")
        self.assertEqual((caught.exception.track, caught.exception.level_idx), (None, None))

    def test_the_three_kinds_of_429(self):
        for code in ("rate_limited", "cooldown", "run_limit_reached"):
            self.script.queue_raw(problem(429, code, {"Retry-After": "12"},
                                          retry_after_seconds=12))
            with self.assertRaises(RateLimitError) as caught:
                self.client.me()
            self.assertEqual((caught.exception.kind, caught.exception.retry_after), (code, 12))

    def test_retry_after_from_the_body_alone(self):
        self.script.queue_raw(problem(429, "cooldown", retry_after_seconds=8))
        with self.assertRaises(RateLimitError) as caught:
            self.client.submit("BL{a_flag}")
        self.assertEqual(caught.exception.retry_after, 8)

    def test_a_429_that_does_not_say_how_long(self):
        self.script.queue_raw(problem(429, "cooldown"))
        with self.assertRaises(RateLimitError) as caught:
            self.client.submit("BL{a_flag}")
        self.assertIsNone(caught.exception.retry_after)

    def test_a_429_without_a_problem_document(self):
        # The device flow and the edge answer in this shape.
        self.script.queue({"error": "Too many requests. Try again later."}, 429)
        with self.assertRaises(RateLimitError) as caught:
            self.client.me()
        self.assertEqual((caught.exception.kind, caught.exception.retry_after),
                         ("rate_limited", None))

    def test_the_edge_block_of_an_unnamed_client(self):
        self.script.queue(b"error code: 1010", 403, {"Content-Type": "text/plain"})
        with self.assertRaises(ForbiddenError) as caught:
            self.client.me()
        self.assertEqual(caught.exception.code, "http_403")
        self.assertIn("blocked before it reached the API", caught.exception.detail)

    def test_answers_from_a_proxy(self):
        for status, cls in ((404, NotFoundError), (413, ValidationError), (502, ServerError),
                            (418, ApiError)):
            self.script.queue(b"<html></html>", status, {"Content-Type": "text/html"})
            with self.assertRaises(cls) as caught:
                self.client.me()
            self.assertEqual(caught.exception.code, "http_%d" % status)

    def test_a_doc_url_that_is_not_https_is_dropped(self):
        self.script.queue_raw(problem(404, "not_found", doc_url="javascript:alert(1)"))
        with self.assertRaises(NotFoundError) as caught:
            self.client.me()
        self.assertIsNone(caught.exception.doc_url)


# -- pagination --------------------------------------------------------------------


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.server = stub_server.StubServer().start()
        self.addCleanup(self.server.stop)
        self.state = self.server.state
        self.client = BreachLab(stub_server.TOKEN, self.server.url)

    def limits(self):
        return [r["query"].get("limit") for r in self.state.requests]

    def test_iteration_follows_the_cursors_to_the_end(self):
        board = self.client.leaderboard()
        self.assertIsInstance(board, Paginator)
        names = [entry["username"] for entry in board]
        self.assertEqual(names, ["player_%03d" % i for i in range(250)])
        self.assertEqual(self.limits(), ["100", "100", "100"])
        self.assertNotIn("starting_after", self.state.requests[0]["query"])
        self.assertIn("starting_after", self.state.requests[1]["query"])
        self.assertIs(board.has_more, False)

    def test_nothing_is_requested_until_iteration(self):
        board = self.client.leaderboard()
        self.assertEqual(self.state.requests, [])
        self.assertIsNone(board.has_more)
        next(iter(board))
        self.assertEqual(len(self.state.requests), 1)

    def test_max_items_stops_early_and_says_more_exist(self):
        board = self.client.leaderboard(max_items=30)
        self.assertEqual(len(board.all()), 30)
        self.assertEqual(self.limits(), ["30"])
        self.assertIs(board.has_more, True)

    def test_max_items_across_pages(self):
        board = self.client.leaderboard(max_items=130)
        self.assertEqual(len(board.all()), 130)
        self.assertEqual(self.limits(), ["100", "30"])
        self.assertIs(board.has_more, True)

    def test_max_items_equal_to_the_whole_list(self):
        solves = self.client.solves(max_items=45)
        self.assertEqual(len(solves.all()), 45)
        self.assertIs(solves.has_more, False)

    def test_page_size(self):
        solves = self.client.solves(page_size=20)
        self.assertEqual(len(solves.all()), 45)
        self.assertEqual(self.limits(), ["20", "20", "20"])

    def test_pages_carry_their_cursors(self):
        pages = list(self.client.solves(page_size=20).pages())
        self.assertEqual([len(p.data) for p in pages], [20, 20, 5])
        self.assertIsInstance(pages[0], Page)
        self.assertEqual([p.has_more for p in pages], [True, True, False])
        self.assertIsNone(pages[0].prev_cursor)
        self.assertIsNotNone(pages[1].prev_cursor)
        self.assertIsNone(pages[2].next_cursor)

    def test_resume_from_a_cursor(self):
        first = next(self.client.solves(page_size=20).pages())
        rest = self.client.solves(starting_after=first.next_cursor).all()
        self.assertEqual(len(rest), 25)
        self.assertEqual(rest[0], self.state.solves[20])

    def test_a_second_iteration_starts_again(self):
        tracks = self.client.tracks()
        self.assertEqual(tracks.all(), tracks.all())
        self.assertEqual(len(self.state.requests), 2)

    def test_single_page_lists(self):
        self.assertEqual([t["slug"] for t in self.client.tracks()], ["ghost", "phantom"])
        self.assertEqual([p["track"] for p in self.client.progress()], ["ghost", "phantom"])
        self.assertEqual(len(self.client.levels("ghost").all()), 5)

    def test_an_unknown_track_raises_when_iterated(self):
        levels = self.client.levels("nope")
        with self.assertRaises(NotFoundError):
            levels.all()

    def test_bad_arguments(self):
        for kwargs in ({"max_items": 0}, {"page_size": 0}, {"page_size": 101}):
            with self.assertRaises(ValueError):
                self.client.solves(**kwargs)

    def test_paginate_any_list_endpoint(self):
        items = self.client.paginate("/api/v2/me/solves", max_items=3).all()
        self.assertEqual(items, self.state.solves[:3])


class PaginationEdgeTests(SdkCase):
    def page(self, data, cursor, more=True):
        self.script.queue({"object": "list", "data": data, "has_more": more,
                           "next_cursor": cursor, "prev_cursor": None})

    def test_a_repeated_cursor_ends_the_iteration(self):
        for _ in range(3):
            self.page([{"n": 1}], "same")
        self.assertEqual(len(self.client.solves().all()), 2)
        self.assertEqual(len(self.script.requests), 2)

    def test_an_empty_page_ends_the_iteration(self):
        self.page([], "next")
        self.assertEqual(self.client.solves().all(), [])
        self.assertEqual(len(self.script.requests), 1)

    def test_a_list_of_the_wrong_shape(self):
        self.script.queue({"object": "list", "data": "nope"})
        with self.assertRaises(UnexpectedResponse):
            self.client.solves().all()

    def test_a_server_that_ignores_the_limit(self):
        self.page([{"n": i} for i in range(10)], "next")
        solves = self.client.solves(max_items=4)
        self.assertEqual(len(solves.all()), 4)
        self.assertIs(solves.has_more, True)


# -- retries -----------------------------------------------------------------------


class RetryTests(SdkCase):
    def limited(self, seconds=7, code="rate_limited"):
        self.script.queue_raw(problem(429, code, {"Retry-After": str(seconds)},
                                      retry_after_seconds=seconds))

    def test_a_get_waits_for_retry_after_and_tries_again(self):
        self.limited(7)
        self.script.queue({"object": "account", "username": "operator_01"})
        self.assertEqual(self.client.me()["username"], "operator_01")
        self.assertEqual(self.sleeps, [7])
        self.assertEqual(len(self.script.requests), 2)

    def test_the_wait_is_tried_a_bounded_number_of_times(self):
        for _ in range(5):
            self.limited(3)
        with self.assertRaises(RateLimitError):
            self.client.me()
        self.assertEqual(self.sleeps, [3, 3])
        self.assertEqual(len(self.script.requests), 3)

    def test_a_long_retry_after_is_not_sat_through(self):
        self.limited(61)
        with self.assertRaises(RateLimitError) as caught:
            self.client.me()
        self.assertEqual(caught.exception.retry_after, 61)
        self.assertEqual(self.sleeps, [])

    def test_max_retry_wait_is_adjustable(self):
        client = BreachLab(TOKEN, self.script.url, max_retry_wait=5)
        self.limited(6)
        with self.assertRaises(RateLimitError):
            client.me()
        self.assertEqual(self.sleeps, [])

    def test_no_retry_after_means_no_retry(self):
        self.script.queue_raw(problem(429, "rate_limited"))
        with self.assertRaises(RateLimitError):
            self.client.me()
        self.assertEqual(len(self.script.requests), 1)

    def test_waiting_can_be_turned_off(self):
        for client in (BreachLab(TOKEN, self.script.url, wait_on_rate_limit=False),
                       BreachLab(TOKEN, self.script.url, max_retries=0)):
            self.limited(1)
            with self.assertRaises(RateLimitError):
                client.me()
        self.assertEqual(self.sleeps, [])

    def test_notify_hears_about_the_wait(self):
        heard = []
        client = BreachLab(TOKEN, self.script.url, notify=heard.append)
        self.limited(1)
        self.script.queue({"object": "account"})
        client.me()
        self.assertEqual(heard, ["Rate limited. Retrying in 1 second."])

    def test_submit_is_never_retried_after_a_429(self):
        for code in ("cooldown", "rate_limited"):
            self.limited(2, code)
            with self.assertRaises(RateLimitError):
                self.client.submit("BL{a_flag}")
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.script.requests), 2)

    def test_submit_is_never_retried_after_a_failure(self):
        self.script.queue_raw(problem(500, "internal_error"))
        with self.assertRaises(ServerError):
            self.client.submit("BL{a_flag}")
        with mock.patch.object(self.client, "send", side_effect=NetworkError("down")) as send:
            with self.assertRaises(NetworkError):
                self.client.submit("BL{a_flag}")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(self.script.requests), 1)

    def test_submit_waits_only_when_asked(self):
        self.limited(4, "cooldown")
        self.script.queue({"result": "incorrect"})
        self.assertEqual(self.client.submit("BL{a_flag}", wait=True)["result"], "incorrect")
        self.assertEqual(self.sleeps, [4])

    def test_a_get_is_retried_after_a_server_error(self):
        self.script.queue_raw(problem(500, "internal_error"))
        self.script.queue(b"bad gateway", 502, {"Content-Type": "text/plain"})
        self.script.queue({"object": "account"})
        self.client.me()
        self.assertEqual(self.sleeps, [0.5, 1.0])
        self.assertEqual(len(self.script.requests), 3)

    def test_server_error_retries_are_bounded(self):
        for _ in range(5):
            self.script.queue_raw(problem(500, "internal_error"))
        with self.assertRaises(ServerError):
            self.client.me()
        self.assertEqual(len(self.script.requests), 3)

    def test_a_get_is_retried_after_a_network_failure(self):
        real = self.client.send
        answers = [NetworkError("down"), NetworkError("down")]

        def flaky(*args, **kwargs):
            if answers:
                raise answers.pop()
            return real(*args, **kwargs)

        self.script.queue({"object": "account", "username": "operator_01"})
        with mock.patch.object(self.client, "send", side_effect=flaky):
            self.assertEqual(self.client.me()["username"], "operator_01")
        self.assertEqual(self.sleeps, [0.5, 1.0])

    def test_network_failure_retries_are_bounded(self):
        with mock.patch.object(self.client, "send", side_effect=NetworkError("down")) as send:
            with self.assertRaises(NetworkError):
                self.client.me()
        self.assertEqual(send.call_count, 3)

    def test_a_switch_that_is_off_is_not_hammered(self):
        for code in ("agents_disabled", "play_disabled", "credentials_unavailable"):
            self.script.queue_raw(problem(503, code))
            with self.assertRaises(ServerError):
                self.client.credentials("ghost", 0)
        self.assertEqual(self.sleeps, [])

    def test_a_post_without_a_key_is_not_retried_after_a_failure(self):
        calls = [
            lambda: self.client.create_play_token(),
            lambda: self.client.create_run("ghost"),
            lambda: self.client.finish_run(RUN_ID, "solved"),
            lambda: self.client.revoke_token(),
        ]
        for call in calls:
            client_requests = len(self.script.requests)
            self.script.queue_raw(problem(500, "internal_error"))
            with self.assertRaises(ServerError):
                call()
            self.assertEqual(len(self.script.requests), client_requests + 1)
        self.assertEqual(self.sleeps, [])

    def test_a_step_append_is_retried_with_the_same_key_and_body(self):
        steps = [{"seq": 1, "ts": "2026-11-09T12:00:00.000Z", "type": "thought", "content": "x"}]
        self.script.queue_raw(problem(500, "internal_error"))
        self.script.queue_raw(problem(409, "idempotency_in_progress"))
        self.script.queue(batch_result(1))
        self.assertEqual(self.client.append_steps(RUN_ID, steps)["accepted"], 1)
        sent = self.script.requests
        self.assertEqual(len(sent), 3)
        self.assertEqual(len({r["headers"]["idempotency-key"] for r in sent}), 1)
        self.assertEqual([r["body"] for r in sent], [{"steps": steps}] * 3)

    def test_a_step_append_waits_out_a_429(self):
        steps = [{"seq": 1, "ts": "2026-11-09T12:00:00.000Z", "type": "thought", "content": "x"}]
        self.limited(2)
        self.script.queue(batch_result(1))
        self.client.append_steps(RUN_ID, steps)
        self.assertEqual(self.sleeps, [2])

    def test_a_keyed_request_is_retried_after_a_failure(self):
        self.script.queue_raw(problem(500, "internal_error"))
        self.script.queue(run_data(), 201)
        self.client.create_run("ghost", idempotency_key="create-1")
        self.assertEqual(len(self.script.requests), 2)

    def test_errors_that_will_not_get_better_are_not_retried(self):
        for status, code in ((400, "invalid_parameter"), (401, "unauthorized"),
                             (403, "insufficient_scope"), (404, "not_found")):
            self.script.queue_raw(problem(status, code))
            with self.assertRaises(ApiError):
                self.client.me()
        self.assertEqual(len(self.script.requests), 4)
        self.assertEqual(self.sleeps, [])

    def test_retry_errors_off(self):
        client = BreachLab(TOKEN, self.script.url, retry_errors=False)
        self.script.queue_raw(problem(500, "internal_error"))
        with self.assertRaises(ServerError):
            client.me()
        self.assertEqual(len(self.script.requests), 1)


# -- the token never shows -----------------------------------------------------------


class RedactionTests(SdkCase):
    def test_repr_of_the_client_hides_the_token(self):
        self.assertNotIn(TOKEN, repr(self.client))
        self.assertNotIn("blp_", repr(self.client))
        self.assertNotIn("blp_", str(self.client))

    def test_debug_lines_never_hold_the_token(self):
        lines = []
        client = BreachLab(TOKEN, self.script.url, debug=lines.append)
        self.script.queue({"object": "account", "username": "operator_01"})
        client.me()
        text = "\n".join(lines)
        self.assertIn("> Authorization: Bearer ***", text)
        self.assertIn("> GET " + self.script.url + "/api/v2/me", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("blp_", text)

    def test_debug_true_logs_to_the_breachlab_logger_redacted(self):
        client = BreachLab(TOKEN, self.script.url, debug=True)
        self.script.queue({"object": "account"})
        with self.assertLogs("breachlab", level="DEBUG") as logs:
            client.me()
        text = "\n".join(logs.output)
        self.assertIn("Authorization: Bearer ***", text)
        self.assertNotIn(TOKEN, text)

    def test_nothing_is_logged_or_printed_without_debug(self):
        import contextlib
        import io
        import logging
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("breachlab")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, logging.NOTSET)
        out, err = io.StringIO(), io.StringIO()
        self.script.queue({"object": "account"})
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.client.me()
        self.assertEqual(records, [])
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_a_token_echoed_by_the_server_is_redacted_in_debug(self):
        lines = []
        client = BreachLab(TOKEN, self.script.url, debug=lines.append)
        other = "blp_" + "q" * 30
        self.script.queue({"object": "account", "echo": TOKEN, "other": other})
        client.me()
        self.assertNotIn(TOKEN, "\n".join(lines))
        self.assertNotIn(other, "\n".join(lines))

    def test_secrets_the_api_hands_out_are_redacted_in_debug(self):
        lines = []
        client = BreachLab(TOKEN, self.script.url, debug=lines.append)
        self.script.queue({"object": "box_token", "token": "box-token-value-123",
                           "expires_at": "2026-11-27T12:00:00.000Z"}, 201)
        self.script.queue({"object": "level_credentials", "password": "per-player-password",
                           "access_header": {"name": "X-BL-Pass", "value": "v1.header.value",
                                             "expires_at": "2026-11-20T13:00:00.000Z"}})
        self.script.queue({"object": "account", "note": "per-player-password v1.header.value"})
        client.create_play_token()
        client.credentials("ghost", 3)
        client.me()
        text = "\n".join(lines)
        for secret in ("box-token-value-123", "per-player-password", "v1.header.value"):
            self.assertNotIn(secret, text)
        self.assertEqual(text.count("body withheld"), 2)

    def test_an_error_never_shows_a_token(self):
        leaked = "blp_" + "k" * 30
        self.script.queue_raw(problem(400, "invalid_request",
                                      detail="Bad header Authorization: Bearer %s" % leaked))
        with self.assertRaises(ValidationError) as caught:
            self.client.me()
        exc = caught.exception
        for text in (repr(exc), str(exc), "%s" % exc, "%r" % (exc,)):
            self.assertNotIn(leaked, text)
            self.assertIn("blp_***", text)
        self.assertIn("ValidationError(status=400, code='invalid_request'", repr(exc))

    def test_no_error_holds_the_clients_own_token(self):
        self.script.queue_raw(problem(401, "unauthorized"))
        with self.assertRaises(AuthenticationError) as caught:
            self.client.me()
        exc = caught.exception
        self.assertNotIn(TOKEN, repr(exc) + str(exc) + repr(exc.args) + repr(vars(exc)))

    def test_a_network_error_does_not_show_the_token(self):
        client = BreachLab(TOKEN, "http://127.0.0.1:1", retry_errors=False, timeout=2)
        with self.assertRaises(NetworkError) as caught:
            client.me()
        self.assertNotIn("blp_", str(caught.exception) + repr(caught.exception))

    def test_the_token_goes_in_the_header_and_never_in_the_url(self):
        self.script.queue({"object": "list", "data": [], "has_more": False, "next_cursor": None})
        self.client.leaderboard(country="us").all()
        self.assertEqual(self.script.last["headers"]["authorization"], "Bearer " + TOKEN)
        self.assertNotIn("blp_", json.dumps(self.script.last["query"]))
        self.assertNotIn("blp_", self.script.last["path"])


# -- device flow login ---------------------------------------------------------------


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.server = stub_server.StubServer().start()
        self.addCleanup(self.server.stop)
        self.state = self.server.state
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.xdg = os.path.join(self.tmp.name, "xdg")
        env = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "XDG_CONFIG_HOME": self.xdg},
                              clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.sleeps = []
        naps = mock.patch("breachlab.client.sleep", self.sleeps.append)
        naps.start()
        self.addCleanup(naps.stop)
        self.client = BreachLab(base_url=self.server.url)
        self.token_file = os.path.join(self.xdg, "breachlab", "token")

    def test_login_shows_the_code_then_returns_the_token(self):
        shown = []
        granted = self.client.login(shown.append)
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0]["user_code"], stub_server.USER_CODE)
        self.assertTrue(shown[0]["verification_uri_complete"].endswith(stub_server.USER_CODE))
        self.assertEqual(granted["access_token"], stub_server.TOKEN)
        self.assertEqual(granted["scope"], "read submit")
        self.assertEqual(self.sleeps, [6, 6])
        # The client is logged in from here on.
        self.assertEqual(self.client.me()["username"], "operator_01")
        for request in self.state.requests[:3]:
            self.assertNotIn("authorization", request["headers"])

    def test_login_saves_the_token_readable_by_its_owner_only(self):
        self.client.login(lambda start: None)
        with open(self.token_file) as handle:
            self.assertEqual(handle.read().strip(), stub_server.TOKEN)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.token_file).st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(os.stat(os.path.dirname(self.token_file)).st_mode), 0o700)
        self.assertEqual(BreachLab(base_url=self.server.url).token, stub_server.TOKEN)

    def test_login_without_saving(self):
        self.client.login(lambda start: None, save=False)
        self.assertFalse(os.path.exists(self.token_file))
        self.assertEqual(self.client.token, stub_server.TOKEN)

    def test_login_scope(self):
        self.client.login(lambda start: None, scope="read", save=False)
        self.assertEqual(json.loads(self.state.requests[0]["body"]), {"scope": "read"})

    def test_a_refused_scope(self):
        with self.assertRaises(ValidationError) as caught:
            self.client.start_device_login("read admin")
        self.assertEqual(caught.exception.code, "invalid_scope")

    def test_start_and_poll_by_hand(self):
        start = self.client.start_device_login()
        self.assertEqual(start["device_code"], stub_server.DEVICE_CODE)
        granted = self.client.poll_device_login(start["device_code"], start["interval"],
                                                start["expires_in"])
        self.assertEqual(granted["token_type"], "bearer")

    def test_slow_down_adds_five_seconds(self):
        self.state.poll_script = ["slow_down", "authorization_pending", "approved"]
        self.client.login(lambda start: None, save=False)
        self.assertEqual(self.sleeps, [6, 11, 11])

    def test_denied(self):
        self.state.poll_script = ["access_denied"]
        with self.assertRaises(DeviceFlowError) as caught:
            self.client.login(lambda start: None)
        self.assertEqual(caught.exception.code, "access_denied")
        self.assertFalse(caught.exception.expired_locally)
        self.assertTrue(caught.exception.doc_url.startswith("https://"))
        self.assertFalse(os.path.exists(self.token_file))
        self.assertIsNone(self.client.token)

    def test_expired_on_the_server(self):
        self.state.poll_script = ["expired_token"]
        with self.assertRaises(DeviceFlowError) as caught:
            self.client.login(lambda start: None)
        self.assertEqual(caught.exception.code, "expired_token")

    def test_the_code_runs_out_while_waiting(self):
        self.state.poll_script = ["authorization_pending"] * 200
        with self.assertRaises(DeviceFlowError) as caught:
            self.client.poll_device_login(stub_server.DEVICE_CODE, 5, 20)
        self.assertTrue(caught.exception.expired_locally)
        self.assertEqual(self.sleeps, [6, 6, 6])

    def test_a_token_of_the_wrong_shape_is_not_kept(self):
        self.state.token = "not-a-token"
        with self.assertRaises(UnexpectedResponse):
            self.client.login(lambda start: None)
        self.assertFalse(os.path.exists(self.token_file))
        self.assertIsNone(self.client.token)

    def test_debug_output_of_a_login_holds_no_secret(self):
        lines = []
        client = BreachLab(base_url=self.server.url, debug=lines.append)
        client.login(lambda start: None, save=False)
        text = "\n".join(lines)
        self.assertNotIn(stub_server.TOKEN, text)
        self.assertNotIn(stub_server.DEVICE_CODE, text)


# -- the Run context manager ---------------------------------------------------------


class RunTests(SdkCase):
    def posts(self, suffix):
        return [r for r in self.script.requests if r["path"].endswith(suffix)]

    def test_a_run_that_solves_is_closed_as_solved(self):
        self.script.queue(run_data(), 201)
        self.script.queue(batch_result(1))
        self.script.queue(batch_result(2))
        self.script.queue({"result": "correct", "track": "ghost", "level_idx": 0,
                           "points_awarded": 100, "first_blood": False, "track_completed": False})
        self.script.queue(run_data(state="finished", reported_status="solved"))
        with self.client.run("ghost", harness="my-harness", harness_version="1.0") as run:
            self.assertIsInstance(run, Run)
            self.assertEqual(run.id, RUN_ID)
            self.assertEqual(run.step("thought", "Reading the banner.")["accepted"], 1)
            run.step("command", "cat /etc/motd")
            self.assertEqual(run.submit("BL{a_flag}")["result"], "correct")
            self.assertFalse(run.finished)
        self.assertTrue(run.finished)
        self.assertEqual(run.data["reported_status"], "solved")

        create, first, second, submit, finish = self.script.requests
        self.assertEqual((create["method"], create["path"]), ("POST", "/api/v2/runs"))
        self.assertEqual(create["body"], {"track": "ghost", "harness": "my-harness",
                                          "harness_version": "1.0"})
        self.assertIn("idempotency-key", create["headers"])
        self.assertEqual(first["path"], "/api/v2/runs/%s/steps" % RUN_ID)
        self.assertEqual([s["seq"] for s in first["body"]["steps"]], [1])
        self.assertEqual([s["seq"] for s in second["body"]["steps"]], [2])
        self.assertEqual(first["body"]["steps"][0]["type"], "thought")
        self.assertEqual(second["body"]["steps"][0]["content"], "cat /etc/motd")
        self.assertRegex(first["body"]["steps"][0]["ts"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
        self.assertNotEqual(first["headers"]["idempotency-key"],
                            second["headers"]["idempotency-key"])
        self.assertEqual(submit["body"], {"flag": "BL{a_flag}", "run_id": RUN_ID})
        self.assertNotIn("idempotency-key", submit["headers"])
        self.assertEqual(finish["path"], "/api/v2/runs/%s/finish" % RUN_ID)
        self.assertEqual(finish["body"], {"status": "solved"})
        self.assertIn("idempotency-key", finish["headers"])

    def test_no_model_is_sent_unless_the_author_names_one(self):
        self.script.queue(run_data(), 201)
        self.script.queue(run_data(state="finished"))
        with self.client.run("ghost"):
            pass
        self.assertEqual(self.script.requests[0]["body"], {"track": "ghost"})

    def test_a_run_without_a_solve_is_closed_as_gave_up(self):
        self.script.queue(run_data(), 201)
        self.script.queue({"result": "incorrect"})
        self.script.queue(run_data(state="finished", reported_status="gave_up"))
        with self.client.run("ghost") as run:
            run.submit("BL{wrong}")
        self.assertEqual(self.script.last["body"], {"status": "gave_up"})

    def test_the_test_flag_does_not_count_as_a_solve(self):
        self.script.queue(run_data(), 201)
        self.script.queue({"result": "correct", "test": True})
        self.script.queue(run_data(state="finished"))
        with self.client.run("ghost") as run:
            run.submit("BL{api_v2_test}")
        self.assertEqual(self.script.last["body"], {"status": "gave_up"})

    def test_an_exception_closes_the_run_as_error_and_still_propagates(self):
        self.script.queue(run_data(), 201)
        self.script.queue(run_data(state="finished", reported_status="error"))
        with self.assertRaises(ZeroDivisionError):
            with self.client.run("ghost") as run:
                1 / 0
        self.assertEqual(self.script.last["body"], {"status": "error"})
        self.assertTrue(run.finished)

    def test_a_failing_close_does_not_hide_the_exception_of_the_block(self):
        self.script.queue(run_data(), 201)
        self.script.queue_raw(problem(503, "agents_disabled"))
        with self.assertRaises(KeyError):
            with self.client.run("ghost"):
                raise KeyError("boom")

    def test_a_failing_close_after_a_clean_block_is_raised(self):
        self.script.queue(run_data(), 201)
        self.script.queue_raw(problem(503, "agents_disabled"))
        with self.assertRaises(ServerError):
            with self.client.run("ghost"):
                pass

    def test_usage_and_cost_are_reported_on_close(self):
        self.script.queue(run_data(), 201)
        self.script.queue(run_data(state="finished"))
        with self.client.run("ghost") as run:
            run.tokens_used = 120000
            run.cost_usd = 0.123456
        self.assertEqual(self.script.last["body"],
                         {"status": "gave_up", "tokens_used": 120000, "cost_usd": 0.1235})

    def test_an_explicit_finish_is_not_repeated_on_exit(self):
        self.script.queue(run_data(), 201)
        self.script.queue(run_data(state="finished", reported_status="timeout"))
        with self.client.run("ghost") as run:
            done = run.finish("timeout", tokens_used=5)
            self.assertEqual(done["reported_status"], "timeout")
        self.assertEqual(len(self.posts("/finish")), 1)
        self.assertEqual(self.script.last["body"], {"status": "timeout", "tokens_used": 5})
        with self.assertRaises(RuntimeError):
            run.step("note", "too late")

    def test_a_run_the_server_already_closed_counts_as_finished(self):
        self.script.queue(run_data(), 201)
        self.script.queue_raw(problem(409, "run_closed"))
        with self.client.run("ghost") as run:
            pass
        self.assertTrue(run.finished)

    def test_batching_holds_steps_back_and_flushes_on_exit(self):
        self.script.queue(run_data(), 201)
        self.script.queue(batch_result(3))
        self.script.queue(batch_result(5))
        self.script.queue(run_data(state="finished"))
        with self.client.run("ghost", batch_size=3) as run:
            self.assertIsNone(run.step("thought", "one"))
            self.assertIsNone(run.step("command", "two"))
            self.assertEqual(run.step("output", "three")["accepted"], 3)
            run.step("note", "four")
            run.step("note", "five")
            self.assertEqual(len(self.posts("/steps")), 1)
        uploads = self.posts("/steps")
        self.assertEqual([[s["seq"] for s in u["body"]["steps"]] for u in uploads],
                         [[1, 2, 3], [4, 5]])
        self.assertEqual(self.script.requests[-1]["path"], "/api/v2/runs/%s/finish" % RUN_ID)

    def test_steps_are_uploaded_before_a_submission(self):
        self.script.queue(run_data(), 201)
        self.script.queue(batch_result(1))
        self.script.queue({"result": "incorrect"})
        self.script.queue(run_data(state="finished"))
        with self.client.run("ghost", batch_size=50) as run:
            run.step("submit", "Trying the flag from the banner.")
            run.submit("BL{wrong}")
        paths = [r["path"].rsplit("/", 1)[-1] for r in self.script.requests]
        self.assertEqual(paths, ["runs", "steps", "submit", "finish"])

    def test_a_failed_upload_is_flushed_again_with_the_same_key(self):
        client = BreachLab(TOKEN, self.script.url, max_retries=0)
        self.script.queue(run_data(), 201)
        self.script.queue_raw(problem(500, "internal_error"))
        self.script.queue(batch_result(1))
        self.script.queue(run_data(state="finished"))
        with client.run("ghost") as run:
            with self.assertRaises(ServerError):
                run.step("thought", "one")
            self.assertEqual(run.flush()["accepted"], 1)
        first, second = self.posts("/steps")
        self.assertEqual(first["body"], second["body"])
        self.assertEqual(first["headers"]["idempotency-key"], second["headers"]["idempotency-key"])

    def test_large_batches_are_split(self):
        self.script.queue(run_data(), 201)
        for _ in range(3):
            self.script.queue(batch_result(1))
        self.script.queue(run_data(state="finished"))
        with self.client.run("ghost", batch_size=100) as run:
            for _ in range(5):
                run.step("output", "x" * 90000)
        sizes = [len(u["body"]["steps"]) for u in self.posts("/steps")]
        self.assertEqual(sizes, [2, 2, 1])

    def test_resume_continues_after_the_last_step_the_server_took(self):
        self.script.queue(run_data(last_seq=41, step_count=41))
        self.script.queue(batch_result(42))
        self.script.queue(run_data(state="finished"))
        with self.client.resume_run(RUN_ID) as run:
            self.assertEqual(run.last_seq, 41)
            run.step("note", "Back after a restart.")
        get, steps, _ = self.script.requests
        self.assertEqual((get["method"], get["path"]), ("GET", "/api/v2/runs/" + RUN_ID))
        self.assertEqual(steps["body"]["steps"][0]["seq"], 42)

    def test_resuming_a_closed_run_leaves_it_alone(self):
        self.script.queue(run_data(state="finished", reported_status="solved"))
        with self.client.resume_run(RUN_ID) as run:
            self.assertTrue(run.finished)
        self.assertEqual(len(self.script.requests), 1)

    def test_bad_steps_are_refused_before_sending(self):
        self.script.queue(run_data(), 201)
        self.script.queue(run_data(state="finished"))
        with self.client.run("ghost") as run:
            with self.assertRaises(ValueError):
                run.step("musing", "not a kind of step")
            with self.assertRaises(TypeError):
                run.step("note", 42)
        self.assertEqual(self.posts("/steps"), [])
        self.assertEqual(run.last_seq, 0)

    def test_a_run_cannot_be_used_before_it_is_open(self):
        run = self.client.run("ghost")
        with self.assertRaises(RuntimeError):
            run.step("note", "early")
        with self.assertRaises(ValueError):
            self.client.run("ghost", batch_size=101)

    def test_a_run_that_cannot_be_created_raises_and_closes_nothing(self):
        self.script.queue_raw(problem(429, "run_limit_reached", {"Retry-After": "3600"},
                                      retry_after_seconds=3600))
        with self.assertRaises(RateLimitError) as caught:
            with self.client.run("ghost"):
                self.fail("the block must not run")
        self.assertEqual(caught.exception.kind, "run_limit_reached")
        self.assertEqual(len(self.script.requests), 1)


if __name__ == "__main__":
    unittest.main()
