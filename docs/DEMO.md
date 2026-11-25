# Demo video: 3-minute shot list

One take of the live system at https://festive.aloysjehwin.com. Screen layout: browser on `/ops` (left), phone
mirror with WhatsApp (right), a terminal at the bottom. Show real timestamps; don't speed up the recovery.

## Before recording

- Reset: `make reset` (flags at defaults, store-api 1 replica, `db_pool` back to 5). Guardian armed: `insidious guard`.
- Flagsmith dashboard open on the environment's Features page (to show the flip and the audit log).
- `ops_voice` = `ta-IN`, `tanglish`, pace 1.25. The founder's phone messaged the bot within the last 24 h.
- Load generator ready (`make festive-rush` or k6 against the public URL).
- Never show `.env`, the passcode, or the Flagsmith/Anthropic keys on screen.

## Shot list

| Time | Shot | Say / show | Proves |
|---|---|---|---|
| 0:00–0:15 | Store home page | "Ravi runs Jeffi on his own stack. Sale night, 8 PM, checkout crawls, and the person on call can't read Grafana. He lives on WhatsApp." | Problem, named user |
| 0:15–0:30 | `/ops` cockpit, green | p95 under 800 ms, 1 replica, DB pool 5, flags panel. "k3s on EC2 in Mumbai, RDS Postgres, behind Cloudflare." | Real deployment |
| 0:30–0:50 | Start the surge | `/ops` p95 climbs to ~4.8 s, DB pool 5/5 busy. Guardian fires at ~30 s; the feed shows the incident and the NudgeBee event. | Guardian + NudgeBee webhook |
| 0:50–1:15 | Phone | Tanglish voice note arrives with text and buttons. Play 5–8 s. Text: "Checkout is very slow right now (about 5 seconds instead of under 1) ... pausing product suggestions, adding one more server and giving the database more room. About ₹4 more per hour." | Sarvam (Bulbul, Mayura) + Claude plan |
| 1:15–1:25 | Optional cut | NudgeBee UI: the event and AI investigation the agent pulled in. | NudgeBee RCA in the plan |
| 1:25–1:40 | Tap "recommended" | Reply comes back: done. Cockpit feed: `heavy_features` OFF, store-api scaled to 2, `db_pool` 10. | Founder approval gate |
| 1:40–1:55 | Flagsmith dashboard | `heavy_features` off (children shed via dependency), `db_pool` value changed, audit log entry from the Admin API. | Flagsmith as control plane |
| 1:55–2:15 | `/ops` | p95 drops back under the SLO, pods 1 → 2, guardian resolves the incident by itself. | End-to-end recovery |
| 2:15–2:30 | `/ops` flag panel | Enter passcode, toggle `heavy_features` back ON from the cockpit; Flagsmith shows the change. | Admin API from the cockpit |
| 2:30–2:45 | Terminal | `insidious scan` on the Jeffi Stores monorepo (or open `examples/jeffi-scan/docs/scan-report.md`): 8 services, 310 routes, 43 env toggles, 27 flags proposed, 8 under `heavy_features`. "Never reads .env." | Works on a real codebase |
| 2:45–3:00 | README sponsor table | "Remove Flagsmith: no levers, only redeploys. Remove NudgeBee: no root cause or incident trail. Remove Sarvam: Ravi can't run his own ops." Repo URL. | Remove-one-tool test |

## Optional extras (if time allows)

- Ravi says "English" (or "Tamil") in a voice note; the next reply switches style.
- Ravi asks "ippo epdi irukku?" ("how is it now?") and gets a voice status.

## Say on camera

Payments are mocked. The cluster is single-node k3s, so "one more server" is one more replica on the same node. The
NudgeBee workflow guides are in the repo but the workflows aren't built in the UI yet.
