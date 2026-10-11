"""Create demo tenants (hotel_001, airline_002), keys, policies; publish template packs.

    python scripts/seed_demo.py                  # local Postgres from .env.local (run setup_local_db.py first)
    OWNER_DATABASE_URL=postgresql://m360:...@postgres:5432/monetize360 python scripts/seed_demo.py     # Docker

Runs as the database OWNER: creating tenants is an operator action the API's own account can't do.
Safe to re-run: tenants and packs are kept, the demo tenants' keys are replaced. New keys are written to
.demo-keys.json (git-ignored) — the only place the secrets exist; the database stores hashes."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from monetize360.security import keys as keylib  # noqa: E402

KEYS_FILE = ROOT / ".demo-keys.json"

# tenant -> (display name, {domain: template file}, {key label: (role, domains)})
TENANTS = {
    "hotel_001": ("Sunrise Hotels", {"hotel": "hotel.yaml"}, {
        "pricing": ("pricing", ["hotel"]), "editor": ("editor", ["hotel"]),
        "approver": ("approver", ["hotel"]), "admin": ("admin", ["*"])}),
    "airline_002": ("SkyWays Airlines", {"airline": "airline.yaml"}, {
        "pricing": ("pricing", ["airline"]), "admin": ("admin", ["*"])}),
    # one company, several domains: shows a key scoped to ONE of its own domains
    "travel_003": ("GoTrip Travel", {"hotel": "hotel.yaml", "ride": "ride.yaml", "parking": "parking.yaml"}, {
        "pricing_hotel_only": ("pricing", ["hotel"]), "pricing_all": ("pricing", ["*"]), "admin": ("admin", ["*"])}),
}


def seed(owner_url: str, *, env: str = "test") -> dict[str, dict[str, str]]:
    """Returns {tenant: {label: api_key}}. The secrets are returned once and stored nowhere else by this function."""
    issued: dict[str, dict[str, str]] = {}
    with psycopg.connect(owner_url) as conn:
        for tenant_id, (name, domains, key_specs) in TENANTS.items():
            conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                         (tenant_id, name))
            conn.execute("INSERT INTO tenant_policy (tenant_id) VALUES (%s) ON CONFLICT (tenant_id) DO NOTHING",
                         (tenant_id,))
            for domain, template in domains.items():
                text = (ROOT / "packs/templates" / template).read_text(encoding="utf-8")
                conn.execute(
                    "INSERT INTO packs (tenant_id, domain, version, status, yaml, owner, approver) "
                    "VALUES (%s, %s, 1, 'published', %s, 'seed', 'seed-approver') "
                    "ON CONFLICT (tenant_id, domain, version) DO NOTHING", (tenant_id, domain, text))
                conn.execute(
                    "INSERT INTO pack_pointer (tenant_id, domain, live_version) VALUES (%s, %s, 1) "
                    "ON CONFLICT (tenant_id, domain) DO NOTHING", (tenant_id, domain))
            conn.execute("DELETE FROM api_keys WHERE tenant_id = %s", (tenant_id,))
            issued[tenant_id] = {}
            for label, (role, scope) in key_specs.items():
                key_id, api_key, secret_hash = keylib.generate(env)
                conn.execute(
                    "INSERT INTO api_keys (key_id, tenant_id, secret_sha256, role, domains) "
                    "VALUES (%s, %s, %s, %s, %s)", (key_id, tenant_id, secret_hash, role, scope))
                issued[tenant_id][label] = api_key
    return issued


if __name__ == "__main__":
    url = os.environ.get("OWNER_DATABASE_URL")
    if not url:
        from setup_local_db import read_env, urls
        local = read_env()
        url = urls(local["POSTGRES_DB"], local)["owner"]
    result = seed(url)
    KEYS_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for tenant, labels in result.items():
        shown = ", ".join(f"{label} ({key.split('_')[2]})" for label, key in labels.items())
        print(f"{tenant}: {shown}")
    print(f"\nkeys written to {KEYS_FILE.name} (git-ignored). Use one as:  Authorization: Bearer <key>")
