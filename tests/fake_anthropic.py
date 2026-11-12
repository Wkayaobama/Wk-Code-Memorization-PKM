"""A local stand-in for the Anthropic Messages API, for tests.  It speaks the streaming protocol the SDK
reads (message_start ... message_stop), records every request, and can be told to refuse or to fail.

Two ways to shape an answer:

- ``fake.mode``, for every request: answer | refusal | status:<code> | midstream_error | error_text | garbage |
  cut | thinking_only | pause | pings:<seconds>
- a plan written in the question itself, between ``[[`` and ``]]``, for that request only, served as a chunked
  HTTP/1.1 stream on a connection that stays open afterwards, as the real API does:

      [[id=B; ping:5:0.1; text:3:0.05; burst:2000; silence:2; end]]

      ping:N:S     N keep-alives, S seconds before each
      text:N:S     N pieces of text "<id>-<i> ", S seconds before each
      burst:N      N pieces of text written at once
      silence:S    S seconds without a byte
      mute:S       no answer at all for S seconds, not even a status line; then the connection is closed
      end          close the answer properly (without it the body just ends)

  The text of a complete answer is "<id>-0 <id>-1 ...", so a caller can tell whose answer it received.

``FakeAnthropic(tls=(certificate, key))`` serves over TLS; ``make_certificate(folder)`` makes a throwaway pair.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_certificate(folder: str):
    """A self-signed certificate for 127.0.0.1 that lasts two days, made with the openssl command.
    Returns (certificate path, key path), or None where that command is not available."""
    if not shutil.which("openssl"):
        return None
    cert, key = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    made = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
                           "-keyout", key, "-out", cert, "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                          capture_output=True)
    return (cert, key) if made.returncode == 0 else None


def expected_text(ident: str, pieces: int) -> str:
    """What a complete planned answer with this many pieces of text reads."""
    return "".join(f"{ident}-{i} " for i in range(pieces))


class FakeAnthropic:
    def __init__(self, tls: tuple[str, str] | None = None):
        self.requests: list[dict] = []        # {"path", "headers", "body"}
        self.hung_up = False                  # True once a client closed an answer before its end
        self.connections = 0                  # how many connections were opened
        self.mode = "answer"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def setup(self):
                super().setup()
                fake.connections += 1

            def chunk(self, data: bytes) -> None:
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()

            def event(self, event: dict) -> None:
                self.chunk(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8"))

            def planned(self, steps: list[str], model: str) -> None:
                ident = next((s[3:] for s in steps if s.startswith("id=")), "anon")
                for step in steps:
                    if step.startswith("mute:"):
                        time.sleep(float(step.split(":")[1]))
                        self.close_connection = True
                        return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                count, opened = 0, False

                def piece() -> dict:
                    nonlocal count, opened
                    if not opened:
                        self.event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                        opened = True
                    count += 1
                    return {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": f"{ident}-{count - 1} "}}

                try:
                    self.event({"type": "message_start", "message": {"id": "msg_" + ident, "type": "message", "role": "assistant", "model": model,
                                                                      "content": [], "stop_reason": None, "stop_sequence": None,
                                                                      "usage": {"input_tokens": 10, "output_tokens": 1}}})
                    for step in steps:
                        name, *args = step.split(":")
                        if name == "ping":
                            for _ in range(int(args[0])):
                                time.sleep(float(args[1]))
                                self.event({"type": "ping"})
                        elif name == "text":
                            for _ in range(int(args[0])):
                                time.sleep(float(args[1]))
                                self.event(piece())
                        elif name == "burst":
                            parts = []
                            for _ in range(int(args[0])):
                                event = piece()
                                data = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8")
                                parts.append(b"%x\r\n" % len(data) + data + b"\r\n")
                            self.wfile.write(b"".join(parts))
                            self.wfile.flush()
                        elif name == "silence":
                            time.sleep(float(args[0]))
                        elif name == "end":
                            if not opened:
                                self.event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                            self.event({"type": "content_block_stop", "index": 0})
                            self.event({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": count}})
                            self.event({"type": "message_stop"})
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (OSError, ssl.SSLError):                  # the client hung up
                    fake.hung_up = True
                    self.close_connection = True

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
                fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                texts = []
                for message in body.get("messages", []):
                    content = message.get("content")
                    for block in content if isinstance(content, list) else [{"type": "text", "text": content}]:
                        if isinstance(block, dict) and block.get("type") == "text":
                            texts.append(block.get("text") or "")
                plan = re.search(r"\[\[(.*?)\]\]", "\n".join(texts), re.S)
                if plan:
                    return self.planned([s.strip() for s in plan.group(1).split(";") if s.strip()], body.get("model", "claude-opus-5-5"))
                if fake.mode.startswith("status:"):
                    code = int(fake.mode.split(":")[1])
                    kind = {401: "authentication_error", 404: "not_found_error", 429: "rate_limit_error"}.get(code, "api_error")
                    payload = json.dumps({"type": "error", "error": {"type": kind, "message": "simulated"}}).encode()
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    if code == 429:
                        self.send_header("retry-after", "7")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if fake.mode.startswith("pings:"):       # an answer that starts, then sends only keep-alives for a while
                    def send(event):
                        self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
                        self.wfile.flush()

                    seconds, mode = float(fake.mode.split(":")[1]), fake.mode
                    fake.mode = "answer"
                    events = fake.events(body)
                    fake.mode = mode
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()                   # no length: the body ends when the connection closes
                    self.close_connection = True
                    try:
                        send(events[0])
                        until = time.monotonic() + seconds
                        while time.monotonic() < until:
                            send({"type": "ping"})
                            time.sleep(0.1)
                        for event in events[1:]:
                            send(event)
                    except OSError:                      # the client hung up
                        fake.hung_up = True
                    return
                events = fake.events(body)
                if fake.mode == "garbage":               # an ordinary event whose data is not JSON
                    data = ("".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events[:2])
                            + "event: content_block_delta\ndata: this is not JSON\n\n").encode("utf-8")
                else:
                    data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):     # a client that hangs up is part of these tests
                if not isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError)):
                    super().handle_error(request, client_address)

        self.httpd = Server(("127.0.0.1", 0), Handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.url = ("https" if tls else "http") + f"://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def events(self, body: dict) -> list[dict]:
        model = body.get("model", "claude-opus-5-5")
        start = {"type": "message_start", "message": {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [],
                                                      "stop_reason": None, "stop_sequence": None,
                                                      "usage": {"input_tokens": 120, "output_tokens": 1}}}
        if self.mode == "refusal":
            return [start,
                    {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_sequence": None,
                                                        "stop_details": {"type": "refusal", "category": None, "explanation": "simulated"}},
                     "usage": {"output_tokens": 1}},
                    {"type": "message_stop"}]
        opening = [start,
                   {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Half an ans"}}]
        if self.mode == "midstream_error":       # the usual shape of an overload after the stream has opened
            return opening + [{"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}]
        if self.mode == "error_text":            # an error event whose "error" is a sentence, not an object
            return opening + [{"type": "error", "error": "upstream request timeout"}]
        if self.mode == "cut":                   # the body simply ends: no stop reason ever arrives
            return opening
        if self.mode == "thinking_only":         # the whole budget went to thinking
            return [start,
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "max_tokens", "stop_sequence": None}, "usage": {"output_tokens": 4000}},
                    {"type": "message_stop"}]
        results = [b for b in body["messages"][0]["content"] if isinstance(b, dict) and b.get("type") == "search_result"]
        events = [start,
                  {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                  {"type": "content_block_stop", "index": 0}]
        if results:        # a cited sentence resting on the first passage, then an uncited one
            first = results[0]
            events += [
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "", "citations": []}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "citations_delta", "citation": {
                    "type": "search_result_location", "cited_text": first["content"][0]["text"], "source": first["source"], "title": first["title"],
                    "search_result_index": 0, "start_block_index": 0, "end_block_index": 1}}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Grounded sentence."}},
                {"type": "content_block_stop", "index": 1},
                {"type": "content_block_start", "index": 2, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": " A closing remark."}},
                {"type": "content_block_stop", "index": 2}]
        else:
            events += [
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "partly. You named the loader but missed the batch key."}},
                {"type": "content_block_stop", "index": 1}]
        events += [{"type": "message_delta", "delta": {"stop_reason": "pause_turn" if self.mode == "pause" else "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 42}},
                   {"type": "message_stop"}]
        return events
