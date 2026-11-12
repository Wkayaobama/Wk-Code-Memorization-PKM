"""A local stand-in for the Kubernetes API server, for the controller's tests.

It speaks just enough of the API for the official Python client to do what the controller does:
discovery, reading one object, listing and watching a collection, server-side apply, a merge patch
of the status subresource, and deletion.  Objects live in a dictionary.  It insists on what a real
server insists on for those requests (an apply names its field manager and uses the apply content
type; a custom resource takes no strategic merge patch; a watch is a chunked stream of JSON lines)
and records every request, so that a test can look at what the client really sent.

It is not a cluster: no admission, no validation of the objects, no controllers, no pods.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BUILT_IN = {
    "v1": {"configmaps": "ConfigMap", "secrets": "Secret", "services": "Service", "persistentvolumeclaims": "PersistentVolumeClaim"},
    "apps/v1": {"deployments": "Deployment"},
    "networking.k8s.io/v1": {"networkpolicies": "NetworkPolicy", "ingresses": "Ingress"},
}
VERBS = ["create", "delete", "get", "list", "patch", "update", "watch"]
APPLY, MERGE = "application/apply-patch+yaml", "application/merge-patch+json"


def _merge(old, patch):
    """JSON merge patch: mappings merge, null removes, everything else replaces."""
    if not isinstance(patch, dict):
        return patch
    out = dict(old) if isinstance(old, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = _merge(out.get(key), value)
    return out


class FakeKubeApi:
    def __init__(self, custom: tuple[str, str, str]):
        """``custom`` is (group/version, plural, kind): one custom resource, with a status subresource."""
        self.kinds = {gv: dict(plurals) for gv, plurals in BUILT_IN.items()}
        self.kinds.setdefault(custom[0], {})[custom[1]] = custom[2]
        self.custom = (custom[0], custom[1])
        self.objects: dict[tuple, dict] = {}       # (group/version, plural, namespace, name) -> object
        self.requests: list[dict] = []             # {"method", "path", "query", "content_type", "body"}
        self.silent_watch = 0.0                    # seconds a watch stays open without sending a byte, then is cut
        self.refuse: dict[tuple, tuple] = {}       # (method, plural) -> (code, reason, message): answered instead
        self._lock = threading.Lock()
        self._version = 100
        self._events: list[tuple] = []             # (group/version, plural, type, object), in order
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _json(self, code: int, payload: dict) -> None:
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _failure(self, code: int, reason: str, message: str) -> None:
                self._json(code, {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure", "message": message,
                                  "reason": reason, "code": code})

            def _handle(self, method: str) -> None:
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
                body = json.loads(raw.decode("utf-8")) if raw else None       # the client sends JSON, which is YAML too
                fake.requests.append({"method": method, "path": url.path, "query": query, "content_type": content_type, "body": body})
                parts = [p for p in url.path.split("/") if p]
                if url.path == "/version":
                    return self._json(200, {"major": "1", "minor": "31", "gitVersion": "v1.31.0", "platform": "linux/amd64"})
                if url.path == "/api":
                    return self._json(200, {"kind": "APIVersions", "versions": ["v1"]})
                if url.path == "/apis":
                    groups = [{"name": gv.split("/")[0], "versions": [{"groupVersion": gv, "version": gv.split("/")[1]}],
                               "preferredVersion": {"groupVersion": gv, "version": gv.split("/")[1]}} for gv in fake.kinds if "/" in gv]
                    return self._json(200, {"kind": "APIGroupList", "apiVersion": "v1", "groups": groups})
                if parts[:1] == ["api"] and len(parts) >= 2:
                    gv, rest = parts[1], parts[2:]
                elif parts[:1] == ["apis"] and len(parts) >= 3:
                    gv, rest = parts[1] + "/" + parts[2], parts[3:]
                else:
                    return self._failure(404, "NotFound", "the server could not find the requested resource")
                if gv not in fake.kinds:
                    return self._failure(404, "NotFound", "the server could not find the requested resource")
                if not rest:                                                  # discovery of one group and version
                    resources = []
                    for plural, kind in fake.kinds[gv].items():
                        resources.append({"name": plural, "singularName": kind.lower(), "namespaced": True, "kind": kind, "verbs": VERBS})
                        if (gv, plural) == fake.custom:
                            resources.append({"name": plural + "/status", "singularName": "", "namespaced": True, "kind": kind,
                                              "verbs": ["get", "patch", "update"]})
                    return self._json(200, {"kind": "APIResourceList", "apiVersion": "v1", "groupVersion": gv, "resources": resources})
                if rest[0] != "namespaces" or len(rest) < 3 or rest[2] not in fake.kinds[gv]:
                    return self._failure(404, "NotFound", "the server could not find the requested resource")
                namespace, plural = rest[1], rest[2]
                name = rest[3] if len(rest) > 3 else None
                sub = rest[4] if len(rest) > 4 else None
                kind = fake.kinds[gv][plural]
                if (method, plural) in fake.refuse:
                    return self._failure(*fake.refuse[(method, plural)])
                key = (gv, plural, namespace, name)
                if method == "GET" and name is None:
                    if str((query.get("watch") or ["false"])[0]).lower() in ("true", "1"):
                        return self._watch(gv, plural, namespace, float((query.get("timeoutSeconds") or ["5"])[0]))
                    with fake._lock:
                        items = [o for (g, p, ns, _), o in sorted(fake.objects.items()) if (g, p, ns) == (gv, plural, namespace)]
                        return self._json(200, {"apiVersion": gv, "kind": kind + "List", "metadata": {"resourceVersion": str(fake._version)}, "items": items})
                with fake._lock:
                    existing = fake.objects.get(key)
                    if method == "GET":
                        if existing is None:
                            return self._failure(404, "NotFound", f'{plural} "{name}" not found')
                        return self._json(200, existing)
                    if method == "DELETE":
                        if existing is None:
                            return self._failure(404, "NotFound", f'{plural} "{name}" not found')
                        del fake.objects[key]
                        fake._events.append((gv, plural, "DELETED", existing))
                        return self._json(200, {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Success",
                                                "details": {"name": name, "kind": plural}})
                    if method != "PATCH":
                        return self._failure(405, "MethodNotAllowed", "the server does not allow this method on the requested resource")
                    if sub == "status":
                        if (gv, plural) != fake.custom:
                            return self._failure(404, "NotFound", "the server could not find the requested resource")
                        if content_type != MERGE:      # a custom resource takes no strategic merge patch
                            return self._failure(415, "UnsupportedMediaType", f"the body of the request was in an unknown format - accepted media types include: application/json-patch+json, {MERGE}, {APPLY}")
                        if existing is None:
                            return self._failure(404, "NotFound", f'{plural} "{name}" not found')
                        updated = dict(existing, status=_merge(existing.get("status"), (body or {}).get("status")))
                        return self._json(200, fake._store(key, updated, "MODIFIED"))
                    if sub is not None:
                        return self._failure(404, "NotFound", "the server could not find the requested resource")
                    if content_type != APPLY:
                        return self._failure(415, "UnsupportedMediaType", "this stand-in only takes apply patches on objects")
                    manager = (query.get("fieldManager") or [""])[0]
                    if not manager:
                        return self._failure(400, "BadRequest", 'PatchOptions.meta.k8s.io "" is invalid: fieldManager: Required value: is required for apply patch')
                    if not isinstance(body, dict) or body.get("apiVersion") != gv or body.get("kind") != kind:
                        return self._failure(400, "BadRequest", "the object in the body does not match the kind in the URL")
                    if (body.get("metadata") or {}).get("name") != name:
                        return self._failure(400, "BadRequest", "the name of the object does not match the name on the URL")
                    applied = dict(body, metadata=dict(body["metadata"], namespace=namespace, managedFields=[{"manager": manager, "operation": "Apply"}]))
                    if existing is not None and "status" in existing:           # an apply of the object never writes the status
                        applied["status"] = existing["status"]
                    code = 200 if existing is not None else 201
                    return self._json(code, fake._store(key, applied, "MODIFIED" if existing is not None else "ADDED"))

            def _watch(self, gv: str, plural: str, namespace: str, seconds: float) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True

                def send(kind: str, obj: dict) -> None:
                    line = (json.dumps({"type": kind, "object": obj}) + "\n").encode("utf-8")
                    self.wfile.write(b"%x\r\n" % len(line) + line + b"\r\n")
                    self.wfile.flush()

                try:
                    if fake.silent_watch:                                     # a connection that died without a word
                        time.sleep(fake.silent_watch)
                        return
                    with fake._lock:
                        seen = len(fake._events)
                        current = [o for (g, p, ns, _), o in sorted(fake.objects.items()) if (g, p, ns) == (gv, plural, namespace)]
                    for obj in current:                                       # no resourceVersion given: the present state first
                        send("ADDED", obj)
                    until = time.monotonic() + seconds
                    while time.monotonic() < until:
                        with fake._lock:
                            fresh, seen = fake._events[seen:], len(fake._events)
                        for g, p, kind, obj in fresh:
                            if (g, p) == (gv, plural) and obj["metadata"].get("namespace") == namespace:
                                send(kind, obj)
                        time.sleep(0.02)
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:                                               # the client hung up
                    pass

            def do_GET(self):
                self._handle("GET")

            def do_PATCH(self):
                self._handle("PATCH")

            def do_DELETE(self):
                self._handle("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _store(self, key: tuple, obj: dict, event: str) -> dict:
        """Save under a new resource version (the caller holds the lock) and return what was saved."""
        self._version += 1
        previous = self.objects.get(key) or {}
        meta = dict(obj.get("metadata") or {})
        meta["uid"] = (previous.get("metadata") or {}).get("uid") or f"uid-{self._version}"
        meta["resourceVersion"] = str(self._version)
        meta.setdefault("generation", 1)
        saved = dict(obj, metadata=meta)
        self.objects[key] = saved
        self._events.append((key[0], key[1], event, saved))
        return saved

    def put(self, obj: dict) -> dict:
        """Create or replace an object directly, as ``kubectl apply`` by a person would."""
        gv = obj["apiVersion"]
        plural = next(p for p, kind in self.kinds[gv].items() if kind == obj["kind"])
        key = (gv, plural, obj["metadata"]["namespace"], obj["metadata"]["name"])
        with self._lock:
            return self._store(key, obj, "MODIFIED" if key in self.objects else "ADDED")

    def get(self, gv: str, plural: str, namespace: str, name: str) -> dict | None:
        with self._lock:
            return self.objects.get((gv, plural, namespace, name))

    def sent(self, method: str, ending: str = "") -> list[dict]:
        """The recorded requests with this method whose path ends with ``ending``."""
        return [r for r in self.requests if r["method"] == method and r["path"].endswith(ending)]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
