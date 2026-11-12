"""The controller behind ``RecurserApp``.

It reads each RecurserApp, checks what the spec refers to (the ConfigMap with ``config.yaml``, the Secret with
the Anthropic credential, the Secret with ``users.json``), and generates the Deployment and the Service, with
the volume claim, the NetworkPolicy and the optional Ingress.  What it finds is written to the object's
status, so ``kubectl get recurserapps`` says whether a deployment is ready and, when it is not, why.

    python -m recurser.operator --namespace recurser

Needs ``pip install kubernetes``.  The decisions live in ``reconcile``, which talks to the cluster through a
four-method interface, so they are tested without a cluster.
"""
from __future__ import annotations

import argparse
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

    problems = k8s.validate_spec(app.get("spec"))
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

    for obj in k8s.render(name, namespace, app["spec"], owner=k8s.owner_reference(app), checksums=checksums):
        cluster.apply(obj)

    deployment = cluster.get("apps/v1", "Deployment", namespace, name) or {}
    if (deployment.get("status") or {}).get("availableReplicas", 0) >= 1:
        note = "" if spec["secretRef"]["name"] else " (no secretRef: the assistant is off)"
        return finish(True, "Available", "the Deployment and the Service are in place and the pod is ready" + note)
    return finish(False, "Progressing", "the Deployment and the Service are in place; the pod is not ready yet")


class KubeCluster:
    """The four calls ``reconcile`` needs, on a real cluster through the official Python client."""

    def __init__(self):
        from kubernetes import client, config, dynamic

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.dyn = dynamic.DynamicClient(client.ApiClient())
        self._cache: dict = {}

    def _resource(self, api_version: str, kind: str):
        key = (api_version, kind)
        if key not in self._cache:
            self._cache[key] = self.dyn.resources.get(api_version=api_version, kind=kind)
        return self._cache[key]

    def get(self, api_version: str, kind: str, namespace: str, name: str) -> dict | None:
        from kubernetes.dynamic.exceptions import NotFoundError

        try:
            return self._resource(api_version, kind).get(name=name, namespace=namespace).to_dict()
        except NotFoundError:
            return None

    def apply(self, obj: dict) -> None:
        self._resource(obj["apiVersion"], obj["kind"]).server_side_apply(
            body=obj, name=obj["metadata"]["name"], namespace=obj["metadata"]["namespace"],
            field_manager=k8s.MANAGER, force_conflicts=True)

    def set_status(self, app: dict, status: dict) -> None:
        self._resource(k8s.API_VERSION, k8s.KIND).status.patch(
            body={"status": status}, name=app["metadata"]["name"], namespace=app["metadata"]["namespace"],
            content_type="application/merge-patch+json")

    def list_apps(self, namespace: str) -> list[dict]:
        return [item.to_dict() for item in self._resource(k8s.API_VERSION, k8s.KIND).get(namespace=namespace).items]

    def watch_apps(self, namespace: str, timeout: int):
        for event in self._resource(k8s.API_VERSION, k8s.KIND).watch(namespace=namespace, timeout=timeout):
            yield event["type"], event["raw_object"]


def run(cluster, namespace: str, resync: int = 30, stop: threading.Event | None = None, log=print) -> None:
    """Reconcile everything, then follow changes; every ``resync`` seconds look at everything again, which is
    also how a pod becoming ready reaches the status."""
    stop = stop or threading.Event()

    def safe(app: dict) -> None:
        try:
            status = reconcile(cluster, app)
            cond = status["conditions"][0]
            log(f"{app['metadata']['name']}: Ready={cond['status']} ({cond['reason']})")
        except Exception as exc:   # one bad object must not stop the others
            log(f"{app['metadata'].get('name')}: reconcile failed: {exc}")

    while not stop.is_set():
        try:
            for app in cluster.list_apps(namespace):
                safe(app)
            for kind, app in cluster.watch_apps(namespace, resync):
                if stop.is_set():
                    break
                if kind in ("ADDED", "MODIFIED"):
                    safe(app)
        except Exception as exc:
            log(f"watch interrupted: {exc}")
            stop.wait(5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recurser.operator", description="Controller for RecurserApp objects.")
    parser.add_argument("--namespace", required=True, help="the namespace to watch")
    parser.add_argument("--resync", type=int, default=30, help="seconds between full passes")
    args = parser.parse_args(argv)
    try:
        cluster = KubeCluster()
    except ImportError:
        sys.exit("the controller needs the kubernetes package: pip install kubernetes")
    print(f"watching {k8s.KIND} objects in namespace {args.namespace}", flush=True)
    run(cluster, args.namespace, args.resync, log=lambda line: print(line, flush=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
