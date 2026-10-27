"""Account billing and team routes. CSRF protection is app-wide (csrf.py)."""

import psycopg2
from flask import Blueprint, current_app, jsonify, render_template, request

import billing
from plans import PlanError
import teams

bp = Blueprint("teams", __name__)


def dsn():
    return current_app.config["BILLING_DSN"]


def user_id():
    from auth import current_user_id
    return current_user_id()


@bp.app_errorhandler(PlanError)
def handle_plan_error(error):
    current_app.logger.warning("Account operation rejected: %s", error.code)
    return jsonify(status="error", code=error.code, message=str(error)), error.status


@bp.route("/api/account-plan")
def account_plan():
    response = jsonify(status="success", account=billing.wallet_summary(dsn(), user_id()))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/discovery/quote", methods=["POST"])
def search_quote():
    import rate_limit
    current_app.config["RATE_LIMITER"].hit(rate_limit.QUOTE, user_id())
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("search"), dict):
        raise PlanError("Quote expects a search object and mode.", "invalid_search", 400)
    return jsonify(status="success", quote=billing.quote(dsn(), user_id(), data.get("mode"), data["search"]))


@bp.route("/user-management")
def user_management():
    team = teams.team_summary(dsn(), user_id())
    return render_template("user_management.html", team=team)


@bp.route("/api/team")
def get_team():
    return jsonify(status="success", team=teams.team_summary(dsn(), user_id()))


@bp.route("/api/team/invitations", methods=["POST"])
def create_invitation():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise PlanError("Enter a member email address.", "invalid_invite", 400)
    from auth import AuthError
    try:
        invite = teams.invite_member(dsn(), user_id(), data.get("email"))
    except AuthError as error:
        raise PlanError(str(error), "invalid_email", 400) from error
    return jsonify(status="success", invitation_id=invite), 201


@bp.route("/api/team/invitations/<uuid:invite_id>", methods=["DELETE"])
def revoke_invitation(invite_id):
    teams.revoke_invitation(dsn(), user_id(), str(invite_id))
    return jsonify(status="success")


@bp.route("/api/team/members/<int:member_id>", methods=["DELETE"])
def remove_member(member_id):
    teams.remove_member(dsn(), user_id(), member_id)
    return jsonify(status="success")


@bp.route("/team-invitation")
def invitation_landing():
    return render_template("team_invitation.html")


@bp.route("/api/team/accept", methods=["POST"])
def accept_invitation():
    data = request.get_json(silent=True)
    teams.accept_invitation(dsn(), user_id(), data.get("token") if isinstance(data, dict) else None)
    return jsonify(status="success")


@bp.errorhandler(psycopg2.Error)
def database_failure(error):
    current_app.logger.exception("Database operation failed")
    return jsonify(status="error", code="database_unavailable",
                   message="The database operation failed. Reload to check the outcome before retrying."), 503


def start_paid(dsn, user, mode, payload, runs, tasks, log, bind_log):
    reservation, repeated = billing.reserve(dsn, user, mode, payload, request.headers.get("Idempotency-Key"))
    run_id = str(reservation["id"])
    if repeated:
        return run_id

    def started():
        bind_log(user)
        billing.bind_run(run_id)
        billing.save_progress(dsn, run_id, {
            "id": run_id, "total": len(tasks), "completed": 0, "saved": 0,
            "successful_tasks": 0, "errors": [], "warnings": [], "logs": [],
        })

    try:
        runs.start(
            tasks, log, owner=user, run_id=run_id, on_thread_start=started,
            on_progress=lambda progress: billing.save_progress(dsn, run_id, progress),
            on_finish=lambda progress: billing.settle(dsn, run_id, progress),
        )
    except Exception:
        # A failed launch releases credits, but a worker that already wrote leads
        # is reconciled from its durable inserted count, never blindly refunded.
        billing.settle(dsn, run_id, interrupted=True, launch_failed=True)
        raise
    return run_id
