"""Seat-limited teams and hashed, single-use email invitations."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import hashlib
import secrets
import smtplib
import ssl
import uuid

from auth import email_key
from billing import ensure_membership, is_active, lock_wallet, refresh_wallet, transaction
from plans import PLANS, PlanError

INVITES_PER_DAY = 20


def owner_wallet(cur, user_id):
    wallet = lock_wallet(cur, user_id)
    refresh_wallet(cur, wallet)
    if wallet["owner_id"] != user_id or not is_active(wallet) or PLANS[wallet["plan_id"]].seats <= 1:
        raise PlanError("User Management is available only to active Small Team and Enterprise owners.",
                        "team_owner_required")
    cur.execute("""UPDATE team_invites SET status='expired'
                   WHERE wallet_id=%s AND status='pending' AND expires_at <= NOW()""", (wallet["id"],))
    return wallet


def seat_usage(cur, wallet_id):
    cur.execute("""SELECT
        (SELECT count(*) FROM team_members WHERE wallet_id=%s) AS members,
        (SELECT count(*) FROM team_invites WHERE wallet_id=%s
            AND status='pending' AND expires_at>NOW()) AS pending""", (wallet_id, wallet_id))
    return dict(cur.fetchone())


def team_summary(dsn, user_id):
    with transaction(dsn) as cur:
        wallet = owner_wallet(cur, user_id)
        usage = seat_usage(cur, wallet["id"])
        cur.execute("""SELECT u.id,u.email,u.display_name, m.joined_at,
                       CASE WHEN u.id=%s THEN 'owner' ELSE 'member' END AS team_role
                       FROM team_members m JOIN users u ON u.id=m.user_id
                       WHERE m.wallet_id=%s ORDER BY (u.id=%s) DESC,m.joined_at""",
                    (user_id, wallet["id"], user_id))
        members = [dict(row) for row in cur.fetchall()]
        cur.execute("""SELECT id,email_key AS email,expires_at,created_at FROM team_invites
                       WHERE wallet_id=%s AND status='pending' ORDER BY created_at""", (wallet["id"],))
        invitations = [dict(row) for row in cur.fetchall()]
        seats = PLANS[wallet["plan_id"]].seats
        return {"plan": PLANS[wallet["plan_id"]].name, "seats": seats,
                "available": seats - usage["members"] - usage["pending"],
                "balance": str(wallet["balance"]), "members": members, "invitations": invitations}


def mail_configuration():
    import system_mail
    try:
        return system_mail.configuration()
    except system_mail.SystemMailUnavailable as error:
        raise PlanError(f"Invitations cannot be sent. {error}", "system_mail_unavailable", 503) from error


def send_invitation(email, token):
    password, sender, base = mail_configuration()
    message = EmailMessage()
    message["From"], message["To"] = sender, email
    message["Subject"] = "Join your AsuraTECH team"
    message.set_content(
        "You have been invited to an AsuraTECH team.\n\n"
        f"Sign in or create an account with this email, then accept:\n{base}/team-invitation#{token}\n\n"
        "This invitation expires in 7 days. Your leads remain private; team members share "
        "the owner's search credit balance. Ignore this email if you did not expect it."
    )
    with smtplib.SMTP_SSL("smtp.resend.com", 465, timeout=15, context=ssl.create_default_context()) as smtp:
        smtp.login("resend", password)
        if smtp.send_message(message):
            raise smtplib.SMTPException("Invitation recipient rejected")


def invite_member(dsn, user_id, address):
    target = email_key(address)
    mail_configuration()
    token = secrets.token_urlsafe(32)
    invite_id = str(uuid.uuid4())
    with transaction(dsn) as cur:
        wallet = owner_wallet(cur, user_id)
        cur.execute("SELECT email_key FROM users WHERE id=%s", (user_id,))
        if target == cur.fetchone()["email_key"]:
            raise PlanError("The owner already occupies a seat.", "already_member", 409)
        cur.execute("""SELECT 1 FROM team_members m JOIN users u ON u.id=m.user_id
                       WHERE m.wallet_id=%s AND u.email_key=%s""", (wallet["id"], target))
        if cur.fetchone():
            raise PlanError("That account is already a team member.", "already_member", 409)
        cur.execute("""SELECT 1 FROM team_invites WHERE wallet_id=%s AND email_key=%s
                       AND status='pending'""", (wallet["id"], target))
        if cur.fetchone():
            raise PlanError("An invitation is already pending for that email.", "invite_pending", 409)
        usage = seat_usage(cur, wallet["id"])
        if usage["members"] + usage["pending"] >= PLANS[wallet["plan_id"]].seats:
            raise PlanError("All team seats are occupied or reserved.", "seat_limit", 409)
        # Revoked and failed invitations still count, so revoke/re-invite loops
        # cannot be used to send unlimited system email.
        cur.execute("""SELECT count(*) AS sent FROM team_invites
                       WHERE wallet_id=%s AND created_at > NOW() - INTERVAL '1 day'""", (wallet["id"],))
        if cur.fetchone()["sent"] >= INVITES_PER_DAY:
            raise PlanError(f"Your team can send up to {INVITES_PER_DAY} invitations per day. Try again tomorrow.",
                            "rate_limited", 429)
        cur.execute("""INSERT INTO team_invites(id,wallet_id,email_key,token_hash,status,expires_at)
                       VALUES (%s,%s,%s,%s,'pending',%s)""",
                    (invite_id, wallet["id"], target, hashlib.sha256(token.encode()).hexdigest(),
                     datetime.now(timezone.utc) + timedelta(days=7)))
    try:
        send_invitation(target, token)
    except (smtplib.SMTPException, OSError) as error:
        with transaction(dsn) as cur:
            cur.execute("UPDATE team_invites SET status='revoked' WHERE id=%s AND status='pending'", (invite_id,))
        raise PlanError("Invitation delivery failed. Its seat was released; retry after checking system mail.",
                        "invite_delivery_failed", 502) from error
    return invite_id


def revoke_invitation(dsn, user_id, invite_id):
    with transaction(dsn) as cur:
        wallet = owner_wallet(cur, user_id)
        cur.execute("""UPDATE team_invites SET status='revoked'
                       WHERE id=%s AND wallet_id=%s AND status='pending' RETURNING id""",
                    (invite_id, wallet["id"]))
        if not cur.fetchone():
            raise PlanError("Pending invitation not found.", "not_found", 404)


def remove_member(dsn, user_id, member_id):
    if member_id == user_id:
        raise PlanError("The owner cannot be removed.", "owner_protected", 409)
    # Match the user -> wallet lock ordering of reservation and acceptance.
    with transaction(dsn) as cur:
        cur.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (member_id,))
        wallet = owner_wallet(cur, user_id)
        cur.execute("SELECT 1 FROM team_members WHERE user_id=%s AND wallet_id=%s",
                    (member_id, wallet["id"]))
        if not cur.fetchone():
            raise PlanError("Team member not found.", "not_found", 404)
        cur.execute("SELECT 1 FROM paid_discovery_runs WHERE user_id=%s AND settled_at IS NULL", (member_id,))
        if cur.fetchone():
            raise PlanError("Wait for this member's running search to settle.", "discovery_busy", 409)
        cur.execute("DELETE FROM team_members WHERE wallet_id=%s AND user_id=%s RETURNING user_id",
                    (wallet["id"], member_id))
        if not cur.fetchone():
            raise PlanError("Team member not found.", "not_found", 404)


def accept_invitation(dsn, user_id, token):
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise PlanError("Invitation is invalid or expired.", "invalid_invite", 400)
    with transaction(dsn) as cur:
        own_wallet_id = ensure_membership(cur, user_id)
        cur.execute("SELECT * FROM team_invites WHERE token_hash=%s", (hashlib.sha256(token.encode()).hexdigest(),))
        invitation = cur.fetchone()
        if not invitation:
            raise PlanError("Invitation is invalid or expired.", "invalid_invite", 400)
        cur.execute("SELECT * FROM team_wallets WHERE id IN (%s,%s) ORDER BY id FOR UPDATE",
                    (own_wallet_id, invitation["wallet_id"]))
        wallets = {row["id"]: dict(row) for row in cur.fetchall()}
        wallet = wallets[invitation["wallet_id"]]
        cur.execute("SELECT * FROM team_invites WHERE id=%s FOR UPDATE", (invitation["id"],))
        invitation = cur.fetchone()
        cur.execute("SELECT email_key FROM users WHERE id=%s AND is_active", (user_id,))
        account = cur.fetchone()
        if not account or invitation["email_key"] != account["email_key"]:
            raise PlanError("Sign in with the email address that received this invitation.", "invite_email_mismatch")
        if invitation["status"] != "pending" or invitation["expires_at"] <= datetime.now(timezone.utc):
            raise PlanError("Invitation is invalid or expired.", "invalid_invite", 400)
        if not is_active(wallet) or PLANS[wallet["plan_id"]].seats <= 1:
            raise PlanError("This team's plan is no longer eligible.", "team_inactive")
        own = wallets[own_wallet_id]
        from payments import assert_no_open_order
        assert_no_open_order(cur, own_wallet_id)
        if (own_wallet_id == wallet["id"] or own["owner_id"] != user_id
                or not (own["plan_id"] == "trial"
                        or (own["plan_id"] == "unassigned" and own["balance"] == 0))):
            raise PlanError("Leave your current team or resolve your own subscription before joining.",
                            "membership_conflict", 409)
        cur.execute("SELECT 1 FROM paid_discovery_runs WHERE user_id=%s AND settled_at IS NULL", (user_id,))
        if cur.fetchone():
            raise PlanError("Wait for your running discovery to settle before joining.", "discovery_busy", 409)
        usage = seat_usage(cur, wallet["id"])
        if usage["members"] + usage["pending"] > PLANS[wallet["plan_id"]].seats:
            raise PlanError("This team has no available seat.", "seat_limit", 409)
        if own["plan_id"] == "trial":
            from billing import ledger
            ledger(cur, own, user_id, f"trial-joined:{invitation['id']}", -own["balance"],
                   "Personal trial closed on joining team; no credits transferred")
            cur.execute("UPDATE team_wallets SET plan_id='unassigned',ends_at=NOW(),next_grant_at=NULL WHERE id=%s",
                        (own_wallet_id,))
        cur.execute("UPDATE team_members SET wallet_id=%s,joined_at=NOW() WHERE user_id=%s",
                    (wallet["id"], user_id))
        cur.execute("UPDATE team_invites SET status='accepted' WHERE id=%s", (invitation["id"],))
        # The invitation token was delivered to this address, which proves ownership.
        cur.execute("UPDATE users SET email_verified_at=COALESCE(email_verified_at,NOW()) WHERE id=%s",
                    (user_id,))
