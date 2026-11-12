"""Deployment settings: one ``config.yaml``, the way a Kubernetes ConfigMap delivers it.

    /app/config/config.yaml        mounted from the ConfigMap (or --config FILE, or $RECURSER_CONFIG)

The file is YAML (JSON is accepted too).  Unknown keys are refused, so a typo never silently does
nothing.  Secrets do not belong here: the Anthropic credential comes from the environment
(``ANTHROPIC_API_KEY``, a Secret referenced with ``secretKeyRef``) and the people who may open the page
from the users file (a Secret mounted as a file).
"""
from __future__ import annotations

import copy
import json
import os

DEFAULT_PATH = "/app/config/config.yaml"

DEFAULTS: dict = {
    "app": {"name": "recurser"},
    "llm": {
        "provider": "anthropic",          # the only provider: answers go through the Anthropic SDK
        "enabled": True,
        "model": "claude-opus-5-5",
        "effort": "medium",               # low | medium | high | xhigh | max
        "max_tokens": 16000,              # thinking counts toward it: leave room
        "timeout_seconds": 60,            # the longest silence tolerated from the API
        "answer_seconds": 180,            # the longest one answer may take in all
        "fallbacks": True,                # server-side refusal fallback
    },
    "retrieval": {
        "engine": "bm25",                 # in-process index over cards and project notes; no vector store to run
        "top_k": 8,
        "per_project": 6,
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "progress_dir": "/var/lib/recurser",
        "ask_per_10_minutes": 30,         # per person
        "max_concurrent_answers": 4,
    },
    "access": {
        "users_file": "",                 # JSON: who may open the page and which projects each person sees
        "trust_user_header": "",          # e.g. X-Forwarded-User, set by an authenticating proxy
        "trusted_proxies": [],            # addresses or ranges of that proxy: the header counts from nowhere else
        "only": [],                       # projects this deployment serves at all (empty: every project in the image)
    },
}

_TYPES = {str: "a string", bool: "true or false", int: "a whole number", list: "a list"}


class ConfigError(ValueError):
    pass


def _parse(text: str, path: str):
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError(f"{path}: reading YAML needs PyYAML (pip install pyyaml), or write the file as JSON") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML ({exc})") from exc


def merge(overrides) -> dict:
    """Defaults with a (partial) settings document laid over them, type-checked."""
    out = copy.deepcopy(DEFAULTS)
    if overrides in (None, ""):
        return out
    if not isinstance(overrides, dict):
        raise ConfigError("the config must be a mapping of sections (app, llm, retrieval, server, access)")
    for section, values in overrides.items():
        if section not in out:
            raise ConfigError(f"unknown section {section!r}: the sections are {', '.join(out)}")
        if values is None:
            continue
        if not isinstance(values, dict):
            raise ConfigError(f"section {section!r} must be a mapping")
        for key, value in values.items():
            if key not in out[section]:
                raise ConfigError(f"unknown setting {section}.{key}: {section} takes {', '.join(out[section])}")
            want = type(out[section][key])
            if want is int and isinstance(value, bool) or not isinstance(value, want):
                raise ConfigError(f"{section}.{key} must be {_TYPES[want]}")
            if want is list and not all(isinstance(x, str) for x in value):
                raise ConfigError(f"{section}.{key} must be a list of names")
            out[section][key] = value
    if out["llm"]["provider"] != "anthropic":
        raise ConfigError("llm.provider must be 'anthropic'")
    if not out["llm"]["model"].strip():
        raise ConfigError("llm.model must name a model")
    if out["access"]["trust_user_header"]:
        import ipaddress

        if not out["access"]["trusted_proxies"]:
            raise ConfigError("access.trust_user_header needs access.trusted_proxies: the addresses of the proxy that sets the header")
        for entry in out["access"]["trusted_proxies"]:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                raise ConfigError(f"access.trusted_proxies: {entry!r} is not an address or a range such as 10.42.0.0/16") from None
    if out["llm"]["effort"] not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("llm.effort must be low, medium, high, xhigh or max")
    if out["retrieval"]["engine"] != "bm25":
        raise ConfigError("retrieval.engine must be 'bm25'")
    for section, key, low, high in (("llm", "max_tokens", 4000, 128000), ("llm", "timeout_seconds", 5, 600), ("llm", "answer_seconds", 10, 1800),
                                    ("retrieval", "top_k", 1, 30),
                                    ("retrieval", "per_project", 1, 30), ("server", "port", 1, 65535),
                                    ("server", "ask_per_10_minutes", 1, 10000), ("server", "max_concurrent_answers", 1, 64)):
        if not low <= out[section][key] <= high:
            raise ConfigError(f"{section}.{key} must be between {low} and {high}")
    if out["llm"]["answer_seconds"] < 2 * out["llm"]["timeout_seconds"]:
        # an API that never answers is given up after two silences (one retry): the whole-answer limit must cover that
        raise ConfigError("llm.answer_seconds must be at least twice llm.timeout_seconds")
    return out


def load(path: str | None = None) -> dict:
    """Settings from ``path``, else ``$RECURSER_CONFIG``, else the ConfigMap mount when it exists, else defaults."""
    path = path or os.environ.get("RECURSER_CONFIG") or (DEFAULT_PATH if os.path.exists(DEFAULT_PATH) else None)
    if not path:
        return merge(None)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise ConfigError(f"cannot read the config file {path}: {exc.strerror}") from exc
    try:
        return merge(_parse(text, path))
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}" if path not in str(exc) else str(exc)) from None
