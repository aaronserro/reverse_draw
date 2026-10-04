"""Email providers for personalized ticket-allocation messages."""

from __future__ import annotations

import html
import json
import os
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Protocol

from . import config


class EmailSendError(RuntimeError):
    """A provider failure with a known or uncertain delivery outcome."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class EmailClient(Protocol):
    def send_ticket_email(
        self,
        *,
        recipient: str,
        name: str,
        new_tickets: list[int],
        all_tickets: list[int],
        trading_code: str,
    ) -> dict: ...


@dataclass(frozen=True)
class SMTPEmailConfig:
    host: str
    port: int
    username: str
    password: str
    security: str
    sender: str
    sender_name: str
    app_url: str
    provider: str = "smtp"

    @classmethod
    def from_env(cls) -> SMTPEmailConfig:
        port_text = os.getenv("SMTP_PORT", "587").strip()
        try:
            port = int(port_text)
        except ValueError:
            port = 0
        username = os.getenv("SMTP_USERNAME", "").strip()
        return cls(
            host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
            port=port,
            username=username,
            password="".join(os.getenv("SMTP_PASSWORD", "").split()),
            security=os.getenv("SMTP_SECURITY", "starttls").strip().lower(),
            sender=(os.getenv("EMAIL_SENDER_ADDRESS", "").strip() or username),
            sender_name=(
                os.getenv("EMAIL_SENDER_NAME", "").strip() or config.ORG_NAME
            ),
            app_url=os.getenv("PUBLIC_APP_URL", "").strip().rstrip("/"),
        )

    @property
    def configured(self) -> bool:
        return not self.missing

    @property
    def missing(self) -> list[str]:
        values = {
            "SMTP_HOST": self.host,
            "SMTP_PORT": self.port,
            "SMTP_USERNAME": self.username,
            "SMTP_PASSWORD": self.password,
            "EMAIL_SENDER_ADDRESS (or SMTP_USERNAME)": self.sender,
            "PUBLIC_APP_URL": self.app_url,
        }
        missing = [name for name, value in values.items() if not value]
        if self.security not in {"starttls", "ssl", "none"}:
            missing.append("SMTP_SECURITY (starttls, ssl, or none)")
        return missing


class SMTPEmailClient:
    def __init__(self, settings: SMTPEmailConfig) -> None:
        self.settings = settings

    def send_ticket_email(
        self,
        *,
        recipient: str,
        name: str,
        new_tickets: list[int],
        all_tickets: list[int],
        trading_code: str,
    ) -> dict:
        if not self.settings.configured:
            raise EmailSendError("SMTP email is not configured.")
        subject, html_body, text_body = render_ticket_email(
            name=name,
            new_tickets=new_tickets,
            all_tickets=all_tickets,
            app_url=self.settings.app_url,
            trading_code=trading_code,
        )
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr(
            (self.settings.sender_name, self.settings.sender)
        )
        message["To"] = formataddr((name, recipient))
        message["Reply-To"] = self.settings.sender
        message["Date"] = formatdate(localtime=False)
        sender_domain = self.settings.sender.rpartition("@")[2]
        message["Message-ID"] = make_msgid(domain=sender_domain or None)
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")

        context = ssl.create_default_context()
        submission_started = False
        try:
            if self.settings.security == "ssl":
                server = smtplib.SMTP_SSL(
                    self.settings.host,
                    self.settings.port,
                    timeout=30,
                    context=context,
                )
            else:
                server = smtplib.SMTP(
                    self.settings.host, self.settings.port, timeout=30
                )
            with server:
                server.ehlo()
                if self.settings.security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                server.login(self.settings.username, self.settings.password)
                submission_started = True
                server.send_message(message)
            return {"status_code": 250, "request_id": ""}
        except smtplib.SMTPAuthenticationError as error:
            raise EmailSendError(
                "SMTP sign-in failed. For Gmail, use a 16-character app "
                "password rather than the normal account password."
            ) from error
        except smtplib.SMTPServerDisconnected as error:
            if not submission_started:
                raise EmailSendError(
                    f"The SMTP connection closed before sending: {error}"
                ) from error
            raise EmailSendError(
                f"The SMTP response was uncertain: {error}",
                outcome_unknown=True,
            ) from error
        except smtplib.SMTPException as error:
            raise EmailSendError(
                f"The SMTP server rejected the message: {error}"
            ) from error
        except (OSError, TimeoutError) as error:
            if not submission_started:
                raise EmailSendError(
                    f"Could not connect to the SMTP server: {error}"
                ) from error
            raise EmailSendError(
                f"The SMTP response was uncertain: {error}",
                outcome_unknown=True,
            ) from error


@dataclass(frozen=True)
class GraphEmailConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    sender: str
    app_url: str
    provider: str = "graph"

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
        trading_code: str,
    ) -> dict:
        token = self._access_token()
        subject, html_body, _ = render_ticket_email(
            name=name,
            new_tickets=new_tickets,
            all_tickets=all_tickets,
            app_url=self.settings.app_url,
            trading_code=trading_code,
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


def email_config() -> SMTPEmailConfig | GraphEmailConfig:
    """Load the selected provider, defaulting to SMTP."""
    selected = os.getenv("EMAIL_PROVIDER", "").strip().lower()
    if selected == "graph":
        return GraphEmailConfig.from_env()
    return SMTPEmailConfig.from_env()


def build_email_client(
    settings: SMTPEmailConfig | GraphEmailConfig,
) -> EmailClient:
    if isinstance(settings, GraphEmailConfig):
        return GraphEmailClient(settings)
    return SMTPEmailClient(settings)


def render_ticket_email(
    *,
    name: str,
    new_tickets: list[int],
    all_tickets: list[int],
    app_url: str,
    trading_code: str = "",
    site_access_code: str | None = None,
) -> tuple[str, str, str]:
    if site_access_code is None:
        site_access_code = (
            os.getenv("PUBLIC_ACCESS_CODE")
            or str(config.PUBLIC_ACCESS_CODE or "")
        ).strip()
    safe_name = html.escape(name)
    safe_org = html.escape(config.ORG_NAME)
    safe_prize = html.escape(config.PRIZE_TEXT)
    safe_url = html.escape(app_url, quote=True)
    safe_trading_url = html.escape(f"{app_url}/trading/login", quote=True)
    safe_trading_code = html.escape(trading_code)
    safe_site_access_code = html.escape(site_access_code)
    site_access_section = ""
    site_access_text = f"View the live draw board: {app_url}\n\n"
    if site_access_code:
        site_access_section = f"""
<tr><td class="email-pad" style="padding:0 38px 28px">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#edf4fa" style="background-color:#edf4fa;border:1px solid #c8d9e8;border-radius:10px">
        <tr><td align="center" style="padding:20px;color:#17324d">
            <div style="font-size:12px;line-height:18px;letter-spacing:1.3px;text-transform:uppercase;font-weight:700">Live draw board access code</div>
            <div style="margin:7px 0 5px;font-family:Courier New,monospace;font-size:32px;line-height:38px;letter-spacing:7px;font-weight:700">{safe_site_access_code}</div>
            <div style="font-size:12px;line-height:18px;color:#526b80">Use this shared code when you open the live draw board.</div>
        </td></tr>
    </table>
</td></tr>
"""
        site_access_text = (
            f"Live draw board access code: {site_access_code}\n"
            f"Open the live draw board: {app_url}\n\n"
        )
    new_chips = "".join(
        f'<span class="ticket-chip" style="display:inline-block;margin:5px;'
        f'padding:12px 16px;border:1px solid #b7dfc2;border-radius:8px;'
        f'background-color:#eaf7ed;color:#145c2b;font-size:18px;'
        f'font-weight:700;line-height:1">#{ticket}</span>'
        for ticket in sorted(new_tickets)
    )
    all_numbers = ", ".join(f"#{ticket}" for ticket in sorted(all_tickets))
    ticket_word = "ticket" if len(new_tickets) == 1 else "tickets"
    allocation_verb = "has" if len(new_tickets) == 1 else "have"
    subject = f"Your {config.ORG_NAME} ticket numbers are confirmed"
    html_body = f"""
<!doctype html>
<html lang="en">
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<style>
@keyframes ticketReveal {{
    0% {{ opacity:0; transform:translateY(8px) scale(.96); }}
    100% {{ opacity:1; transform:translateY(0) scale(1); }}
}}
.ticket-chip {{ animation:ticketReveal .65s ease-out both; }}
@media (prefers-reduced-motion: reduce) {{
    .ticket-chip {{ animation:none !important; }}
}}
@media only screen and (max-width:620px) {{
    .email-shell {{ width:100% !important; }}
    .email-pad {{ padding-left:22px !important; padding-right:22px !important; }}
    .email-title {{ font-size:28px !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background-color:#eef1f4;color:#172534;font-family:Arial,Helvetica,sans-serif;-webkit-text-size-adjust:100%">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent">Your personalized {safe_org} ticket confirmation is inside.</div>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#eef1f4">
<tr><td align="center" style="padding:30px 12px">
<table class="email-shell" role="presentation" width="620" cellspacing="0" cellpadding="0" border="0" style="width:620px;max-width:620px;background-color:#ffffff;border:1px solid #dfe4e8;border-radius:14px">
<tr><td height="7" bgcolor="#e9ad24" style="height:7px;line-height:7px;font-size:1px;border-radius:14px 14px 0 0">&nbsp;</td></tr>
<tr><td class="email-pad" bgcolor="#17324d" style="padding:32px 38px;background-color:#17324d;color:#ffffff">
    <div style="font-size:12px;line-height:18px;letter-spacing:1.8px;text-transform:uppercase;color:#a9dce5;font-weight:700">{safe_org}</div>
    <h1 class="email-title" style="margin:8px 0 6px;font-size:34px;line-height:42px;color:#ffffff;font-weight:700">You're officially in.</h1>
    <p style="margin:0;font-size:16px;line-height:24px;color:#dce8f1">Your personalized ticket confirmation</p>
</td></tr>
<tr><td class="email-pad" style="padding:34px 38px 12px">
    <p style="margin:0 0 16px;font-size:18px;line-height:28px;color:#172534">Hello {safe_name},</p>
    <p style="margin:0;font-size:16px;line-height:25px;color:#425466">Your new {ticket_word} for the <strong style="color:#172534">{safe_org}</strong> {allocation_verb} been allocated. Keep this email for your records.</p>
</td></tr>
<tr><td class="email-pad" align="center" style="padding:18px 38px 28px">
    <div style="margin-bottom:12px;font-size:12px;line-height:18px;letter-spacing:1.5px;text-transform:uppercase;color:#657687;font-weight:700">Your new {ticket_word}</div>
    <div>{new_chips}</div>
</td></tr>
<tr><td class="email-pad" style="padding:0 38px 26px">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#f5f7f8" style="background-color:#f5f7f8;border:1px solid #e1e6e9;border-radius:10px">
        <tr><td style="padding:18px 20px">
            <div style="font-size:13px;line-height:19px;color:#657687;font-weight:700">ALL OF YOUR CURRENT TICKETS</div>
            <div style="margin-top:5px;font-size:16px;line-height:26px;color:#172534;font-weight:700">{html.escape(all_numbers)}</div>
        </td></tr>
    </table>
</td></tr>
<tr><td class="email-pad" style="padding:0 38px 28px">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#edf7f2" style="background-color:#edf7f2;border:1px solid #bfdfcd;border-radius:10px">
        <tr><td align="center" style="padding:20px;color:#174b32">
            <div style="font-size:12px;line-height:18px;letter-spacing:1.3px;text-transform:uppercase;font-weight:700">Your private trading login code</div>
            <div style="margin:7px 0 5px;font-family:Courier New,monospace;font-size:32px;line-height:38px;letter-spacing:7px;font-weight:700">{safe_trading_code}</div>
            <div style="font-size:12px;line-height:18px;color:#496b5a">Sign in with your full name. Keep this code private.</div>
        </td></tr>
    </table>
</td></tr>
{site_access_section}
<tr><td class="email-pad" style="padding:0 38px 28px">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" bgcolor="#fff5d9" style="background-color:#fff5d9;border-left:5px solid #e9ad24;border-radius:8px">
        <tr><td style="padding:18px 20px;color:#614900">
            <div style="font-size:12px;line-height:18px;letter-spacing:1.3px;text-transform:uppercase;font-weight:700">Grand prize</div>
            <div style="margin-top:2px;font-size:24px;line-height:32px;font-weight:700">{safe_prize}</div>
        </td></tr>
    </table>
</td></tr>
<tr><td class="email-pad" align="center" style="padding:0 38px 32px">
    <table role="presentation" cellspacing="0" cellpadding="0" border="0"><tr><td bgcolor="#176b3a" style="background-color:#176b3a;border-radius:8px">
        <a href="{safe_trading_url}" style="display:inline-block;padding:14px 24px;color:#ffffff;font-size:16px;line-height:20px;text-decoration:none;font-weight:700">Sign in to ticket trading&nbsp; →</a>
    </td></tr></table>
    <p style="margin:14px 0 0;font-size:12px;line-height:19px;color:#71808e">If the button does not work, copy this address:<br><a href="{safe_trading_url}" style="color:#315f83;word-break:break-all">{safe_trading_url}</a></p>
    <p style="margin:10px 0 0;font-size:12px;line-height:19px"><a href="{safe_url}" style="color:#315f83">View the live draw board</a></p>
</td></tr>
<tr><td class="email-pad" bgcolor="#f7f8f9" style="padding:24px 38px;background-color:#f7f8f9;border-top:1px solid #e5e9ec;border-radius:0 0 14px 14px">
    <p style="margin:0 0 8px;font-size:13px;line-height:20px;color:#526271"><strong style="color:#283846">Why did I receive this?</strong><br>This confirmation was sent because tickets were allocated to your email address.</p>
    <p style="margin:0;font-size:12px;line-height:19px;color:#71808e">Questions or incorrect ticket numbers? Reply directly to this email to contact the draw organizer.</p>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>
""".strip()
    text_body = (
        f"{config.ORG_NAME} — TICKET CONFIRMATION\n\n"
        f"Hello {name},\n\n"
        f"You're officially in. Your new {config.ORG_NAME} {ticket_word}: "
        f"{', '.join(f'#{ticket}' for ticket in sorted(new_tickets))}\n\n"
        f"All current tickets: {all_numbers}\n"
        f"Grand prize: {config.PRIZE_TEXT}\n\n"
        f"Your private trading login code: {trading_code}\n"
        f"Sign in to ticket trading: {app_url}/trading/login\n"
        "Use your full name and keep this code private.\n\n"
        f"{site_access_text}"
        "This confirmation was sent because tickets were allocated to your "
        "email address. Reply to this email if anything looks incorrect.\n"
    )
    return subject, html_body, text_body
