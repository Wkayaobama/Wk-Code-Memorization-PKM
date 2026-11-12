"""Kubernetes objects for a Recurser deployment, generated from one small spec.

The layers, as in the usual "application behind a custom resource" pattern:

1. **Container**: the web server, an API service on port ``8000``.
2. **ConfigMap**: ``config.yaml`` (model, retrieval and access settings), mounted at ``/app/config/config.yaml``.
3. **Secret**: the Anthropic credential, injected as ``ANTHROPIC_API_KEY`` with ``secretKeyRef``; a second
   Secret holds ``users.json`` (who may open the page), mounted as a file.
4. **Service**: port ``80`` to the container's ``8000``; ``kubectl port-forward svc/<name> 8000:80`` for local
   access, with the API described at ``/docs`` and ``/openapi.json``.
5. **Custom resource**: a ``RecurserApp`` names the image, the ``projects`` it serves, ``configRef``, ``secretRef`` and ``usersRef``; the
   controller (``recurser.operator``) validates them and generates the Deployment and the Service, plus the
   volume claim for the review histories, a NetworkPolicy and, when a host is given, an Ingress.

Everything here is a pure function returning plain dicts, so the controller, the ``recurser k8s`` command
and the tests share one source and cannot drift apart.
"""
from __future__ import annotations

import copy
import hashlib
import json

GROUP = "recurser.wkayaobama.github.io"
VERSION = "v1alpha1"
KIND = "RecurserApp"
PLURAL = "recurserapps"
API_VERSION = f"{GROUP}/{VERSION}"
MANAGER = "recurser-controller"

PORT = 8000                      # the container's API port
SERVICE_PORT = 80                # what the Service exposes inside the cluster
CONFIG_DIR = "/app/config"       # the ConfigMap lands here: /app/config/config.yaml
CONFIG_KEY = "config.yaml"
SECRETS_DIR = "/app/secrets"     # the users Secret lands here: /app/secrets/users.json
USERS_KEY = "users.json"
API_KEY_ENV = "ANTHROPIC_API_KEY"
DATA_DIR = "/var/lib/recurser"   # one history file per person, on the volume claim
RUN_AS = 10001

SPEC_DEFAULTS = {
    "image": "",
    "imagePullPolicy": "IfNotPresent",
    "imagePullSecrets": [],
    "projects": [],
    "configRef": {"name": ""},
    "secretRef": {"name": "", "key": API_KEY_ENV},
    "usersRef": {"name": ""},
    "storage": {"size": "1Gi", "storageClassName": "", "existingClaim": ""},
    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "512Mi"}},
    "networkPolicy": {"enabled": True, "ingressNamespaces": []},
    "ingress": {"host": "", "className": "", "tlsSecretName": "", "controllerNamespace": "ingress-nginx"},
}

# Addresses a pod may never reach on port 443: other tenants of the cluster network and the node metadata service.
PRIVATE_RANGES = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10"]


def crd() -> dict:
    """The CustomResourceDefinition for ``RecurserApp``."""
    ref = lambda extra=None: {"type": "object", "required": ["name"],                      # noqa: E731
                              "properties": {"name": {"type": "string", "minLength": 1}, **(extra or {})}}
    quantity = {"type": "string", "pattern": r"^[0-9]+(\.[0-9]+)?(m|k|Ki|Mi|Gi|Ti|M|G|T)?$"}
    spec = {
        "type": "object",
        "required": ["image", "configRef", "usersRef"],     # a shared deployment always names its people
        "properties": {
            "image": {"type": "string", "minLength": 1, "description": "The Recurser image, with the decks this deployment serves."},
            "imagePullPolicy": {"type": "string", "enum": ["Always", "IfNotPresent", "Never"]},
            "imagePullSecrets": {"type": "array", "items": ref()},
            "projects": {"type": "array", "items": {"type": "string", "minLength": 1},
                         "description": "What this deployment serves: project slugs, repository names or two-digit codes. Empty: every project in the image."},
            "configRef": ref() | {"description": f"ConfigMap holding {CONFIG_KEY}; mounted at {CONFIG_DIR}/{CONFIG_KEY}."},
            "secretRef": ref({"key": {"type": "string", "minLength": 1}})
            | {"description": f"Secret holding the Anthropic credential (key {API_KEY_ENV} unless 'key' says otherwise). Without it the assistant is off."},
            "usersRef": ref() | {"description": f"Secret holding {USERS_KEY}: who may open the page and which projects each person sees."},
            "storage": {"type": "object", "properties": {"size": quantity, "storageClassName": {"type": "string"}, "existingClaim": {"type": "string"}}},
            "resources": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
            "networkPolicy": {"type": "object", "properties": {
                "enabled": {"type": "boolean"},
                "ingressNamespaces": {"type": "array", "items": {"type": "string"},
                                      "description": "Namespaces whose pods may call the service, besides its own and the ingress controller's."}}},
            "ingress": {"type": "object", "properties": {
                "host": {"type": "string"}, "className": {"type": "string"}, "tlsSecretName": {"type": "string"},
                "controllerNamespace": {"type": "string"}}},
        },
    }
    status = {"type": "object", "x-kubernetes-preserve-unknown-fields": True}
    return {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": f"{PLURAL}.{GROUP}"},
        "spec": {
            "group": GROUP,
            "scope": "Namespaced",
            "names": {"kind": KIND, "plural": PLURAL, "singular": "recurserapp", "shortNames": ["rapp"]},
            "versions": [{
                "name": VERSION, "served": True, "storage": True,
                "subresources": {"status": {}},
                "additionalPrinterColumns": [
                    {"name": "Ready", "type": "string", "jsonPath": ".status.conditions[?(@.type=='Ready')].status"},
                    {"name": "Reason", "type": "string", "jsonPath": ".status.conditions[?(@.type=='Ready')].reason"},
                    {"name": "Image", "type": "string", "jsonPath": ".spec.image", "priority": 1},
                    {"name": "Age", "type": "date", "jsonPath": ".metadata.creationTimestamp"}],
                "schema": {"openAPIV3Schema": {"type": "object", "required": ["spec"], "properties": {"spec": spec, "status": status}}},
            }],
        },
    }


def with_defaults(spec: dict | None) -> dict:
    out = copy.deepcopy(SPEC_DEFAULTS)
    for key, value in (spec or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def check_schema(value, schema: dict, where: str = "spec") -> list[str]:
    """The structural checks the API server applies with the CRD schema, enough to test examples offline."""
    import re

    problems: list[str] = []
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int}
    if kind and (not isinstance(value, types[kind]) or (kind == "integer" and isinstance(value, bool))):
        return [f"{where}: expected {kind}"]
    if kind == "string":
        if len(value) < schema.get("minLength", 0):
            problems.append(f"{where}: must not be empty")
        if schema.get("enum") and value not in schema["enum"]:
            problems.append(f"{where}: must be one of {', '.join(schema['enum'])}")
        if schema.get("pattern") and not re.match(schema["pattern"], value):
            problems.append(f"{where}: {value!r} does not match {schema['pattern']}")
    if kind == "array":
        for i, item in enumerate(value):
            problems += check_schema(item, schema.get("items", {}), f"{where}[{i}]")
    if kind == "object":
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{where}.{name}: required")
        props = schema.get("properties")
        if props is not None and not schema.get("x-kubernetes-preserve-unknown-fields"):
            for name, item in value.items():
                if name not in props:
                    problems.append(f"{where}.{name}: unknown field")
                else:
                    problems += check_schema(item, props[name], f"{where}.{name}")
    return problems


def validate_spec(spec) -> list[str]:
    """Problems in a RecurserApp spec, in words; an empty list means it can be rendered."""
    if not isinstance(spec, dict):
        return ["spec: expected an object"]
    schema = crd()["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    problems = check_schema(spec, schema)
    full = with_defaults(spec)
    if full["ingress"]["tlsSecretName"] and not full["ingress"]["host"]:
        problems.append("spec.ingress.tlsSecretName: needs spec.ingress.host")
    return problems


def labels(name: str) -> dict:
    return {"app.kubernetes.io/name": "recurser", "app.kubernetes.io/instance": name, "app.kubernetes.io/managed-by": MANAGER}


def selector(name: str) -> dict:
    return {"app.kubernetes.io/name": "recurser", "app.kubernetes.io/instance": name}


def owner_reference(app: dict) -> dict:
    return {"apiVersion": app.get("apiVersion", API_VERSION), "kind": app.get("kind", KIND), "name": app["metadata"]["name"],
            "uid": app["metadata"]["uid"], "controller": True, "blockOwnerDeletion": True}


def checksum(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def render(name: str, namespace: str, spec: dict, *, owner: dict | None = None, checksums: dict | None = None) -> list[dict]:
    """The objects for one RecurserApp: PersistentVolumeClaim, Deployment, Service, NetworkPolicy, Ingress.

    ``checksums`` (resource versions of the ConfigMap and Secrets) go on the pod template, so that editing
    the settings or the users file rolls the pod.
    """
    s = with_defaults(spec)
    meta = {"name": name, "namespace": namespace, "labels": labels(name)}
    if owner:
        meta["ownerReferences"] = [owner]
    objects: list[dict] = []

    claim = s["storage"]["existingClaim"] or f"{name}-data"
    if not s["storage"]["existingClaim"]:
        pvc_spec = {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": s["storage"]["size"]}}}
        if s["storage"]["storageClassName"]:
            pvc_spec["storageClassName"] = s["storage"]["storageClassName"]
        objects.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {**copy.deepcopy(meta), "name": claim}, "spec": pvc_spec})

    env = []
    if s["secretRef"]["name"]:        # layer 3: the credential never appears in a manifest, only a reference to it
        env.append({"name": API_KEY_ENV, "valueFrom": {"secretKeyRef": {"name": s["secretRef"]["name"], "key": s["secretRef"].get("key") or API_KEY_ENV}}})
    mounts = [{"name": "config", "mountPath": CONFIG_DIR, "readOnly": True},
              {"name": "data", "mountPath": DATA_DIR},
              {"name": "tmp", "mountPath": "/tmp"}]
    volumes = [{"name": "config", "configMap": {"name": s["configRef"]["name"], "items": [{"key": CONFIG_KEY, "path": CONFIG_KEY}]}},
               {"name": "data", "persistentVolumeClaim": {"claimName": claim}},
               {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]
    if s["usersRef"]["name"]:
        mounts.append({"name": "users", "mountPath": SECRETS_DIR, "readOnly": True})
        volumes.append({"name": "users", "secret": {"secretName": s["usersRef"]["name"], "defaultMode": 0o440,
                                                    "items": [{"key": USERS_KEY, "path": USERS_KEY}]}})
    probe = {"httpGet": {"path": "/healthz", "port": "http"}}
    container = {
        "name": "recurser",
        "image": s["image"],
        "imagePullPolicy": s["imagePullPolicy"],
        # the users file is passed explicitly: a mounted users Secret is always in force, whatever config.yaml says
        "args": ["serve", "--config", f"{CONFIG_DIR}/{CONFIG_KEY}"] + (["--users", f"{SECRETS_DIR}/{USERS_KEY}"] if s["usersRef"]["name"] else [])
                + (["--only", ",".join(s["projects"])] if s["projects"] else []),      # the sources this app is declared to serve
        "ports": [{"name": "http", "containerPort": PORT, "protocol": "TCP"}],        # layer 1
        "env": env,
        "volumeMounts": mounts,
        "readinessProbe": {**probe, "periodSeconds": 10, "failureThreshold": 3},
        "livenessProbe": {**probe, "periodSeconds": 20, "failureThreshold": 3, "initialDelaySeconds": 10},
        "resources": copy.deepcopy(s["resources"]),
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "runAsNonRoot": True,
                            "capabilities": {"drop": ["ALL"]}},
    }
    template_meta = {"labels": labels(name)}
    if checksums:
        template_meta["annotations"] = {f"{GROUP}/inputs": checksum(checksums)}
    pod = {
        "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": RUN_AS, "runAsGroup": RUN_AS, "fsGroup": RUN_AS,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container],
        "volumes": volumes,
    }
    if s["imagePullSecrets"]:
        pod["imagePullSecrets"] = copy.deepcopy(s["imagePullSecrets"])
    objects.append({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": copy.deepcopy(meta),
        "spec": {"replicas": 1,                                   # one writer for the history files on a ReadWriteOnce claim
                 "strategy": {"type": "Recreate"},
                 "selector": {"matchLabels": selector(name)},
                 "template": {"metadata": template_meta, "spec": pod}},
    })
    objects.append({                                              # layer 4: 80 inside the cluster, 8000 in the container
        "apiVersion": "v1", "kind": "Service", "metadata": copy.deepcopy(meta),
        "spec": {"type": "ClusterIP", "selector": selector(name),
                 "ports": [{"name": "http", "port": SERVICE_PORT, "targetPort": "http", "protocol": "TCP"}]},
    })
    if s["networkPolicy"]["enabled"]:
        sources = [{"podSelector": {}}]                           # pods of its own namespace
        namespaces = list(s["networkPolicy"]["ingressNamespaces"])
        if s["ingress"]["host"] and s["ingress"]["controllerNamespace"]:
            namespaces.append(s["ingress"]["controllerNamespace"])
        for ns in dict.fromkeys(namespaces):
            sources.append({"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ns}}})
        objects.append({
            "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": copy.deepcopy(meta),
            "spec": {
                "podSelector": {"matchLabels": selector(name)},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [{"from": sources, "ports": [{"protocol": "TCP", "port": PORT}]}],
                "egress": [
                    {"ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},              # name resolution
                    {"to": [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE_RANGES)}}],                 # the Anthropic API,
                     "ports": [{"protocol": "TCP", "port": 443}]},                                               # and nothing inside the cluster
                ],
            },
        })
    if s["ingress"]["host"]:
        ingress_spec = {"rules": [{"host": s["ingress"]["host"], "http": {"paths": [
            {"path": "/", "pathType": "Prefix", "backend": {"service": {"name": name, "port": {"name": "http"}}}}]}}]}
        if s["ingress"]["className"]:
            ingress_spec["ingressClassName"] = s["ingress"]["className"]
        if s["ingress"]["tlsSecretName"]:
            ingress_spec["tls"] = [{"hosts": [s["ingress"]["host"]], "secretName": s["ingress"]["tlsSecretName"]}]
        objects.append({"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": copy.deepcopy(meta), "spec": ingress_spec})
    return objects


def controller_manifests(namespace: str, image: str) -> list[dict]:
    """ServiceAccount, Role, RoleBinding and Deployment of the controller, for one namespace."""
    name = "recurser-controller"
    meta = {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/name": name}}
    rules = [
        {"apiGroups": [GROUP], "resources": [PLURAL], "verbs": ["get", "list", "watch"]},
        {"apiGroups": [GROUP], "resources": [f"{PLURAL}/status"], "verbs": ["get", "patch", "update"]},
        # owner references that block deletion need this where the cluster enforces it
        {"apiGroups": [GROUP], "resources": [f"{PLURAL}/finalizers"], "verbs": ["update"]},
        {"apiGroups": [""], "resources": ["configmaps", "secrets"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["services", "persistentvolumeclaims"], "verbs": ["get", "create", "patch"]},
        {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "create", "patch"]},
        {"apiGroups": ["networking.k8s.io"], "resources": ["networkpolicies", "ingresses"], "verbs": ["get", "create", "patch"]},
    ]
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": copy.deepcopy(meta)},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": copy.deepcopy(meta), "rules": rules},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": copy.deepcopy(meta),
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
         "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}]},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": copy.deepcopy(meta),
         "spec": {"replicas": 1, "strategy": {"type": "Recreate"},
                  "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
                  "template": {"metadata": {"labels": {"app.kubernetes.io/name": name}},
                               "spec": {"serviceAccountName": name,
                                        "securityContext": {"runAsNonRoot": True, "runAsUser": RUN_AS, "seccompProfile": {"type": "RuntimeDefault"}},
                                        "containers": [{
                                            "name": "controller", "image": image, "imagePullPolicy": "IfNotPresent",
                                            "command": ["python", "-m", "recurser.operator"],
                                            "args": ["--namespace", namespace],
                                            "resources": {"requests": {"cpu": "20m", "memory": "96Mi"}, "limits": {"memory": "192Mi"}},
                                            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                                "capabilities": {"drop": ["ALL"]}}}],
                                        "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "16Mi"}}]}}}},
    ]


def example(name: str = "team", namespace: str = "recurser", image: str = "ghcr.io/OWNER/recurser:0.2.0") -> dict:
    """The objects a person writes by hand: the ConfigMap, the two Secrets (placeholders only) and the RecurserApp."""
    from . import config as config_mod

    settings = {
        "llm": {k: config_mod.DEFAULTS["llm"][k] for k in ("provider", "model", "effort", "max_tokens")},
        "retrieval": dict(config_mod.DEFAULTS["retrieval"]),
        "server": {k: config_mod.DEFAULTS["server"][k] for k in ("host", "port", "progress_dir", "ask_per_10_minutes")},
        "access": {"users_file": f"{SECRETS_DIR}/{USERS_KEY}"},
    }
    return {
        "namespace": {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}},
        "config": {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{name}-config", "namespace": namespace}, "data": {CONFIG_KEY: settings}},
        "app": {"apiVersion": API_VERSION, "kind": KIND, "metadata": {"name": name, "namespace": namespace},
                "spec": {"image": image, "configRef": {"name": f"{name}-config"}, "secretRef": {"name": f"{name}-anthropic", "key": API_KEY_ENV},
                         "usersRef": {"name": f"{name}-users"}, "storage": {"size": "1Gi"}}},
    }


def to_yaml(objects) -> str:
    """Several objects as one YAML stream (needs PyYAML)."""
    import yaml

    class Plain(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True

    def text(dumper, data):          # a settings file inside a ConfigMap reads best as a block
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)

    Plain.add_representer(str, text)
    return "---\n".join(yaml.dump(obj, Dumper=Plain, sort_keys=False, default_flow_style=False, width=120) for obj in objects)
