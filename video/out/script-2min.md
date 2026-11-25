# Insidious demo: narration script

Total runtime **1:50.97** (111.0 s). Narrator: Sarvam AI **bulbul:v4-flash**, speaker `deven_en_conversation`, `en-IN`, pace 1.22. Tanglish voice-note line: `ta-IN`. Edit `narration.json`, then run `./render.sh`.

Money figures are labelled as estimates on screen: ₹1,800 average order × 600 orders/hour; ≈7% fewer orders per extra second; checkout under 1 s → 4.8 s measured ⇒ ≈28% ⇒ 168 orders ⇒ ₹3,02,400 per hour.

## 0:00.00–0:11.47 · Sale night (`hook`)

- **0:01.20–0:10.97** `c1`: Diwali sale night, eight PM. Ravi's handmade jutti store runs on his own website. Last year it froze at the worst moment, and Ravi can't read a server dashboard.

## 0:11.47–0:31.82 · Live incident · detect (`detect`)

- **0:11.17–0:21.37** `c4a`: Insidious watches his store. On a real surge, in about thirty seconds, its guardian fires an incident. Checkout is crawling at nearly eight seconds.
- **0:21.72–0:26.84** `c4m`: The console prices it: about five point three lakh rupees an hour at risk.
- **0:27.18–0:31.32** `c4b`: NudgeBee's AI finds the root cause, and Claude writes a plan.

## 0:31.82–0:51.42 · Live incident · ask Ravi (`chat`)

- **0:31.52–0:40.66** `c5a`: Ravi asks how the shop is doing, in Tamil. He can use whatever language he thinks in: Tamil, Hindi, Tanglish or English, by voice or text. Sarvam understands, and replies in kind.
- **0:41.01–0:44.08** `c5a2`: Claude's plan arrives as a Tanglish voice note, with three buttons.
- **0:44.43–0:50.22** `c5b`: Pause extras and add servers for about eight rupees an hour, only pause extras, or do nothing. Ravi taps one.

## 0:51.42–1:10.30 · Live incident · fix (`fix`)

- **0:51.12–1:01.32** `c6a`: One tap. Flagsmith switches off the heavy extras. Kubernetes adds a server. The database pool doubles. Checkout drops to under half a second, as NudgeBee measured.
- **1:01.72–1:09.70** `c6b`: A second approved plan added a real AWS read replica, and Flagsmith moved reads onto it.

## 1:10.30–1:14.13 · Live incident · proof (`rca`)

- **1:10.00–1:12.73** `c7`: NudgeBee's own AI confirms it, independently.

## 1:14.13–1:22.83 · The money outcome (`outcome`)

- **1:13.83–1:22.03** `c8`: So, four to eight rupees an hour protects about five lakh an hour, by the console's estimate. It pays for itself, and Ravi never opened a terminal.

## 1:22.83–1:44.43 · Under the hood (`hood`)

- **1:22.53–1:29.41** `c9a`: Under the hood: thirteen Flagsmith flags, from load-shedding and the database pool to an AI kill switch.
- **1:29.71–1:34.78** `c9b`: NudgeBee finds root causes, and a NuBi-built automation warms everything up.
- **1:35.08–1:37.98** `c9c`: Sarvam listens, understands, and speaks.
- **1:38.28–1:43.83** `c9d`: And insidious scan found twenty-seven flags in Jeffi's real code. All on AWS, in Mumbai.

## 1:44.43–1:50.97 · Close (`close`)

- **1:44.13–1:48.77** `c10`: Insidious. Festive-season peace of mind, for every self-hosted store.
