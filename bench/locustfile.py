"""Locust load test for /v1/price; reports req/s and p50/p95/p99.

    locust -f bench/locustfile.py --headless -u 32 -r 32 -t 20s --host http://127.0.0.1:8010 --csv bench/results/run

Needs a running server with a database and the seeded demo tenants (scripts/seed_demo.py writes
.demo-keys.json). At start it creates one load-test key per tenant with a high rate limit, using each
tenant's admin key, so the normal 200 req/s per-key limit doesn't cap the test.

M360_SCENARIO picks what is measured:
  many_chains  (default) real prices spread over 5 (tenant, domain) audit chains
  one_chain    real prices for ONE domain: every price appends to the same audit chain
  simulate     /v1/simulate: engine + API only, nothing written
Every request prices a different entity, so the per-entity lock and max_change anchor aren't what is measured."""
from __future__ import annotations

import itertools
import json
import os
import random
from pathlib import Path

import requests
from locust import FastHttpUser, constant, events, task

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = os.environ.get("M360_SCENARIO", "many_chains")
KEYS_FILE = Path(os.environ.get("M360_KEYS_FILE", ROOT / ".demo-keys.json"))
AS_OF = "2026-10-14T09:00:00Z"
_entity = itertools.count()

# (tenant, domain, body without the entity)
TARGETS = [
    ("hotel_001", "hotel", lambda: {"base_price": "3000", "signals": {"occupancy": round(random.random(), 2)}}),
    ("airline_002", "airline", lambda: {"signals": {"seats_sold": str(round(random.random(), 2)),
                                                    "days_to_departure": random.randint(0, 90),
                                                    "cabin": random.choice(["economy", "business"])}}),
    ("travel_003", "hotel", lambda: {"base_price": "2500", "signals": {"occupancy": round(random.random(), 2)}}),
    ("travel_003", "ride", lambda: {"signals": {"distance_km": random.randint(1, 40),
                                                "duration_min": random.randint(5, 90),
                                                "demand_ratio": round(1 + random.random() * 2, 1),
                                                "vehicle": random.choice(["bike", "auto", "sedan", "suv"])}}),
    ("travel_003", "parking", lambda: {"signals": {"hours": random.randint(1, 20),
                                                   "occupancy": str(round(random.random(), 2)),
                                                   "vehicle": random.choice(["bike", "car", "suv"])}}),
]
HEADERS: dict[str, dict[str, str]] = {}


@events.test_start.add_listener
def create_load_keys(environment, **_):
    """One key per tenant, all domains, 10,000 req/s, made with that tenant's admin key."""
    admin = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    for tenant in {t for t, _, _ in TARGETS}:
        r = requests.post(f"{environment.host}/v1/admin/keys", timeout=30,
                          headers={"Authorization": f"Bearer {admin[tenant]['admin']}"},
                          json={"role": "pricing", "domains": ["*"], "name": "load test", "rate_limit_rps": 10_000})
        r.raise_for_status()
        HEADERS[tenant] = {"Authorization": f"Bearer {r.json()['api_key']}", "Content-Type": "application/json"}


class Pricer(FastHttpUser):
    wait_time = constant(0)          # closed loop: each user sends its next request as soon as one returns

    @task
    def price(self):
        tenant, domain, make = TARGETS[0] if SCENARIO == "one_chain" else random.choice(TARGETS)
        body = {"entity": f"load-{next(_entity)}", "as_of": AS_OF, **make()}
        path = f"/v1/simulate/{domain}" if SCENARIO == "simulate" else f"/v1/price/{domain}"
        with self.client.post(path, data=json.dumps(body), headers=HEADERS[tenant], name=f"{SCENARIO}",
                              catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"{r.status_code} {r.text[:120]}")
