"""A local stand-in for the Anthropic Messages API, for tests.  It speaks the streaming protocol the SDK
reads (message_start ... message_stop), records every request, and can be told to refuse or to fail."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeAnthropic:
    def __init__(self):
        self.requests: list[dict] = []        # {"path", "headers", "body"}
        self.hung_up = False                  # True once a client closed a pings answer before its end
        # answer | refusal | status:<code> | midstream_error | cut | thinking_only | pause | pings:<seconds>
        self.mode = "answer"
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
                fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
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
                    import time

                    def send(event):
                        self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("utf-8"))
                        self.wfile.flush()

                    seconds, mode = float(fake.mode.split(":")[1]), fake.mode
                    fake.mode = "answer"
                    events = fake.events(body)
                    fake.mode = mode
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()                   # no length: the body ends when the connection closes
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
                data = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
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
