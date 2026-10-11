"""Gate 2 — static checks before publish: schema, compile (types, unknown signals, step/ending, rule cap),
FAIRNESS (protected attributes and personal data can't be pricing signals), tenant limits and banned signals,
rules that can never fire or are shadowed, expired rules, missing defaults, the highest base price a pack can
price, band and lookup gaps, unused signals. Returns {ok, errors, warnings}; any error blocks publish.
What a static check can't prove (formula bounds, overlapping exclusive rules) is the fuzz verifier's job."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import product
from typing import Any, Iterable, Mapping

from monetize360.control.schema import validate
from monetize360.engine import PackInvalid, Policy, compile_pack

# A price must never depend on who someone IS. Words are matched against whole snake_case segments of a signal
# name. Words that are always about a person block anywhere (`customer_religion`, `religion_group`).
PROTECTED = ("religion", "religious", "caste", "ethnicity", "ethnic", "tribe", "tribal", "gender",
             "sexual_orientation", "pregnancy", "pregnant", "marital_status", "nationality", "disability",
             "skin_colour", "skin_color", "political_party")
# Identifies a person: a price set with it is "personalised pricing" (e.g. New York's disclosure law).
PERSONAL = ("email", "aadhaar", "pan_number", "passport", "ssn", "ip_address", "user_id", "customer_id",
            "device_id", "full_name", "first_name", "last_name", "date_of_birth", "dob", "phone_number",
            "mobile_number")
# Words with an innocent meaning too. SAFE DEFAULT: they block (`age_group`, `child_age`, `health_condition`,
# `home_address`, `contact_phone`) unless the name says what THING they describe and names no person
# (`vehicle_age`, `stock_age_days`, `race_day`, `battery_health`, `phone_model`, `mobile_app`).
AMBIGUOUS_PROTECTED = ("age", "sex", "race", "health", "disabled")
AMBIGUOUS_PERSONAL = ("phone", "mobile", "address")
PERSON = ("customer", "user", "guest", "buyer", "passenger", "rider", "driver", "traveller", "traveler", "member",
          "patient", "person", "client", "shopper", "visitor", "student", "employee", "applicant", "cardholder",
          "child", "kid", "adult", "senior", "owner", "holder")
THINGS = ("vehicle", "car", "bike", "battery", "stock", "inventory", "product", "item", "listing", "room",
          "building", "property", "asset", "machine", "server", "system", "fleet", "aircraft", "plane", "tyre",
          "tire", "engine", "cache", "page", "content", "order", "booking", "ticket", "app", "model", "brand", "os",
          "platform", "track", "event", "day", "season", "horse", "course", "data", "network", "zone", "area",
          "region", "warehouse", "store", "outlet", "crop", "fruit")
# Values that ARE a protected attribute, whatever the signal is called: `segment: [male, female]` (review L2).
PROTECTED_VALUES = frozenset((
    "male", "female", "men", "women", "man", "woman", "boy", "girl", "boys", "girls", "ladies", "gents",
    "trans", "transgender", "trans_man", "trans_woman", "transman", "transwoman", "non_binary", "nonbinary",
    "intersex", "gay", "lesbian", "bisexual", "hindu", "muslim", "christian", "sikh", "jain", "buddhist", "parsi",
    "jewish", "atheist", "dalit", "brahmin", "adivasi", "obc", "scheduled_caste", "scheduled_tribe", "pregnant"))
_LITERAL = re.compile(r"'((?:[^'\\]|\\.)*)'" + r'|"((?:[^"\\]|\\.)*)"')   # 'text' or "text" in a rule
# Not banned, but can stand in for a protected attribute (where someone lives can reveal caste or religion).
PROXIES = ("pincode", "pin_code", "zip", "zipcode", "postcode", "postal_code", "neighbourhood", "neighborhood")

_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def _has(name: str, word: str) -> bool:
    return f"_{word}_" in f"_{name}_"


def _blocks(name: str, word: str) -> bool:
    """An ambiguous word blocks unless the name is about a thing and names no person."""
    if not _has(name, word):
        return False
    about_a_thing = any(_has(name, t) for t in THINGS) and not any(_has(name, p) for p in PERSON)
    return not about_a_thing


def _protected_word(name: str) -> str | None:
    return (next((w for w in PROTECTED if _has(name, w)), None)
            or next((w for w in AMBIGUOUS_PROTECTED if _blocks(name, w)), None))


def _personal_word(name: str) -> str | None:
    return (next((w for w in PERSONAL if _has(name, w)), None)
            or next((w for w in AMBIGUOUS_PERSONAL if _blocks(name, w)), None))


def _protected_values(values) -> list[str]:
    return sorted({str(v) for v in values if str(v).lower().replace("-", "_") in PROTECTED_VALUES})


def lint(pack: Mapping[str, Any], *, policy: Policy | None = None, banned_signals: Iterable[str] = (),
         fairness_exceptions: Iterable[str] = (), as_of: datetime | None = None) -> dict[str, Any]:
    """Lint a pack dict. `policy` / `banned_signals` = the tenant's (L2); `fairness_exceptions` = signal names the
    tenant has cleared (a fairness error becomes a warning an approver must sign off); `as_of` = "now"."""
    out = _Lint()
    schema_errors = validate(pack)
    for e in schema_errors:
        out.error("schema", e["path"], e["message"])
    if schema_errors:
        return out.result()                       # the other checks need a well-formed pack

    exceptions = set(fairness_exceptions)
    out.names(pack, set(banned_signals), exceptions)
    try:
        compiled = compile_pack(pack)
    except PackInvalid as e:
        for d in e.details:
            out.error("compile", d["path"], d["message"])
        return out.result()
    out.protected_values(pack, compiled, exceptions)

    out.tenant_limits(compiled, policy)
    now = as_of or datetime.now(timezone.utc)
    out.rules(compiled, now)
    out.calendars(compiled, now)
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

    def names(self, pack: Mapping[str, Any], banned: set[str], exceptions: set[str]) -> None:
        for name in pack["signals"]:
            path = f"signals/{name}"
            if name in banned:                                      # always, exception or not
                self.error("banned_signal", path, f"'{name}' is banned by this tenant's policy")
            protected, personal = _protected_word(name), _personal_word(name)
            if protected:
                what = "a protected attribute" if name == protected else f"a protected attribute ({protected})"
                self.fairness(exceptions, {name}, path,
                              f"Fairness violation: '{name}' is {what}; a price can't depend on who someone is")
            if personal:                                            # never excepted: no personalised pricing
                self.error("personal_data", path, f"'{name}' identifies a person ({personal}); prices are never "
                                                  "set from personal data")
            if not (protected or personal):
                hit = next((w for w in PROXIES if _has(name, w)), None)
                if hit:
                    self.warn("fairness_proxy", path, f"'{name}' ({hit}) can stand in for a protected attribute; "
                                                      "check prices don't differ unfairly by area")

    def protected_values(self, pack: Mapping[str, Any], compiled, exceptions: set[str]) -> None:
        """Protected VALUES under any name: enum values, table keys, and string literals in rules (review L2)."""
        taken: dict[str, set[str]] = {n: set(s.values) for n, s in compiled.signals.items()}
        for adj in [compiled.base] + [r.then for g in compiled.groups for r in g.rules]:
            if adj.op == "lookup_table" and adj.table:
                for key in adj.table:
                    for sig, k in zip((adj.signal,) if adj.signal else adj.signals,
                                      (key,) if adj.signal else key, strict=False):
                        taken.setdefault(sig, set()).add(k)
        for name, values in taken.items():
            hits = _protected_values(values)
            if hits:
                self.fairness(exceptions, {name}, f"signals/{name}",
                              f"Fairness violation: '{name}' takes protected values ({', '.join(hits)}); a price "
                              "can't depend on who someone is, whatever the signal is called")
        raw = {r["id"]: (gi, ri, r) for gi, g in enumerate(pack["groups"]) for ri, r in enumerate(g["rules"])}
        for group in compiled.groups:
            for rule in group.rules:
                gi, ri, r = raw[rule.id]
                text = r["when"] + " " + str(r["then"].get("expr", ""))
                hits = _protected_values(a or b for a, b in _LITERAL.findall(text))
                if hits:
                    names = set(rule.when.names) | set(rule.then.expr.names if rule.then.expr else ())
                    self.fairness(exceptions, names, f"groups/{gi}/rules/{ri}",
                                  f"Fairness violation: rule {rule.id} compares with protected value(s) "
                                  f"{', '.join(hits)}; a price can't depend on who someone is")

    def fairness(self, exceptions: set[str], signals: set[str], path: str, message: str) -> None:
        """A fairness error, unless the tenant cleared every signal involved: then a warning an approver signs off.
        Personal data and banned signals are never excepted."""
        if signals and signals <= exceptions:
            self.warn("fairness_exception", path, f"{message} — allowed by a tenant exception; an approver must "
                                                  "sign this off before publish")
        else:
            self.error("fairness", path, message)

    # ------------------------------------------------------------ calendars that run out (review C1)

    def calendars(self, compiled, now: datetime) -> None:
        """Warn when a calendar is about to stop working, not merely because it's finite: no date at all in the
        next 12 months, or a recurring (multi-year) calendar whose last date is under 6 months away."""
        today = now.date()
        for spec in compiled.signals.values():
            if not spec.calendar:
                continue
            last = max(spec.calendar)
            upcoming = [d for d in spec.calendar if today <= d <= today + timedelta(days=365)]
            recurring = len({d.year for d in spec.calendar}) > 1
            if not upcoming:
                self.warn("calendar_ends", f"signals/{spec.name}",
                          f"{spec.name} has no dates in the next 12 months (last: {last}): every day is 'none' "
                          "and those rules won't fire. Add dates (a calendar: section in the pack)")
            elif recurring and last < today + timedelta(days=183):
                self.warn("calendar_ends", f"signals/{spec.name}",
                          f"{spec.name} runs out on {last}: add next year's dates before then")

    # ------------------------------------------------------------ tenant limits (L2 vs L1)

    def tenant_limits(self, compiled, policy: Policy | None) -> None:
        if policy is None:
            return
        g = compiled.guards
        if policy.max_price is not None and g.ceiling.op == "abs" and g.ceiling.value > Decimal(policy.max_price):
            self.error("tenant_limit", "guards/ceiling", f"ceiling {g.ceiling.value} is above the tenant maximum "
                                                         f"{policy.max_price}")
        # the next two only add information when the OTHER guard isn't a fixed amount (else a check below fires)
        if (policy.min_price is not None and g.ceiling.op == "abs" and g.floor.op != "abs"
                and Decimal(policy.min_price) > g.ceiling.value):
            self.error("tenant_limit", "guards/ceiling", f"tenant minimum {policy.min_price} is above the ceiling "
                                                         f"{g.ceiling.value}: no price is ever valid (review S5)")
        if (policy.max_price is not None and g.floor.op == "abs" and g.ceiling.op != "abs"
                and Decimal(policy.max_price) < g.floor.value):
            self.error("tenant_limit", "guards/floor", f"tenant maximum {policy.max_price} is below the floor "
                                                       f"{g.floor.value}: no price is ever valid (review S5)")
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

