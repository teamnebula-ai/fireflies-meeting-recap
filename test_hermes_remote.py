#!/usr/bin/env python3
"""Tests for contrib/hermes-remote, the RECAP_GEN_BIN wrapper the live box uses.

The wrapper forwards a prompt to an HTTP generate endpoint. The property under
test is the transport: with `-z -` the prompt travels over stdin and the HTTP
body, never argv, so a two-hour transcript (about 100 KB) plus the Linear
backlog cannot hit Linux's 128 KiB single-argument ceiling. That ceiling is
what produced `OSError: [Errno 7] Argument list too long` on the live box.
"""
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "contrib" / "hermes-remote"


class _Recorder(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _Recorder.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        out = json.dumps({"text": "OK from server", "via": "test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        return


class TestHermesRemote(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Recorder)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Recorder.seen.clear()
        self.env = dict(os.environ)
        self.env["HERMES_LLM_URL"] = f"http://127.0.0.1:{self.port}/llm/generate"
        self.env["HERMES_LLM_TOKEN"] = "test-token"

    def _run(self, args, stdin=None):
        return subprocess.run([sys.executable, str(WRAPPER), *args], input=stdin,
                              capture_output=True, text=True, timeout=30, env=self.env)

    def test_argv_prompt_still_works(self):
        proc = self._run(["-m", "some/model", "-z", "hello there"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "OK from server")
        body = _Recorder.seen[0]["body"]
        self.assertEqual(body["prompt"], "hello there")
        self.assertEqual(body["model"], "some/model")
        self.assertEqual(_Recorder.seen[0]["auth"], "Bearer test-token")

    def test_stdin_prompt_carries_a_prompt_far_past_the_argv_ceiling(self):
        prompt = "Speaker A: we discussed the roadmap. " * 12000  # ~450 KB, well past 128 KiB
        proc = self._run(["-m", "some/model", "-z", "-"], stdin=prompt)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "OK from server")
        self.assertEqual(_Recorder.seen[0]["body"]["prompt"], prompt)

    def test_stdin_mode_with_empty_stdin_is_an_error_not_an_empty_prompt(self):
        proc = self._run(["-m", "some/model", "-z", "-"], stdin="")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("empty prompt", proc.stderr)
        self.assertEqual(_Recorder.seen, [])

    def test_missing_token_fails_before_any_request(self):
        self.env.pop("HERMES_LLM_TOKEN")
        proc = self._run(["-m", "some/model", "-z", "hello"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("HERMES_LLM_TOKEN", proc.stderr)
        self.assertEqual(_Recorder.seen, [])


if __name__ == "__main__":
    unittest.main()
