"""Microsoft Graph sender for personalized ticket-allocation messages."""

from __future__ import annotations

import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import config


class EmailSendError(RuntimeError):
    """A provider failure with a known or uncertain delivery outcome."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class GraphEmailConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    sender: str
    app_url: str

    @classmethod
    def from_env(cls) -> GraphEmailConfig:
        return cls(
            tenant_id=os.getenv("MS_GRAPH_TENANT_ID", "").strip(),
            client_id=os.getenv("MS_GRAPH_CLIENT_ID", "").strip(),
            client_secret=os.getenv("MS_GRAPH_CLIENT_SECRET", "").strip(),
            sender=os.getenv("EMAIL_SENDER_ADDRESS", "").strip(),
            app_url=os.getenv("PUBLIC_APP_URL", "").strip().rstrip("/"),
        )

    @property
    def configured(self) -> bool:
        return all(
            (
                self.tenant_id,
                self.client_id,
                self.client_secret,
                self.sender,
                self.app_url,
            )
        )

    @property
    def missing(self) -> list[str]:
        values = {
            "MS_GRAPH_TENANT_ID": self.tenant_id,
            "MS_GRAPH_CLIENT_ID": self.client_id,
            "MS_GRAPH_CLIENT_SECRET": self.client_secret,
            "EMAIL_SENDER_ADDRESS": self.sender,
            "PUBLIC_APP_URL": self.app_url,
        }
        return [name for name, value in values.items() if not value]


class GraphEmailClient:
    def __init__(self, settings: GraphEmailConfig) -> None:
        self.settings = settings
        self._token = ""
        self._token_expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        if not self.settings.configured:
            raise EmailSendError("Microsoft Graph email is not configured.")

        url = (
            "https://login.microsoftonline.com/"
            f"{urllib.parse.quote(self.settings.tenant_id)}/oauth2/v2.0/token"
        )
        body = urllib.parse.urlencode(
            {
                "client_id": self.settings.client_id,
                "client_secret": self.settings.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            }
        ).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise EmailSendError(
                f"Microsoft authentication failed ({error.code}): {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise EmailSendError(
                f"Could not reach Microsoft authentication: {error}"
            ) from error

        self._token = str(payload["access_token"])
        self._token_expires_at = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    def send_ticket_email(
        self,
        *,
        recipient: str,
        name: str,
        new_tickets: list[int],
        all_tickets: list[int],
    ) -> dict:
        token = self._access_token()
        subject, html_body, _ = render_ticket_email(
            name=name,
            new_tickets=new_tickets,
            all_tickets=all_tickets,
            app_url=self.settings.app_url,
        )
        endpoint = (
            "https://graph.microsoft.com/v1.0/users/"
            f"{urllib.parse.quote(self.settings.sender)}/sendMail"
        )
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [
                    {"emailAddress": {"address": recipient, "name": name}}
                ],
            },
            "saveToSentItems": True,
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return {
                    "status_code": response.status,
                    "request_id": response.headers.get("request-id", ""),
                }
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            raise EmailSendError(
                f"Microsoft Graph rejected the message ({error.code}): {detail}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise EmailSendError(
                f"The Microsoft Graph response was uncertain: {error}",
                outcome_unknown=True,
            ) from error


def render_ticket_email(
    *, name: str, new_tickets: list[int], all_tickets: list[int], app_url: str
) -> tuple[str, str, str]:
    safe_name = html.escape(name)
    safe_org = html.escape(config.ORG_NAME)
    safe_prize = html.escape(config.PRIZE_TEXT)
    safe_url = html.escape(app_url, quote=True)
    new_chips = "".join(
        f'<span style="display:inline-block;margin:4px;padding:10px 14px;'
        f'border-radius:9px;background:#e7f2ea;color:#1f6b33;font-weight:700">'
        f"#{ticket}</span>"
        for ticket in sorted(new_tickets)
    )
    all_numbers = ", ".join(f"#{ticket}" for ticket in sorted(all_tickets))
    subject = f"Your {config.ORG_NAME} ticket numbers"
    html_body = f"""
<!doctype html>
<html><body style="margin:0;background:#f6f5f1;font-family:Arial,sans-serif;color:#14202b">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="padding:24px 12px">
<tr><td align="center"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:620px;background:#fff;border:1px solid #e3e1da;border-radius:16px;overflow:hidden">
<tr><td style="padding:30px;background:#17324d;color:#fff"><div style="font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:#8ec9d6">{safe_org}</div><h1 style="margin:8px 0 0;font-size:30px">Your tickets are ready</h1></td></tr>
<tr><td style="padding:30px"><p style="font-size:17px">Hello {safe_name},</p><p>Your newly allocated Reverse Draw ticket numbers are:</p><div style="margin:18px 0">{new_chips}</div><p style="color:#3e4852"><b>All of your current tickets:</b><br>{html.escape(all_numbers)}</p><div style="margin:26px 0;padding:18px;border-radius:12px;background:#fdf1cc;color:#7a5700"><b>Prize: {safe_prize}</b><br>Keep these numbers handy and follow the live draw board.</div><p><a href="{safe_url}" style="display:inline-block;padding:13px 20px;border-radius:10px;background:#17324d;color:#fff;text-decoration:none;font-weight:700">Open the public board</a></p><p style="margin-top:30px;font-size:13px;color:#5b6570">This message was sent because new tickets were allocated to your email address. Please contact the draw organizer if anything looks incorrect.</p></td></tr>
</table></td></tr></table></body></html>
""".strip()
    text_body = (
        f"Hello {name},\n\n"
        f"Your new {config.ORG_NAME} tickets: "
        f"{', '.join(f'#{ticket}' for ticket in sorted(new_tickets))}\n\n"
        f"All current tickets: {all_numbers}\n"
        f"Prize: {config.PRIZE_TEXT}\n"
        f"Public board: {app_url}\n"
    )
    return subject, html_body, text_body
