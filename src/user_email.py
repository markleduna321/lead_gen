"""Per-account outgoing email.

Each account stores its own SMTP credentials so outreach is sent from the
sender's own mailbox rather than one shared address.

SMTP passwords are *encrypted*, not hashed: unlike a login password they have
to be replayed to the mail server, so the transformation must be reversible.
The key lives in SMTP_ENCRYPTION_KEY and is never stored in the database.
"""

import os
import smtplib
import ssl
import unicodedata
from email.header import Header
from email.mime.text import MIMEText

import psycopg2
from psycopg2.extras import RealDictCursor

SECURITY_MODES = ("starttls", "ssl")
DEFAULT_PORTS = {"starttls": 587, "ssl": 465}
CONNECT_TIMEOUT = 15


class EmailSettingsError(ValueError):
    """A user-correctable problem with the stored mail configuration."""


class EmailNotConfigured(EmailSettingsError):
    """The account has not set up a sending mailbox yet."""


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
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise EmailSettingsError("Port must be a whole number between 1 and 65535.")

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
        "smtp_host": _text(data.get("smtp_host"), "Mail server", 255),
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


def _connect(row: dict, password: str):
    host, port = row["smtp_host"], row["smtp_port"]
    if row["smtp_security"] == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=CONNECT_TIMEOUT,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=CONNECT_TIMEOUT)
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
    server.login(row["smtp_username"], password)
    return server


def send_email(dsn: str, user_id: int, recipient: str, subject: str,
               html_body: str, mark_verified: bool = True) -> dict:
    """Sends one message using the owning account's own mailbox."""
    row = _load_raw(dsn, user_id)
    if row is None:
        raise EmailNotConfigured(
            "Set up your sending email in Settings before sending outreach."
        )
    # App passwords are commonly copied with spacing or dashes.
    password = decrypt_secret(row["smtp_password_encrypted"]).replace(" ", "").replace("-", "").strip()
    sender = row["from_email"]
    display = row["from_name"] or sender

    message = MIMEText(html_body, "html", "utf-8")
    message["Subject"] = Header(subject, "utf-8")
    message["From"] = f'"{display}" <{sender}>'
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
    except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
        raise EmailSettingsError(f"Your mail server could not be reached: {error}") from error

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
