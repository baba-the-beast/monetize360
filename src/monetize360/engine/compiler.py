"""Schema-valid pack dict -> immutable CompiledPack: numbers via Decimal(str(x)), compile rule expressions
once, index lookup tables, sort bands/points/tiers, fix rule run order.
Reads NO files: JSON Schema validation happens before, in control/schema.py. Raises PackInvalid listing
every problem it finds (unknown signal names, bad expressions, reserved names, ...)."""
from __future__ import annotations

import keyword
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .expr import BOOL, NUM, STR, ExprError, compile_expr
from .models import (
    CENT,
    DEC,
    Adjustment,
    CompiledPack,
    Group,
    Guards,
    Limit,
    PackInvalid,
    Rounding,
    Rule,
    SignalInvalid,
    SignalSpec,
)
from .operators import key_of
from .publicsignals import PUBLIC_SIGNALS, WEEKDAYS
from .signals import NUMERIC, coerce

_DURATION = re.compile(r"([1-9][0-9]*)(s|m|h|d)")
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
MAX_RULES = 500          # whole pack; caps worst-case work per price (PACK_SCHEMA size limits)
FACTOR_OPS = {"pct", "multiplier", "lookup_table", "band", "piecewise_curve", "tier"}
NUMERIC_SIGNAL_OPS = {"band", "piecewise_curve", "tier", "cost_plus", "competitor_rel"}
EXPR_TYPE = {"ratio": NUM, "number": NUM, "money": NUM, "int": NUM, "bool": BOOL, "enum": STR}


def D(x: Any) -> Decimal:
    if isinstance(x, bool):
        raise InvalidOperation(f"{x!r} is not a number")
    return Decimal(str(x))


def compile_pack(pack: Mapping[str, Any]) -> CompiledPack:
    with localcontext(DEC):
        c = _Compiler()
        try:
            compiled = c.pack(pack)
        except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError, IndexError) as e:
            c.errors.append({"path": "(unknown)", "message":
                             f"pack doesn't match the schema ({type(e).__name__}: {e}) — run control.schema first"})
        if c.errors:
            raise PackInvalid("pack is invalid", c.errors)
        return compiled


class _Compiler:
    def __init__(self) -> None:
        self.errors: list[dict] = []
        self.signals: dict[str, SignalSpec] = {}

    def bad(self, path: str, message: str) -> None:
        self.errors.append({"path": path, "message": message})

    # ------------------------------------------------------------ top level

    def pack(self, p: Mapping[str, Any]) -> CompiledPack:
        tz_name = p.get("timezone", "Asia/Kolkata")
        try:
            tz: Any = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            self.bad("timezone", f"unknown timezone {tz_name}")
            tz = timezone.utc

        for name, spec in p["signals"].items():
            s = self.signal(name, spec)
            if s is not None:
                self.signals[name] = s

        base = self.adjustment(p["base"], "base", is_base=True)
        groups, ids = [], set()
        for gi, g in enumerate(p["groups"]):
            rules = []
            for ri, r in enumerate(g["rules"]):
                path = f"groups/{gi}/rules/{ri}"
                rule = self.rule(r, path)
                if rule is None:
                    continue
                if rule.id in ids:
                    self.bad(f"{path}/id", f"duplicate rule id {rule.id}")
                ids.add(rule.id)
                if g["stacking"] == "add" and rule.then.kind == "price":
                    self.bad(f"{path}/then", f"'{rule.then.op}' can't be used in an 'add' group")
                rules.append(rule)
            rules.sort(key=lambda r: -r.priority)          # stable: equal priorities keep file order
            groups.append(Group(g["name"], g["stacking"], tuple(rules)))
        total = sum(len(g["rules"]) for g in p["groups"])
        if total > MAX_RULES:
            self.bad("groups", f"{total} rules in the pack; the limit is {MAX_RULES}")

        return CompiledPack(
            domain=p["domain"], currency=p["currency"], timezone=tz, signals=self.signals, base=base,
            groups=tuple(groups), guards=self.guards(p["guards"]), rounding=self.rounding(p["rounding"]),
            rule_ids=frozenset(ids))

    # ------------------------------------------------------------ signals

    def signal(self, name: str, s: Mapping[str, Any]) -> SignalSpec | None:
        path = f"signals/{name}"
        if keyword.iskeyword(name) or name in ("price", "true", "false", "null"):
            self.bad(path, f"'{name}' is a reserved word and can't be a signal name")
            return None
        t, source = s["type"], s["source"]
        values = tuple(s.get("values", ()))
        rng = None
        if "range" in s:
            rng = (D(s["range"][0]), D(s["range"][1]))
            if rng[0] > rng[1]:
                self.bad(f"{path}/range", "range minimum is above its maximum")
        if t == "ratio":
            rng = (max(rng[0], Decimal(0)), min(rng[1], Decimal(1))) if rng else (Decimal(0), Decimal(1))

        if source == "public":
            want = PUBLIC_SIGNALS.get(name)
            if want is None:
                self.bad(path, f"unknown public signal '{name}' (known: {', '.join(PUBLIC_SIGNALS)})")
                return None
            if t != want:
                self.bad(f"{path}/type", f"public signal {name} must have type {want}")
            if name == "festival" and "none" not in values:
                self.bad(f"{path}/values", "festival values must include 'none'")
            if name == "weekday" and not set(WEEKDAYS) <= set(values):
                self.bad(f"{path}/values", f"weekday values must include {', '.join(WEEKDAYS)}")

        per = None
        if "max_staleness" in s:
            per = self.duration(s["max_staleness"], f"{path}/max_staleness")
        spec = SignalSpec(name=name, type=t, source=source, range=rng, values=values,
                          max_staleness=per, on_stale=s.get("on_stale", "use_default"))
        if s.get("default") is not None:
            try:
                default = coerce(spec, s["default"])
            except SignalInvalid as e:
                self.bad(f"{path}/default", e.message)
                return spec
            spec = SignalSpec(**{**spec.__dict__, "has_default": True, "default": default})
        return spec

    def duration(self, text: str, path: str) -> timedelta | None:
        m = _DURATION.fullmatch(str(text))
        if not m:
            self.bad(path, f"bad duration {text!r}")
            return None
        return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})

    def expr_types(self) -> dict[str, str]:
        return {n: EXPR_TYPE[s.type] for n, s in self.signals.items()}

    def enums(self) -> dict[str, tuple[str, ...]]:
        return {n: s.values for n, s in self.signals.items() if s.type == "enum"}

    def table_key(self, signal: str, raw: Any, path: str) -> str:
        """Normalise a lookup key the way the signal's VALUE will be normalised, so "0.50" matches 0.5.
        A key the signal can never take is a typo, not a table row."""
        spec = self.signals.get(signal)
        if spec is None:
            return key_of(raw)
        if spec.type in NUMERIC or spec.type == "int":
            try:
                return key_of(D(raw))
            except InvalidOperation:
                self.bad(path, f"key {raw!r} is not a number, but {signal} is numeric")
        elif spec.type == "bool":
            k = key_of(raw)
            if k not in ("true", "false"):
                self.bad(path, f"key {raw!r} can never match true/false signal {signal}")
            return k
        elif spec.type == "enum" and key_of(raw) not in spec.values:
            self.bad(path, f"key {raw!r} can never match: {signal} is one of {', '.join(spec.values)}")
        return key_of(raw)

    def need_signal(self, name: str, path: str, numeric: bool = False) -> None:
        spec = self.signals.get(name)
        if spec is None:
            self.bad(path, f"unknown signal '{name}'")
        elif numeric and spec.type not in NUMERIC | {"int"}:
            self.bad(path, f"signal '{name}' must be numeric, not {spec.type}")

    # ------------------------------------------------------------ operators

    def adjustment(self, a: Mapping[str, Any], path: str, is_base: bool = False) -> Adjustment:
        op = a["op"]
        kind = "factor" if op in FACTOR_OPS else "amount" if op == "abs" else "price"
        if is_base:
            kind = "price"
        for key in ("signal", "cost_signal", "competitor_signal"):
            if key in a:
                self.need_signal(a[key], f"{path}/{key}", numeric=op in NUMERIC_SIGNAL_OPS)
        for i, s in enumerate(a.get("signals", ())):
            self.need_signal(s, f"{path}/signals/{i}")
        value_key = "price" if is_base and op == "band" else "factor" if op == "band" else None

        if op in ("from_request",):
            return Adjustment(op, kind)
        if op == "pct":
            return Adjustment(op, kind, value=1 + D(a["value"]) / 100)
        if op in ("multiplier", "abs"):
            return Adjustment(op, kind, value=D(a["value"]))
        if op == "set":
            return Adjustment(op, kind, value=D(a["price"] if is_base else a["value"]))
        if op == "cost_plus":
            return Adjustment(op, kind, value=D(a["margin_pct"]), signal=a["cost_signal"])
        if op == "competitor_rel":
            return Adjustment(op, kind, value=D(a["pct"]), signal=a["competitor_signal"])
        if op == "band":
            bands = sorted(((D(b["upto"]), D(b[value_key])) for b in a["bands"]), key=lambda b: b[0])
            return Adjustment(op, kind, signal=a["signal"], bands=tuple(bands))
        if op == "lookup_table":
            default = D(a["default"]) if "default" in a else None
            if "table" in a:
                table = {self.table_key(a["signal"], k, f"{path}/table/{k}"): D(v) for k, v in a["table"].items()}
                return Adjustment(op, kind, signal=a["signal"], table=table, default=default)
            table = {}
            for i, row in enumerate(a["rows"]):
                if len(row["key"]) != len(a["signals"]):
                    self.bad(f"{path}/rows/{i}/key", f"key needs {len(a['signals'])} values")
                table[tuple(self.table_key(sig, k, f"{path}/rows/{i}/key")
                            for sig, k in zip(a["signals"], row["key"], strict=False))] = D(row["value"])
            return Adjustment(op, kind, signals=tuple(a["signals"]), table=table, default=default)
        if op == "piecewise_curve":
            points = sorted(((D(x), D(f)) for x, f in a["points"]), key=lambda p: p[0])
            if len({x for x, _ in points}) != len(points):
                self.bad(f"{path}/points", "two curve points have the same x")
            return Adjustment(op, kind, signal=a["signal"], points=tuple(points))
        if op == "tier":
            tiers = sorted(((D(t["from"]), 1 + D(t["pct"]) / 100) for t in a["tiers"]), key=lambda t: t[0])
            return Adjustment(op, kind, signal=a["signal"], tiers=tuple(tiers))
        if op == "formula":
            try:
                expr = compile_expr(a["expr"], {**self.expr_types(), "price": NUM}, enums=self.enums(), result=NUM)
            except ExprError as e:
                self.bad(f"{path}/expr", str(e))
                expr = None
            return Adjustment(op, kind, expr=expr)
        self.bad(f"{path}/op", f"unknown operator {op}")
        return Adjustment(op, kind)

    # ------------------------------------------------------------ rules, guards, rounding

    def rule(self, r: Mapping[str, Any], path: str) -> Rule | None:
        try:
            when = compile_expr(r["when"], self.expr_types(), enums=self.enums(), result=BOOL)
        except ExprError as e:
            self.bad(f"{path}/when", str(e))
            when = None
        then = self.adjustment(r["then"], f"{path}/then")
        for name in _PLACEHOLDER.findall(r["reason"]):
            if name not in self.signals:
                self.bad(f"{path}/reason", f"placeholder {{{name}}} is not a signal")
        start, end = self.instant(r.get("effective_from")), self.instant(r.get("effective_to"))
        if start and end and start >= end:
            self.bad(f"{path}/effective_to", "effective_to must be after effective_from")
        if when is None:
            return None
        return Rule(id=r["id"], when=when, then=then, reason=r["reason"], priority=int(r.get("priority", 0)),
                    effective_from=start, effective_to=end)

    @staticmethod
    def instant(v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, str):
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)

    def guards(self, g: Mapping[str, Any]) -> Guards:
        floor = Limit(g["floor"]["op"], D(g["floor"]["value"]))
        ceiling = Limit(g["ceiling"]["op"], D(g["ceiling"]["value"]))
        if floor.op == ceiling.op and floor.value > ceiling.value:
            self.bad("guards/floor", "floor is above ceiling")
        mc = g.get("max_change")
        pct = D(mc["pct"]) if mc else None
        per = self.duration(mc["per"], "guards/max_change/per") if mc else None
        return Guards(floor, ceiling, pct, per)

    def rounding(self, r: Mapping[str, Any]) -> Rounding:
        step = D(r["step"])
        if step <= 0 or step % CENT != 0:
            self.bad("rounding/step", "step must be a positive multiple of 0.01")
        ending = r.get("ending")
        if ending is not None and step > 0 and (100 % step != 0 or ending % step != 0):
            # e.g. step 50 + ending 99: no multiple of 50 ends in 99, so prices would break the step
            self.bad("rounding/ending", f"ending {ending:02d} is unreachable with step {step} "
                                        "(need 100 % step == 0 and ending % step == 0)")
        return Rounding(step, r["mode"], ending)
