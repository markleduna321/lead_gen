import math
from collections.abc import Callable, Mapping


PLACE_ID_INDEX = 4


def attach_owner(rows: list[tuple], user_id: int) -> list[tuple]:
    """Appends the owning account to each insert row.

    The social and Yelp collectors build positional tuples, so ownership is
    appended last to match the trailing user_id column in their INSERTs.
    """
    if user_id is None:
        raise ValueError("Prospects must belong to a signed-in account.")
    return [tuple(row) + (user_id,) for row in rows]


def deduplicate_prospect_rows(
    rows: list[tuple],
    log: Callable[[str], None] | None = None,
) -> list[tuple]:
    """Collapse repeated google_place_id values inside a single insert batch.

    ``ON CONFLICT ... DO UPDATE`` aborts if one statement touches the same
    conflicting row twice, so each place ID may appear only once per batch.
    NULL/blank place IDs never conflict in Postgres and are always kept.
    """
    unique: list[tuple] = []
    seen: set = set()
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= PLACE_ID_INDEX:
            unique.append(row)
            continue
        place_id = row[PLACE_ID_INDEX]
        if place_id is None or place_id == "":
            unique.append(row)
            continue
        if place_id in seen:
            if log:
                log(f"[DEDUP] Merged repeated business in this batch: {row[0]}")
            continue
        seen.add(place_id)
        unique.append(row)
    return unique


def within_search_radius(
    business: Mapping[str, object],
    lat: float,
    lng: float,
    radius_km: float,
    log: Callable[[str], None],
) -> bool:
    coordinates = business.get("gps_coordinates")
    try:
        if not isinstance(coordinates, Mapping):
            raise ValueError("GPS coordinates unavailable")
        raw_lat, raw_lng = coordinates.get("latitude"), coordinates.get("longitude")
        if (
            isinstance(raw_lat, bool) or not isinstance(raw_lat, (int, float, str))
            or isinstance(raw_lng, bool) or not isinstance(raw_lng, (int, float, str))
        ):
            raise ValueError("invalid GPS coordinates")
        result_lat, result_lng = float(raw_lat), float(raw_lng)
        if not math.isfinite(result_lat) or not math.isfinite(result_lng) or abs(result_lat) > 90 or abs(result_lng) > 180:
            raise ValueError("GPS coordinates outside valid range")
    except (ValueError, TypeError, OverflowError) as error:
        log(f"[RADIUS] Skipped '{business.get('title')}': distance cannot be verified ({error}).")
        return False
    origin_lat, target_lat = math.radians(lat), math.radians(result_lat)
    delta_lat = target_lat - origin_lat
    delta_lng = math.radians(result_lng - lng)
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(origin_lat) * math.cos(target_lat) * math.sin(delta_lng / 2) ** 2
    distance = 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))
    if distance > radius_km + 1e-9:
        log(f"[RADIUS] Skipped '{business.get('title')}': {distance:.3f} km exceeds {radius_km} km.")
        return False
    return True


def filter_business_metrics(
    business: Mapping[str, object],
    rating_min: float,
    max_reviews: int | None,
    log: Callable[[str], None],
) -> tuple[float | None, int | None] | None:
    name = business.get("title") or "Unnamed business"
    rating = None
    reviews = None
    try:
        raw_rating = business.get("rating")
        raw_reviews = business.get("reviews")
        if raw_rating is not None:
            if isinstance(raw_rating, bool) or not isinstance(raw_rating, (str, int, float)):
                raise ValueError("rating must be numeric")
            rating = float(raw_rating)
            if not math.isfinite(rating) or not 0 <= rating <= 5:
                raise ValueError("rating must be between 0 and 5")
        if raw_reviews is not None:
            if isinstance(raw_reviews, bool) or not isinstance(raw_reviews, (str, int, float)):
                raise ValueError("review count must be numeric")
            count = float(raw_reviews)
            if not math.isfinite(count) or count < 0 or not count.is_integer():
                raise ValueError("review count must be a nonnegative integer")
            reviews = int(count)
    except (ValueError, OverflowError) as error:
        log(f"[FILTER] Skipped '{name}': invalid rating/review data ({error}).")
        return None

    if rating_min > 0 and (rating is None or rating < rating_min):
        log(f"[FILTER] Skipped '{name}': minimum rating {rating_min} not met or unavailable.")
        return None
    if max_reviews is not None and (reviews is None or reviews > max_reviews):
        log(f"[FILTER] Skipped '{name}': maximum reviews {max_reviews} exceeded or unavailable.")
        return None
    return rating, reviews
