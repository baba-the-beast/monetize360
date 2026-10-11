"""Asks the running server for some hotel prices, so the console's Price history and Impact preview have data.

    python scripts/demo_traffic.py                     # 30 prices from http://127.0.0.1:8000
    python scripts/demo_traffic.py http://host:8080 100

Uses the hotel_001 pricing key from .demo-keys.json (written by scripts/seed_demo.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
OCCUPANCY = ("0.35", "0.55", "0.72", "0.85", "0.92", "0.97")


def main(base_url: str, count: int) -> int:
    key = json.loads((ROOT / ".demo-keys.json").read_text())["hotel_001"]["pricing"]
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {key}"}, timeout=30) as client:
        for n in range(count):
            # an ordinary Wednesday, quiet to nearly full, so a change to the occupancy rule moves some prices
            body = {"entity": f"room-{100 + n}@2026-10-14", "base_price": str(1800 + 200 * (n % 7)),
                    "signals": {"occupancy": OCCUPANCY[n % len(OCCUPANCY)]}, "as_of": "2026-10-14T09:00:00Z"}
            r = client.post("/v1/price/hotel", json=body)
            if r.status_code != 200:
                print(f"request {n} failed: {r.status_code} {r.text[:200]}")
                return 1
    print(f"{count} hotel prices issued and recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000",
                  int(sys.argv[2]) if len(sys.argv) > 2 else 30))
