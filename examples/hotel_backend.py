"""A customer's side of the integration: what a hotel's own booking server does to use Monetize360.

It knows three things about each room tonight (its normal price, how full the hotel is) and asks the engine
what to charge. That is the whole integration: one HTTPS call, standard library only.

    python examples/hotel_backend.py            # price tonight's rooms once, and show the working for one
    python examples/hotel_backend.py --watch    # ask again every 3 s: change something in the admin console
                                                # (switch the engine off, publish a new rule) and watch it land

The key comes from M360_API_KEY; for the demo it falls back to hotel_001's pricing key in .demo-keys.json."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

print = partial(print, flush=True)      # show each line at once, also when piped
API = os.environ.get("M360_URL", "http://127.0.0.1:8001")
ROOT = Path(__file__).resolve().parents[1]

# What the hotel's own system already knows. Nothing here is about pricing rules: those live in the engine.
ROOMS = [
    {"room": "standard-101", "normal_price": "2000", "occupancy": "0.45"},
    {"room": "deluxe-204", "normal_price": "3000", "occupancy": "0.85"},
    {"room": "suite-501", "normal_price": "3500", "occupancy": "0.92"},
    {"room": "family-310", "normal_price": "2600", "occupancy": "0.70"},
]


def api_key() -> str:
    key = os.environ.get("M360_API_KEY")
    if not key:
        key = json.loads((ROOT / ".demo-keys.json").read_text())["hotel_001"]["pricing"]
    return key


def ask_prices(key: str, night: str) -> list[dict]:
    """One call for all rooms. Each answer is a price with its working, or an error for that room only."""
    items = [{"entity": f"{r['room']}@{night}", "base_price": r["normal_price"],
              "signals": {"occupancy": r["occupancy"]}} for r in ROOMS]
    request = urllib.request.Request(   # noqa: S310 - API is our own http(s) address
        f"{API}/v1/price/hotel/batch", data=json.dumps({"items": items}).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:   # noqa: S310
            return json.load(response)["results"]
    except urllib.error.HTTPError as e:
        sys.exit(f"the engine refused the request: {e.code} {json.load(e).get('message')}")
    except urllib.error.URLError as e:
        sys.exit(f"can't reach {API}: {e.reason}")


def show(results: list[dict]) -> None:
    print(f"{'room':<14} {'normal':>8} {'full':>5} {'charge':>10}   why")
    for room, answer in zip(ROOMS, results, strict=True):
        if "error" in answer:
            print(f"{room['room']:<14} {room['normal_price']:>8} {room['occupancy']:>5} {'-':>10}   {answer['error']}")
            continue
        reasons = [step["reason"] for step in answer["waterfall"] if step["kind"] != "base"] + answer["flags"]
        print(f"{room['room']:<14} {room['normal_price']:>8} {float(room['occupancy']):>5.0%} "
              f"{answer['price']:>10}   {'; '.join(reasons) or 'no rule applied'}")


def show_working(answer: dict) -> None:
    print(f"\nHow {answer['price']} {answer['currency']} was reached (price_id {answer['price_id']}):")
    for step in answer["waterfall"]:
        change = "" if step["kind"] == "base" else f"{float(step['delta']):+.2f}"
        print(f"  {step['reason']:<34} {change:>10}  ->  {step['after']:>10}")
    for other in answer["counterfactuals"]:
        if "price" in other:
            print(f"  ({other['if']}: {other['price']})")


if __name__ == "__main__":
    key, night = api_key(), time.strftime("%Y-%m-%d")
    if "--watch" not in sys.argv:
        results = ask_prices(key, night)
        show(results)
        priced = [r for r in results if "error" not in r]
        if priced:
            show_working(max(priced, key=lambda r: len(r["waterfall"])))
        sys.exit(0)
    try:
        while True:
            results = ask_prices(key, night)
            print(f"\n--- {time.strftime('%H:%M:%S')}  rules v{results[0].get('pack_version', '?')} ---")
            show(results)
            time.sleep(3)
    except KeyboardInterrupt:
        pass
