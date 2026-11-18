"""Isolated application fixtures for the schema 2 Mininet validation."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import sys
import time


def slow_http():
    class Response(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "10")
            self.end_headers()
            print(json.dumps({"event": "headers", "at_unix_ns": time.time_ns()}), flush=True)
            for offset in range(10):
                time.sleep(0.15)
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except OSError:
                    print(json.dumps({"event": "closed", "bytes_before_error": offset,
                                      "at_unix_ns": time.time_ns()}), flush=True)
                    return
                print(json.dumps({"event": "body_byte", "offset": offset,
                                  "at_unix_ns": time.time_ns()}), flush=True)

        def log_message(self, _format, *_args):
            pass

    with HTTPServer(("10.0.40.10", 80), Response) as server:
        server.timeout = 3
        print('{"event": "ready"}', file=sys.stderr, flush=True)
        server.handle_request()


def duplicate_udp():
    # This test-only socket adapter emits one additional copy of sequence zero.
    sys.path.insert(0, str(Path.cwd()))
    from traffic import __main__ as cli, workloads

    real_socket = socket.socket

    class DuplicateSocket(real_socket):
        def sendto(self, data, address):
            sent = super().sendto(data, address)
            if workloads.UDP_HEADER.unpack_from(data)[1] == 0:
                duplicate = super().sendto(data, address)
                print(json.dumps({"event": "fixture_duplicate", "sequence": 0,
                                  "bytes": duplicate, "at_unix_ns": time.time_ns()}),
                      file=sys.stderr, flush=True)
            return sent

    socket.socket = DuplicateSocket
    try:
        return cli.main(["udp-send", *sys.argv[2:]])
    finally:
        socket.socket = real_socket


if __name__ == "__main__":
    if sys.argv[1] == "slow-http":
        slow_http()
    else:
        sys.exit(duplicate_udp())
