#!/usr/bin/env python3
"""find_leads.py — Find local businesses WITHOUT a website using the Google
Places API (new Places API v1).

Two-stage flow:
  Stage 1  Text Search (places:searchText) — cheap, returns place IDs + basics.
  Stage 2  Place Details (places/{id})      — billed per call, returns websiteUri.

Only businesses whose websiteUri is missing/empty are kept, so you can
cold-call shops that have no web presence.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    load_dotenv = None


# --- Constants ---------------------------------------------------------------

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Field masks. We request ONLY what we need to keep the bill (and SKU tier)
# as low as possible.
SEARCH_FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.rating",
        "places.userRatingCount",
        "nextPageToken",
    ]
)
DETAILS_FIELD_MASK = ",".join(
    [
        "id",
        "displayName",
        "websiteUri",
        "nationalPhoneNumber",
        "formattedAddress",
        "rating",
        "userRatingCount",
    ]
)

# Rough cost of a single Place Details (Pro/Enterprise SKU) call in USD. This is
# only used for the cost-guard estimate; consult current Google pricing for the
# exact figure. Text Search is billed separately and far cheaper.
DETAILS_COST_PER_CALL_USD = 0.017

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
# Google requires a short delay before a nextPageToken becomes valid.
NEXT_PAGE_DELAY_SECONDS = 2.0


# --- HTTP helpers ------------------------------------------------------------


class PlacesAPIError(Exception):
    """Raised for non-retryable Places API failures."""


def _request_with_retries(method, url, *, headers, json=None, params=None):
    """Issue an HTTP request, retrying on 429 / 5xx with exponential backoff."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(
                method, url, headers=headers, json=json, params=params, timeout=30
            )
        except requests.RequestException as exc:  # network-level failure
            last_exc = exc
            wait = BACKOFF_BASE_SECONDS * (2 ** attempt)
            print(
                f"  ! network error ({exc}); retrying in {wait:.0f}s "
                f"[{attempt + 1}/{MAX_RETRIES}]",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp.json()

        # Fail fast on auth problems — retrying will not help.
        if resp.status_code in (401, 403):
            raise PlacesAPIError(
                f"Authentication/authorization failed (HTTP {resp.status_code}). "
                f"Check that GOOGLE_PLACES_API_KEY is valid and the Places API "
                f"(New) is enabled for the project.\nResponse: {resp.text}"
            )

        if resp.status_code == 429 or resp.status_code >= 500:
            wait = BACKOFF_BASE_SECONDS * (2 ** attempt)
            print(
                f"  ! HTTP {resp.status_code}; retrying in {wait:.0f}s "
                f"[{attempt + 1}/{MAX_RETRIES}]",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue

        # Other 4xx — not retryable.
        raise PlacesAPIError(
            f"Places API request failed (HTTP {resp.status_code}): {resp.text}"
        )

    if last_exc is not None:
        raise PlacesAPIError(f"Request failed after {MAX_RETRIES} retries: {last_exc}")
    raise PlacesAPIError(f"Request failed after {MAX_RETRIES} retries: {url}")


# --- Stage 1: Text Search ----------------------------------------------------


def text_search(api_key, query, max_pages):
    """Return a list of place dicts from Text Search, following pagination."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": SEARCH_FIELD_MASK,
    }
    places = []
    page_token = None
    pages_fetched = 0

    while pages_fetched < max_pages:
        body = {"textQuery": query}
        if page_token:
            body["pageToken"] = page_token

        data = _request_with_retries("POST", TEXT_SEARCH_URL, headers=headers, json=body)
        pages_fetched += 1

        page_places = data.get("places", [])
        places.extend(page_places)
        print(
            f"  Stage 1: page {pages_fetched} -> {len(page_places)} places "
            f"(total {len(places)})"
        )

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        # nextPageToken needs a moment to become valid.
        time.sleep(NEXT_PAGE_DELAY_SECONDS)

    return places


# --- Stage 2: Place Details --------------------------------------------------


def place_details(api_key, place_id):
    """Fetch a single place's details. This is a BILLED call."""
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": DETAILS_FIELD_MASK,
    }
    url = DETAILS_URL.format(place_id=place_id)
    return _request_with_retries("GET", url, headers=headers)


# --- Helpers -----------------------------------------------------------------


def prefilter_places(places, min_reviews, min_rating):
    """Drop places below the review/rating thresholds before Stage 2 so we
    don't spend Details calls on dead businesses."""
    kept = []
    for p in places:
        rating = p.get("rating", 0) or 0
        reviews = p.get("userRatingCount", 0) or 0
        if reviews < min_reviews:
            continue
        if rating < min_rating:
            continue
        kept.append(p)
    return kept


def confirm_spend(num_calls, assume_yes):
    """Print the cost estimate and require confirmation before Stage 2."""
    est_cost = num_calls * DETAILS_COST_PER_CALL_USD
    print("")
    print("=" * 60)
    print("COST GUARD")
    print(f"  Place Details calls to make : {num_calls}")
    print(f"  Est. cost (@ ${DETAILS_COST_PER_CALL_USD:.3f}/call) : ${est_cost:.2f}")
    print("  (Text Search calls already made are billed separately.)")
    print("=" * 60)

    if num_calls == 0:
        print("Nothing to do.")
        return False

    if assume_yes:
        print("Proceeding (--yes supplied).")
        return True

    try:
        answer = input("Proceed with billed Details calls? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer in ("y", "yes"):
        return True
    print("Aborted by user.")
    return False


def get_display_name(place):
    name = place.get("displayName")
    if isinstance(name, dict):
        return name.get("text", "")
    return name or ""


def write_csv(rows, output_dir, query):
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_query = "".join(c if c.isalnum() else "_" for c in query).strip("_")[:40]
    filename = f"leads_{safe_query}_{timestamp}.csv"
    path = os.path.join(output_dir, filename)

    fieldnames = [
        "name",
        "phone",
        "address",
        "rating",
        "review_count",
        "place_id",
        "social_only",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


# --- CLI ---------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Find local businesses without a website via Google Places API."
    )
    parser.add_argument(
        "--query", required=True, help='Search query, e.g. "plumbers in Asheville NC"'
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=50,
        help="Hard cap on Place Details (billed) calls. Default 50.",
    )
    parser.add_argument(
        "--min-reviews",
        type=int,
        default=5,
        help="Skip businesses with fewer reviews than this. Default 5.",
    )
    parser.add_argument(
        "--min-rating",
        type=float,
        default=0,
        help="Skip businesses rated below this. Default 0.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=3,
        help="Max Text Search pages to fetch (pagination). Default 3.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive cost confirmation prompt.",
    )
    parser.add_argument(
        "--output-dir",
        default="./output",
        help="Directory for the timestamped CSV. Default ./output",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    if load_dotenv is not None:
        load_dotenv()

    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        print(
            "ERROR: GOOGLE_PLACES_API_KEY is not set.\n"
            "Set it in your environment or a .env file (see .env.example).",
            file=sys.stderr,
        )
        return 1

    print(f'Query: "{args.query}"')
    print("Stage 1: Text Search...")
    try:
        places = text_search(api_key, args.query, args.max_pages)
    except PlacesAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Stage 1 complete: {len(places)} places found.")

    candidates = prefilter_places(places, args.min_reviews, args.min_rating)
    print(
        f"After min-reviews={args.min_reviews} / min-rating={args.min_rating} "
        f"filter: {len(candidates)} candidates."
    )

    # Hard cap on billed Details calls.
    if len(candidates) > args.max_results:
        print(
            f"Capping Details calls at --max-results={args.max_results} "
            f"(was {len(candidates)})."
        )
        candidates = candidates[: args.max_results]

    if not confirm_spend(len(candidates), args.yes):
        return 0

    print("\nStage 2: Place Details (billed)...")
    rows = []
    billed_calls = 0
    no_website = 0
    for i, place in enumerate(candidates, start=1):
        place_id = place.get("id")
        if not place_id:
            continue
        try:
            details = place_details(api_key, place_id)
        except PlacesAPIError as exc:
            print(f"  ! Details failed for {place_id}: {exc}", file=sys.stderr)
            continue
        billed_calls += 1
        print(f"  [{i}/{len(candidates)}] billed Details calls so far: {billed_calls}")

        website = (details.get("websiteUri") or "").strip()
        if website:
            continue  # has a website — not a lead.

        no_website += 1
        rows.append(
            {
                "name": get_display_name(details),
                "phone": details.get("nationalPhoneNumber", ""),
                "address": details.get("formattedAddress", ""),
                "rating": details.get("rating", ""),
                "review_count": details.get("userRatingCount", ""),
                "place_id": details.get("id", place_id),
                # We cannot reliably detect socials via this API/field mask, so
                # default to FALSE. The column is kept for downstream use.
                "social_only": "FALSE",
            }
        )

    path = write_csv(rows, args.output_dir, args.query)

    print("")
    print("=" * 60)
    print(f"SUMMARY: {billed_calls} businesses checked, {no_website} with no website.")
    print(f"Total billed Place Details calls: {billed_calls}")
    print(f"CSV written to: {path}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
