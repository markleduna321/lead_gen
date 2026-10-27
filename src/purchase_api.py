"""Owner-only billing pages and PayMongo's signed, public notification endpoint."""

import psycopg2
from flask import Blueprint, current_app, jsonify, render_template, request

import payments
import billing
from plans import PLANS
from team_api import user_id

bp = Blueprint("purchases", __name__)


def dsn():
    return current_app.config["BILLING_DSN"]


@bp.route("/billing")
def billing_page():
    state = payments.overview(dsn(), user_id())
    prices_json = {plan: {str(key): value for key, value in price.items()}
                   for plan, price in payments.PRICES.items()}
    return render_template("billing.html", billing=state, prices=payments.PRICES,
                           prices_json=prices_json, plans=PLANS)


@bp.route("/api/billing")
def billing_summary():
    response = jsonify(status="success", billing=payments.overview(dsn(), user_id()))
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.route("/api/billing/checkout", methods=["POST"])
def checkout():
    order = payments.create_checkout(dsn(), user_id(), request.get_json(silent=True),
                                     request.headers.get("Idempotency-Key"))
    return jsonify(status="success", order=order), 201


@bp.route("/api/billing/trial", methods=["POST"])
def start_trial():
    billing.claim_trial(dsn(), user_id())
    return jsonify(status="success")


@bp.route("/api/billing/orders/<uuid:order_id>/recheck", methods=["POST"])
def recheck(order_id):
    return jsonify(status="success", order=payments.recheck(dsn(), user_id(), order_id))


@bp.route("/api/billing/orders/<uuid:order_id>/cancel", methods=["POST"])
def cancel(order_id):
    return jsonify(status="success", order=payments.recheck(dsn(), user_id(), order_id, cancel=True))


@bp.route("/api/paymongo/webhook", methods=["POST"])
def paymongo_webhook():
    payments.receive_webhook(dsn(), request.get_data(), request.headers.get("Paymongo-Signature", ""))
    return jsonify(status="success")


@bp.errorhandler(psycopg2.Error)
def database_failure(error):
    current_app.logger.exception("Purchase database operation failed")
    return jsonify(status="error", code="database_unavailable",
                   message="The payment record could not be updated. Check Billing before retrying."), 503
