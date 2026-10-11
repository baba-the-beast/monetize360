"""Gate 2 — static checks before publish: schema, compile (types, unknown signals, step/ending, rule cap),
FAIRNESS (protected attributes and personal data can't be pricing signals), tenant limits and banned signals,
rules that can never fire or are shadowed, expired rules, missing defaults, the highest base price a pack can
price, band and lookup gaps, unused signals. Returns {ok, errors, warnings}; any error blocks publish.
What a static check can't prove (formula bounds, overlapping exclusive rules) is the fuzz verifier's job."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from itertools import product
from typing import Any, Iterable, Mapping

from monetize360.control.schema import validate
from monetize360.engine import PackInvalid, Policy, compile_pack

# A price must never depend on who someone IS. Matched against whole snake_case segments of a signal name,
# so `customer_religion` is caught and `package` (contains "age") is not.
PROTECTED = ("gender", "sex", "religion", "caste", "race", "ethnicity", "ethnic", "tribe", "disability",
             "disabled", "pregnancy", "pregnant", "sexual_orientation", "marital_status", "nationality", "age",
             "skin_colour", "skin_color", "health", "political_party")
# Identifies a person: a price set with it is "personalised pricing" (e.g. New York's disclosure law).
PERSONAL = ("email", "phone", "mobile", "aadhaar", "pan_number", "passport", "ssn", "ip_address", "user_id",
            "customer_id", "device_id", "full_name", "first_name", "last_name", "date_of_birth", "dob", "address")
# Not banned, but can stand in for a protected attribute (where someone lives can reveal caste or religion).
PROXIES = ("pincode", "pin_code", "zip", "zipcode", "postcode", "postal_code", "neighbourhood", "neighborhood")

_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def _has(name: str, word: str) -> bool:
    return f"_{word}_" in f"_{name}_"


def lint(pack: Mapping[str, Any], *, policy: Policy | None = None, banned_signals: Iterable[str] = (),
         as_of: datetime | None = None) -> dict[str, Any]:
    """Lint a pack dict. `policy` / `banned_signals` = the tenant's (L2); `as_of` = "now" for expiry checks."""
    out = _Lint()
    schema_errors = validate(pack)
    for e in schema_errors:
        out.error("schema", e["path"], e["message"])
    if schema_errors:
        return out.result()                       # the other checks need a well-formed pack

    out.fairness(pack["signals"], set(banned_signals))
    try:
        compiled = compile_pack(pack)
    except PackInvalid as e:
        for d in e.details:
            out.error("compile", d["path"], d["message"])
        return out.result()

    out.tenant_limits(compiled, policy)
    out.rules(compiled, as_of or datetime.now(timezone.utc))
    out.defaults(compiled)
    out.max_base(compiled, policy)
    out.coverage(compiled)
    out.unused(pack, compiled)
    return out.result()


class _Lint:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []
        self.warnings: list[dict[str, str]] = []

    def error(self, code: str, path: str, message: str) -> None:
        self.errors.append({"code": code, "path": path, "message": message})

    def warn(self, code: str, path: str, message: str) -> None:
        self.warnings.append({"code": code, "path": path, "message": message})

    def result(self) -> dict[str, Any]:
        return {"ok": not self.errors, "errors": self.errors, "warnings": self.warnings}

    # ------------------------------------------------------------ fairness (platform policy L3 + tenant L2)

    def fairness(self, signals: Mapping[str, Any], banned: set[str]) -> None:
        for name in signals:
            path = f"signals/{name}"
            hit = next((w for w in PROTECTED if _has(name, w)), None)
            if hit:
                what = "a protected attribute" if name == hit else f"a protected attribute ({hit})"
                self.error("fairness", path, f"Fairness violation: '{name}' is {what}; "
                                             "a price can't depend on who someone is")
                continue
            hit = next((w for w in PERSONAL if _has(name, w)), None)
            if hit:
                self.error("personal_data", path, f"'{name}' identifies a person ({hit}); prices are never set "
                                                  "from personal data")
                continue
            if name in banned:
                self.error("banned_signal", path, f"'{name}' is banned by this tenant's policy")
                continue
            hit = next((w for w in PROXIES if _has(name, w)), None)
            if hit:
                self.warn("fairness_proxy", path, f"'{name}' ({hit}) can stand in for a protected attribute; "
                                                  "check prices don't differ unfairly by area")

    # ------------------------------------------------------------ tenant limits (L2 vs L1)

    def tenant_limits(self, compiled, policy: Policy | None) -> None:
        if policy is None:
            return
        g = compiled.guards
        if policy.max_price is not None and g.ceiling.op == "abs" and g.ceiling.value > Decimal(policy.max_price):
            self.error("tenant_limit", "guards/ceiling", f"ceiling {g.ceiling.value} is above the tenant maximum "
                                                         f"{policy.max_price}")
        if policy.min_price is not None and g.floor.op == "abs" and g.floor.value < Decimal(policy.min_price):
            self.error("tenant_limit", "guards/floor", f"floor {g.floor.value} is below the tenant minimum "
                                                       f"{policy.min_price}")
        if (policy.max_surge_x is not None and g.floor.op == "pct_of_base"
                and Decimal(policy.max_surge_x) * 100 < g.floor.value):
            self.error("tenant_limit", "guards/floor", f"floor {g.floor.value}% of base is above the tenant surge "
                                                       f"cap {policy.max_surge_x}x base: no price is ever valid")

    # ------------------------------------------------------------ rules that never fire, shadowed, expired

    def rules(self, compiled, now: datetime) -> None:
        for gi, group in enumerate(compiled.groups):
            always = None                                   # first rule in run order that always fires
            for rule in group.rules:
                path = f"groups/{gi}/rules/{rule.id}"
                timed = rule.effective_from is not None or rule.effective_to is not None
                constant = None if rule.when.names else rule.when({})
                if constant is False:
                    self.warn("unreachable", path, f"rule {rule.id} can never fire: its condition is always false")
                if rule.effective_to is not None and rule.effective_to <= now:
                    self.warn("expired", path, f"rule {rule.id} expired on {rule.effective_to:%Y-%m-%d}")
                if always is not None and constant is not False:
                    if group.stacking == "first_match":
                        self.warn("shadowed", path, f"rule {rule.id} is never used: {always} always fires first "
                                                    f"in first_match group {group.name}")
                    elif group.stacking == "exclusive":
                        self.error("exclusive_overlap", path, f"{always} always fires, so whenever {rule.id} "
                                                              f"fires too, exclusive group {group.name} fails")
                if constant is True and not timed and always is None:
                    always = rule.id

    # ------------------------------------------------------------ inputs callers must always send

    def defaults(self, compiled) -> None:
        for spec in compiled.signals.values():
            if spec.source == "request" and not spec.has_default:
                self.warn("missing_default", f"signals/{spec.name}", f"{spec.name} has no default: every request "
                                                                     "must send it or gets signal_missing (422)")

    # ------------------------------------------------------------ the highest base price the pack can price

    def max_base(self, compiled, policy: Policy | None) -> None:
        g = compiled.guards
        if g.floor.op == "pct_of_base" and g.ceiling.op == "abs" and compiled.base.op != "set":
            limit = (g.ceiling.value * 100 / g.floor.value).quantize(Decimal("0.01"))
            self.warn("max_base_price", "guards", f"a base price above {limit} can't be priced: floor "
                                                  f"{g.floor.value}% of base passes ceiling {g.ceiling.value} "
                                                  "(bounds_conflict, 422)")

    # ------------------------------------------------------------ bands and tables that leave inputs unpriced

    def coverage(self, compiled) -> None:
        adjustments = [("base", compiled.base)] + [
            (f"groups/{gi}/rules/{r.id}/then", r.then) for gi, g in enumerate(compiled.groups) for r in g.rules]
        for path, adj in adjustments:
            if adj.op == "band":
                spec = compiled.signals[adj.signal]
                top = adj.bands[-1][0]
                if spec.range is None:
                    self.warn("band_gap", path, f"{adj.signal} has no range: values above {top} have no band "
                                                "(lookup_miss, 422)")
                elif spec.range[1] > top:
                    self.warn("band_gap", path, f"{adj.signal} can reach {spec.range[1]} but the last band stops "
                                                f"at {top}: those inputs get lookup_miss (422)")
            if adj.op == "lookup_table" and adj.default is None:
                names = (adj.signal,) if adj.signal else adj.signals
                domains = [self._domain(compiled.signals[n]) for n in names]
                if any(d is None for d in domains):
                    continue                                 # numeric keys: can't enumerate, the verifier checks
                keys = [k[0] if adj.signal else k for k in product(*domains)]
                missing = [k for k in keys if (k if adj.signal else tuple(k)) not in adj.table]
                if missing:
                    shown = ", ".join(map(str, missing[:5])) + (" ..." if len(missing) > 5 else "")
                    self.warn("lookup_gap", path, f"no entry and no default for {len(missing)} value(s): {shown} "
                                                  "(lookup_miss, 422)")

    @staticmethod
    def _domain(spec) -> list[str] | None:
        if spec.type == "enum":
            return list(spec.values)
        if spec.type == "bool":
            return ["true", "false"]
        return None

    # ------------------------------------------------------------ declared but never used

    def unused(self, pack: Mapping[str, Any], compiled) -> None:
        used: set[str] = set()
        adjustments = [compiled.base] + [r.then for g in compiled.groups for r in g.rules]
        for adj in adjustments:
            used.update(n for n in (adj.signal, *adj.signals) if n)
            if adj.expr is not None:
                used.update(adj.expr.names)
        for g in compiled.groups:
            for r in g.rules:
                used.update(r.when.names)
                used.update(_PLACEHOLDER.findall(r.reason))
        used.update(lim.signal for lim in (compiled.guards.floor, compiled.guards.ceiling) if lim.signal)
        for name in compiled.signals:
            if name not in used:
                self.warn("unused_signal", f"signals/{name}", f"{name} is declared but no rule, table or guard "
                                                              "uses it")
