"""Mapping engine: jsonpath extract + transform chain (PLAN §3.5)."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

import yaml
from jsonpath_ng import parse as jp_parse
from jsonpath_ng.jsonpath import JSONPath

from app.core.logging import get_logger
from app.core.metrics import events_canonical_unknown_total
from app.core.timeutils import iso_to_dt3

log = get_logger(__name__)

# ---- Canonical tables ------------------------------------------------------

_CANONICAL_CACHE: dict[str, dict[str, str]] = {}
_MAPPINGS_DIR = Path(__file__).resolve().parent.parent / "mappings"


def _load_canonical(name: str) -> dict[str, str]:
    if name in _CANONICAL_CACHE:
        return _CANONICAL_CACHE[name]
    path = _MAPPINGS_DIR / f"canonical_{name}.yml"
    if not path.exists():
        log.warning("canonical.missing", name=name, path=str(path))
        _CANONICAL_CACHE[name] = {}
        return {}
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    table: dict[str, str] = {}
    for canonical, raws in data.items():
        for raw in (raws or []):
            table[str(raw).strip().lower()] = canonical
    _CANONICAL_CACHE[name] = table
    return table


def reload_canonical() -> None:
    _CANONICAL_CACHE.clear()


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    return s


# ---- Transforms ------------------------------------------------------------

TransformFn = Callable[[Any, dict[str, Any]], Any]


def _t_lower_snake(v: Any, _ctx: dict[str, Any]) -> Any:
    if v is None:
        return None
    return _slug(str(v))


def _t_truncate(arg: str) -> TransformFn:
    n = int(arg)

    def fn(v: Any, _ctx: dict[str, Any]) -> Any:
        if v is None:
            return None
        b = str(v).encode("utf-8")[:n]
        return b.decode("utf-8", errors="ignore")
    return fn


def _t_coalesce(arg: str) -> TransformFn:
    expr = jp_parse(arg)

    def fn(v: Any, ctx: dict[str, Any]) -> Any:
        if v not in (None, "", [], {}):
            return v
        matches = [m.value for m in expr.find(ctx)]
        for m in matches:
            if m not in (None, "", [], {}):
                return m
        return None
    return fn


def _t_canonicalize(arg: str) -> TransformFn:
    table_name = arg

    def fn(v: Any, ctx: dict[str, Any]) -> Any:
        if v is None:
            ss = ctx.get("__subsource__", "")
            events_canonical_unknown_total.labels(subsource=ss, target=table_name).inc()
            return "unknown.none"
        table = _load_canonical(table_name)
        raw = str(v).strip().lower()
        if raw in table:
            return table[raw]
        ss = ctx.get("__subsource__", "")
        events_canonical_unknown_total.labels(subsource=ss, target=table_name).inc()
        return f"unknown.{_slug(str(v))}"
    return fn


def _t_iso_to_dt3(v: Any, _ctx: dict[str, Any]) -> Any:
    if v is None:
        return None
    return iso_to_dt3(str(v))


def _t_to_string(v: Any, _ctx: dict[str, Any]) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), sort_keys=True, default=str)
    return v


def _t_null_if_empty(v: Any, _ctx: dict[str, Any]) -> Any:
    if v in (None, "", [], {}):
        return None
    return v


def _t_signin_status(v: Any, _ctx: dict[str, Any]) -> str:
    """Graph signIns: errorCode==0 → success, else failure."""
    try:
        code = int(v) if v is not None else 0
    except (TypeError, ValueError):
        code = 0
    return "user.signin.success" if code == 0 else "user.signin.failure"


def _t_const(arg: str) -> TransformFn:
    def fn(_v: Any, _ctx: dict[str, Any]) -> str:
        return arg
    return fn


# Dispatch — keyed by transform name (token before ':')
TRANSFORM_BUILDERS: dict[str, Callable[..., TransformFn]] = {
    "lower_snake": lambda: _t_lower_snake,
    "truncate": _t_truncate,
    "coalesce": _t_coalesce,
    "canonicalize": _t_canonicalize,
    "iso_to_dt3": lambda: _t_iso_to_dt3,
    "to_string": lambda: _t_to_string,
    "null_if_empty": lambda: _t_null_if_empty,
    "signin_status": lambda: _t_signin_status,
    "const": _t_const,
}


def _build_transform(spec: str | None) -> list[TransformFn]:
    if not spec:
        return []
    fns: list[TransformFn] = []
    for piece in spec.split(";"):
        piece = piece.strip()
        if not piece:
            continue
        name, _, arg = piece.partition(":")
        name = name.strip()
        arg = arg.strip()
        builder = TRANSFORM_BUILDERS.get(name)
        if builder is None:
            raise ValueError(f"unknown transform: {name}")
        fns.append(builder(arg) if arg else builder())
    return fns


# ---- Rule wrapper ----------------------------------------------------------

class Rule:
    __slots__ = (
        "subsource",
        "target_column",
        "source_jsonpath",
        "transform_spec",
        "_jp",
        "_fns",
    )

    def __init__(
        self,
        subsource: str,
        target_column: str,
        source_jsonpath: str,
        transform_spec: str | None,
    ):
        self.subsource = subsource
        self.target_column = target_column
        self.source_jsonpath = source_jsonpath
        self.transform_spec = transform_spec
        self._jp: JSONPath = jp_parse(source_jsonpath)
        self._fns = _build_transform(transform_spec)

    def apply(self, event: dict[str, Any]) -> Any:
        matches = [m.value for m in self._jp.find(event)]
        v: Any = matches[0] if matches else None
        for fn in self._fns:
            v = fn(v, event)
        return v


def load_rules_from_yaml(path: Path) -> list[Rule]:
    with path.open() as f:
        data = yaml.safe_load(f) or []
    return [
        Rule(
            subsource=r["subsource"],
            target_column=r["target_column"],
            source_jsonpath=r["source_jsonpath"],
            transform_spec=r.get("transform"),
        )
        for r in data
    ]


def load_default_rules(subsource: str) -> list[Rule]:
    fname = "default_" + subsource.replace(".", "_") + ".yml"
    path = _MAPPINGS_DIR / fname
    if not path.exists():
        log.warning("default_rules.missing", subsource=subsource, path=str(path))
        return []
    return load_rules_from_yaml(path)


def rules_from_db_rows(rows: list[dict[str, Any]]) -> list[Rule]:
    return [
        Rule(r["subsource"], r["target_column"], r["source_jsonpath"], r.get("transform"))
        for r in rows
    ]


# ---- Event normalization ---------------------------------------------------

TARGET_COLUMNS = {"operation", "instance", "user_name", "user_id", "comments"}


def normalize(event: dict[str, Any], subsource: str, rules: list[Rule]) -> dict[str, Any]:
    """Apply rules to one event → row dict suitable for repo.insert_events()."""
    row: dict[str, Any] = {
        "timestamp": None,
        "source": "Microsoft365",
        "operation": "",
        "instance": "",
        "user_name": None,
        "user_id": None,
        "extra_data": {"subsource": subsource, "raw": event},
        "comments": None,
        "dedup_hash": "",
    }
    # Pass subsource through ctx for transforms that need it (canonicalize metrics).
    ctx_event = dict(event)
    ctx_event["__subsource__"] = subsource
    for rule in rules:
        if rule.subsource != subsource:
            continue
        value = rule.apply(ctx_event)
        col = rule.target_column
        if col == "timestamp":
            row["timestamp"] = value
        elif col in TARGET_COLUMNS:
            row[col] = value
        elif col.startswith("extra_data."):
            row["extra_data"][col.split(".", 1)[1]] = value
    if row["timestamp"] is None:
        raise ValueError("normalizer did not produce timestamp")
    if not row["operation"]:
        row["operation"] = "unknown.none"
    if not row["instance"]:
        row["instance"] = "unknown"
    return row
