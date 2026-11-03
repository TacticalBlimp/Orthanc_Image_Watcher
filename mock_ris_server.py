#!/usr/bin/env python3
"""A stand-in for the RIS API, for demos and tests.

POST /api/ris/study-ready stores the notification it receives; any GET lists everything
received so far. Set `fail = True` to make it answer 503 instead of accepting.
"""
import argparse
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("mock-ris")
POST_PATH = "/api/ris/study-ready"


class MockRIS:
    """HTTP server that keeps the notifications it receives in `self.received`.
    Call start() and stop() from tests, or run this file to serve until Ctrl+C."""

    def __init__(self, port=0):
        self.received = []
        self.fail = False
        ris = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path != POST_PATH:
                    self.reply(404, {"error": "unknown endpoint"})
                elif ris.fail:
                    self.reply(503, {"error": "RIS unavailable (simulated)"})
                else:
                    ris.received.append(payload)
                    log.info("Received %s", payload)
                    self.reply(200, {"status": "accepted"})

            def do_GET(self):
                self.reply(200, ris.received)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}{POST_PATH}"

    def start(self):
        """Serve in a background thread and return self, so tests can chain it.
        The short poll interval makes stop() return quickly."""
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        return self

    def stop(self):
        """Stop the server and release its port. shutdown() blocks until the serving loop
        has exited, which the short poll interval keeps well under a tenth of a second."""
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = MockRIS(parser.parse_args().port)
    log.info("Mock RIS listening on %s", server.url)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
