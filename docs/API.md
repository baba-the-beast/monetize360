# API Contract

Base URL (demo): `http://localhost:8080` · Interactive docs: `/docs` · All bodies JSON.

## Conventions

| Rule | Detail |
|---|---|
| Auth | `Authorization: Bearer m360_live_<key_id>_<secret>` on every `/v1` call |
| Money | **Send and receive money as strings**: `"3000"`, `"4950.00"`. Numbers are accepted and converted with `str()`, never treated as floats |
| Time | ISO-8601 UTC: `2026-10-11T09:00:00Z` |
| IDs | `price_id` is a ULID (sortable by time) |
| Request ID | Every response has header `X-Request-Id` |
| Tenant | **Never in the body.** Always taken from the key |

### Error format (every error)

```json
{ "error": "signal_invalid",
  "message": "occupancy must be between 0 and 1",
  "details": [{ "path": "signals.occupancy", "value": 7 }],
  "request_id": "b1946ac9..." }
```

| HTTP | `error` | When |
|---|---|---|
| 400 | `bad_request` | malformed JSON |
| 401 | `unauthorized` | missing, wrong, expired or revoked key |
| 403 | `forbidden` | key role not allowed for this endpoint |
| 404 | `pack_not_found` | domain not live for your tenant, **or not yours** (same answer on purpose) |
| 404 | `price_not_found` | unknown or not-yours `price_id` |
| 409 | `exclusive_conflict` | two rules fired in an `exclusive` group |
| 409 | `idempotency_conflict` | `idempotency_key` reused within 24 h with a different request body |
| 409 | `approval_required` / `same_person` | publish without approval / approver = author |
| 422 | `signal_invalid`, `signal_missing`, `lookup_miss`, `pack_invalid` | bad input / bad pack |
| 429 | `rate_limited` | with `Retry-After` header |
| 422 | `bounds_conflict` | no price can satisfy the limits for this request (e.g. `base_price` so high that floor > ceiling); names both limits |
| 500 | `rule_error` | a pack rule failed at runtime (e.g. formula divides by zero); names the rule — fix the pack |
| 500 | `invariant_violation` | engine self-check failed — an engine bug; no price returned |
| 503 | `signal_stale`, `killed` | stale signal with `on_stale: reject`; domain in kill mode `freeze` without a stored price |

---

## Pricing (role: `pricing`)

### `POST /v1/price/{domain}`

```json
{ "entity": "deluxe-101@2026-11-08",
  "base_price": "3000",
  "signals": { "occupancy": 0.85 },
  "as_of": "2026-11-08T09:00:00Z",
  "idempotency_key": "booking-8812" }
```

- `entity` required (used for `max_change` anchoring, kill-switch `freeze` and audit). `max_change` is
  tracked per `entity`, so put anything that must be kept apart into it — for hotels the stay date:
  `deluxe-101@2026-11-08`.
- `as_of` optional → server time, **recorded**.
- Public signals (`is_weekend`, `festival`) are computed; sending them → 422.
- Same `idempotency_key` within 24 h → same response, same `price_id`, no new audit row.
  Same key with a different body → 409 `idempotency_conflict`.

**200**
```json
{ "price_id": "01JA7Z3K9X...",
  "price": "4950.00", "currency": "INR",
  "domain": "hotel", "pack_version": 3,
  "valid_until": "2026-11-08T09:15:00Z",
  "waterfall": [
    { "step": "base",     "kind": "base", "delta": "3000.00", "after": "3000.00", "reason": "Base price" },
    { "step": "high-occ", "kind": "rule", "delta": "600.00",  "after": "3600.00", "reason": "Occupancy above 80%" },
    { "step": "weekend",  "kind": "rule", "delta": "360.00",  "after": "3960.00", "reason": "Weekend" },
    { "step": "festival", "kind": "rule", "delta": "990.00",  "after": "4950.00", "reason": "Festival: diwali" } ],
  "counterfactuals": [
    { "if": "without: Festival: diwali", "price": "3950.00" },
    { "if": "without: Occupancy above 80%", "price": "4150.00" },
    { "if": "without: Weekend", "price": "4500.00" } ],
  "signals_used": {
    "occupancy":  { "value": "0.85", "source": "request" },
    "is_weekend": { "value": true,   "source": "public" },
    "festival":   { "value": "diwali", "source": "public" } },
  "bounds": { "lower": "3825.00", "upper": "5000.00" },
  "flags": [] }
```

`valid_until` = `as_of` + 15 min by default (clients may reuse the price until then).

### `POST /v1/price/{domain}/batch`

`{ "items": [ <price request>, ... ] }` — max 500. Returns `{ "results": [ ... ] }` in the same order; each
item is either a price response or `{ "error": ... }`. One bad item doesn't fail the batch.

### `POST /v1/simulate/{domain}`

Same body as `/price` plus optional `"pack_version": 4` (try a draft) and public-signal overrides allowed.
Same response **without** `price_id`; nothing is audited, `reference_price` may be given explicitly.

---

## Packs (role: `editor` unless stated)

| Method & path | Body → Response |
|---|---|
| `GET /v1/packs/{domain}` | → `{ live_version, versions: [{version, status, owner, approver, created_at}] }` |
| `GET /v1/packs/{domain}/{version}` | → `{ version, status, yaml, lint_report, verify_report }` |
| `PUT /v1/packs/{domain}` | YAML text (`Content-Type: application/yaml`) → `201 { version, status: "draft" }` or 422 `pack_invalid` with every schema error |
| `POST /v1/packs/{domain}/{version}/lint` | → `{ ok, errors: [...], warnings: [...] }` |
| `POST /v1/packs/{domain}/{version}/verify` | `{ "cases": 10000 }` → `{ ok, cases, violations: [...first 10], badge: "verified_safe" }` |
| `POST /v1/packs/{domain}/{version}/impact` | `{ "last_n": 1000 }` → `{ compared, up_pct, down_pct, unchanged_pct, change_p10, change_p50, change_p90, new_floor_hits, new_ceiling_hits }` |
| `POST /v1/packs/{domain}/{version}/approve` | role `approver`, must differ from author → `{ status: "approved" }` |
| `POST /v1/packs/{domain}/{version}/publish` | role `approver`; requires lint ok + verify ok + approved → `{ live_version }` |
| `POST /v1/packs/{domain}/rollback` | role `approver`, `{ "to_version": 2 }` → `{ live_version }` |

---

## Audit & replay (role: `admin`)

| Method & path | Response |
|---|---|
| `GET /v1/audit/{price_id}` | full record: inputs, signals_used, reference_price, pack_version, owner, approver, waterfall, hash, prev_hash |
| `POST /v1/audit/verify` `{ "domain": "hotel" }` | `{ ok: true, rows_checked }` or `{ ok: false, first_broken_seq, price_id }` |
| `GET /v1/audit/export?domain=hotel&format=csv\|json&from=&to=` | file download |
| `POST /v1/replay` `{ "price_id": "...", "pack_version": 4? }` | `{ original, replayed, identical: true/false, diff }` — never writes audit |

## Admin (role: `admin`)

| Method & path | Body → Response |
|---|---|
| `POST /v1/admin/keys` | `{ role, domains, rate_limit_rps?, expires_at? }` → `{ key_id, api_key }` — **secret shown once** |
| `POST /v1/admin/keys/{key_id}/revoke` | → `{ revoked_at }`; effective on all replicas immediately |
| `PUT /v1/admin/policy` | `{ min_price?, max_price?, max_surge_x?, banned_signals?, require_two_person? }` |
| `POST /v1/admin/kill` | `{ domain, mode: "normal" \| "freeze" \| "base_only" }` |

## Ops (no auth)

| Path | Meaning |
|---|---|
| `GET /healthz` | process alive |
| `GET /readyz` | packs loaded + Redis reachable + self-test price correct → 200, else 503 |
| `GET /metrics` | Prometheus |
| `GET /v1/scoreboard` | measured KPIs |
