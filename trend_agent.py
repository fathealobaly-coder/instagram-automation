# trend_agent.py
# Python 3.11
# Supabase schema: public.payment_events
# Required environment variables:
# SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
# DRY_RUN=true by default

import gc
import json
import logging
import os
import re
import unicodedata
from urllib.parse import quote

import requests

PROVIDER = "instagram_trend_crawler"
MAX_RECORDS = 3
ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata/{identifier}"
REQUEST_TIMEOUT = (10, 30)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("trend_agent")


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def dry_run_enabled():
    return os.getenv("DRY_RUN", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }


def sanitize_title(value):
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = "".join(
        char
        for char in normalized
        if unicodedata.category(char)[0] in {"L", "N", "P", "Z"}
        or char in "_-"
    )
    return re.sub(r"\s+", " ", normalized).strip()[:180]


def compact_json(payload):
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def request_checked(session, method, url, **kwargs):
    response = session.request(
        method,
        url,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )
    response.raise_for_status()
    return response


def load_existing_ids(session, base_url, headers):
    url = f"{base_url}/rest/v1/payment_events"
    existing_ids = set()
    offset = 0
    page_size = 500

    while True:
        try:
            response = request_checked(
                session,
                "GET",
                url,
                headers=headers,
                params={
                    "select": "id,provider_payment_id",
                    "provider": f"eq.{PROVIDER}",
                    "limit": str(page_size),
                    "offset": str(offset),
                    "order": "id.asc",
                },
            )
            rows = response.json()

            if not isinstance(rows, list):
                raise ValueError("Supabase returned a non-list response")

            for row in rows:
                try:
                    if not isinstance(row, dict):
                        continue

                    raw_payload = row.get("provider_payment_id")
                    if not isinstance(raw_payload, str) or not raw_payload:
                        continue

                    payload = json.loads(raw_payload)
                    if not isinstance(payload, dict):
                        continue

                    video_id = payload.get("video_id")
                    if isinstance(video_id, str) and video_id:
                        existing_ids.add(video_id)

                except (ValueError, TypeError, AttributeError):
                    LOG.warning("Skipping malformed existing database payload")

            if len(rows) < page_size:
                return existing_ids

            offset += page_size

        except Exception as exc:
            raise RuntimeError(
                "CRITICAL: Supabase SELECT failed; ingestion halted "
                "to prevent duplicate insertion."
            ) from exc


def discover_archive_candidates(session):
    queries = (
        'mediatype:movies AND (title:action OR subject:action)',
        'mediatype:movies AND (title:stunt OR subject:stunt)',
        'mediatype:movies AND (title:martial OR subject:martial)',
        'mediatype:movies AND (title:chase OR subject:chase)',
    )

    seen_identifiers = set()

    for query in queries:
        try:
            response = request_checked(
                session,
                "GET",
                ARCHIVE_SEARCH_URL,
                params=[
                    ("q", query),
                    ("fl[]", "identifier"),
                    ("fl[]", "title"),
                    ("fl[]", "description"),
                    ("fl[]", "subject"),
                    ("rows", "100"),
                    ("page", "1"),
                    ("output", "json"),
                    ("sort[]", "downloads desc"),
                ],
            )
            body = response.json()
            documents = body.get("response", {}).get("docs", [])

            if not isinstance(documents, list):
                continue

        except (
            requests.RequestException,
            ValueError,
            TypeError,
            AttributeError,
        ) as exc:
            LOG.warning("Archive discovery request failed: %s", exc)
            continue

        for document in documents:
            if not isinstance(document, dict):
                continue

            identifier = document.get("identifier")
            if not isinstance(identifier, str) or not identifier:
                continue

            if identifier in seen_identifiers:
                continue

            seen_identifiers.add(identifier)

            title = sanitize_title(document.get("title", ""))
            description = str(document.get("description", ""))
            subjects = document.get("subject", [])

            if isinstance(subjects, list):
                subject_text = " ".join(str(item) for item in subjects)
            else:
                subject_text = str(subjects or "")

            searchable = f"{title} {description} {subject_text}".lower()

            action_terms = (
                "action",
                "stunt",
                "martial arts",
                "chase",
                "fight",
                "adventure",
                "kung fu",
                "parkour",
                "thriller",
            )

            if not title or not any(
                term in searchable for term in action_terms
            ):
                continue

            try:
                metadata_response = request_checked(
                    session,
                    "GET",
                    ARCHIVE_METADATA_URL.format(
                        identifier=quote(identifier, safe="")
                    ),
                )
                metadata = metadata_response.json()
                files = metadata.get("files", [])

                if not isinstance(files, list):
                    continue

            except (
                requests.RequestException,
                ValueError,
                TypeError,
                AttributeError,
            ) as exc:
                LOG.warning(
                    "Could not retrieve metadata for %s: %s",
                    identifier,
                    exc,
                )
                continue

            for file_info in files:
                if not isinstance(file_info, dict):
                    continue

                filename = file_info.get("name")
                if not isinstance(filename, str):
                    continue

                if not filename.lower().endswith(".mp4"):
                    continue

                if str(file_info.get("private", "0")) == "1":
                    continue

                video_url = (
                    "https://archive.org/download/"
                    f"{quote(identifier, safe='')}/"
                    f"{quote(filename, safe='/')}"
                )

                yield {
                    "video_id": f"archive:{identifier}:{filename}",
                    "title": title,
                    "video_url": video_url,
                    "state": "pending_upload",
                }
                break


def validate_mp4_url(session, url):
    try:
        response = session.head(
            url,
            allow_redirects=True,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 405:
            response.close()
            response = session.get(
                url,
                headers={"Range": "bytes=0-0"},
                allow_redirects=True,
                stream=True,
                timeout=REQUEST_TIMEOUT,
            )

        status_ok = response.status_code == 200
        content_type = response.headers.get("Content-Type", "").lower()
        final_url = response.url.lower().split("?", 1)[0]

        content_ok = (
            content_type.startswith("video/")
            or final_url.endswith(".mp4")
        )

        response.close()
        return status_ok and content_ok

    except requests.RequestException as exc:
        LOG.info("Rejected inaccessible media URL: %s", exc)
        return False


def insert_payload(session, base_url, headers, payload):
    response = request_checked(
        session,
        "POST",
        f"{base_url}/rest/v1/payment_events",
        headers={
            **headers,
            "Prefer": "return=minimal",
        },
        json={
            "provider": PROVIDER,
            "provider_payment_id": compact_json(payload),
        },
    )
    response.close()


def main():
    base_url = required_env("SUPABASE_URL").rstrip("/")
    service_key = required_env("SUPABASE_SERVICE_ROLE_KEY")
    dry_run = dry_run_enabled()

    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }

    session = requests.Session()
    candidates = []
    existing_ids = set()

    try:
        existing_ids = load_existing_ids(session, base_url, headers)

        for candidate in discover_archive_candidates(session):
            if len(candidates) >= MAX_RECORDS:
                break

            if candidate["video_id"] in existing_ids:
                continue

            if not validate_mp4_url(session, candidate["video_url"]):
                continue

            candidates.append(candidate)

        for candidate in candidates:
            if dry_run:
                LOG.info(
                    "DRY_RUN: validated candidate; no database insert: %s",
                    candidate["video_id"],
                )
                continue

            insert_payload(
                session,
                base_url,
                headers,
                candidate,
            )

            existing_ids.add(candidate["video_id"])

            LOG.info(
                "Inserted candidate into Supabase: %s",
                candidate["video_id"],
            )

        LOG.info(
            "Run completed: matched=%d dry_run=%s",
            len(candidates),
            dry_run,
        )

    finally:
        candidates.clear()
        existing_ids.clear()
        session.close()
        del candidates
        del existing_ids
        gc.collect()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("Trend agent terminated")
        raise SystemExit(1)
