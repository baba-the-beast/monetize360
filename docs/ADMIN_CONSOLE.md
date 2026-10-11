# Admin console

A web page at **`/admin/`** where an organisation's admin runs their pricing without writing API calls: switch
the engine on or off per domain, upload rules, check them, publish them, set limits, hand out API keys and look
at every price ever given.

## How it is built (and why)

| Decision | Why |
|---|---|
| **It is only a client of the public API.** The page keeps the admin's API key in the browser tab and calls `/v1/...` with it. | There is no second, hidden way into the system. Everything the console can do, a customer's own tool can do, and every rule the API enforces (tenant isolation, roles, two-person approval) applies to the console automatically. |
| **No login system, no cookies, no sessions.** The API key is the identity. | Nothing new to secure. Cookies would need CSRF protection; a bearer key sent only by our own script does not. |
| **Key in `sessionStorage`**, never `localStorage`. | Closing the tab forgets the key. |
| **Static files, no build step, no framework** (`src/monetize360/api/console/`: one HTML, one CSS, four small JS modules). | Runs anywhere the API runs, nothing to install, and judges can read it. Served by the control app, so in Docker it is behind the same nginx. |
| **Strict Content-Security-Policy** (`default-src 'self'`), and the code never uses `innerHTML`. | A rule's `reason` or a key's name is text typed by someone. It is always shown as text, so it can never run as a script. A unit test fails if `innerHTML` ever appears. |
| **Fonts (Inter, JetBrains Mono) and icons (Lucide) are files in the repo.** | Free licences (OFL, ISC), no CDN, works offline at the demo. |

Three small API additions were needed, and they are useful to any client: `GET /v1/me` (whose key is this),
`GET /v1/audit?domain=` (newest prices first) and a `pack` field on `GET /v1/packs/{domain}/{version}` (the rules
as JSON, so a UI can show them without parsing YAML).

## Which API each screen uses

| Screen | What the admin does | API calls |
|---|---|---|
| Sign in | pastes an admin key | `GET /v1/me` |
| Sidebar status | sees if the system is healthy | `GET /readyz` every 15 s |
| Overview | sees domains, engine state, limits, health | `GET /v1/packs`, `GET /v1/admin/policy`, `GET /v1/scoreboard`, `GET /readyz` |
| Domains | switches the engine per domain: Dynamic / Base price only / Frozen | `GET /v1/packs`, `GET /v1/admin/policy`, `POST /v1/admin/kill` |
| Upload rules / New version | uploads or edits a YAML file; saved as a draft | `PUT /v1/packs/{domain}` |
| One domain | sees every version; rolls back | `GET /v1/packs/{domain}`, `POST /v1/packs/{domain}/rollback` |
| One version – Path to live | check → safety test → impact → approve → publish | `GET /v1/packs/{domain}/{version}`, `POST .../lint`, `.../verify`, `.../impact`, `.../approve`, `.../publish` |
| One version – Try a price | tests a price with that version; nothing is recorded | `POST /v1/simulate/{domain}` |
| Limits | sets organisation-wide price limits and banned signals | `GET` / `PUT /v1/admin/policy` |
| API keys | creates, lists and revokes keys | `GET` / `POST /v1/admin/keys`, `POST /v1/admin/keys/{id}/revoke` |
| Price history | lists prices, opens one, recalculates it, checks the records, exports | `GET /v1/audit`, `GET /v1/audit/{price_id}`, `POST /v1/replay`, `POST /v1/audit/verify`, `GET /v1/audit/export` |

## Start it

```sh
python scripts/setup_local_db.py --reset
python scripts/seed_demo.py              # writes .demo-keys.json
# PowerShell: $env:DATABASE_URL = "<from .env.local>"   (and REDIS_URL if you use Redis)
uvicorn --factory monetize360.api.app:create_app --port 8000
python scripts/demo_traffic.py           # in a second terminal: 30 sample hotel prices, so history and impact have data
```

Open <http://localhost:8000/admin/> and sign in with the `admin` key of `hotel_001` from `.demo-keys.json`.

## Demo script (about 6 minutes)

Say the sentence in *italics* while you do each step.

**1. Sign in.** Paste the admin key.
*"There is no separate login. The console is just another client of our API: the key decides which organisation
you are and what you may do. A pricing key is refused here."* (Try the `pricing` key first to show the refusal.)

**2. Overview.**
*"One screen: which domains are live, whether the engine is on, the organisation's limits, and system health.
The green dot at the bottom left turns red if the database goes away, and prices are still served."*

**3. Domains → the engine switch.** On `hotel`, click **Base price only**, confirm.
*"This is the admin choosing when to use the engine. Dynamic follows the rules. Base price only switches the
rules off. Frozen repeats the last price each item had. It reaches every server at once."*
Switch back to **Dynamic**.

**4. Open `hotel` → open version 1 → tab "Rules".**
*"The same YAML file, shown as a table a manager can read: what goes in, each rule with its reason, and the
floor and ceiling no price can cross."*

**5. Tab "Try a price".** Base price `3000`, occupancy `0.9`, **Calculate**.
*"Every price comes with its working: each step, why, and what the price would be without each rule. This test
is not recorded."*

**6. Change a rule.** Tab **YAML → Edit as a new version**. Change `value: 20` to `value: 30`. **Save as draft**.
*"A saved version never changes. Any change is a new version, and a draft affects no customer."*

**7. Path to live.** Click **Check rules**, then **Run safety test**, then **Preview impact**.
*"Step 1 finds mistakes and unfair rules. Step 2 prices ten thousand random requests and proves none breaks a
limit. Step 3 re-runs our real recent prices with the new rules: half of them, the busy nights, would rise
by about 8%, and the rest stay the same."*

**8. Approve.** Click **Approve with another key**. Paste your **own admin key** first: it is refused.
Then paste the `approver` key: accepted.
*"Whoever wrote the rules cannot approve them. That is enforced by the database, not by this page."*

**9. Publish**, confirm. Go back to `hotel`.
*"Version 2 is live on every server. And if it was a mistake:"* click **Roll back** on version 1.
*"One click, and the old rules are back."*

**10. A brand-new domain, no code.** First **API keys → Create key**: role `approver`, all domains (the seeded
approver key is limited to `hotel`). Copy it. Then **Domains → Upload rules**, choose
`packs/templates/parking.yaml`, save, run steps 1–2, approve with the new key, publish. Try a price.
*"A new industry is a file, not a release. The engine code did not change."*
(`python scripts/check_engine_frozen.py` proves it.)

**11. Limits.** Set **Highest price** to `100` → refused, with the exact clash. Then set **Highest surge** to `3`.
*"Limits for the whole organisation sit on top of every domain's rules. The system refuses a limit that
contradicts rules that are live instead of silently breaking prices."*

**12. API keys.** Show the key you created, then **Revoke** it.
*"The full key is shown once; we only store its fingerprint. A revoked key is refused at once on every server."*

**13. Price history.** Click **Check records**, then open any row and click **Recalculate**.
*"Every price is recorded before it is returned and sealed to the record before it. We can prove no record was
changed, and we can recalculate any old price from its record and get exactly the same answer."*

## What is not built

- The console needs an `admin` key. Editor and approver keys cannot sign in (the approver's key is pasted for
  the approval step only).
- No user accounts or passwords: identity is the API key.
- Creating a new organisation (tenant) is done by `scripts/seed_demo.py`, not in the console.
- The narrow-screen layout is written but was not tested on a phone.

## Developer portal (`/portal`)

The page for whoever connects an app to the engine. It uses the same frame, stylesheet and components as the
admin console (`js/core.js`, `app.css`), so the two cannot drift apart. Any role may sign in. Prices asked for
here are real: they go through `POST /v1/price` and are recorded.

| Page | What it does | API calls |
|---|---|---|
| Sign in | paste any key of the organisation | `GET /v1/me` |
| Get a price | pick a domain; the form lists exactly the inputs that domain takes; the answer shows the price, every step and the `price_id`; the same request is shown as curl, Python and JavaScript; a table lists the prices this tab was given | `GET /v1/domains`, `POST /v1/price/{domain}` (or `/v1/simulate/{domain}` when "Preview only" is ticked) |
| API key | the key you are signed in with. An admin can create a `pricing` key here and switch the portal to it ("Use it here") | `POST /v1/admin/keys` (admin only) |
| Reference | the calls, the response fields and what each refusal means | none |

Demo (1 minute): open `/portal`, sign in with the admin key, **API key → Create key → Use it here** (the role
at the bottom left turns to `pricing` and the admin link disappears). On **Get a price** set occupancy to `0.9`
and press **Get price**: a real price with a `price_id`. Open the admin console's **Price history**: the same
price is there. *"This is everything a customer's developer needs: a key, the inputs, and code to paste."*
