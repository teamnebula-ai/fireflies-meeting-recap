#!/usr/bin/env python3
"""receiver.py — thin Fireflies-webhook listener (stdlib only).

Binds 127.0.0.1:<RECAP_RECEIVER_PORT> (default 8765). On a Fireflies webhook
POST it spawns run_recap.py detached and returns 202 immediately. No business
logic lives here — all of it is in run_recap.py (deterministic) + the LLM
generation step. Keep the listener loopback-bound and expose it through a
reverse proxy / tunnel (e.g. Tailscale Funnel, Cloudflare, nginx) with TLS.

  POST /webhooks/fireflies  -> spawn run_recap.py --meeting-id <id> -> 202
  GET  /health              -> 200 {"ok": true}
"""
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_RECAP = HERE / "run_recap.py"
SPAWN_LOG = Path(os.environ.get("RECAP_SPAWN_LOG", os.path.expanduser("~/.recap/meeting-recap.log")))
PORT = int(os.environ.get("RECAP_RECEIVER_PORT", "8765"))


def log(msg):
    sys.stderr.write(f"[recap-receiver] {msg}\n")
    sys.stderr.flush()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/hooks/health"):
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        # Accept the canonical path, a legacy /hooks path, and bare root —
        # some tunnels strip the mounted path prefix before forwarding.
        if self.path.rstrip("/") not in ("/webhooks/fireflies", "/hooks/fireflies", ""):
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, {"error": f"bad body: {e}"})

        mid = (payload.get("meetingId") or payload.get("meeting_id")
               or payload.get("transcript_id") or "").strip()
        event = payload.get("eventType") or payload.get("event") or "Transcription completed"
        if not mid:
            log(f"no meetingId in payload: {str(payload)[:200]}")
            return self._json(202, {"status": "accepted", "note": "no meetingId; ignored"})

        SPAWN_LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = open(SPAWN_LOG, "a")
        fh.write(f"\n===== spawn meeting-id={mid} event={event} =====\n")
        fh.flush()
        subprocess.Popen(
            [sys.executable, str(RUN_RECAP), "--meeting-id", mid, "--event", event],
            stdout=fh, stderr=fh, start_new_session=True, env=dict(os.environ),
        )
        log(f"spawned run_recap for meeting-id={mid} event={event}")
        return self._json(202, {"status": "accepted", "meetingId": mid})

    def log_message(self, *a):  # silence default access logging
        return


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"listening on 127.0.0.1:{PORT} -> {RUN_RECAP}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
