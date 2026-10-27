"""Per-account outgoing email.

Each account stores its own SMTP credentials so outreach is sent from the
sender's own mailbox rather than one shared address.

SMTP passwords are *encrypted*, not hashed: unlike a login password they have
to be replayed to the mail server, so the transformation must be reversible.
The key lives in SMTP_ENCRYPTION_KEY and is never stored in the database.
"""

import os
import re
import smtplib
import socket
import ssl
import sys
import unicodedata
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import psycopg2
from psycopg2.extras import RealDictCursor

import net_guard

SECURITY_MODES = ("starttls", "ssl")
DEFAULT_PORTS = {"starttls": 587, "ssl": 465}
ALLOWED_PORTS = frozenset({465, 587, 2525})
CONNECT_TIMEOUT = 15
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
PORT_MESSAGE = "Port must be 587 or 2525 (STARTTLS) or 465 (SSL/TLS)."
HOST_MESSAGE = "Enter your provider's public mail server name, for example smtp.gmail.com."
CONNECT_MESSAGES = {
    "not_found": "Your mail server could not be found. Check the server name.",
    "not_allowed": "That mail server address is not allowed. " + HOST_MESSAGE,
    "timeout": "Your mail server did not respond in time. Check the server name and port.",
    "tls": "A secure connection to your mail server failed. Use SSL/TLS with port 465, "
           "or STARTTLS with port 587 or 2525.",
    "rejected": "Your mail server refused the message. Check the sending address and recipient.",
    "unreachable": "Could not connect to your mail server. Check the server name, port and security setting.",
}


class EmailSettingsError(ValueError):
    """A user-correctable problem with the stored mail configuration."""


class EmailNotConfigured(EmailSettingsError):
    """The account has not set up a sending mailbox yet."""


class RecipientError(ValueError):
    """The recipient address is missing or unusable."""


# Address syntax characters that would let one "address" smuggle extra
# recipients or display names into the To header.
_RECIPIENT_SPECIALS = set('<>()[],;:"\\')
GMAIL_HOSTS = frozenset({"smtp.gmail.com", "smtp.googlemail.com"})
_GMAIL_APP_PASSWORD = re.compile(r"[a-z]{4}(?: ?[a-z]{4}){3}")


def validate_recipient(value: object) -> str:
    from auth import AuthError, normalize_email
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RecipientError("Enter the prospect's email address before sending.")
    try:
        address = normalize_email(value)
    except AuthError:
        raise RecipientError("Enter a valid recipient email address.") from None
    if _RECIPIENT_SPECIALS & set(address):
        raise RecipientError("Enter a valid recipient email address.")
    return address


def smtp_login_password(host: str, password: str) -> str:
    """Gmail shows app passwords as "abcd efgh ijkl mnop"; only that exact shape
    is unspaced. Every other password is used exactly as saved."""
    if (host or "").lower() in GMAIL_HOSTS:
        candidate = password.strip()
        if _GMAIL_APP_PASSWORD.fullmatch(candidate):
            return candidate.replace(" ", "")
    return password


def _from_header(display: str, sender: str) -> str:
    name = " ".join("".join(ch for ch in (display or "")
                            if not unicodedata.category(ch).startswith("C")).split())
    return formataddr((name or sender, sender), charset="utf-8")


def _fernet():
    from cryptography.fernet import Fernet
    key = (os.getenv("SMTP_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise EmailSettingsError(
            "SMTP_ENCRYPTION_KEY is not set, so mail passwords cannot be stored "
            "securely. Generate one with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode())
    except Exception as error:
        raise EmailSettingsError("SMTP_ENCRYPTION_KEY is not a valid Fernet key.") from error


def encrypt_secret(value: str) -> bytes:
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt_secret(value) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(bytes(value)).decode("utf-8")
    except InvalidToken as error:
        raise EmailSettingsError(
            "Your saved mail password could not be decrypted. This usually means "
            "SMTP_ENCRYPTION_KEY changed. Re-enter it in Settings."
        ) from error


def _text(value: object, label: str, maximum: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise EmailSettingsError(f"{label} must be text.")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise EmailSettingsError(f"{label} cannot contain control characters.")
    cleaned = " ".join(value.split())
    if not cleaned:
        if required:
            raise EmailSettingsError(f"{label} is required.")
        return ""
    if len(cleaned) > maximum:
        raise EmailSettingsError(f"{label} must be {maximum} characters or fewer.")
    return cleaned


def _smtp_host(value: object) -> str:
    host = _text(value, "Mail server", 255).lower().rstrip(".")
    if (" " in host or "/" in host or ":" in host or "@" in host or "." not in host
            or host == "localhost" or host.endswith(BLOCKED_HOST_SUFFIXES)):
        raise EmailSettingsError(HOST_MESSAGE)
    try:
        public = net_guard.is_public_ip(host)
    except ValueError:
        public = True  # a host name; resolved and re-checked at connect time
    if not public:
        raise EmailSettingsError(HOST_MESSAGE)
    return host


def validate_settings(data: object, has_existing_password: bool = False) -> dict:
    """Validates a settings payload.

    An empty password is allowed only when one is already stored, so the form
    can be re-saved without the browser ever receiving the current secret.
    """
    if not isinstance(data, dict):
        raise EmailSettingsError("Email settings expect a JSON object.")

    security = _text(data.get("smtp_security") or "starttls", "Security", 10).lower()
    if security not in SECURITY_MODES:
        raise EmailSettingsError("Security must be either 'starttls' or 'ssl'.")

    port = data.get("smtp_port", DEFAULT_PORTS[security])
    if isinstance(port, str) and port.strip().isdigit():
        port = int(port.strip())
    if not isinstance(port, int) or isinstance(port, bool) or port not in ALLOWED_PORTS:
        raise EmailSettingsError(PORT_MESSAGE)

    from auth import normalize_email, AuthError
    try:
        from_email = normalize_email(data.get("from_email") or data.get("smtp_username"))
    except AuthError as error:
        raise EmailSettingsError("Enter a valid sending email address.") from error

    password = data.get("smtp_password")
    if password in (None, ""):
        if not has_existing_password:
            raise EmailSettingsError("Enter your mail password or app password.")
        password = None
    elif not isinstance(password, str):
        raise EmailSettingsError("Mail password must be text.")
    elif len(password) > 512:
        raise EmailSettingsError("Mail password must be 512 characters or fewer.")

    return {
        "smtp_host": _smtp_host(data.get("smtp_host")),
        "smtp_port": port,
        "smtp_security": security,
        "smtp_username": _text(data.get("smtp_username"), "Mail username", 255),
        "from_name": _text(data.get("from_name"), "Sender name", 120, required=False) or None,
        "from_email": from_email,
        "smtp_password": password,
        "signature_title": _text(data.get("signature_title"), "Job title", 120, required=False) or None,
        "signature_company": _text(data.get("signature_company"), "Company", 120, required=False) or None,
        "signature_phone": _text(data.get("signature_phone"), "Phone", 60, required=False) or None,
        "signature_website": _text(data.get("signature_website"), "Website", 255, required=False) or None,
    }


def save_settings(dsn: str, user_id: int, data: object) -> dict:
    if user_id is None:
        raise EmailSettingsError("Email settings must belong to a signed-in account.")
    existing = _load_raw(dsn, user_id)
    values = validate_settings(data, has_existing_password=existing is not None)

    if values["smtp_password"] is None:
        secret = existing["smtp_password_encrypted"]
    else:
        secret = encrypt_secret(values["smtp_password"])

    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_email_settings (
                    user_id, smtp_host, smtp_port, smtp_security, smtp_username,
                    smtp_password_encrypted, from_name, from_email, verified_at, updated_at,
                    signature_title, signature_company, signature_phone, signature_website
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, NOW(), %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    smtp_host = EXCLUDED.smtp_host,
                    smtp_port = EXCLUDED.smtp_port,
                    smtp_security = EXCLUDED.smtp_security,
                    smtp_username = EXCLUDED.smtp_username,
                    smtp_password_encrypted = EXCLUDED.smtp_password_encrypted,
                    from_name = EXCLUDED.from_name,
                    from_email = EXCLUDED.from_email,
                    signature_title = EXCLUDED.signature_title,
                    signature_company = EXCLUDED.signature_company,
                    signature_phone = EXCLUDED.signature_phone,
                    signature_website = EXCLUDED.signature_website,
                    verified_at = NULL,
                    updated_at = NOW();
                """,
                (user_id, values["smtp_host"], values["smtp_port"], values["smtp_security"],
                 values["smtp_username"], psycopg2.Binary(secret),
                 values["from_name"], values["from_email"],
                 values["signature_title"], values["signature_company"],
                 values["signature_phone"], values["signature_website"]),
            )
        conn.commit()
    return public_settings(dsn, user_id)


def _load_raw(dsn: str, user_id: int) -> dict | None:
    if user_id is None:
        return None
    with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT user_id, smtp_host, smtp_port, smtp_security, smtp_username,
                          smtp_password_encrypted, from_name, from_email, verified_at,
                          signature_title, signature_company, signature_phone, signature_website
                   FROM user_email_settings WHERE user_id = %s;""",
                (user_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


def public_settings(dsn: str, user_id: int) -> dict:
    """Settings safe to send to the browser — never includes the password."""
    row = _load_raw(dsn, user_id)
    if row is None:
        return {"configured": False}
    return {
        "configured": True,
        "smtp_host": row["smtp_host"],
        "smtp_port": row["smtp_port"],
        "smtp_security": row["smtp_security"],
        "smtp_username": row["smtp_username"],
        "from_name": row["from_name"],
        "from_email": row["from_email"],
        "signature_title": row["signature_title"],
        "signature_company": row["signature_company"],
        "signature_phone": row["signature_phone"],
        "signature_website": row["signature_website"],
        "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
    }


def delete_settings(dsn: str, user_id: int) -> None:
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_email_settings WHERE user_id = %s;", (user_id,))
        conn.commit()


def _mark_verified(dsn: str, user_id: int) -> None:
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE user_email_settings SET verified_at = NOW() WHERE user_id = %s;",
                        (user_id,))
        conn.commit()


def sender_identity(dsn: str, user_id: int) -> dict:
    """The From name/address this account sends as."""
    row = _load_raw(dsn, user_id)
    if row is None:
        raise EmailNotConfigured(
            "Set up your sending email in Settings before sending outreach."
        )
    return {"name": row["from_name"] or row["from_email"], "email": row["from_email"]}


def _escape(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_signature(dsn: str, user_id: int) -> str:
    """Renders the sender's own signature.

    A shared, hardcoded signature would sign every account's outreach with one
    person's name and contact details, so every line here comes from the
    sending account's own settings and empty fields are simply omitted.
    """
    row = _load_raw(dsn, user_id)
    if row is None:
        return ""
    name = _escape(row["from_name"] or row["from_email"])
    rows = []
    if row["signature_title"]:
        rows.append('<p style="margin: 0; font-size: 15px; color: #0056b3; font-weight: 600;">'
                    f'{_escape(row["signature_title"])}</p>')
    if row["signature_company"]:
        rows.append('<p style="margin: 2px 0 12px 0; font-size: 13px; color: #666666; '
                    'font-weight: 700; text-transform: uppercase; letter-spacing: 1px;">'
                    f'{_escape(row["signature_company"])}</p>')
    contacts = []
    if row["signature_phone"]:
        contacts.append(f'<tr><td style="padding-right: 12px;">Phone</td>'
                        f'<td>{_escape(row["signature_phone"])}</td></tr>')
    contacts.append(
        '<tr><td style="padding-right: 12px;">Email</td><td>'
        f'<a href="mailto:{_escape(row["from_email"])}" style="text-decoration: none; '
        f'color: #333333; font-weight: 500;">{_escape(row["from_email"])}</a></td></tr>'
    )
    if row["signature_website"]:
        website = _escape(row["signature_website"])
        href = website if website.startswith(("http://", "https://")) else f"https://{website}"
        contacts.append(
            '<tr><td style="padding-right: 12px;">Web</td><td>'
            f'<a href="{href}" style="text-decoration: none; color: #333333; '
            f'font-weight: 500;">{website}</a></td></tr>'
        )
    return f"""
            <br><br>
            <div>
              <table cellpadding="0" cellspacing="0" border="0" style="font-family: 'Segoe UI', Arial, sans-serif; color: #333333; line-height: 1.6; font-size: 14px;">
                <tbody>
                  <tr>
                    <td style="border-left: 2px solid #0056b3; padding-left: 25px; vertical-align: top;">
                      <h3 style="margin: 0 0 2px 0; font-size: 20px; color: #111111; font-weight: 700;">{name}</h3>
                      {''.join(rows)}
                      <table cellpadding="0" cellspacing="0" border="0" style="font-size: 13px; color: #555555; line-height: 1.8;">
                        <tbody>{''.join(contacts)}</tbody>
                      </table>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            """


class _PinnedSMTP(smtplib.SMTP):
    """Connects only to public addresses on mail ports, pinned to the IP that was checked.

    TLS (STARTTLS or implicit) still verifies the certificate against the
    configured host name, because smtplib keeps it in ``self._host``.
    """

    def _get_socket(self, host, port, timeout):
        return net_guard.connect_public(host, port, allowed_ports=ALLOWED_PORTS,
                                        timeout=timeout or CONNECT_TIMEOUT)


class _PinnedSMTP_SSL(smtplib.SMTP_SSL, _PinnedSMTP):
    pass


def _connect(row: dict, password: str):
    host, port = row["smtp_host"], row["smtp_port"]
    if port not in ALLOWED_PORTS:
        raise EmailSettingsError(PORT_MESSAGE + " Update it in Settings.")
    if row["smtp_security"] == "ssl":
        server = _PinnedSMTP_SSL(host, port, timeout=CONNECT_TIMEOUT,
                                 context=ssl.create_default_context())
    else:
        server = _PinnedSMTP(host, port, timeout=CONNECT_TIMEOUT)
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
    server.login(row["smtp_username"], password)
    return server


def _connect_failure(error: BaseException) -> str:
    if isinstance(error, net_guard.UnresolvableHost):
        return "not_found"
    if isinstance(error, net_guard.UnsafeTarget):
        return "not_allowed"
    if isinstance(error, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                          smtplib.SMTPDataError)):
        return "rejected"
    if isinstance(error, (ssl.SSLError, ssl.CertificateError, smtplib.SMTPNotSupportedError)):
        return "tls"
    if isinstance(error, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(error, OSError) and isinstance(error.__cause__, (socket.timeout, TimeoutError)):
        return "timeout"
    return "unreachable"


def send_email(dsn: str, user_id: int, recipient: str, subject: str,
               html_body: str, mark_verified: bool = True) -> dict:
    """Sends one message using the owning account's own mailbox."""
    recipient = validate_recipient(recipient)
    row = _load_raw(dsn, user_id)
    if row is None:
        raise EmailNotConfigured(
            "Set up your sending email in Settings before sending outreach."
        )
    password = smtp_login_password(row["smtp_host"], decrypt_secret(row["smtp_password_encrypted"]))
    sender = row["from_email"]

    message = MIMEText(html_body, "html", "utf-8")
    message["Subject"] = Header(" ".join((subject or "").split()), "utf-8")
    message["From"] = _from_header(row["from_name"], sender)
    message["To"] = recipient
    message["Reply-To"] = sender

    try:
        with _connect(row, password) as server:
            server.sendmail(sender, [recipient], message.as_string())
    except smtplib.SMTPAuthenticationError as error:
        raise EmailSettingsError(
            "Your mail server rejected those credentials. If your provider uses "
            "two-factor authentication, use an app password."
        ) from error
    except (smtplib.SMTPException, OSError, ValueError) as error:
        if isinstance(error, EmailSettingsError):
            raise
        category = _connect_failure(error)
        print(f"[SMTP] user {user_id} via {row['smtp_host']}:{row['smtp_port']} failed "
              f"({category}): {type(error).__name__}: {error}", file=sys.stderr)
        raise EmailSettingsError(CONNECT_MESSAGES[category]) from error

    if mark_verified and row["verified_at"] is None:
        _mark_verified(dsn, user_id)
    return {"sender": sender, "recipient": recipient}


def send_test_email(dsn: str, user_id: int) -> dict:
    """Proves the stored credentials work by mailing the account itself."""
    row = _load_raw(dsn, user_id)
    if row is None:
        raise EmailNotConfigured("Save your mail settings before sending a test.")
    body = (
        "<html><body style=\"font-family: Arial, sans-serif; font-size: 15px;\">"
        "<p>Your AsuraTECH Lead Workspace sending address is working.</p>"
        "<p>Outreach you send will arrive from this mailbox.</p>"
        "</body></html>"
    )
    return send_email(dsn, user_id, row["from_email"],
                      "AsuraTECH Lead Workspace test message", body)
