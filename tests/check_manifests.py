#!/usr/bin/env python3
"""Check the Kubernetes files offline, against the typed models of the official Python client.

    pip install kubernetes pyyaml
    python3 tests/check_manifests.py [deploy/k8s]

Every field of every built-in object must exist in the client's model of that kind (a misspelt or
misplaced field is reported with its path), required fields must be present, and the RecurserApp
must satisfy the schema of its own CustomResourceDefinition.  This is not a cluster: admission
rules, quotas and the controller's API calls are only exercised on a real one.
"""
import glob
import os
import sys

import yaml
from kubernetes import client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from recurser import k8s  # noqa: E402

MODELS = {
    ("v1", "Namespace"): "V1Namespace", ("v1", "ConfigMap"): "V1ConfigMap", ("v1", "Secret"): "V1Secret",
    ("v1", "Service"): "V1Service", ("v1", "ServiceAccount"): "V1ServiceAccount",
    ("v1", "PersistentVolumeClaim"): "V1PersistentVolumeClaim", ("apps/v1", "Deployment"): "V1Deployment",
    ("networking.k8s.io/v1", "NetworkPolicy"): "V1NetworkPolicy", ("networking.k8s.io/v1", "Ingress"): "V1Ingress",
    ("rbac.authorization.k8s.io/v1", "Role"): "V1Role", ("rbac.authorization.k8s.io/v1", "RoleBinding"): "V1RoleBinding",
    ("apiextensions.k8s.io/v1", "CustomResourceDefinition"): "V1CustomResourceDefinition",
}
FREE_FORM = {"object", "dict(str, str)", "dict(str, object)"}


def check(value, type_name: str, where: str) -> list[str]:
    """Problems found walking ``value`` along the client's model ``type_name``."""
    if type_name.startswith("list["):
        if not isinstance(value, list):
            return [f"{where}: expected a list"]
        return [p for i, item in enumerate(value) for p in check(item, type_name[5:-1], f"{where}[{i}]")]
    if type_name.startswith("dict("):
        if not isinstance(value, dict):
            return [f"{where}: expected a mapping"]
        inner = type_name[5:-1].split(", ", 1)[1]
        return [] if inner in ("str", "object") else [p for k, item in value.items() for p in check(item, inner, f"{where}.{k}")]
    model = getattr(client, type_name, None)
    if model is None or not hasattr(model, "openapi_types"):
        return []                                    # a scalar, or a free-form field
    if not isinstance(value, dict):
        return [f"{where}: expected an object ({type_name})"]
    problems = []
    by_json = {json_name: attr for attr, json_name in model.attribute_map.items()}
    for key, item in value.items():
        if key not in by_json:
            problems.append(f"{where}.{key}: no such field in {type_name}")
            continue
        problems += check(item, model.openapi_types[by_json[key]], f"{where}.{key}")
    try:                                             # required fields: let the model's own setters say
        kwargs = {by_json[k]: v for k, v in value.items() if k in by_json}
        model(**{k: (v if not isinstance(v, (dict, list)) else v) for k, v in kwargs.items()})
    except (ValueError, TypeError) as exc:
        problems.append(f"{where}: {exc}")
    return problems


def main(folder: str) -> int:
    problems, count = [], 0
    for path in sorted(glob.glob(os.path.join(folder, "**", "*.yaml"), recursive=True)):
        rel = os.path.relpath(path, folder)
        with open(path, encoding="utf-8") as fh:
            for obj in yaml.safe_load_all(fh):
                if not obj:
                    continue
                count += 1
                key = (obj.get("apiVersion"), obj.get("kind"))
                name = f"{rel}: {obj.get('kind')}/{obj.get('metadata', {}).get('name')}"
                if key == (k8s.API_VERSION, k8s.KIND):
                    found = k8s.validate_spec(obj.get("spec"))
                elif key in MODELS:
                    found = check(obj, MODELS[key], obj["kind"])
                else:
                    found = [f"unknown kind {key}"]
                print(("ok    " if not found else "FAIL  ") + name)
                problems += [f"{name}: {p}" for p in found]
    # the generator and the files on disk must agree, and every spec variant must render valid objects
    variants = [k8s.example()["app"]["spec"],
                {"image": "x:1", "configRef": {"name": "c"}},
                {"image": "x:1", "configRef": {"name": "c"}, "storage": {"existingClaim": "kept", "storageClassName": "fast"},
                 "networkPolicy": {"enabled": True, "ingressNamespaces": ["tools"]},
                 "ingress": {"host": "recurser.example.internal", "className": "nginx", "tlsSecretName": "recurser-tls"},
                 "imagePullSecrets": [{"name": "regcred"}]}]
    for i, spec in enumerate(variants):
        for obj in k8s.render("team", "recurser", spec, owner={"apiVersion": k8s.API_VERSION, "kind": k8s.KIND, "name": "team", "uid": "uid-1",
                                                               "controller": True, "blockOwnerDeletion": True}, checksums={"c": "1"}):
            count += 1
            found = check(obj, MODELS[(obj["apiVersion"], obj["kind"])], obj["kind"])
            problems += [f"variant {i}: {obj['kind']}: {p}" for p in found]
    print(f"\n{count} objects checked; " + ("no problem found" if not problems else f"{len(problems)} problem(s):"))
    for p in problems:
        print("  " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "deploy", "k8s")))
