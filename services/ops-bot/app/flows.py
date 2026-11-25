"""Business flows: founder messages, approvals, status, why, notify, flag feed, readiness brief."""
from __future__ import annotations

import asyncio
import re
import time
import unicodedata

import httpx

from . import audio
from .agent import AgentClient, AgentError
from .config import Settings
from .log import log
from .nudgebee import NudgeBeeClient, NudgeBeeError, conversation_answer, find_waiting_approval
from .sarvam import LANG_NAMES, SarvamClient, SarvamCreditsError, SarvamError
from .store import Store, now_iso
from .voiceconf import VoiceConfig
from .whatsapp import Inbound, WhatsAppClient

HEAVY_FLAGS = ("heavy_features", "recommendations", "hd_images", "live_reviews")

SCRIPT_LANG = [
    (r"[஀-௿]", "ta-IN"), (r"[ऀ-ॿ]", "hi-IN"), (r"[ঀ-৿]", "bn-IN"),
    (r"[ఀ-౿]", "te-IN"), (r"[ಀ-೿]", "kn-IN"), (r"[ഀ-ൿ]", "ml-IN"),
    (r"[઀-૿]", "gu-IN"), (r"[਀-੿]", "pa-IN"),
]


def script_language(text: str) -> str | None:
    for pat, code in SCRIPT_LANG:
        if re.search(pat, text or ""):
            return code
    return None


# ---------- founder reply style: native script or Tanglish ----------

STYLES = ("native", "tanglish")
TANGLISH_GOAL = ("Write the reply in Tanglish: Tamil in English (Latin) letters mixed naturally with English words, "
                 "like Chennai WhatsApp chats. Do not use Tamil script.")


def _style(v) -> str | None:
    v = str(v or "").strip().lower()
    return v if v in STYLES else None


def _language(v) -> str | None:
    v = str(v or "").strip()
    if v in LANG_NAMES:
        return v
    v2 = v.lower() + "-IN"
    return v2 if v2 in LANG_NAMES else None


def resolve_voice(founder: dict, remote: dict, default_style: str) -> tuple[str, str]:
    """(language, style) for replies. Priority: the founder's switch message > Flagsmith ops_voice language/style
    > detected from his messages > env (FOUNDER_LANGUAGE / FOUNDER_STYLE). Tanglish always means ta-IN."""
    lang = founder.get("language") or "hi-IN"
    if founder.get("switched"):
        style = _style(founder.get("style")) or "native"
    else:
        style = _style(founder.get("style")) or _style(default_style) or "native"
        raw_lang = str((remote or {}).get("language") or "").strip()
        r_style = _style((remote or {}).get("style")) or ("tanglish" if raw_lang.lower() == "tanglish" else None)
        r_lang = "ta-IN" if raw_lang.lower() == "tanglish" else _language(raw_lang)
        if r_lang:
            lang = r_lang
            if r_lang != "ta-IN":
                style = "native"
        if r_style:
            style = r_style
    return ("ta-IN" if style == "tanglish" else lang), style


# Switch messages: "tanglish", "tamil la pesu", "hindi mein bolo", "english please", "తెలుగు" ... (<= 6 words).
SWITCH_KEYS = {
    "tanglish": ("ta-IN", "tanglish"), "thanglish": ("ta-IN", "tanglish"), "tamglish": ("ta-IN", "tanglish"),
    "தங்கிலீஷ்": ("ta-IN", "tanglish"), "டாங்கிலிஷ்": ("ta-IN", "tanglish"), "டேங்லிஷ்": ("ta-IN", "tanglish"),
    "tamil": ("ta-IN", "native"), "thamizh": ("ta-IN", "native"), "tamizh": ("ta-IN", "native"),
    "thamil": ("ta-IN", "native"), "தமிழ்": ("ta-IN", "native"), "தமிழில்": ("ta-IN", "native"),
    "hindi": ("hi-IN", "native"), "हिंदी": ("hi-IN", "native"), "हिन्दी": ("hi-IN", "native"),
    "english": ("en-IN", "native"), "angrezi": ("en-IN", "native"), "इंग्लिश": ("en-IN", "native"),
    "अंग्रेज़ी": ("en-IN", "native"), "अंग्रेजी": ("en-IN", "native"), "ஆங்கிலம்": ("en-IN", "native"),
    "இங்கிலீஷ்": ("en-IN", "native"),
    "telugu": ("te-IN", "native"), "తెలుగు": ("te-IN", "native"),
    "kannada": ("kn-IN", "native"), "ಕನ್ನಡ": ("kn-IN", "native"),
    "malayalam": ("ml-IN", "native"), "മലയാളം": ("ml-IN", "native"),
    "bengali": ("bn-IN", "native"), "bangla": ("bn-IN", "native"), "বাংলা": ("bn-IN", "native"),
    "marathi": ("mr-IN", "native"), "मराठी": ("mr-IN", "native"),
    "gujarati": ("gu-IN", "native"), "ગુજરાતી": ("gu-IN", "native"),
}
SWITCH_KEYS = {unicodedata.normalize("NFC", k): v for k, v in SWITCH_KEYS.items()}
SWITCH_SUFFIXES = ("la", "le", "lo", "il", "me", "mein", "u", "ல", "லே", "ல்", "லயே")
SWITCH_FILLER = {
    "in", "la", "le", "lo", "il", "me", "mein", "mai", "main", "mujhse", "se", "pesu", "pesunga", "pesungal",
    "pesalam", "reply", "replies", "respond", "answer", "pannu", "pannunga", "bolo", "boliye", "baat", "karo",
    "kariye", "speak", "talk", "write", "use", "switch", "change", "set", "to", "please", "pls", "plz", "only",
    "now", "from", "onwards", "language", "mode", "style", "bhasha", "mozhi", "can", "you", "u", "with",
    "matladu", "matladandi", "maathadi", "mathadi", "samsarikku", "bolun", "bola", "ok", "okay",
    "ல", "பேசு", "பேசுங்க", "பேசுங்கள்", "में", "बोलो", "बोलिए", "बात", "करो", "कीजिए", "भाषा",
    "hey", "hi", "hello", "could", "would", "will", "back", "just", "kindly", "i", "want", "need", "prefer",
    "on", "forward", "lets", "let", "us", "we", "sir", "bro", "da", "machan", "me", "talking", "speaking",
    "replying", "reply", "messages", "message", "voice", "notes", "note", "and", "text", "texts", "instead",
    "start", "keep", "continue", "be", "is", "it", "all", "the", "of", "your", "my", "for", "am", "are",
}
SWITCH_FILLER = {unicodedata.normalize("NFC", w) for w in SWITCH_FILLER}


def _switch_tokens(text: str) -> list[str]:
    t = unicodedata.normalize("NFC", text or "").lower()
    t = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in t)
    return t.split()


def parse_switch(text: str) -> tuple[str, str] | None:
    """A short "reply in X" message -> (language, style), else None. Every other word must be filler, so a real
    question that mentions a language ("tamil customers ku enna aachu") is not a switch."""
    words = _switch_tokens(text)
    if not words or len(words) > 12:
        return None
    found: set[tuple[str, str]] = set()
    for w in words:
        hit = SWITCH_KEYS.get(w)
        if hit is None:
            hit = next((v for k, v in SWITCH_KEYS.items() if w.startswith(k) and w[len(k):] in SWITCH_SUFFIXES), None)
        if hit:
            found.add(hit)
        elif w not in SWITCH_FILLER:
            return None
    return found.pop() if len(found) == 1 else None


# Common Tamil words people type in Latin letters; two different ones in a typed message = Tanglish.
TANGLISH_WORDS = {
    "enna", "yenna", "ennachu", "epdi", "eppadi", "irukku", "iruku", "irukka", "irukkanga", "illa", "illai",
    "pannu", "pannunga", "panna", "panni", "panren", "pannren", "pannalam", "pannitten", "seri", "sari", "romba",
    "venum", "venam", "vendam", "kadai", "kada", "sollu", "sollunga", "eppo", "ippo", "inniku", "innaiku", "naan",
    "unga", "enga", "da", "machan", "macha", "nalla", "aachu", "achu", "konjam", "paaru", "paarunga", "theriyum",
    "theriyala", "puriyala", "mudiyala", "kelu", "kelunga", "yen", "vandhu", "podu", "evlo", "evvalavu",
}


def looks_tanglish(text: str) -> bool:
    """Best-effort: a Latin-script message with >= 2 different common Tamil words."""
    if not text or script_language(text):
        return False
    return len(set(re.findall(r"[a-z]+", text.lower())) & TANGLISH_WORDS) >= 2


def _num(v, default="?"):
    if v is None or v == "":
        return default
    try:
        f = float(v)
        return str(int(round(f)))
    except (TypeError, ValueError):
        return str(v)


# ---------- fallback phrasing (used when SARVAM_MOCK=1 or the LLM is unavailable) ----------

def action_phrase(action: str, numbers: dict, lang: str) -> str:
    n = _num(numbers.get("replicas_to"), "")
    hi = lang == "hi-IN"
    if action == "shed_heavy_features":
        base = "recommendations जैसे भारी फ़ीचर बंद कर दूँ" if hi else "turn off heavy features like recommendations"
        if n:
            base += f" और {n} सर्वर चला दूँ" if hi else f" and run {n} servers"
        return base
    if action == "scale_out":
        return f"सर्वर बढ़ाकर {n or 3} कर दूँ" if hi else f"add servers, up to {n or 3}"
    if action == "failover_payment":
        return "पेमेंट Cashfree पर बदल दूँ" if hi else "switch payments to Cashfree"
    if action == "ignore":
        return "अभी कुछ न करूँ" if hi else "do nothing for now"
    return action.replace("_", " ")


def done_phrase(action: str, numbers: dict, approved: bool, lang: str) -> str:
    hi = lang == "hi-IN"
    if not approved:
        return "ठीक है, रद्द कर दिया। कुछ नहीं बदला।" if hi else "Okay, rejected. Nothing will change."
    n = _num(numbers.get("replicas_to"), "")
    if action == "shed_heavy_features":
        if hi:
            return "मंज़ूर: भारी फ़ीचर बंद कर रहे हैं" + (f" और {n} सर्वर चला रहे हैं।" if n else "।")
        return "Approved: turning off recommendations and heavy features" + (f" and running {n} servers." if n else ".")
    if action == "scale_out":
        return f"मंज़ूर: सर्वर बढ़ाकर {n or 3} कर रहे हैं।" if hi else f"Approved: adding servers, up to {n or 3}."
    if action == "failover_payment":
        return "मंज़ूर: पेमेंट Cashfree पर बदल रहे हैं।" if hi else "Approved: switching payments to Cashfree."
    return "मंज़ूर।" if hi else f"Approved: {action.replace('_', ' ')}."


# ---------- multi-option plans (v2) ----------

OPTION_RANK = {"recommended": 0, "alt1": 1, "alt2": 2, "reject": 9}
ORDINAL_INDEX = {"recommended": 0, "alt1": 1, "alt2": 2}
DEFAULT_PLAN_OPTIONS = [{"key": "recommended", "title_en": "Go ahead", "impact_en": "", "cost_en": ""},
                        {"key": "reject", "title_en": "Do nothing for now", "impact_en": "", "cost_en": ""}]
BUTTON_TITLE_MAX = 20  # Meta reply-button title limit

# Fixed phrases: English, plus hand-written Hindi/Tamil and Tanglish ("ta-Latn"); other languages go through
# Sarvam translate (cached).
PHRASES = {
    "go_ahead": {"en": "Shall I go ahead?", "hi-IN": "क्या मैं आगे बढ़ूँ?", "ta-IN": "நான் தொடரலாமா?",
                 "ta-Latn": "Naan proceed pannattuma?"},
    "my_suggestion": {"en": "My suggestion", "hi-IN": "मेरा सुझाव", "ta-IN": "என் பரிந்துரை",
                      "ta-Latn": "En suggestion"},
    "recommended": {"en": "recommended", "hi-IN": "सुझाव", "ta-IN": "பரிந்துரை", "ta-Latn": "naan recommend panradhu"},
    "on_it": {"en": "On it", "hi-IN": "ठीक है, कर रहा हूँ", "ta-Latn": "Seri, panren"},
    "which_one": {"en": "{n} approvals are waiting. Tap the button on the one you mean.",
                  "hi-IN": "{n} मंज़ूरियाँ बाकी हैं। जिसकी बात कर रहे हैं, उसका बटन दबाइए।",
                  "ta-Latn": "{n} approvals wait pannitu irukku. Neenga sollradhu edhu-nu andha button-a press pannunga."},
    "agent_down": {"en": "Sorry, I could not do that right now. Nothing was changed. Please try again in a minute.",
                   "hi-IN": "माफ़ कीजिए, अभी यह नहीं हो पाया। कुछ नहीं बदला। एक मिनट बाद फिर कोशिश करें।",
                   "ta-Latn": "Sorry, ippo adhu panna mudiyala. Edhuvum change aagala. Oru nimisham kalichu "
                              "thirumba try pannunga."},
    "agent_slow": {"en": "This is taking longer than usual. Ask me for the status in a minute.",
                   "hi-IN": "इसमें सामान्य से ज़्यादा समय लग रहा है। एक मिनट बाद मुझसे हाल पूछिए।",
                   "ta-Latn": "Idhu konjam time edukkudhu. Oru nimisham kalichu status kelunga."},
    "ask_failed": {"en": "Sorry, I could not get an answer right now. Please ask again in a minute.",
                   "hi-IN": "माफ़ कीजिए, अभी जवाब नहीं मिल पाया। एक मिनट बाद फिर पूछिए।",
                   "ta-Latn": "Sorry, ippo answer kidaikkala. Oru nimisham kalichu thirumba kelunga."},
    "bad_option": {"en": "I did not understand which option you mean. Please tap one of the buttons.",
                   "hi-IN": "मैं समझ नहीं पाया आप कौन सा विकल्प चाहते हैं। कृपया एक बटन दबाइए।",
                   "ta-Latn": "Endha option-nu puriyala. Oru button-a press pannunga."},
    "pick_button": {"en": "Please tap Approve or Reject.", "hi-IN": "कृपया Approve या Reject दबाइए।",
                    "ta-Latn": "Approve illa Reject button-a press pannunga."},
    "nothing_pending": {"en": "Nothing is waiting for your approval right now.",
                        "hi-IN": "अभी कोई मंज़ूरी बाकी नहीं है।",
                        "ta-IN": "இப்போது உங்கள் ஒப்புதலுக்காக எதுவும் காத்திருக்கவில்லை.",
                        "ta-Latn": "Ippo unga approval-ku edhuvum wait pannala."},
    "monitor_down": {"en": "I cannot reach the store monitor right now.",
                     "hi-IN": "मैं अभी स्टोर मॉनिटर से जुड़ नहीं पा रहा।",
                     "ta-IN": "இப்போது ஸ்டோர் மானிட்டரை அணுக முடியவில்லை.",
                     "ta-Latn": "Ippo store monitor-a reach panna mudiyala."},
    "looking_into_it": {"en": "Looking into it…", "hi-IN": "जाँच शुरू कर दी है…", "ta-IN": "பார்க்கிறேன்…",
                        "ta-Latn": "Check panren, konjam wait pannunga…"},
    "switched": {"en": "Okay, I will reply in {language} from now on.",
                 "hi-IN": "ठीक है, अब से मैं हिंदी में जवाब दूँगा।",
                 "ta-IN": "சரி, இனிமேல் தமிழில் பதில் சொல்கிறேன்.",
                 "ta-Latn": "Seri, ippo irundhu Tanglish-la reply panren."},
}


def normalize_options(raw) -> list[dict]:
    """Dedupe by key, order recommended, alt1, alt2, ..., reject; keep at most 3 (always keeping reject)."""
    out: list[dict] = []
    seen: set[str] = set()
    for o in raw or []:
        if not isinstance(o, dict) or not o.get("key") or str(o["key"]) in seen:
            continue
        key = str(o["key"]).strip()
        seen.add(key)
        out.append({"key": key, "title_en": str(o.get("title_en") or key.replace("_", " ")).strip(),
                    "impact_en": str(o.get("impact_en") or ""), "cost_en": str(o.get("cost_en") or "")})
    out.sort(key=lambda o: OPTION_RANK.get(o["key"], 5))
    if len(out) > 3:
        rej = [o for o in out if o["key"] == "reject"]
        out = [o for o in out if o["key"] != "reject"][: 3 - len(rej)] + rej
    return out


def _strip_dangling(text: str) -> str:
    """Drop trailing separators and a dangling virama so a cut Indic word still renders cleanly."""
    text = text.rstrip(" ,.;:-+&/|(—–")
    while text and unicodedata.category(text[-1]) == "Mn" and "VIRAMA" in unicodedata.name(text[-1], ""):
        text = text[:-1]
    return text


def short_title(text: str, limit: int = BUTTON_TITLE_MAX) -> str:
    """Fit a button title into `limit` characters, cutting at a word boundary and adding '…'."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[: limit - 1]
    sp = cut.rfind(" ")
    if sp >= (limit - 1) // 2:
        cut = cut[:sp]
    return _strip_dangling(cut)[: limit - 1] + "…"


def clip(text: str, limit: int) -> str:
    """Shorten to `limit` chars, preferring a sentence end, then a word boundary."""
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    ends = [cut.rfind(ch) for ch in (". ", "। ", "? ", "! ")]
    end = max(ends)
    if end >= limit // 3:
        return cut[: end + 1].strip()
    sp = cut.rfind(" ")
    return (_strip_dangling(cut[:sp]) if sp > limit // 3 else cut[: limit - 1]) + "…"


def outcome_en(resp: dict, option: dict) -> str:
    """Plain-English read-back of an agent decision response."""
    for k in ("founder_text_en", "message_en", "summary_en"):
        if resp.get(k):
            return str(resp[k])
    status = str(resp.get("status", "")).lower()
    title = option.get("title_en") or option.get("key", "")
    results = [r for r in resp.get("results") or [] if isinstance(r, dict)]
    ok = [clip(str(r.get("detail") or r.get("type") or ""), 120) for r in results if r.get("ok")]
    ok = [d for d in ok if d]
    bad = [r for r in results if not r.get("ok")]
    if status == "rejected" or option.get("key") == "reject":
        return "Okay, I will not change anything."
    if status in ("executing", "accepted", "running", "queued", "started", "pending"):
        return f"Started: {title}. I will tell you when it is done."
    if status == "failed" or bad:
        why = clip(str((bad[0].get("detail") or bad[0].get("error")) if bad else resp.get("error") or "")
                   or "one of the steps did not work", 160)
        text = f"Sorry, I could not finish \"{title}\": {why.rstrip('.')}."
        if ok:
            text += f" This part worked: {', '.join(ok[:2])}."
        return text
    return "Done: " + (", ".join(ok[:3]) if ok else title) + "."


class OpsBot:
    def __init__(self, settings: Settings):
        self.s = settings
        self.store = Store(settings.data_dir, settings.founder_language)
        self.wa = WhatsAppClient(settings)
        self.sarvam = SarvamClient(settings)
        self.nb = NudgeBeeClient(settings)
        self.voice = VoiceConfig(settings)
        self.agent = AgentClient(settings)
        self.decide_lock = asyncio.Lock()
        self._phrase_cache: dict[tuple[str, str, str], str] = {}
        self.credits_out_until = 0.0
        self._remote_voice: dict = {}  # last Flagsmith ops_voice (refreshed per inbound message / outbound flow)

    # ---------- helpers ----------
    @property
    def founder_to(self) -> str:
        return self.s.founder_phone

    async def refresh_voice(self) -> dict:
        """Re-read Flagsmith ops_voice (cached ~15 s) so language/style follow remote config."""
        self._remote_voice = await self.voice.get()
        return self._remote_voice

    @property
    def lang(self) -> str:
        """Effective reply language (ta-IN whenever the style is Tanglish)."""
        return resolve_voice(self.store.founder, self._remote_voice, self.s.founder_style)[0]

    @property
    def style(self) -> str:
        return resolve_voice(self.store.founder, self._remote_voice, self.s.founder_style)[1]

    @property
    def tanglish(self) -> bool:
        return self.style == "tanglish"

    def is_founder(self, m: Inbound) -> bool:
        if self.s.founder_phone and m.phone and m.phone == self.s.founder_phone:
            return True
        return bool(self.s.founder_user_id and m.user_id and m.user_id == self.s.founder_user_id)

    def _credits_out(self) -> bool:
        return time.monotonic() < self.credits_out_until

    def _mark_credits_out(self) -> None:
        self.credits_out_until = time.monotonic() + 300
        log("SARVAM_CREDITS_EXHAUSTED", level="error", note="text-only replies for the next 5 minutes")

    async def compose(self, facts: dict, goal: str, fallback: str) -> str:
        if self._credits_out():
            return fallback
        if self.tanglish:
            goal = f"{goal} {TANGLISH_GOAL}"
        try:
            return await self.sarvam.compose(facts, goal, self.lang, fallback, style=self.style)
        except SarvamCreditsError:
            self._mark_credits_out()
            return fallback

    async def translate(self, text_en: str) -> str:
        if self._credits_out() or self.lang.startswith("en"):
            return text_en
        try:
            if self.tanglish:
                return await self.sarvam.to_tanglish(text_en)
            return await self.sarvam.translate(text_en, self.lang)
        except SarvamCreditsError:
            self._mark_credits_out()
            return text_en

    async def local(self, text_en: str, text_hi: str | None = None) -> str:
        """Fallback text: the hand-written Hindi for a Hindi founder, else the English translated (Tanglish too)."""
        if text_hi and self.lang == "hi-IN":
            return text_hi
        return await self.translate(text_en)

    async def phrase(self, key: str, **fmt) -> str:
        """A fixed bot phrase in the founder's language (hand-written for en/hi/ta/Tanglish, else translated once)."""
        p = PHRASES[key]
        en = p["en"].format(**fmt)
        if self.tanglish:
            if "ta-Latn" in p:
                return p["ta-Latn"].format(**fmt)
        elif self.lang.startswith("en"):
            return en
        elif self.lang in p:
            return p[self.lang].format(**fmt)
        ck = (self.lang, self.style, en)
        if ck not in self._phrase_cache:
            out = await self.translate(en)
            if out == en:
                return en  # translation unavailable; do not cache
            self._phrase_cache[ck] = out
        return self._phrase_cache[ck]

    async def to_english(self, text: str, lang: str | None) -> str:
        lang = lang or self.lang
        if lang.startswith("en") or self._credits_out():
            return text
        try:
            if self.tanglish and not script_language(text):
                # Romanized Tamil: mayura:v1 auto-detects code-mixed / Latin-script input.
                return await self.sarvam.translate(text, "en-IN", source="auto", model="mayura:v1",
                                                   fallback_source="ta-IN")
            return await self.sarvam.translate(text, "en-IN", source=lang if lang != "unknown" else "hi-IN")
        except SarvamCreditsError:
            self._mark_credits_out()
            return text

    async def post_feed(self, source: str, text: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                await c.post(f"{self.s.guardian_url}/api/feed", json={"source": source, "text": text})
        except Exception as e:  # noqa: BLE001
            log("guardian_feed_failed", error=str(e)[:200])

    async def send_voice_note(self, text: str) -> bool:
        """TTS -> OGG/Opus -> WhatsApp voice note. Returns False (and logs) if voice is unavailable."""
        if self._credits_out():
            log("voice_skipped_no_credits")
            return False
        try:
            v = await self.voice.get()
            # Speak in the language the text was written in (self.lang: switch > ops_voice > detected > env);
            # ops_voice supplies speaker and pace. Tanglish is turned into Tamil script first so Bulbul (ta-IN)
            # pronounces it like Tamil, not like English.
            spoken = await self.sarvam.tanglish_to_tamil(text) if self.tanglish else text
            wav = await self.sarvam.tts(spoken, self.lang, speaker=v.get("speaker") or None,
                                        pace=v.get("pace") or None)
            ogg = await audio.wav_to_ogg_opus(wav)
            await self.wa.send_voice(self.founder_to, ogg)
            return True
        except SarvamCreditsError:
            self._mark_credits_out()
        except (SarvamError, audio.AudioError, httpx.HTTPError) as e:
            log("voice_note_failed", error=str(e)[:300])
        return False

    async def say(self, text: str, *, voice: bool = True, buttons: list[tuple[str, str]] | None = None,
                  gloss_en: str | None = None, spoken: str | None = None) -> None:
        """Voice note (optional; `spoken` overrides what is said) then the text, with buttons when given."""
        if voice:
            await self.send_voice_note(spoken or text)
        body = text if not gloss_en or self.lang.startswith("en") else f"{text}\n\n({gloss_en})"
        if buttons:
            await self.wa.send_buttons(self.founder_to, body, buttons)
        else:
            await self.wa.send_text(self.founder_to, body)

    # ---------- inbound ----------
    async def process_inbound(self, items: list[Inbound]) -> None:
        for m in items:
            try:
                if not self.is_founder(m):
                    log("non_founder_message", msg_id=m.msg_id, has_phone=bool(m.phone))
                    if m.phone:
                        await self.wa.send_text(m.phone, "This is a private ops line.")
                    continue
                await self.refresh_voice()
                await self.handle_founder(m)
            except Exception as e:  # noqa: BLE001
                log("inbound_failed", msg_id=m.msg_id, error=str(e)[:300])
                try:
                    await self.wa.send_text(self.founder_to, "Sorry, something went wrong. Please try again.")
                except Exception:  # noqa: BLE001
                    pass

    async def handle_founder(self, m: Inbound) -> None:
        log("founder_message", msg_id=m.msg_id, type=m.type)
        if m.type == "button":
            kind, _, rest = m.button_id.partition(":")
            if kind == "opt" and ":" in rest:
                pid, _, key = rest.partition(":")
                await self.choose(pid, key, channel="whatsapp-button", transcript=m.button_title or key)
            elif kind in ("approve", "reject") and rest:
                if (self.store.get(rest) or {}).get("plan_id"):
                    await self.choose(rest, "recommended" if kind == "approve" else "reject",
                                      channel="whatsapp-button", transcript=m.button_title or kind)
                else:
                    await self.decide(rest, "approved" if kind == "approve" else "rejected", channel="button",
                                      transcript=m.button_title or kind)
            else:
                await self.wa.send_text(self.founder_to, "I did not understand that button.")
            return
        if m.type == "audio":
            data, mime = await self.wa.download_media(m.media_id)
            try:
                if self._credits_out():
                    raise SarvamCreditsError(402, "credits exhausted (cached)")
                stt = await self.sarvam.stt(data, mime or m.mime_type)
            except SarvamCreditsError:
                self._mark_credits_out()
                await self.wa.send_text(self.founder_to, "Voice is unavailable right now. Please type, or tap "
                                                         "Approve / Reject.")
                return
            text = stt.get("transcript", "")
            lang, prob = stt.get("language_code"), stt.get("language_probability")
            self.store.set_language(lang, prob)
            log("founder_transcript", language=lang, prob=prob, text=text[:200])
            if not text.strip():
                await self.wa.send_text(self.founder_to, "I could not hear that, please try again.")
                return
            if await self.maybe_switch(text):
                return
            await self.route(text, channel="voice", lang=lang or self.lang, prob=prob)
            return
        if m.type == "text":
            if await self.maybe_switch(m.text):
                return
            detected = script_language(m.text)
            if detected:
                self.store.set_language(detected, None, style="native")
            elif looks_tanglish(m.text) and self.store.set_language("ta-IN", None, style="tanglish"):
                log("tanglish_detected", text=m.text[:120])
            await self.route(m.text, channel="text", lang=self.lang, prob=None)
            return
        await self.wa.send_text(self.founder_to, "Send a voice note, a text, or tap a button.")

    async def maybe_switch(self, text: str) -> bool:
        """'tanglish' / 'tamil la pesu' / 'hindi' / 'english' ...: pin that language + style, confirm, stop."""
        sw = parse_switch(text)
        if not sw:
            return False
        self.store.switch(*sw)
        log("founder_language_switch", language=self.lang, style=self.style, text=text[:60])
        name = "Tanglish" if self.tanglish else LANG_NAMES.get(self.lang, "English")
        await self.say(await self.phrase("switched", language=name), voice=True)
        await self.post_feed("ops-bot", f"{self.s.founder_name} switched replies to {name}")
        return True

    async def route(self, text: str, *, channel: str, lang: str, prob: float | None) -> None:
        try:
            if self._credits_out():
                raise SarvamCreditsError(402, "credits exhausted (cached)")
            res = await self.sarvam.intent(text)
        except SarvamCreditsError:
            from .sarvam import keyword_intent

            self._mark_credits_out()
            res = keyword_intent(text)
        intent = res.get("intent", "smalltalk")
        option = res.get("option")
        log("intent", intent=intent, confidence=res.get("confidence"), option=option, channel=channel)
        if intent in ("approve", "reject") or (option and intent == "smalltalk"):
            await self.choice_flow(text, intent, option, channel=channel, lang=lang, prob=prob)
        elif intent == "status":
            await self.status_flow()
        elif intent == "why":
            await self.why_flow(text, lang)
        elif intent == "ask":
            await self.ask_flow(text, lang)
        elif intent == "rehearse":
            reply = await self.compose(
                {"command": "insidious rehearse"},
                "Tell him a festive load rehearsal is run from the laptop and the result will come to him as a "
                "voice note. One short sentence, no jargon.",
                await self.translate("A festive rehearsal runs from the laptop; I will send you the result as a "
                                     "voice note."))
            await self.say(reply, voice=True)
        elif intent == "restore":
            await self.post_feed("ops-bot", f"{self.s.founder_name} asked to go back to normal mode")
            planned = False
            if self.agent.configured:
                import time as _t
                try:
                    res = await self.agent.incident({
                        "id": f"restore-{int(_t.time())}", "status": "firing", "kind": "restore",
                        "rule_name": "founder-request",
                        "title": "Founder asked to go back to normal mode now that the rush is over",
                        "description": ("Plan the return to normal: turn heavy_features (and its children) back ON "
                                        "in Flagsmith, scale store-api back to its normal replica count, and reset "
                                        "db_pool to its default. Only if current load is healthy; otherwise say so."),
                    })
                    planned = bool(res.get("plan_id"))
                except Exception as e:  # noqa: BLE001
                    log("restore_plan_failed", error=str(e)[:200])
            reply = await self.compose(
                {"planning": planned},
                ("Tell him: sure, I'm checking it's safe and will send a plan to switch everything back to normal in a "
                 "moment, he just taps yes." if planned else
                 "Tell him: sorry, I could not plan that right now, nothing was changed; ask again in a minute."),
                await self.translate("Sure, I'm checking it's safe and will send you a plan to go back to normal."
                                     if planned else "Sorry, I could not plan that right now. Nothing was changed."))
            await self.say(reply, voice=True)
        else:
            # Small talk / help: Sarvam writes a warm reply in the founder's language, spoken by Bulbul.
            reply = await self.compose(
                {"founder_said": text, "store": "Jeffi Stores", "bot": "Insidious ops assistant"},
                "Reply warmly to what he said in one short sentence, then tell him in one short sentence that he "
                "can ask how the store is doing, why it is slow, or answer yes/no when I suggest a fix. "
                "Plain words, no technical terms.",
                await self.translate("Hello! You can ask me how the store is doing, why it is slow, or say yes or "
                                     "no when I suggest a fix."))
            await self.say(reply, voice=True)

    async def choice_flow(self, text: str, intent: str, option: str | None, *, channel: str, lang: str,
                          prob: float | None) -> None:
        """A spoken/typed yes/no/'pehla wala'/'doosra' for whatever approval is waiting."""
        pending = self.store.pending()
        if not pending:
            await self.say(await self.compose({}, "Tell him nothing is waiting for his approval right now.",
                                              await self.phrase("nothing_pending")),
                           voice=False)
            return
        if len(pending) > 1:
            await self.ask_which(pending)
            return
        p = pending[0]
        if p.get("plan_id"):
            key = option or ("reject" if intent == "reject" else "recommended")
            await self.choose(p["pending_id"], self.resolve_option_key(p, key),
                              channel="whatsapp-voice" if channel == "voice" else "whatsapp-text",
                              transcript=text, lang=lang, prob=prob)
            return
        if intent not in ("approve", "reject"):
            await self.wa.send_text(self.founder_to, await self.phrase("pick_button"))
            return
        await self.decide(p["pending_id"], "approved" if intent == "approve" else "rejected",
                          channel=channel, transcript=text, lang=lang, prob=prob)

    async def ask_which(self, pending: list[dict]) -> None:
        await self.wa.send_text(self.founder_to, await self.phrase("which_one", n=len(pending)))
        for p in pending[:3]:
            if p.get("plan_id"):
                await self.wa.send_buttons(self.founder_to, p.get("explanation") or p.get("founder_summary")
                                           or p.get("summary_en") or p["plan_id"], self.plan_buttons(p))
            else:
                await self.wa.send_buttons(self.founder_to, p.get("explanation") or p.get("action", ""),
                                           [(f"approve:{p['pending_id']}", "Approve"),
                                            (f"reject:{p['pending_id']}", "Reject")])

    # ---------- approvals ----------
    async def approval_request(self, req: dict) -> dict:
        req = dict(req)
        if req.get("plan_id"):
            req["options"] = normalize_options(req.get("options")) or [dict(o) for o in DEFAULT_PLAN_OPTIONS]
            req["action"] = req.get("action") or "agent_plan"
        else:
            req["action"] = req.get("action") or "shed_heavy_features"
        rec = self.store.add_approval(req)
        log("approval_stored", pending_id=rec["pending_id"], action=req.get("action"), plan_id=req.get("plan_id"),
            options=[o["key"] for o in req.get("options") or []],
            has_execution=bool(req.get("execution_id")), has_task=bool(req.get("task_id")))
        return rec

    async def announce_approval(self, pid: str) -> None:
        await self.refresh_voice()
        rec = self.store.get(pid)
        if not rec:
            return
        if rec.get("plan_id"):
            await self.announce_plan(pid)
            return
        numbers = rec.get("numbers") or {}
        action = rec.get("action", "")
        p95, rps, gain = _num(numbers.get("p95_ms")), _num(numbers.get("rps")), numbers.get("gain") or ""
        fallback = await self.local(
            f"Checkout is slow: {p95} ms at {rps} requests per second. Shall I "
            f"{action_phrase(action, numbers, 'en-IN')}?" + (f" That frees {gain} capacity." if gain else ""),
            f"चेकआउट धीमा है, {p95} मिलीसेकंड लग रहे हैं। क्या मैं {action_phrase(action, numbers, 'hi-IN')}?"
            + (f" इससे {gain} ज़्यादा ग्राहक संभलेंगे।" if gain else ""))
        gloss = (f"Checkout p95 {p95} ms at {rps} rps. Proposed: {action_phrase(action, numbers, 'en-IN')}"
                 + (f" ({gain} capacity)." if gain else "."))
        facts = {"problem": rec.get("summary_en", "")[:600], "checkout_delay_ms": numbers.get("p95_ms"),
                 "requests_per_second": numbers.get("rps"), "capacity_gain_if_done": gain,
                 "proposed_action": action_phrase(action, numbers, "en-IN")}
        text = await self.compose(facts, "Explain the problem in one sentence, then ask if he approves the proposed "
                                         "action, as a yes/no question.", fallback)
        self.store.update_approval(pid, explanation=text, language=self.lang)
        await self.say(text, buttons=[(f"approve:{pid}", "Approve"), (f"reject:{pid}", "Reject")], gloss_en=gloss)
        await self.post_feed("ops-bot", f"Asked {self.s.founder_name} on WhatsApp to approve: {action}")

    # ---------- multi-option plans (agent) ----------
    def plan_buttons(self, rec: dict) -> list[tuple[str, str]]:
        if rec.get("buttons"):
            return [(b[0], b[1]) for b in rec["buttons"]]
        return [(f"opt:{rec['pending_id']}:{o['key']}", short_title(f"{i}. {o.get('title_en') or o['key']}"))
                for i, o in enumerate(rec.get("options") or [], 1)]

    @staticmethod
    def resolve_option_key(rec: dict, key: str) -> str:
        """Map a parsed option onto this plan: exact key, else by position (pehla/doosra/teesra = 1st/2nd/3rd)."""
        keys = [o["key"] for o in rec.get("options") or []]
        if key in keys:
            return key
        idx = ORDINAL_INDEX.get(key)
        if idx is not None and idx < len(keys):
            return keys[idx]
        return key

    async def announce_plan(self, pid: str) -> None:
        """Founder message for an agent plan: summary + numbered options + 'Shall I go ahead?', option buttons,
        and a short voice note (summary + recommended option)."""
        rec = self.store.get(pid)
        options = rec.get("options") or DEFAULT_PLAN_OPTIONS
        summary_en = (rec.get("founder_summary") or rec.get("founder_summary_en") or rec.get("summary_en")
                      or "I have a plan to fix a problem in the store.")
        summary_en = clip(summary_en, 600)
        titles_en = [o.get("title_en") or o["key"] for o in options]
        costs_en = [o.get("cost_en") or "" for o in options]
        translated = await asyncio.gather(self.translate(summary_en), *(self.translate(t) for t in titles_en),
                                          *(self.translate(c) if c else asyncio.sleep(0, "") for c in costs_en))
        summary = translated[0]
        titles = list(translated[1:1 + len(options)])
        costs = list(translated[1 + len(options):])
        go_ahead = await self.phrase("go_ahead")
        rec_label = await self.phrase("recommended")
        lines = []
        for i, (o, t, c) in enumerate(zip(options, titles, costs), 1):
            line = f"{i}. {t}" + (f" ({rec_label})" if o["key"] == "recommended" else "")
            lines.append(line + (f" — {c}" if c else ""))
        body = f"{clip(summary, 500)}\n\n" + "\n".join(lines) + f"\n\n{go_ahead}"
        if not self.lang.startswith("en") and summary != summary_en:
            gloss = "(EN: " + summary_en + " | " + " ".join(f"{i}. {t}" for i, t in enumerate(titles_en, 1)) + ")"
            if len(body) + len(gloss) + 2 <= 1024:
                body += "\n\n" + gloss
        def _label(i: int, key: str, t: str) -> str:
            if key == "recommended":
                return short_title(f"{i} ⭐ {t}")
            if key == "reject":
                return short_title(f"{i} ✖ {t}")
            return short_title(f"{i} {t}")
        buttons = [(f"opt:{pid}:{o['key']}", _label(i, o["key"], t))
                   for i, (o, t) in enumerate(zip(options, titles), 1)]
        first = next((t for o, t in zip(options, titles) if o["key"] != "reject"), None)
        tail = (f"{await self.phrase('my_suggestion')}: {clip(first, 60)}. " if first else "") + go_ahead
        spoken = clip(summary, max(60, 210 - len(tail))) + " " + tail
        self.store.update_approval(pid, explanation=body[:1024], language=self.lang, spoken=spoken,
                                   option_titles={o["key"]: t for o, t in zip(options, titles)},
                                   buttons=[list(b) for b in buttons])
        await self.send_voice_note(spoken)
        await self.wa.send_buttons(self.founder_to, body[:1024], buttons)
        await self.post_feed("ops-bot", f"Asked {self.s.founder_name} on WhatsApp to choose for {rec['plan_id']}: "
                                        + " / ".join(f"{o['key']}={t}" for o, t in zip(options, titles_en)))

    async def choose(self, pid: str, key: str, *, channel: str, transcript: str = "", lang: str | None = None,
                     prob: float | None = None) -> dict | None:
        """Founder picked option `key` of plan approval `pid`: tell the agent, complete NudgeBee, read back."""
        rec = self.store.get(pid)
        if rec and not rec.get("plan_id"):  # an option on a plain approval: treat as approve/reject
            return await self.decide(pid, "rejected" if key == "reject" else "approved",
                                     channel="voice" if channel.endswith("voice") else
                                     ("button" if channel.endswith("button") else "text"),
                                     transcript=transcript, lang=lang, prob=prob)
        async with self.decide_lock:
            rec = self.store.get(pid)
            if not rec:
                await self.wa.send_text(self.founder_to, "I could not find that approval.")
                return None
            if rec.get("status") != "pending":
                await self.wa.send_text(self.founder_to, f"Already {rec.get('status')}.")
                return rec
            opt = next((o for o in rec.get("options") or [] if o.get("key") == key), None)
            if opt is None and key == "reject":
                opt = {"key": "reject", "title_en": "Do nothing for now"}
            if opt is None:
                log("plan_option_unknown", pending_id=pid, option=key)
                await self.wa.send_buttons(self.founder_to, await self.phrase("bad_option"), self.plan_buttons(rec))
                return rec
            rec = self.store.update_approval(pid, status="executing", chosen_option=key, channel=channel)
        lang = lang or self.lang
        detail = f"{lang}" + (f", {float(prob):.2f}" if prob is not None else "")
        comments = f"{channel}: {transcript[:200]} ({detail}) via Insidious ops-bot"
        title = (rec.get("option_titles") or {}).get(key) or opt.get("title_en") or key
        plan_id = rec["plan_id"]
        log("plan_choice", pending_id=pid, plan_id=plan_id, option=key, channel=channel)
        if key != "reject":
            await self.wa.send_text(self.founder_to, f"{await self.phrase('on_it')}: {title}…")
        try:
            resp = await self.agent.decision(plan_id, key, channel, comments)
        except AgentError as e:
            if e.timeout:
                status = "unknown"
            else:
                status = "pending" if e.retryable else "failed"
            self.store.update_approval(pid, status=status, agent_error=str(e)[:300], decided_at=now_iso())
            log("plan_decision_failed", pending_id=pid, plan_id=plan_id, option=key, error=str(e)[:300],
                new_status=status)
            await self.say(await self.phrase("agent_slow" if e.timeout else "agent_down"), voice=False)
            await self.post_feed("ops-bot", f"{self.s.founder_name} chose {key} for {plan_id}, but the agent "
                                            f"{'is still working' if e.timeout else 'could not be reached'}")
            return self.store.get(pid)
        nb_res = None
        if rec.get("execution_id") or rec.get("task_id"):
            nb_status = "rejected" if key == "reject" else "approved"
            try:
                exec_id, task_id = await self.resolve_ids(rec)
                nb_res = await self.nb.workflow_complete_approval(exec_id, task_id, nb_status,
                                                                  f"option={key} ({comments})")
                self.store.update_approval(pid, execution_id=exec_id, task_id=task_id)
                log("plan_nudgebee_completed", pending_id=pid, execution_id=exec_id, task_id=task_id,
                    status=nb_status)
            except NudgeBeeError as e:
                log("plan_nudgebee_failed", pending_id=pid, error=str(e)[:300])
        agent_status = str(resp.get("status", "")).lower()
        final = "rejected" if key == "reject" else ("failed" if agent_status == "failed" else "approved")
        self.store.update_approval(pid, status=final, decided_at=now_iso(), comments=comments,
                                   agent_response=resp, nudgebee_response=nb_res)
        readback_en = outcome_en(resp, opt)
        text = await self.translate(readback_en)
        await self.say(text, voice=True, spoken=clip(text, 250),
                       gloss_en=readback_en if text != readback_en else None)
        await self.post_feed("ops-bot", f"{self.s.founder_name} chose {key} ({opt.get('title_en') or key}) for "
                                        f"{plan_id} by {channel}: {agent_status or final}")
        return self.store.get(pid)

    async def resolve_ids(self, rec: dict) -> tuple[str, str]:
        exec_id, task_id = rec.get("execution_id") or "", rec.get("task_id") or ""
        if exec_id and task_id:
            return exec_id, task_id
        wf_id = rec.get("workflow_id") or self.s.nudgebee_wf_incident_id
        for attempt in range(max(1, self.s.approval_resolve_attempts)):
            try:
                if exec_id:
                    ex = await self.nb.workflow_get_execution(exec_id, wf_id or None)
                    task_id = find_waiting_approval(ex) or ""
                    if task_id:
                        return exec_id, task_id
                elif wf_id:
                    for e in await self.nb.workflow_list_executions(wf_id, limit=5):
                        eid = e.get("execution_id") or e.get("id")
                        status = str(e.get("status", "")).upper()
                        if not eid or status in ("COMPLETED", "FAILED", "CANCELLED", "CANCELED", "TERMINATED",
                                                 "TIMED_OUT", "SUCCEEDED"):
                            continue
                        ex = await self.nb.workflow_get_execution(eid, wf_id)
                        tid = find_waiting_approval(ex)
                        if tid:
                            return eid, tid
            except NudgeBeeError as e:
                log("resolve_ids_error", attempt=attempt, error=str(e)[:300])
            if attempt + 1 < self.s.approval_resolve_attempts:
                await asyncio.sleep(self.s.approval_resolve_gap_s)
        raise NudgeBeeError("no waiting core.approval task found")

    async def decide(self, pid: str, status: str, *, channel: str, transcript: str = "",
                     lang: str | None = None, prob: float | None = None) -> dict | None:
        async with self.decide_lock:
            rec = self.store.get(pid)
            if not rec:
                await self.wa.send_text(self.founder_to, "I could not find that approval.")
                return None
            if rec.get("status") != "pending":
                await self.wa.send_text(self.founder_to, f"Already {rec.get('status')}.")
                return rec
            lang = lang or self.lang
            detail = f"{lang}" + (f", {float(prob):.2f}" if prob is not None else "")
            comments = f"{channel}: {transcript[:200]} ({detail}) via Insidious ops-bot"
            try:
                exec_id, task_id = await self.resolve_ids(rec)
                res = await self.nb.workflow_complete_approval(exec_id, task_id, status, comments)
            except NudgeBeeError as e:
                log("approval_failed", pending_id=pid, error=str(e)[:300])
                await self.wa.send_text(self.founder_to, "I could not reach NudgeBee. Please tap the button again "
                                                         "in a few seconds.")
                return rec
            rec = self.store.update_approval(pid, status=status, decided_at=now_iso(), channel=channel,
                                             comments=comments, execution_id=exec_id, task_id=task_id,
                                             nudgebee_response=res)
        log("approval_completed", pending_id=pid, status=status, channel=channel, execution_id=exec_id,
            task_id=task_id)
        numbers = rec.get("numbers") or {}
        approved = status == "approved"
        fallback = await self.local(done_phrase(rec.get("action", ""), numbers, approved, "en-IN"),
                                    done_phrase(rec.get("action", ""), numbers, approved, "hi-IN"))
        text = await self.compose({"decision": status, "action": action_phrase(rec.get("action", ""), numbers, "en-IN")},
                                  "Read back what was just " + status + ", starting with the word for "
                                  + ("'Approved:'" if approved else "'Rejected:'"), fallback)
        await self.say(text, voice=True)
        verb = "approved" if approved else "rejected"
        await self.post_feed("ops-bot", f"{self.s.founder_name} {verb} {rec.get('action')} by {channel}"
                                        + (f" ({detail})" if channel == "voice" else ""))
        return rec

    # ---------- status ----------
    async def status_flow(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.s.guardian_url}/api/state")
                r.raise_for_status()
                state = r.json()
        except Exception as e:  # noqa: BLE001
            log("guardian_state_failed", error=str(e)[:200])
            await self.say(await self.phrase("monitor_down"), voice=False)
            return
        slo = (state.get("slo") or {}).get("/checkout") or next(iter((state.get("slo") or {}).values()), {}) or {}
        flags = state.get("flags") or {}
        shed = [f for f in HEAVY_FLAGS if flags.get(f) is False]
        replicas = (state.get("replicas") or {}).get("store-api")
        facts = {"checkout_p95_ms": slo.get("p95_ms"), "target_ms": slo.get("target_p95_ms"),
                 "slow_now": slo.get("breaching"), "requests_per_second": state.get("rps"),
                 "servers": replicas, "features_turned_off": shed, "incident": (state.get("incident") or {}).get("status")}
        p95, tgt, rps = _num(slo.get("p95_ms")), _num(slo.get("target_p95_ms"), "800"), _num(state.get("rps"))
        fallback = await self.local(
            f"{'Checkout is slow' if slo.get('breaching') else 'All good'}: {p95} ms against {tgt} ms. "
            f"{rps} requests per second on {_num(replicas)} servers." + (f" Turned off: {', '.join(shed)}." if shed else ""),
            f"{'अभी चेकआउट धीमा है' if slo.get('breaching') else 'सब ठीक है'}: {p95} मिलीसेकंड, लक्ष्य {tgt}। "
            f"{rps} रिक्वेस्ट प्रति सेकंड, {_num(replicas)} सर्वर।" + (f" बंद फ़ीचर: {', '.join(shed)}।" if shed else ""))
        text = await self.compose(facts, "Give a short spoken status of the store right now.", fallback)
        await self.say(text, gloss_en=None if self.lang.startswith("en") else
                       f"p95 {p95} ms / SLO {tgt} ms, {rps} rps, {_num(replicas)} pods, shed: {', '.join(shed) or 'none'}")

    # ---------- why (NuBi) ----------
    async def why_flow(self, transcript: str, lang: str) -> None:
        q = await self.to_english(transcript, lang)
        nb_live = not self.nb.dry and bool(self.s.nudgebee_api_token)
        if not nb_live and self.agent.configured:
            # NudgeBee is in dry-run / not configured: let the Claude agent answer from live telemetry.
            if await self.ask_flow(transcript, lang, question_en=q, apologize=False):
                return
        query = (f"{q} Context: Jeffy store, Deployment store-api in namespace store; checkout p95 SLO 800 ms. "
                 "Answer in two plain sentences for a non-technical founder.")
        try:
            session = await self.nb.ai_execute_investigation(query)
        except NudgeBeeError as e:
            log("why_start_failed", error=str(e)[:300])
            if await self.ask_flow(transcript, lang, question_en=q, apologize=False):
                return
            await self.wa.send_text(self.founder_to, "NudgeBee is not reachable right now.")
            return
        await self.wa.send_text(self.founder_to, await self.phrase("looking_into_it"))
        seen_tools = 0
        updates = 0
        last_update = 0.0
        deadline = time.monotonic() + self.s.why_timeout_s
        answer = ""
        while time.monotonic() < deadline:
            await asyncio.sleep(self.s.why_poll_interval_s)
            try:
                conv = await self.nb.ai_get_conversation_v3(session)
            except NudgeBeeError as e:
                log("why_poll_error", error=str(e)[:200])
                continue
            tools = conv.get("tool_calls") or []
            if len(tools) > seen_tools:
                new = tools[seen_tools:]
                seen_tools = len(tools)
                now = time.monotonic()
                if updates < self.s.why_max_updates and (updates == 0 or now - last_update >= self.s.why_update_min_gap_s):
                    names = ", ".join(dict.fromkeys(str(t.get("tool_name", "tool")) for t in new))
                    await self.wa.send_text(self.founder_to, f"NudgeBee: checking {names}…")
                    updates += 1
                    last_update = now
            done, answer = conversation_answer(conv)
            if done:
                break
        if not answer:
            if nb_live and await self.ask_flow(transcript, lang, question_en=q, apologize=False):
                return
            await self.wa.send_text(self.founder_to, "NudgeBee is still investigating. I will tell you when the "
                                                     "incident changes.")
            return
        fallback = await self.translate(answer[:400])
        text = await self.compose({"engineer_answer": answer[:1200]},
                                  "Explain simply why the store is slow and what is being done.", fallback)
        await self.say(text, gloss_en=answer[:300])
        await self.post_feed("nudgebee", f"NuBi: {answer[:160]}")

    # ---------- ask (Claude agent) ----------
    async def ask_flow(self, transcript: str, lang: str | None, *, question_en: str | None = None,
                       apologize: bool = True) -> bool:
        """Forward a free-form founder question to the agent (/api/ask) and speak/text the answer back."""
        lang = lang or self.lang
        q = question_en if question_en is not None else await self.to_english(transcript, lang)
        try:
            answer_en = await self.agent.ask(q, lang if lang != "unknown" else self.lang)
        except AgentError as e:
            log("ask_failed", error=str(e)[:300])
            if apologize:
                await self.say(await self.phrase("ask_failed"), voice=False)
            return False
        text = await self.translate(answer_en[:1500])
        await self.say(text, voice=True, spoken=clip(text, 300),
                       gloss_en=clip(answer_en, 400) if text != answer_en else None)
        await self.post_feed("ops-bot", f"{self.s.founder_name} asked: {clip(q, 100)} | agent: {clip(answer_en, 140)}")
        return True

    # ---------- notify / brief / flags ----------
    async def notify(self, text_en: str, severity: str = "info", voice: bool = False) -> None:
        await self.refresh_voice()
        text = await self.translate(text_en)
        if voice:
            text = await self.compose({"message": text_en, "severity": severity},
                                      "Say this update simply.", text)
        await self.say(text, voice=voice, gloss_en=text_en if text != text_en else None)
        await self.post_feed("ops-bot", f"Told {self.s.founder_name}: {text_en[:160]}")

    async def brief(self, kind: str, report: dict) -> None:
        p1 = (report.get("pass1") or {}).get("breaking_rps")
        p2 = (report.get("pass2") or {}).get("breaking_rps")
        gain = report.get("capacity_gain_if_shed") or ""
        await self.refresh_voice()
        if p1 is None:
            fallback = await self.local("In the rehearsal your store held up at full load.",
                                        "रिहर्सल में स्टोर पूरे लोड पर भी नहीं टूटा।")
        else:
            fallback = await self.local(
                f"Your store breaks at {_num(p1)} requests per second; turning off heavy features raises it to "
                f"{_num(p2) if p2 is not None else 'full load'}.",
                f"आपका स्टोर {_num(p1)} रिक्वेस्ट प्रति सेकंड पर धीमा हो जाता है; भारी फ़ीचर बंद करने से यह "
                f"{_num(p2) if p2 is not None else 'पूरे लोड'} तक संभल जाता है।")
        facts = {"breaks_at_rps_with_all_features": p1, "breaks_at_rps_with_heavy_features_off": p2,
                 "capacity_gain": gain, "recommendation": (report.get("recommendation") or "")[:300]}
        text = await self.compose(facts, f"Give a short {kind} brief: where the store breaks and how much turning off "
                                         "heavy features helps.", fallback)
        await self.say(text, gloss_en=f"Breaks at {p1} rps; {p2} rps with heavy features off ({gain}).")
        await self.post_feed("ops-bot", f"Readiness brief sent: {p1} -> {p2} rps ({gain})")

    async def flag_event(self, summary: str) -> None:
        await self.post_feed("flagsmith", summary)
        try:
            await self.wa.send_text(self.founder_to, f"Flag change: {summary}")
        except Exception as e:  # noqa: BLE001
            log("flag_text_failed", error=str(e)[:200])


def summarize_flagsmith(payload: dict) -> str:
    """'<feature> turned ON/OFF (by <changed_by>)' from a Flagsmith environment webhook."""
    data = payload.get("data") or payload
    new = data.get("new_state") or {}
    prev = data.get("previous_state") or {}
    state = new or prev
    feature = ((state.get("feature") or {}).get("name")) or data.get("feature_name") or "a flag"
    who = data.get("changed_by") or "an API key"
    event = payload.get("event_type", "FLAG_UPDATED")
    scope = ""
    if state.get("feature_segment"):
        seg = state["feature_segment"]
        seg_name = (seg.get("segment") or {}).get("name") if isinstance(seg.get("segment"), dict) else seg.get("segment")
        scope = f" for segment {seg_name}" if seg_name else " for a segment"
    elif state.get("identity") or state.get("identity_identifier"):
        scope = f" for {state.get('identity_identifier') or 'one user'}"
    if event == "FLAG_DELETED" or not new:
        return f"{feature} override removed{scope} (by {who})"
    new_val, old_val = new.get("feature_state_value"), prev.get("feature_state_value") if prev else None
    if new_val not in (None, "") and new_val != old_val and new.get("enabled") == (prev or {}).get("enabled", new.get("enabled")):
        return f"{feature} set to {new_val}{scope} (by {who})"
    return f"{feature} turned {'ON' if new.get('enabled') else 'OFF'}{scope} (by {who})"
