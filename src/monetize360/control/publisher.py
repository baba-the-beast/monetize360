"""Draft -> approve (different user) -> publish / rollback. Moves pack_pointer and publishes pack_published event.

The rules, in one place:

  save a draft   the YAML must pass the schema AND compile, or it is not stored at all
  approve        needs lint ok + verify ok on THIS version, and an approver who is not its author
  publish        needs an approved version; lint is run again first (tenant limits may have changed since)
  rollback       back to a version that has been live before

Every check that decides whether rules go live happens here, against rows read inside a locked transaction.
Always schema-validate before compiling: the engine trusts a schema-valid dict (engine review #12)."""
from __future__ import annotations

import difflib
from datetime import datetime, timezone
from typing import Any

import yaml

from monetize360.control.linter import lint
from monetize360.control.schema import validate
from monetize360.engine import PackInvalid, Policy, compile_pack
from monetize360.store import packs
from monetize360.store.db import Database

MAX_YAML_BYTES = 256 * 1024


class LifecycleError(Exception):
    """A request the lifecycle refuses. `status` is the HTTP status the API answers with."""

    def __init__(self, status: int, code: str, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details or []


def _not_found(domain: str, version: int) -> LifecycleError:
    return LifecycleError(404, "pack_not_found", f"no version {version} of '{domain}'")


def parse(text: bytes | str, domain: str) -> tuple[dict[str, Any], str]:
    """YAML text -> (schema-valid, compilable pack dict, the text). Raises LifecycleError(422) listing every problem."""
    if len(text) > MAX_YAML_BYTES:
        raise LifecycleError(413, "payload_too_large", f"a pack may be at most {MAX_YAML_BYTES // 1024} KB")
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise LifecycleError(422, "pack_invalid", "the pack must be UTF-8 text",
                                 [{"path": "(yaml)", "message": "the file is not UTF-8 encoded"}]) from None
    try:
        # anchors/aliases let a few lines expand into gigabytes once walked ("billion laughs"); packs don't need them
        if any(isinstance(t, (yaml.AliasToken, yaml.AnchorToken)) for t in yaml.scan(text)):
            raise LifecycleError(422, "pack_invalid", "YAML anchors and aliases (&name, *name) are not allowed",
                                 [{"path": "(yaml)", "message": "remove anchors and aliases"}])
        pack = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise LifecycleError(422, "pack_invalid", "the pack is not valid YAML",
                             [{"path": "(yaml)", "message": str(e).splitlines()[0]}]) from None
    if not isinstance(pack, dict):
        raise LifecycleError(422, "pack_invalid", "the pack must be a YAML mapping",
                             [{"path": "(root)", "message": "expected key: value pairs"}])
    errors = validate(pack)                                    # 1. shape
    if not errors and pack.get("domain") != domain:
        errors = [{"path": "domain", "message": f"the pack says '{pack.get('domain')}' but the URL says '{domain}'"}]
    if not errors:
        try:
            compile_pack(pack)                                 # 2. meaning: types, unknown signals, limits
        except PackInvalid as e:
            errors = e.details or [{"path": "(pack)", "message": e.message}]
    if errors:
        raise LifecycleError(422, "pack_invalid", f"the pack has {len(errors)} problem(s)", errors)
    return pack, text


def run_lint(db: Database, tenant_id: str, pack: dict[str, Any]) -> dict[str, Any]:
    policy, banned = packs.policy(db, tenant_id)
    return lint(pack, policy=policy, banned_signals=banned, as_of=datetime.now(timezone.utc))


def tenant_policy(db: Database, tenant_id: str) -> Policy | None:
    return packs.policy(db, tenant_id)[0]


def diff(old_yaml: str | None, new_yaml: str, old_label: str, new_label: str) -> str:
    """Unified diff of two versions' YAML, for a human approver."""
    return "".join(difflib.unified_diff((old_yaml or "").splitlines(keepends=True),
                                        new_yaml.splitlines(keepends=True), old_label, new_label))


def approve(db: Database, tenant_id: str, domain: str, version: int, approver: str) -> dict[str, Any]:
    with packs.locked(db, tenant_id, domain) as conn:
        row = packs.row(conn, domain, version)
        if row is None:
            raise _not_found(domain, version)
        if row["status"] != "draft":
            raise LifecycleError(409, "not_a_draft", f"version {version} is already {row['status']}")
        missing = [name for name, report in (("lint", row["lint_report"]), ("verify", row["verify_report"]))
                   if not (report or {}).get("ok")]
        if missing:
            raise LifecycleError(409, "checks_required",
                                 f"version {version} needs a passing {' and '.join(missing)} before it can be approved",
                                 [{"path": m, "message": "not run, or it found problems"} for m in missing])
        if approver == row["owner"]:
            raise LifecycleError(409, "same_person", "the author of a version can't approve it; "
                                                     "use a different approver key")
        packs.mark_approved(conn, domain, version, approver)
    return {"version": version, "status": "approved", "owner": row["owner"], "approver": approver}


def publish(db: Database, tenant_id: str, domain: str, version: int) -> dict[str, Any]:
    with packs.locked(db, tenant_id, domain) as conn:
        row = packs.row(conn, domain, version)
        if row is None:
            raise _not_found(domain, version)
        if row["status"] == "draft":
            raise LifecycleError(409, "approval_required", f"version {version} has not been approved")
        if row["status"] == "published":
            raise LifecycleError(409, "already_live", f"version {version} is already live")
        if row["status"] != "approved":
            raise LifecycleError(409, "not_approved", f"version {version} is {row['status']}; "
                                                      "to make an old version live again use rollback")
        # the stored text is exactly what was approved; check it once more against TODAY's rules before it prices
        pack, _ = parse(row["yaml"], domain)
        report = run_lint(db, tenant_id, pack)
        if not report["ok"]:
            raise LifecycleError(409, "checks_required", "lint no longer passes (did the tenant's limits change?)",
                                 report["errors"])
        previous = packs.make_live(conn, tenant_id, domain, version)
    return {"live_version": version, "previous_version": previous}


def rollback(db: Database, tenant_id: str, domain: str, to_version: int) -> dict[str, Any]:
    with packs.locked(db, tenant_id, domain) as conn:
        row = packs.row(conn, domain, to_version)
        if row is None:
            raise _not_found(domain, to_version)
        live = packs.live_version(conn, domain)
        if live == to_version:
            raise LifecycleError(409, "already_live", f"version {to_version} is already live")
        if row["status"] not in ("archived", "published"):
            raise LifecycleError(409, "never_live", f"version {to_version} has never been live "
                                                    f"(it is {row['status']}); publish it instead")
        parse(row["yaml"], domain)             # an old version must still load with today's engine
        previous = packs.make_live(conn, tenant_id, domain, to_version)
    return {"live_version": to_version, "previous_version": previous}
