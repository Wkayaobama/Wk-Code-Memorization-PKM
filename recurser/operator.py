"""The controller behind ``RecurserApp``.

It reads each RecurserApp, checks what the spec refers to (the ConfigMap with ``config.yaml``, the Secret with
the Anthropic credential, the Secret with ``users.json``), and generates the Deployment and the Service, with
the volume claim, the NetworkPolicy and the optional Ingress.  What it finds is written to the object's
status, so ``kubectl get recurserapps`` says whether a deployment is ready and, when it is not, why.

    python -m recurser.operator --namespace recurser

Needs ``pip install kubernetes``.  The decisions live in ``reconcile``, which talks to the cluster through a
small interface (get, apply, delete, set_status, list and watch), so they are tested without a cluster.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone

try:
    from . import config as config_mod, k8s
except ImportError:  # pragma: no cover
    import config as config_mod, k8s  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _condition(ok: bool, reason: str, message: str, previous: dict | None, generation) -> dict:
    status = "True" if ok else "False"
    last = previous.get("lastTransitionTime") if previous and previous.get("status") == status else None
    return {"type": "Ready", "status": status, "reason": reason, "message": message, "observedGeneration": generation,
            "lastTransitionTime": last or _now()}


def _why(exc: Exception) -> str:
    """Why a call to the cluster failed, on one line: the API's own sentence when the error carries one."""
    body = getattr(exc, "body", None)
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    if isinstance(body, str):
        try:
            message = json.loads(body).get("message")
        except (ValueError, AttributeError):
            message = None
        if message:
            return " ".join(str(message).split())[:300]
    return " ".join(str(exc).split())[:300]


def _parse_config(text: str):
    """The settings document inside the ConfigMap, checked the way the server will check it."""
    return config_mod.merge(config_mod._parse(text, k8s.CONFIG_KEY))


def reconcile(cluster, app: dict) -> dict:
    """Bring the cluster in line with one RecurserApp.  Returns the status that was (or would be) written."""
    meta = app["metadata"]
    name, namespace, generation = meta["name"], meta["namespace"], meta.get("generation")
    previous = next((c for c in (app.get("status") or {}).get("conditions") or [] if c.get("type") == "Ready"), None)

    def finish(ok: bool, reason: str, message: str) -> dict:
        status = {"observedGeneration": generation, "url": f"http://{name}.{namespace}.svc",
                  "conditions": [_condition(ok, reason, message, previous, generation)]}
        same = previous and all(previous.get(k) == status["conditions"][0].get(k) for k in ("status", "reason", "message", "observedGeneration"))
        if not same or (app.get("status") or {}).get("observedGeneration") != generation:
            cluster.set_status(app, status)
        return status

    problems = k8s.validate_name(name) + k8s.validate_spec(app.get("spec"))
    if problems:
        return finish(False, "InvalidSpec", "; ".join(problems[:4]))
    spec = k8s.with_defaults(app["spec"])
    checksums = {}

    config_map = cluster.get("v1", "ConfigMap", namespace, spec["configRef"]["name"])
    if config_map is None:
        return finish(False, "ConfigMissing", f"ConfigMap {spec['configRef']['name']} was not found")
    if k8s.CONFIG_KEY not in (config_map.get("data") or {}):
        return finish(False, "ConfigKeyMissing", f"ConfigMap {spec['configRef']['name']} has no key {k8s.CONFIG_KEY}")
    try:
        _parse_config(config_map["data"][k8s.CONFIG_KEY])
    except config_mod.ConfigError as exc:
        return finish(False, "ConfigInvalid", str(exc))
    checksums["config"] = config_map["metadata"].get("resourceVersion")

    for ref, key, missing, key_missing in ((spec["secretRef"], spec["secretRef"].get("key") or k8s.API_KEY_ENV, "SecretMissing", "SecretKeyMissing"),
                                           (spec["usersRef"], k8s.USERS_KEY, "UsersMissing", "UsersKeyMissing")):
        if not ref["name"]:
            continue
        secret = cluster.get("v1", "Secret", namespace, ref["name"])
        if secret is None:
            return finish(False, missing, f"Secret {ref['name']} was not found")
        if key not in (secret.get("data") or {}):          # only the key names are looked at, never a value
            return finish(False, key_missing, f"Secret {ref['name']} has no key {key}")
        checksums[ref["name"]] = secret["metadata"].get("resourceVersion")

    rendered = k8s.render(name, namespace, app["spec"], owner=k8s.owner_reference(app), checksums=checksums)
    refused = []
    for obj in rendered:              # one refused object (a volume claim asked to shrink) must not hold the others back
        try:
            cluster.apply(obj)
        except Exception as exc:
            refused.append(f"{obj['kind']} {obj['metadata']['name']} was refused: {_why(exc)}")
    wanted = {(o["apiVersion"], o["kind"]) for o in rendered}
    for api_version, kind in k8s.OPTIONAL_KINDS:      # an Ingress or NetworkPolicy the spec no longer asks for
        if (api_version, kind) in wanted:
            continue
        stale = cluster.get(api_version, kind, namespace, name)
        owners = (stale or {}).get("metadata", {}).get("ownerReferences") or []
        if stale and meta.get("uid") and any(o.get("uid") == meta["uid"] for o in owners):       # only what this RecurserApp created
            try:
                cluster.delete(api_version, kind, namespace, name)
            except Exception as exc:
                refused.append(f"{kind} {name} could not be removed: {_why(exc)}")
    if refused:
        return finish(False, "ApplyFailed", "; ".join(refused[:3]))

    deployment = cluster.get("apps/v1", "Deployment", namespace, name) or {}
    if (deployment.get("status") or {}).get("availableReplicas", 0) >= 1:
        note = "" if spec["secretRef"]["name"] else " (no secretRef: the assistant is off)"
        return finish(True, "Available", "the Deployment and the Service are in place and the pod is ready" + note)
    return finish(False, "Progressing", "the Deployment and the Service are in place; the pod is not ready yet")


class KubeCluster:
    """The calls ``reconcile`` needs, on a real cluster through the official Python client.  Every call has a
    time limit: a connection that died without a word must not stop the controller for good."""

    TIMEOUT = (5, 30)          # seconds to connect, seconds to read
    WATCH_MARGIN = 15          # seconds of silence tolerated after the moment the server was to end the watch

    def __init__(self, api_client=None):
        """``api_client``: a configured ``kubernetes.client.ApiClient``; by default the pod's own service
        account, or the current kubectl context outside a cluster."""
        from kubernetes import client, config, dynamic

        if api_client is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            api_client = client.ApiClient()
        self.dyn = dynamic.DynamicClient(api_client)
        self._cache: dict = {}

    def _resource(self, api_version: str, kind: str):
        key = (api_version, kind)
        if key not in self._cache:
            self._cache[key] = self.dyn.resources.get(api_version=api_version, kind=kind)
        return self._cache[key]

    def get(self, api_version: str, kind: str, namespace: str, name: str) -> dict | None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            return self._resource(api_version, kind).get(name=name, namespace=namespace, _request_timeout=self.TIMEOUT).to_dict()
        except NotFoundError:
            return None

    def apply(self, obj: dict) -> None:
        self._resource(obj["apiVersion"], obj["kind"]).server_side_apply(
            body=obj, name=obj["metadata"]["name"], namespace=obj["metadata"]["namespace"],
            field_manager=k8s.MANAGER, force_conflicts=True, _request_timeout=self.TIMEOUT)

    def delete(self, api_version: str, kind: str, namespace: str, name: str) -> None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            self._resource(api_version, kind).delete(name=name, namespace=namespace, _request_timeout=self.TIMEOUT)
        except NotFoundError:
            pass

    def set_status(self, app: dict, status: dict) -> None:
        self._resource(k8s.API_VERSION, k8s.KIND).status.patch(
            body={"status": status}, name=app["metadata"]["name"], namespace=app["metadata"]["namespace"],
            content_type="application/merge-patch+json", _request_timeout=self.TIMEOUT)

    def list_apps(self, namespace: str) -> list[dict]:
        found = self._resource(k8s.API_VERSION, k8s.KIND).get(namespace=namespace, _request_timeout=self.TIMEOUT)
        return [item.to_dict() for item in found.items]

    def watch_apps(self, namespace: str, timeout: int):
        from kubernetes import watch

        # the server ends the watch after ``timeout`` seconds; the read limit a little beyond it catches a dead connection
        stream = watch.Watch().stream(self._resource(k8s.API_VERSION, k8s.KIND).get, namespace=namespace, serialize=False,
                                      timeout_seconds=timeout, _request_timeout=(self.TIMEOUT[0], timeout + self.WATCH_MARGIN))
        for event in stream:
            yield event["type"], event["raw_object"]


def run(cluster, namespace: str, resync: int = 30, stop: threading.Event | None = None, log=print, pause: float = 5) -> None:
    """Reconcile everything, then follow changes; every ``resync`` seconds look at everything again, which is
    also how a pod becoming ready reaches the status.  After a failed pass, wait ``pause`` seconds."""
    stop = stop or threading.Event()

    def safe(app: dict) -> None:
        try:
            status = reconcile(cluster, app)
            cond = status["conditions"][0]
            log(f"{app['metadata']['name']}: Ready={cond['status']} ({cond['reason']})")
        except Exception as exc:   # one bad object must not stop the others
            log(f"{app['metadata'].get('name')}: reconcile failed: {_why(exc)}")

    def alive() -> None:          # the liveness probe reads the age of this file
        try:
            with open(os.environ.get("RECURSER_ALIVE_FILE", k8s.ALIVE_FILE), "w") as fh:
                fh.write(_now())
        except OSError:
            pass

    while not stop.is_set():
        try:
            alive()
            for app in cluster.list_apps(namespace):
                safe(app)
            alive()
            for kind, app in cluster.watch_apps(namespace, resync):
                if stop.is_set():
                    break
                if kind in ("ADDED", "MODIFIED"):
                    safe(app)
        except Exception as exc:
            log(f"watch interrupted: {_why(exc)}")
            stop.wait(pause)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recurser.operator", description="Controller for RecurserApp objects.")
    parser.add_argument("--namespace", required=True, help="the namespace to watch")
    parser.add_argument("--resync", type=int, default=30, help="seconds between full passes")
    args = parser.parse_args(argv)

    def leave(signum, frame):     # as the first process of a container, a stop request must be handled explicitly
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, leave)
    try:
        cluster = KubeCluster()
    except ImportError:
        sys.exit("the controller needs the kubernetes package: pip install kubernetes")
    print(f"watching {k8s.KIND} objects in namespace {args.namespace}", flush=True)
    run(cluster, args.namespace, args.resync, log=lambda line: print(line, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
