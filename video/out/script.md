# Insidious demo: narration script

Total runtime **3:05.40** (185.4 s). Narrator: Sarvam AI **bulbul:v4-flash**, speaker `simran_en_conversation`, `en-IN`, pace 1.2. Tanglish voice-note line: `ta-IN`. Edit `narration.json`, then run `./render.sh`.

Money figures are labelled as estimates on screen: ₹1,800 average order × 600 orders/hour; ≈7% fewer orders per extra second; checkout under 1 s → 4.8 s measured ⇒ ≈28% ⇒ 168 orders ⇒ ₹3,02,400 per hour.

## 0:00.00–0:14.62 · Sale night (`hook`)

- **0:01.80–0:14.02** `c1`: Diwali sale night. Eight PM. Ravi runs Jeffi Stores, a handmade jutti brand, on his own website. Last year, it froze at the worst possible moment. And Ravi can't read a server dashboard.

## 0:14.62–0:39.75 · The money problem (`money`)

- **0:14.32–0:38.85** `c2`: Let's talk money, and these are estimates. An average order is eighteen hundred rupees. Sale night brings six hundred orders an hour. A common rule of thumb: every extra second of delay costs about seven percent of orders. Checkout went from under a second to almost five. That's about twenty-eight percent fewer orders. A hundred and sixty-eight orders. Roughly three lakh rupees, lost, every hour.

## 0:39.75–0:57.30 · Meet Insidious (`meet`)

- **0:39.45–0:43.39** `c3a`: Meet Insidious. One SDK, and three commands.
- **0:43.69–0:56.50** `c3b`: It watches checkout speed, the database and the servers. When something breaks, a Claude AI agent plans the fix. Then Ravi approves it on WhatsApp, in his own language. Nothing changes without his yes.

## 0:57.30–1:18.54 · Live incident · detect (`detect`)

- **0:57.00–1:05.84** `c4a`: Here's our live console during a real surge. In about thirty seconds, the guardian fires an incident. Checkout is crawling at nearly eight seconds.
- **1:06.19–1:10.40** `c4m`: The console prices it too: about five point three lakh rupees an hour at risk.
- **1:10.75–1:17.94** `c4b`: NudgeBee opens an incident, and its AI starts a root-cause analysis. Claude pulls in all the evidence, and writes a plan.

## 1:18.54–1:48.39 · Live incident · ask Ravi (`chat`)

- **1:18.24–1:29.65** `c5a`: Ravi asks how the shop is doing, in a Tamil voice note. He can use whatever language he thinks in: Tamil, Hindi, Tanglish or English, by voice or text. Sarvam understands, and replies in kind.
- **1:30.10–1:34.58** `c5a2`: Then Sarvam turns Claude's plan into a Tanglish voice note, with three buttons.
- **1:35.03–1:47.09** `c5b`: Each option is priced. Pause extras, add two servers and widen the database: about eight rupees an hour. Or only pause extras and widen the database. Or do nothing for now. Ravi taps one.

## 1:48.39–2:12.19 · Live incident · fix (`fix`)

- **1:48.09–2:00.04** `c6a`: One tap. Flagsmith switches off the heavy extras. Kubernetes adds two servers. The database gets more room. Checkout drops back under a second, down to a hundred and eighty milliseconds, with zero errors.
- **2:00.44–2:11.40** `c6b`: In another run, Ravi chose a database copy, and the agent created a real AWS read replica, with Flagsmith moving reads onto it, live.

## 2:12.19–2:18.09 · Live incident · proof (`rca`)

- **2:11.90–2:16.49** `c7`: Even NudgeBee's own AI investigator confirms what happened, independently.

## 2:18.09–2:29.77 · The money outcome (`outcome`)

- **2:17.79–2:28.77** `c8`: So, about eight rupees an hour, to protect around three lakh rupees an hour. The fix pays for itself thousands of times over. And Ravi never opened a terminal. He just tapped a button.

## 2:29.77–2:58.22 · Under the hood (`hood`)

- **2:29.47–2:37.00** `c9a`: Under the hood, Flagsmith is the control panel. Thirteen flags, from a load-shedding switch and a live database pool, to an AI kill switch.
- **2:37.35–2:44.27** `c9b`: NudgeBee watches the cluster and finds root causes. Before the sale, a NuBi-built automation warms everything up.
- **2:44.62–2:49.66** `c9c`: Sarvam listens, understands, and speaks Ravi's language.
- **2:50.01–2:57.42** `c9d`: And insidious scan found twenty-seven flags in Jeffi's real codebase. It all runs on AWS, in Mumbai.

## 2:58.22–3:05.40 · Close (`close`)

- **2:57.92–3:03.21** `c10`: Insidious. Festive-season peace of mind, for every self-hosted store.
