"""Direct SMTP / IMAP send-and-save path.

Bypasses Mail.app's compose editor entirely. The editor injects share-wrapper
HTML and `color: rgb(0,0,0)` into pasted bodies, which breaks nested-quote
rendering on iOS Mail. By building MIME ourselves and sending via SMTP, we
get full control over what hits the wire.

Credentials live in the macOS Keychain. Look up by:
    security find-generic-password -s "apple-mail-mcp/<account>" -a <email> -w

Currently only iCloud is supported. Add to ACCOUNT_CONFIG below to extend.
"""

import email as email_mod
import imaplib
import mimetypes
import re
import smtplib
import subprocess
import time
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime
from pathlib import Path
from typing import Optional, List, Tuple


# ---------------------------------------------------------------------------
# Per-account server config. Only iCloud for now; extend per-account here.
# ---------------------------------------------------------------------------

ACCOUNT_CONFIG = {
    "iCloud": {
        "smtp_host": "smtp.mail.me.com",
        "smtp_port": 587,
        "smtp_use_starttls": True,
        "imap_host": "imap.mail.me.com",
        "imap_port": 993,
        "sent_folder": "Sent Messages",
        "drafts_folder": "Drafts",
    },
}


# ---------------------------------------------------------------------------
# Credential lookup
# ---------------------------------------------------------------------------


def _keychain_service_name(account: str) -> str:
    return f"apple-mail-mcp/{account}"


def _get_keychain_password(account: str, email_address: str) -> Optional[str]:
    """Return the app-specific password for an account, or None if absent."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                _keychain_service_name(account),
                "-a",
                email_address,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        password = result.stdout.strip()
        return password or None
    except (subprocess.SubprocessError, OSError):
        return None


def has_smtp_credentials(account: str, email_address: str) -> bool:
    """True if both an account config and a Keychain password exist."""
    if account not in ACCOUNT_CONFIG:
        return False
    return _get_keychain_password(account, email_address) is not None


# ---------------------------------------------------------------------------
# MIME builder
# ---------------------------------------------------------------------------


def _split_addresses(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [a.strip() for a in value.split(",") if a and a.strip()]


def _normalize_msgid(value: Optional[str]) -> Optional[str]:
    """Wrap a bare Message-ID in angle brackets if needed; pass through None."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    if not value.startswith("<"):
        value = "<" + value
    if not value.endswith(">"):
        value = value + ">"
    return value


def _build_message(
    sender_email: str,
    to: str,
    subject: str,
    plain: str,
    html: Optional[str],
    cc: Optional[str],
    bcc: Optional[str],
    attachments: Optional[List[str]],
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> EmailMessage:
    """Construct an EmailMessage with plain (+ optional html) + attachments.

    When *in_reply_to* (and optionally *references*) are given, the proper
    threading headers are set so the message shows up as a real reply in the
    recipient's client. Values may be passed with or without angle brackets.
    """
    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = ", ".join(_split_addresses(to))
    if cc:
        msg["Cc"] = ", ".join(_split_addresses(cc))
    if bcc:
        msg["Bcc"] = ", ".join(_split_addresses(bcc))
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender_email.split("@", 1)[-1])

    # Threading headers (for replies)
    irt = _normalize_msgid(in_reply_to)
    if irt:
        msg["In-Reply-To"] = irt
    refs = references.strip() if references else None
    if not refs and irt:
        refs = irt
    if refs:
        msg["References"] = refs

    # Plain text body (always required as fallback)
    msg.set_content(plain or "")

    # Optional HTML alternative
    if html:
        msg.add_alternative(html, subtype="html")

    # Attachments
    for path_str in attachments or []:
        path = Path(path_str)
        if not path.is_file():
            raise FileNotFoundError(f"Attachment not found: {path}")
        ctype, encoding = mimetypes.guess_type(str(path))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    return msg


# ---------------------------------------------------------------------------
# SMTP send + IMAP archive
# ---------------------------------------------------------------------------


def _smtp_send(
    config: dict, username: str, password: str, msg: EmailMessage
) -> None:
    """Send via SMTP. Raises on failure."""
    with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=30) as smtp:
        smtp.ehlo()
        if config.get("smtp_use_starttls"):
            smtp.starttls()
            smtp.ehlo()
        smtp.login(username, password)
        # send_message handles To/Cc/Bcc envelope derivation and strips Bcc
        smtp.send_message(msg)


def _imap_append(
    config: dict,
    username: str,
    password: str,
    folder: str,
    flags: str,
    msg: EmailMessage,
) -> None:
    """Append message to IMAP folder. Raises on failure.

    Note: Python's imaplib.append does NOT auto-quote mailbox names that
    contain spaces or other special characters, so we wrap the name in
    double quotes ourselves. iCloud rejects unquoted multi-word names
    with a 'Parse Error' (e.g. "Sent Messages", "Deleted Messages").
    """
    quoted_folder = f'"{folder}"' if not (folder.startswith('"') and folder.endswith('"')) else folder
    with imaplib.IMAP4_SSL(config["imap_host"], config["imap_port"]) as imap:
        imap.login(username, password)
        status, _ = imap.append(
            quoted_folder,
            flags,
            imaplib.Time2Internaldate(time.time()),
            bytes(msg),
        )
        if status != "OK":
            raise RuntimeError(f"IMAP APPEND to {folder!r} returned {status}")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def send_via_smtp(
    account: str,
    sender_email: str,
    to: str,
    subject: str,
    plain: str,
    html: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> Tuple[bool, str]:
    """Send an email via SMTP and archive a copy to the IMAP Sent folder.

    Returns (success, message). The message is human-readable confirmation
    or error text, suitable for returning to the MCP caller.
    """
    if account not in ACCOUNT_CONFIG:
        return False, f"SMTP send not configured for account: {account}"

    password = _get_keychain_password(account, sender_email)
    if not password:
        return (
            False,
            f"No Keychain password for {account} ({sender_email}). "
            f"Run: security add-generic-password -a {sender_email!r} "
            f"-s {_keychain_service_name(account)!r} -w",
        )

    config = ACCOUNT_CONFIG[account]

    try:
        msg = _build_message(
            sender_email, to, subject, plain, html, cc, bcc, attachments,
            in_reply_to=in_reply_to, references=references,
        )
    except FileNotFoundError as e:
        return False, f"Attachment error: {e}"

    # 1) Send via SMTP
    try:
        _smtp_send(config, sender_email, password, msg)
    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP authentication failed: {e}"
    except smtplib.SMTPException as e:
        return False, f"SMTP send failed: {e}"
    except OSError as e:
        return False, f"Network error during SMTP send: {e}"

    # 2) Append a copy to Sent (best-effort — don't fail the whole call if this fails)
    sent_warning = ""
    try:
        _imap_append(
            config,
            sender_email,
            password,
            config["sent_folder"],
            "(\\Seen)",
            msg,
        )
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        sent_warning = (
            f"\n(Note: sent successfully, but couldn't archive to "
            f"{config['sent_folder']!r}: {e}. iCloud server-side copy may still appear shortly.)"
        )

    lines = [
        "COMPOSING EMAIL (via SMTP)",
        "",
        "✓ Email sent successfully!",
        f"From: {sender_email}",
        f"To: {to}",
        f"Subject: {subject}",
    ]
    if cc:
        lines.append(f"CC: {cc}")
    if bcc:
        lines.append(f"BCC: {bcc}")
    if attachments:
        lines.append("Attachments:")
        for p in attachments:
            lines.append(f"  {p}")
    if sent_warning:
        lines.append(sent_warning)
    return True, "\n".join(lines)


def save_draft_via_imap(
    account: str,
    sender_email: str,
    to: str,
    subject: str,
    plain: str,
    html: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> Tuple[bool, str]:
    """Build a draft and append it to the IMAP Drafts folder with the \\Draft flag.

    Returns (success, message).
    """
    if account not in ACCOUNT_CONFIG:
        return False, f"Draft-via-IMAP not configured for account: {account}"

    password = _get_keychain_password(account, sender_email)
    if not password:
        return (
            False,
            f"No Keychain password for {account} ({sender_email}). "
            f"Run: security add-generic-password -a {sender_email!r} "
            f"-s {_keychain_service_name(account)!r} -w",
        )

    config = ACCOUNT_CONFIG[account]

    try:
        msg = _build_message(
            sender_email, to, subject, plain, html, cc, bcc, attachments,
            in_reply_to=in_reply_to, references=references,
        )
    except FileNotFoundError as e:
        return False, f"Attachment error: {e}"

    try:
        _imap_append(
            config,
            sender_email,
            password,
            config["drafts_folder"],
            "(\\Draft)",
            msg,
        )
    except imaplib.IMAP4.error as e:
        return False, f"IMAP error saving draft: {e}"
    except OSError as e:
        return False, f"Network error saving draft: {e}"

    lines = [
        "SAVING EMAIL AS DRAFT (via IMAP)",
        "",
        "✓ Draft saved successfully!",
        f"From: {sender_email}",
        f"To: {to}",
        f"Subject: {subject}",
    ]
    if cc:
        lines.append(f"CC: {cc}")
    if bcc:
        lines.append(f"BCC: {bcc}")
    if attachments:
        lines.append("Attachments:")
        for p in attachments:
            lines.append(f"  {p}")
    return True, "\n".join(lines)


# ---------------------------------------------------------------------------
# Reply support: fetch the original via IMAP and build a real Reply / Reply-All
# ---------------------------------------------------------------------------


def _imap_part_bytes(fetch_data) -> Optional[bytes]:
    """Extract the raw message bytes from an imaplib fetch() response."""
    for part in fetch_data or []:
        if isinstance(part, tuple) and len(part) > 1:
            return part[1]
    return None


def _find_newest_message(imap, subject_keyword: str, sender: Optional[str] = None):
    """Return the IMAP sequence number of the newest message matching subject."""
    safe_subj = subject_keyword.replace('"', "")
    if sender:
        criteria = f'(FROM "{sender.replace(chr(34), "")}" SUBJECT "{safe_subj}")'
    else:
        criteria = f'(SUBJECT "{safe_subj}")'
    typ, data = imap.search(None, criteria)
    if typ != "OK" or not data or not data[0]:
        return None
    best = None
    best_dt = None
    for num in data[0].split():
        typ, md = imap.fetch(num, "(BODY.PEEK[HEADER])")
        raw = _imap_part_bytes(md)
        if not raw:
            continue
        hdr = email_mod.message_from_bytes(raw, policy=policy.default)
        try:
            dt = parsedate_to_datetime(str(hdr["Date"]))
        except (TypeError, ValueError):
            dt = None
        if best is None or (dt is not None and (best_dt is None or dt > best_dt)):
            best, best_dt = num, dt
    return best


def _plain_body(msg) -> str:
    """Return the best-effort plain-text body of a parsed email message."""
    target = None
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.is_multipart():
                target = part
                break
    else:
        target = msg
    if target is None:
        return ""
    try:
        return target.get_content()
    except (LookupError, ValueError):
        payload = target.get_payload(decode=True)
        if payload is None:
            return ""
        return payload.decode(target.get_content_charset() or "utf-8", "replace")


_REPLY_PREFIX_RE = re.compile(r"^\s*(re|aw|wg|fwd?)\s*:", re.IGNORECASE)


def _build_reply_parts(orig, my_email: str, reply_body: str, reply_to_all: bool):
    """Compute (to, cc_list, subject, body, in_reply_to, references) for a reply."""
    from_pairs = getaddresses([str(orig["From"] or "")])
    from_name, from_addr = from_pairs[0] if from_pairs else ("", "")

    to = from_addr or str(orig["From"] or "")
    cc_list: List[str] = []
    if reply_to_all:
        skip = {my_email.lower(), (from_addr or "").lower()}
        for _name, addr in getaddresses([str(orig["To"] or ""), str(orig["Cc"] or "")]):
            al = addr.lower()
            if al and al not in skip and al not in {c.lower() for c in cc_list}:
                cc_list.append(addr)

    subject = str(orig["Subject"] or "")
    if not _REPLY_PREFIX_RE.match(subject):
        subject = "Re: " + subject

    try:
        dt = parsedate_to_datetime(str(orig["Date"])).astimezone()
        intro = f"Am {dt.strftime('%d.%m.%Y um %H:%M')} schrieb {from_name or from_addr}:"
    except (TypeError, ValueError):
        intro = f"{from_name or from_addr} schrieb:"
    quoted = "\n".join("> " + line for line in _plain_body(orig).splitlines())
    body = reply_body.rstrip() + "\n\n" + intro + "\n" + quoted

    msg_id = str(orig["Message-ID"] or "").strip()
    references = (str(orig["References"] or "").strip() + " " + msg_id).strip()
    return to, cc_list, subject, body, msg_id, references


def reply_via_smtp(
    account: str,
    sender_email: str,
    subject_keyword: str,
    reply_body: str,
    reply_to_all: bool = False,
    mode: str = "draft",
    mailbox: str = "INBOX",
    sender: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    attachments: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """Reply to the newest *mailbox* message matching *subject_keyword*, server-direct.

    Fetches the original over IMAP, reconstructs a real Reply / Reply-All
    (recipients, quoted original, threading headers), then saves a draft
    (mode="draft") or sends (mode="send") via SMTP. Returns (success, message).
    """
    if account not in ACCOUNT_CONFIG:
        return False, f"Reply-via-server not configured for account: {account}"
    password = _get_keychain_password(account, sender_email)
    if not password:
        return False, f"No Keychain password for {account} ({sender_email})."

    config = ACCOUNT_CONFIG[account]
    try:
        with imaplib.IMAP4_SSL(config["imap_host"], config["imap_port"]) as imap:
            imap.login(sender_email, password)
            imap.select(f'"{mailbox}"', readonly=True)
            num = _find_newest_message(imap, subject_keyword, sender)
            if not num:
                return False, f"No message found in {mailbox} matching subject: {subject_keyword}"
            typ, md = imap.fetch(num, "(BODY.PEEK[])")
            raw = _imap_part_bytes(md)
            if not raw:
                return False, f"Could not fetch original message for: {subject_keyword}"
            orig = email_mod.message_from_bytes(raw, policy=policy.default)
    except (imaplib.IMAP4.error, OSError) as e:
        return False, f"IMAP lookup failed: {e}"

    to, cc_list, subject, body, msg_id, references = _build_reply_parts(
        orig, sender_email, reply_body, reply_to_all
    )
    for addr in _split_addresses(cc):
        if addr.lower() not in {c.lower() for c in cc_list}:
            cc_list.append(addr)
    cc_str = ", ".join(cc_list) if cc_list else None

    if mode == "send":
        return send_via_smtp(
            account, sender_email, to, subject, body,
            html=None, cc=cc_str, bcc=bcc, attachments=attachments,
            in_reply_to=msg_id, references=references,
        )
    return save_draft_via_imap(
        account, sender_email, to, subject, body,
        html=None, cc=cc_str, bcc=bcc, attachments=attachments,
        in_reply_to=msg_id, references=references,
    )
