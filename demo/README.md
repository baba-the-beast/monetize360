# Demo files: upload these live in the admin console

Every file in `packs/` is a complete, ready-to-upload pricing pack. Each header says what it shows, how to upload it,
and the **exact price** each sample request should give (computed by the engine, checked by
`tests/unit/test_demo_packs.py`, so the numbers on screen will match).

| File | Industry | What it shows |
|---|---|---|
| `01-hotel.yaml` | Hotel rooms | Occupancy, weekends, festivals from the pack's own calendar |
| `02-airline.yaml` | Airline seats | Fare buckets, days-to-departure curve, cabin class |
| `03-ride.yaml` | Ride hailing | Fare from km + minutes, demand surge, night surcharge |
| `04-retail.yaml` | Retail | Cost + margin, overstock vs low stock, bulk tiers |
| `05-parking.yaml` | Parking | Hours parked, vehicle type, nearly full lot, weekends |
| `06-ev_charging.yaml` | EV charging | Grid price + 40%, charger speed, time of day, members |
| `07-event_tickets.yaml` | Event tickets | Section, sell-through vs last minute, early bird |
| `08-saas.yaml` | SaaS | Plan × region table, seat tiers, annual discount |
| `09-cloud_gpu.yaml` | Cloud GPU (USD) | GPU type, utilisation curve, spot discount |
| `10-food_delivery.yaml` | Food delivery | Distance bands, rain + few riders |
| `11-hotel-v2-weekend-20.yaml` | Hotel, new version | Weekend +10% → +20%: shows Impact, approve, publish, rollback |

`packs/should-fail/` has one file per safety gate. Each is meant to be stopped:

| File | Stopped by | What the audience sees |
|---|---|---|
| `gate1-typo-hotel.yaml` | Gate 1, schema | Upload refused at once: `ceilng` is not a field. Nothing is saved |
| `gate2-unfair-hotel.yaml` | Gate 2, linter | Saved as a draft, then Check fails: *"Fairness violation: 'religion' is a protected attribute"*. Can't be approved |
| `gate3-overlap-retail.yaml` | Gate 3, fuzz verifier | Lint passes, but the Safety test runs 10,000 requests and shows the smallest breaking input: `inventory = 501` |

## Before the demo

```sh
python scripts/setup_local_db.py --reset
python scripts/seed_demo.py              # keys go to .demo-keys.json
uvicorn --factory monetize360.api.app:create_app --port 8000
python scripts/demo_traffic.py           # 30 hotel prices, so Impact has real data
```

Open <http://localhost:8000/admin/> and sign in with the **admin** key of `hotel_001` from `.demo-keys.json`.
The console needs an admin key; approving asks for a *second* person's approver key, pasted once and not kept.
The seeded approver key only covers `hotel`, so for other industries first create one: **API keys → Create key**,
role `approver`, all domains.

## Suggested order (about 8 minutes)

1. **A new industry with no code** — Domains → Upload rules → drop `06-ev_charging.yaml`. Check → Safety test
   ("Verified Safe", 10,000 cases) → Approve with the approver key → Publish. Try a price from the file's header:
   fast charger at 6 pm for a member → **INR 19.50**. *"No engine code changed. The rules are a file."*
2. **Same engine, very different rules** — upload `03-ride.yaml` or `09-cloud_gpu.yaml` the same way.
3. **Gate 1** — drop `gate1-typo-hotel.yaml`: refused before it is even saved.
4. **Gate 2** — drop `gate2-unfair-hotel.yaml`: saved, Check fails on fairness, Approve stays blocked.
   *"A price can't depend on who someone is. The system won't let it go live."*
5. **Gate 3** — drop `gate3-overlap-retail.yaml`: looks fine to the linter; the Safety test finds `inventory = 501`.
   *"We don't trust the rule author, or ourselves: 10,000 generated requests, and the smallest one that breaks."*
6. **A change to live prices** — on Hotel, upload `11-hotel-v2-weekend-20.yaml`. Impact shows how recent real prices
   would move *before* anyone approves. Approve, publish; a Saturday room goes **INR 3,300 → 3,600**.
   Then roll back with one click.

The files are copies of `packs/templates/*.yaml` with a demo header; if a template changes, regenerate them so the
two stay the same.
