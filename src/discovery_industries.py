import re
import unicodedata
from collections.abc import Callable, Mapping


INDUSTRY_ALIASES = {
    "Roofing Contractor": ("roofing", "roofer"),
    "HVAC Contractor": ("hvac", "air conditioning", "heating", "cooling"),
    "Plumbing Service": ("plumbing", "plumber"),
    "Law Firm": ("law firm", "lawyer", "attorney", "legal services"),
    "Accounting Firm": ("accounting", "accountant", "bookkeeping"),
    "Real Estate Agency": ("real estate", "realtor"),
    "Medical Clinic": ("medical clinic", "medical center", "doctor", "physician", "family practice"),
    "Dental Clinic": ("dental", "dentist", "dentistry"),
    "Chiropractor": ("chiropractor", "chiropractic"),
    "Boutique Hotel": ("hotel", "lodging", "inn"),
    "Fitness Center": ("fitness", "gym", "personal trainer"),
    "Restaurant": ("restaurant", "diner"),
    "Cafe": ("cafe", "coffee shop", "coffeehouse"),
    "Auto Repair Shop": ("auto repair", "car repair", "mechanic", "automotive repair"),
    "Beauty Salon": ("beauty salon", "hair salon", "beautician"),
    "Barber Shop": ("barber", "barbershop"),
    "Landscaping Service": ("landscaping", "landscaper", "lawn care"),
    "Photography Studio": ("photography", "photographer"),
    "Veterinary Clinic": ("veterinary", "veterinarian", "animal hospital"),
    "Electrician": ("electrician", "electrical contractor"),
    "Cleaning Service": ("cleaning", "cleaner", "janitorial"),
    "Event Planner": ("event planning", "event planner", "wedding planner"),
    "Insurance Agency": ("insurance",),
    "Travel Agency": ("travel agency", "travel agent", "tour operator"),
    "Pet Grooming": ("pet grooming", "pet groomer", "dog groomer"),
    "Tutoring Center": ("tutoring", "tutor"),
    "Construction Company": ("construction", "builder", "general contractor"),
    "Florist": ("florist", "flower shop"),
    "Pharmacy": ("pharmacy", "drugstore", "drug store"),
    "Logistics Company": ("logistics", "freight", "courier", "shipping company"),
    "Car Dealership": ("car dealer", "auto dealer", "car dealership", "used car dealer"),
    "Spa": ("spa", "massage"),
}


def normalize_industry_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", re.sub(r"<[^>]*>", " ", value)).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    return " ".join(re.findall(r"\w+", text))


def has_industry_evidence(industry: str, evidence: str) -> bool:
    normalized = f" {normalize_industry_text(evidence)} "
    aliases = INDUSTRY_ALIASES.get(industry, (industry,))
    for alias in aliases:
        term = normalize_industry_text(alias)
        if not term:
            continue
        variants = {term, term + "s"}
        if term.endswith("y"):
            variants.add(term[:-1] + "ies")
        if any(f" {variant} " in normalized for variant in variants):
            return True
    return False


def matching_industry_category(
    business: Mapping[str, object],
    industry: str,
    source: str,
    log: Callable[[str], None],
) -> str | None:
    if source in {"facebook", "instagram"}:
        evidence = f"{business.get('title') or ''} {business.get('snippet') or ''}"
        if has_industry_evidence(industry, evidence):
            return industry
    else:
        categories = []
        for key in ("type", "category"):
            value = business.get(key)
            if isinstance(value, str):
                categories.append(value)
        for key in ("types", "categories"):
            values = business.get(key)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, str):
                        categories.append(value)
                    elif isinstance(value, Mapping) and isinstance(value.get("title"), str):
                        categories.append(value["title"])
        for category in categories:
            if has_industry_evidence(industry, category):
                return category
    log(f"[INDUSTRY] Skipped '{business.get('title')}': no matching category/text evidence for '{industry}'.")
    return None
