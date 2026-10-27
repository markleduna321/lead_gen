"""Server-owned plan capabilities and credit prices."""

from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Plan:
    name: str
    credits: int
    seats: int
    industries: int
    daily: int
    sources: tuple[str, ...]
    hiring_pages: int = 0
    custom_services: bool = False
    rollover: int = 0
    ai_drafts: bool = True


PLANS = {
    "unassigned": Plan("No active plan", 0, 1, 0, 0, (), ai_drafts=False),
    "trial": Plan("Free Trial", 30, 1, 1, 10, ("google",), ai_drafts=False),
    "solo": Plan("Solo", 750, 1, 3, 50, ("google", "yelp"), rollover=350),
    "small_team": Plan("Small Team", 2500, 4, 5, 200,
                       ("google", "yelp", "facebook", "instagram", "linkedin"), 3, False, 1250),
    "enterprise": Plan("Enterprise", 7500, 15, 10, 1000,
                       ("google", "yelp", "facebook", "instagram", "linkedin"), 5, True, 3500),
}
LOCAL_PRICE = Decimal("10")
HIRING_PRICE = Decimal("15")
PITCH_PRICE = Decimal("1")
ZERO_RESULT_REFUND = Decimal("0.70")
MAX_INDUSTRY_LENGTH = 60


class PlanError(ValueError):
    def __init__(self, message, code="plan_required", status=403):
        self.code, self.status = code, status
        super().__init__(message)


def capabilities(plan_id, active):
    result = asdict(PLANS[plan_id if active else "unassigned"])
    result.update(id=plan_id, active=active, local_price=str(LOCAL_PRICE),
                  hiring_price=str(HIRING_PRICE), pitch_price=str(PITCH_PRICE),
                  zero_result_refund="70%")
    return result


def quote_search(plan_id, active, mode, payload, services=None):
    if not active:
        raise PlanError("Ask the account owner to activate a plan before searching.")
    plan = PLANS[plan_id]
    if mode == "local":
        categories = payload.get("categories")
        sources = payload.get("sources", ["google"])
        if (not isinstance(categories, list) or not categories
                or any(not isinstance(item, str) or not item.strip() for item in categories)
                or not isinstance(sources, list) or not sources
                or any(not isinstance(item, str) for item in sources)):
            raise PlanError("Choose valid industries and sources.", "invalid_search", 400)
        industries = list(dict.fromkeys(item.strip() for item in categories))
        if any(len(item) > MAX_INDUSTRY_LENGTH for item in industries):
            raise PlanError(f"Industry names can be at most {MAX_INDUSTRY_LENGTH} characters.",
                            "invalid_search", 400)
        if len(industries) > plan.industries:
            raise PlanError(f"{plan.name} allows up to {plan.industries} industries per submission.",
                            "industry_limit")
        if set(sources) - set(plan.sources):
            raise PlanError("Upgrade to Small Team to unlock the selected discovery sources.",
                            "source_locked")
        return LOCAL_PRICE * len(industries)
    if mode != "hiring":
        raise PlanError("Unknown discovery mode.", "invalid_search", 400)
    if not plan.hiring_pages:
        raise PlanError("Upgrade to Small Team to unlock Hiring Companies discovery.", "hiring_locked")
    from discovery_jobs import validate_hiring_pages
    try:
        pages = validate_hiring_pages(payload.get("pages_per_role", 1))
    except ValueError as error:
        raise PlanError(str(error), "invalid_search", 400) from error
    if pages > plan.hiring_pages:
        raise PlanError(f"{plan.name} allows up to {plan.hiring_pages} pages per role.", "depth_locked")
    selected = payload.get("service_types")
    if not isinstance(selected, list) or not selected or any(not isinstance(s, str) for s in selected):
        raise PlanError("Select valid hiring services.", "invalid_search", 400)
    selected = list(dict.fromkeys(selected))
    if any(s.startswith("custom_") for s in selected) and not plan.custom_services:
        raise PlanError("Upgrade to Enterprise to search custom hiring services.", "custom_service_locked")
    if services is None or any(s not in services for s in selected):
        raise PlanError("A selected hiring service is unavailable. Reload the service list.",
                        "invalid_search", 400)
    return HIRING_PRICE * pages * sum(len(services[s].roles) for s in selected)
