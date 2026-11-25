"""Sarvam AI client: Saaras STT, sarvam-105b chat (intent + reply writing), Bulbul TTS, translate.

Field names checked against docs.sarvam.ai API reference on 2026-11-25:
- POST /speech-to-text (multipart): file, model, mode, [language_code omitted = auto-detect]
  -> {request_id, transcript, language_code, language_probability}
- POST /text-to-speech (json): text, language_code, model, speaker, pace, speech_sample_rate -> {request_id, audios:[b64]}
- POST /translate (json): input, source_language_code, target_language_code, model -> {translated_text}
  mayura:v1 only (input <= 1000 chars): mode formal|modern-colloquial|classic-colloquial|code-mixed,
  output_script roman|fully-native|spoken-form-in-native, source_language_code may be "auto".
  sarvam-translate:v1: formal only, no output_script (input <= 2000 chars).
- POST /transliterate (json): input, source_language_code, target_language_code, [spoken_form bool,
  numerals_format] -> {transliterated_text}; en-IN -> ta-IN turns Romanized Tamil into Tamil script.
- POST /v1/chat/completions: OpenAI shape, response_format json_schema {name, strict, schema}
Auth header: api-subscription-key. 402 = credits exhausted, 429 = rate limited, 503 = overloaded.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import deque

import httpx

from . import audio
from .config import Settings
from .log import log

INTENTS = ["approve", "reject", "status", "why", "ask", "rehearse", "restore", "help", "smalltalk"]
# Which numbered plan option the founder picked (multi-option approvals). "none" in the schema = null.
OPTIONS = ["recommended", "alt1", "alt2", "reject"]

LANG_NAMES = {
    "hi-IN": "Hindi", "ta-IN": "Tamil", "en-IN": "English", "bn-IN": "Bengali", "te-IN": "Telugu",
    "kn-IN": "Kannada", "ml-IN": "Malayalam", "mr-IN": "Marathi", "gu-IN": "Gujarati", "pa-IN": "Punjabi",
    "od-IN": "Odia",
}
TTS_LANGS = set(LANG_NAMES)
TAMIL_SCRIPT = re.compile(r"[\u0B80-\u0BFF]")
TANGLISH_MODEL = "mayura:v1"  # the only translate model with mode=code-mixed + output_script=roman

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "confidence": {"type": "number"},
        "option": {"type": "string", "enum": OPTIONS + ["none"]},
    },
    "required": ["intent", "confidence", "option"],
    "additionalProperties": False,
}

INTENT_SYSTEM = (
    "Classify a shop owner's WhatsApp message to his store's ops bot. Message may be Hindi, Tamil, Tanglish (Tamil "
    "in English letters), English or mixed. "
    "approve=yes/do it/haan karo/theek hai/seri/pannunga/pehla wala karo; reject=no/don't/mat karo/nahi/ruko/vendam; "
    "status=how is the site now/kya haal hai/sales kaisi; why=why slow or down/kyun/enna aachu; "
    "ask=any other question about the store, sales, servers, database, costs or what to do; "
    "rehearse=run load test/practice; restore=turn features back on; help=what can you do; smalltalk=anything else. "
    "option = which numbered option he picked: recommended=haan/yes/ok/karo/pehla/first/recommended/option 1; "
    "alt1=doosra/second/option 2; alt2=teesra/third/option 3; reject=nahi/no/ruko/mat karo; none if he picked nothing. "
    "Return JSON only."
)

KEYWORDS = [
    ("reject", r"\b(reject|no|nahi|nahin|mat|ruko|cancel|stop|vendam|venda|illai)\b|नहीं|मत|रुको|வேண்டாம்"),
    ("approve", r"\b(approve|yes|haan|han|ha|karo|kar do|ok|okay|theek|thik|seri|sari|pannunga|go ahead|do it)\b"
                r"|हाँ|हां|करो|ठीक|சரி|பண்ணுங்க"),
    ("why", r"\b(why|kyun|kyon|kyu|reason|slow|enna aachu|yen)\b|क्यों|धीमा|ஏன்"),
    ("status", r"\b(status|haal|kaisa|kaisi|how is|update|sales|state|nilai)\b|हाल|कैसा|நிலை"),
    ("rehearse", r"\b(rehearse|load test|practice|rehearsal)\b"),
    ("restore", r"\b(restore|wapas|turn on|back on|chalu)\b|वापस"),
    ("help", r"\b(help|madad|commands)\b|मदद"),
    ("ask", r"\?|\b(what|how|when|which|should|can|could|will|kya|kaise|kitne|kitna|kitni|kab|kaun|kahan|"
            r"chahiye|batao|bataiye|enna|evlo|eppadi)\b|क्या|कैसे|कितने|कितना|कब|कौन|बताओ|என்ன|எப்படி"),
]
QUESTION_FIRST = ("why", "status", "ask")  # a message with '?' is a question before it is a yes/no

OPTION_KEYWORDS = [
    ("alt2", r"\b(teesra|teesri|tisra|tisri|third|3rd|moonavathu|moondravathu)\b|तीसरा|तीसरी|\boption\s*3\b|^\s*3\s*$"),
    ("alt1", r"\b(doosra|dusra|doosri|dusri|second|2nd|irandavathu|rendavathu)\b|दूसरा|दूसरी|\boption\s*2\b"
             r"|^\s*2\s*$"),
    ("reject", r"\b(reject|no|nahi|nahin|ruko|mat|cancel|stop|vendam|venda|illai|dont|don't)\b|नहीं|मत|रुको|வேண்டாம்"),
    ("recommended", r"\b(recommended|suggested|pehla|pehli|pahla|pahli|first|1st|haan|han|ha|yes|ok|okay|karo|kar do|"
                    r"theek|thik|seri|sari|go ahead|do it|pannunga)\b|पहला|पहली|हाँ|हां|करो|ठीक|சரி|பண்ணுங்க"
                    r"|\boption\s*1\b|^\s*1\s*$"),
]


def keyword_option(text: str) -> str | None:
    """Which numbered plan option a message picks: recommended|alt1|alt2|reject, or None."""
    t = (text or "").lower()
    for key, pat in OPTION_KEYWORDS:
        if re.search(pat, t):
            return key
    return None


def keyword_intent(text: str) -> dict:
    t = (text or "").lower()
    rules = KEYWORDS
    if "?" in t:
        rules = [r for r in KEYWORDS if r[0] in QUESTION_FIRST] + [r for r in KEYWORDS if r[0] not in QUESTION_FIRST]
    for intent, pat in rules:
        if re.search(pat, t):
            return {"intent": intent, "confidence": 0.6, "option": keyword_option(text)}
    return {"intent": "smalltalk", "confidence": 0.3, "option": keyword_option(text)}


class SarvamError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"sarvam {status}: {message}")
        self.status = status


class SarvamCreditsError(SarvamError):
    """HTTP 402: the account is out of credits. Retrying will not help."""


class RateLimiter:
    """Client-side sliding window so the whole bot stays under Sarvam's shared 60 req/min."""

    def __init__(self, per_minute: int):
        self.per_minute = max(1, per_minute)
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] > 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_minute:
                    self.calls.append(now)
                    return
                await asyncio.sleep(60 - (now - self.calls[0]) + 0.05)


class SarvamClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.limiter = RateLimiter(settings.sarvam_rpm)
        self._model_rejected: dict[tuple, bool] = {}

    @property
    def mock(self) -> bool:
        return self.s.sarvam_mock

    def _headers(self) -> dict:
        return {"api-subscription-key": self.s.sarvam_api_key}

    async def _request(self, method: str, path: str, *, retries: int = 3, timeout: float = 60, **kw) -> dict:
        url = f"{self.s.sarvam_base_url}{path}"
        attempt = 0
        while True:
            await self.limiter.acquire()
            try:
                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.request(method, url, headers=self._headers(), **kw)
            except httpx.TransportError as e:
                if attempt >= retries:
                    raise SarvamError(0, f"transport: {e}") from e
                attempt += 1
                await asyncio.sleep(self.s.sarvam_retry_base_s * 2 ** (attempt - 1))
                continue
            if r.status_code == 402:
                log("SARVAM_CREDITS_EXHAUSTED", path=path, level="error",
                    note="Sarvam returned 402: top up credits; falling back to text-only replies")
                raise SarvamCreditsError(402, r.text[:300])
            if r.status_code in (429, 503) and attempt < retries:
                attempt += 1
                delay = self.s.sarvam_retry_base_s * 2 ** (attempt - 1)
                log("sarvam_retry", path=path, status=r.status_code, attempt=attempt, delay_s=delay)
                await asyncio.sleep(delay)
                continue
            if r.status_code >= 400:
                log("sarvam_error", path=path, status=r.status_code, response=r.text[:300])
                raise SarvamError(r.status_code, r.text[:300])
            return r.json()

    # ---------- STT ----------
    async def stt(self, data: bytes, mime: str = "audio/ogg", filename: str = "voice.ogg") -> dict:
        """-> {transcript, language_code, language_probability}"""
        if self.mock:
            log("sarvam_mock_stt", bytes=len(data))
            return {"transcript": self.s.sarvam_mock_transcript, "language_code": self.s.sarvam_mock_language,
                    "language_probability": 0.97}
        suffix = ".ogg" if "ogg" in mime or "opus" in mime else "." + (mime.split("/")[-1].split(";")[0] or "ogg")
        try:
            data, dur, trimmed = await audio.ensure_stt_length(data, suffix)
            if trimmed:
                mime, filename = "audio/ogg", "voice.ogg"
        except audio.AudioError as e:
            log("stt_trim_failed", error=str(e))
        res = await self._request(
            "POST", "/speech-to-text",
            files={"file": (filename, data, mime.split(";")[0].strip() or "audio/ogg")},
            data={"model": self.s.sarvam_stt_model, "mode": "codemix"},
        )
        out = {"transcript": res.get("transcript", "") or "", "language_code": res.get("language_code"),
               "language_probability": res.get("language_probability")}
        log("sarvam_stt", language=out["language_code"], prob=out["language_probability"],
            chars=len(out["transcript"]))
        return out

    # ---------- LLM ----------
    async def chat(self, messages: list[dict], *, schema: dict | None = None, schema_name: str = "out",
                   max_tokens: int = 1024, temperature: float | None = None, model: str | None = None) -> str:
        body: dict = {"model": model or self.s.sarvam_llm_model, "messages": messages,
                      "max_tokens": max(1024, max_tokens)}
        if temperature is not None:
            body["temperature"] = temperature
        if schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
        res = await self._request("POST", "/v1/chat/completions", json=body, timeout=60)
        choice = (res.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if not content:
            log("sarvam_llm_empty", finish_reason=choice.get("finish_reason"))
        return content.strip()

    async def intent(self, text: str) -> dict:
        if self.mock:
            return keyword_intent(text)
        try:
            raw = await self.chat([{"role": "system", "content": INTENT_SYSTEM},
                                   {"role": "user", "content": text[:500]}],
                                  schema=INTENT_SCHEMA, schema_name="ops_intent")
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0) if m else raw)
            if data.get("intent") not in INTENTS:
                raise ValueError(f"bad intent {data.get('intent')}")
            data["confidence"] = float(data.get("confidence", 0.5))
            opt = data.get("option")
            data["option"] = opt if opt in OPTIONS else keyword_option(text)
            log("sarvam_intent", intent=data["intent"], confidence=data["confidence"], option=data["option"])
            return data
        except SarvamCreditsError:
            raise
        except Exception as e:  # noqa: BLE001
            log("sarvam_intent_fallback", error=str(e)[:200])
            return keyword_intent(text)

    async def compose(self, facts: dict, goal: str, language: str, fallback: str, style: str = "native") -> str:
        """1-2 plain sentences in the founder's language from structured facts. Falls back to `fallback`."""
        if self.mock:
            return fallback
        lang_name = LANG_NAMES.get(language, "Hindi")
        how = ("in Tanglish: Tamil written in English (Latin) letters, mixed naturally with English words, never "
               "Tamil script" if style == "tanglish" else
               f"in {lang_name} using native script (common English tech words are fine)")
        system = (
            f"You write WhatsApp voice replies for {self.s.founder_name}, a shop owner who is not technical. "
            f"Write 1-2 short sentences {how}. "
            "Sound like a friendly colleague talking casually on a voice note: warm, natural, conversational, "
            "contractions and everyday words, the way people actually speak, not formal or robotic. "
            "No jargon, no markdown, no emojis. Under 200 characters. Write numbers as words where natural."
        )
        user = f"Goal: {goal}\nFacts: {json.dumps(facts, ensure_ascii=False)[:1200]}"
        try:
            # Voice replies use the conversations model (built for voice agents, no reasoning tokens);
            # fall back to the default model if it returns nothing.
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            text = await self.chat(msgs, model=getattr(self.s, "sarvam_compose_model", None))
            if not text:
                text = await self.chat(msgs, max_tokens=4096)
            text = text.strip().strip('"').strip()
            return text[:320] if text else fallback
        except SarvamCreditsError:
            raise
        except Exception as e:  # noqa: BLE001
            log("sarvam_compose_fallback", error=str(e)[:200])
            return fallback

    # ---------- Translate ----------
    async def translate(self, text: str, target: str, source: str = "en-IN", *, model: str | None = None,
                        mode: str | None = None, output_script: str | None = None,
                        fallback_source: str | None = None) -> str:
        """Sarvam /translate. With `model` (mayura:v1) the extra fields are sent; if that call fails we retry
        once with the default model (native script, formal), using `fallback_source` when source is "auto"."""
        if not text or (target == source and not output_script):
            return text
        if self.mock:
            return text
        variant = (model, mode, output_script)
        if model and not self._model_rejected.get(variant):
            body = {"input": text[:1000 if model.startswith("mayura") else 2000], "source_language_code": source,
                    "target_language_code": target, "model": model}
            if mode:
                body["mode"] = mode
            if output_script:
                body["output_script"] = output_script
            try:
                res = await self._request("POST", "/translate", json=body)
                if res.get("translated_text"):
                    return res["translated_text"]
            except SarvamCreditsError:
                raise
            except Exception as e:  # noqa: BLE001
                if isinstance(e, SarvamError) and e.status in (400, 422):
                    self._model_rejected[variant] = True  # do not pay a failing call on every message
                log("sarvam_translate_model_fallback", model=model, mode=mode, output_script=output_script,
                    error=str(e)[:200])
        if source == "auto":  # sarvam-translate:v1 needs an explicit source
            source = fallback_source or "en-IN"
        if source == target:
            return text
        try:
            res = await self._request("POST", "/translate", json={
                "input": text[:2000], "source_language_code": source, "target_language_code": target,
                "model": self.s.sarvam_translate_model,
            })
            return res.get("translated_text") or text
        except SarvamCreditsError:
            raise
        except Exception as e:  # noqa: BLE001
            log("sarvam_translate_fallback", error=str(e)[:200])
            return text

    async def to_tanglish(self, text_en: str) -> str:
        """English -> Tanglish (Romanized Tamil, code-mixed). Falls back to native Tamil script."""
        return await self.translate(text_en, "ta-IN", model=TANGLISH_MODEL, mode="code-mixed", output_script="roman")

    # ---------- Transliterate ----------
    async def transliterate(self, text: str, source: str, target: str, spoken_form: bool = False) -> str:
        body: dict = {"input": text[:2000], "source_language_code": source, "target_language_code": target}
        if spoken_form:
            body["spoken_form"] = True
        try:
            res = await self._request("POST", "/transliterate", json=body)
        except SarvamError as e:
            if not spoken_form or isinstance(e, SarvamCreditsError) or e.status not in (400, 422):
                raise
            body.pop("spoken_form")
            res = await self._request("POST", "/transliterate", json=body)
        return res.get("transliterated_text") or ""

    async def tanglish_to_tamil(self, text: str) -> str:
        """Tamil-script version of a Tanglish sentence so Bulbul (ta-IN) pronounces it properly:
        /transliterate en-IN -> ta-IN, else the LLM rewrites it, else the Tanglish text as is."""
        if self.mock or not text or TAMIL_SCRIPT.search(text):
            return text
        try:
            out = await self.transliterate(text, "en-IN", "ta-IN", spoken_form=True)
            if TAMIL_SCRIPT.search(out):
                log("tanglish_transliterated", chars=len(text))
                return out
            log("tanglish_transliterate_empty")
        except SarvamCreditsError:
            raise
        except Exception as e:  # noqa: BLE001
            log("tanglish_transliterate_failed", error=str(e)[:200])
        try:
            out = await self.chat([
                {"role": "system", "content": "Rewrite the user's Tanglish (Tamil written in English letters) in Tamil "
                                              "script so a Tamil voice can read it aloud. Keep English words in "
                                              "English. Same meaning, nothing added. Output only the rewritten text."},
                {"role": "user", "content": text[:1000]}])
            out = out.strip().strip('"').strip()
            if TAMIL_SCRIPT.search(out):
                log("tanglish_llm_rewritten", chars=len(text))
                return out
        except SarvamCreditsError:
            raise
        except Exception as e:  # noqa: BLE001
            log("tanglish_llm_rewrite_failed", error=str(e)[:200])
        return text

    # ---------- TTS ----------
    async def tts(self, text: str, language: str, speaker: str | None = None, pace: float | None = None) -> bytes:
        """-> WAV bytes. Always sends `model` explicitly (the API default is bulbul:v3)."""
        if self.mock:
            log("sarvam_mock_tts", chars=len(text), language=language)
            return await audio.tone_wav(seconds=min(3.0, 0.5 + len(text) / 80))
        lang = language if language in TTS_LANGS else "en-IN"
        body: dict = {"text": text[:2500], "language_code": lang, "model": self.s.sarvam_tts_model,
                      "speech_sample_rate": 24000}
        if speaker:
            body["speaker"] = speaker
        if pace:
            body["pace"] = max(0.5, min(2.0, float(pace)))
        try:
            res = await self._request("POST", "/text-to-speech", json=body)
        except SarvamError as e:
            # Older API revisions named the field target_language_code; retry once with it.
            if e.status in (400, 422) and "language" in str(e).lower() and not isinstance(e, SarvamCreditsError):
                body["target_language_code"] = body.pop("language_code")
                res = await self._request("POST", "/text-to-speech", json=body)
            else:
                raise
        audios = res.get("audios") or []
        if not audios:
            raise SarvamError(200, "no audios in TTS response")
        wav = base64.b64decode(audios[0])
        log("sarvam_tts", chars=len(text), language=lang, speaker=speaker or "default", wav_bytes=len(wav))
        return wav
