"""Meta WhatsApp Cloud API: signature check, inbound parsing, media download, outbound text/buttons/voice."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass, field

import httpx

from .config import Settings, normalize_phone
from .log import log


def verify_meta_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: 'sha256=' + hex HMAC-SHA256 of the raw body keyed with the app secret."""
    if not app_secret or not header:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.strip())


@dataclass
class Inbound:
    msg_id: str
    sender: str  # phone (digits) or BSUID when the user has a username and no phone is shared
    phone: str
    user_id: str
    type: str  # text | audio | button | other
    text: str = ""
    media_id: str = ""
    mime_type: str = ""
    button_id: str = ""
    button_title: str = ""
    raw: dict = field(default_factory=dict)


def parse_webhook(payload: dict) -> list[Inbound]:
    """Flatten entry[].changes[].value.messages[] into Inbound items. Status-only payloads give []."""
    out: list[Inbound] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            contacts = value.get("contacts", []) or []
            for m in value.get("messages", []) or []:
                phone = normalize_phone(m.get("from"))
                user_id = m.get("from_user_id") or m.get("user_id") or ""
                if not user_id and contacts:
                    user_id = contacts[0].get("user_id", "") or ""
                if not phone and contacts:
                    phone = normalize_phone(contacts[0].get("wa_id"))
                mtype = m.get("type", "")
                item = Inbound(msg_id=m.get("id", ""), sender=phone or user_id, phone=phone, user_id=user_id,
                               type="other", raw=m)
                if mtype == "text":
                    item.type = "text"
                    item.text = (m.get("text") or {}).get("body", "")
                elif mtype in ("audio", "voice"):
                    a = m.get("audio") or m.get("voice") or {}
                    item.type = "audio"
                    item.media_id = a.get("id", "")
                    item.mime_type = a.get("mime_type", "audio/ogg")
                elif mtype == "interactive":
                    inter = m.get("interactive") or {}
                    rep = inter.get("button_reply") or inter.get("list_reply") or {}
                    item.type = "button"
                    item.button_id = rep.get("id", "")
                    item.button_title = rep.get("title", "")
                elif mtype == "button":  # template quick-reply buttons
                    b = m.get("button") or {}
                    item.type = "button"
                    item.button_id = b.get("payload", "")
                    item.button_title = b.get("text", "")
                out.append(item)
    return out


class WhatsAppClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.sent: list[dict] = []  # dry-run journal (also handy in tests / debugging)

    @property
    def base(self) -> str:
        return f"{self.s.whatsapp_base_url}/{self.s.whatsapp_api_version}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.whatsapp_token}"}

    def _audio_dir(self) -> str:
        d = self.s.dry_run_audio_dir or os.path.join(self.s.data_dir, "outbox")
        os.makedirs(d, exist_ok=True)
        return d

    async def _post_message(self, body: dict) -> dict:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **body}
        if self.s.whatsapp_dry_run:
            self.sent.append(body)
            log("wa_dry_run_send", type=body.get("type"), to=body.get("to"), body=body)
            return {"messages": [{"id": f"wamid.dry.{uuid.uuid4().hex[:12]}"}]}
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{self.base}/{self.s.whatsapp_phone_number_id}/messages", json=body,
                             headers=self._headers())
        if r.status_code >= 400:
            log("wa_send_failed", status=r.status_code, response=r.text[:500], type=body.get("type"))
            r.raise_for_status()
        data = r.json()
        log("wa_sent", type=body.get("type"), to=body.get("to"),
            wamid=(data.get("messages") or [{}])[0].get("id"))
        return data

    @staticmethod
    def _recipient(to: str) -> dict:
        return {"to": to}

    async def send_text(self, to: str, text: str) -> dict:
        return await self._post_message({**self._recipient(to), "type": "text",
                                         "text": {"preview_url": False, "body": text[:4096]}})

    async def send_buttons(self, to: str, body_text: str, buttons: list[tuple[str, str]]) -> dict:
        """buttons = [(id, title)], max 3, title <= 20 chars, body <= 1024 chars."""
        return await self._post_message({
            **self._recipient(to),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text[:1024]},
                "action": {"buttons": [{"type": "reply", "reply": {"id": bid[:256], "title": title[:20]}}
                                       for bid, title in buttons[:3]]},
            },
        })

    async def upload_media(self, data: bytes, mime: str = "audio/ogg", filename: str = "voice.ogg") -> str:
        if self.s.whatsapp_dry_run:
            path = os.path.join(self._audio_dir(), f"{int(time.time() * 1000)}-{filename}")
            with open(path, "wb") as f:
                f.write(data)
            log("wa_dry_run_upload", path=path, bytes=len(data), mime=mime)
            return f"dry-media-{os.path.basename(path)}"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.base}/{self.s.whatsapp_phone_number_id}/media", headers=self._headers(),
                             data={"messaging_product": "whatsapp", "type": mime},
                             files={"file": (filename, data, mime)})
        if r.status_code >= 400:
            log("wa_upload_failed", status=r.status_code, response=r.text[:500])
            r.raise_for_status()
        media_id = r.json().get("id", "")
        log("wa_uploaded", media_id=media_id, bytes=len(data))
        return media_id

    async def send_voice(self, to: str, ogg: bytes) -> dict:
        media_id = await self.upload_media(ogg, "audio/ogg", "voice.ogg")
        return await self._post_message({**self._recipient(to), "type": "audio",
                                         "audio": {"id": media_id, "voice": True}})

    async def download_media(self, media_id: str) -> tuple[bytes, str]:
        """GET /{media_id} -> url (valid 5 min) -> GET url with the bearer token."""
        if self.s.whatsapp_dry_run:
            # Offline: use DRY_RUN_INBOUND_AUDIO if given, else a generated 1 s tone, instead of calling Meta.
            path = os.environ.get("DRY_RUN_INBOUND_AUDIO", "")
            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    return f.read(), "audio/ogg"
            from .audio import _run

            ogg = await _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                              "sine=frequency=300:duration=1", "-ac", "1", "-c:a", "libopus", "-f", "ogg", "pipe:1"])
            log("wa_dry_run_download", media_id=media_id, bytes=len(ogg))
            return ogg, "audio/ogg"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            meta = await c.get(f"{self.base}/{media_id}", headers=self._headers())
            meta.raise_for_status()
            info = meta.json()
            url = info.get("url")
            if not url:
                raise RuntimeError("media url missing")
            r = await c.get(url, headers=self._headers())
            if r.status_code == 404:  # URL expired: fetch a fresh one once
                info = (await c.get(f"{self.base}/{media_id}", headers=self._headers())).json()
                r = await c.get(info.get("url", url), headers=self._headers())
            r.raise_for_status()
        return r.content, info.get("mime_type", "audio/ogg")
