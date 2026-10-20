# trend_agent.py
# Python 3.11
# Supabase table: public.payment_events
# Required secrets: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
# DRY_RUN defaults to true.
#
# Uses public Internet Archive metadata for discovery.
# No Meta credentials or third-party discovery API keys are required.

import gc
import json
import logging
import os
import re
import sys
import unicodedata
from urllib.parse import quote

import requests

PROVIDER = "instagram_trend_crawler"
MAX_RECORDS_PER_RUN = 3
ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata/{}"
REQUEST_TIMEOUT = (10, 30)
PAGE_SIZE = 500

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("trend_agent")


def dry_run_enabled():
    return os.getenv("DRY_RUN", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def compact_json(payload):
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def sanitize_title(value):
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = "".join(
        char
        for char in value
        if unicodedata.category(char)[0] in {"L", "N", "P", "Z"}
        or char in "_-"
    )
    return re.sub(r"\s+", " ", value).strip()[:180]


def checked_request(session, method, url, **kwargs):
    response = None
    try:
        response = session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        response.raise_for_status()
        return response
    except requests.RequestException:
        if response is not None:
            response.close()
        raise


def load_existing_ids(session, base_url, headers):
    existing_ids = set()
    offset = 0

    while True:
        response = None
        try:
            response = checked_request(
                session,
                "GET",
                f"{base_url}/rest/v1/payment_events",
                headers=headers,
                params={
                    "select": "id,provider_payment_id",
                    "provider": f"eq.{PROVIDER}",
                    "limit": str(PAGE_SIZE),
                    "offset": str(offset),
                    "order": "id.asc",
                },
            )
            rows = response.json()

            if not isinstance(rows, list):
                raise ValueError("Supabase SELECT did not return a JSON list")

            for row in rows:
                try:
                    if not isinstance(row, dict):
                        continue

                    raw = row.get("provider_payment_id")
                    if not isinstance(raw, str) or not raw.strip():
                        continue

                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        continue

                    video_id = payload.get("video_id")
                    if isinstance(video_id, str) and video_id:
                        existing_ids.add(video_id)

                except (ValueError, TypeError, AttributeError):
                    LOG.warning("Skipping malformed existing payload")

            if len(rows) < PAGE_SIZE:
                return existing_ids

            offset += PAGE_SIZE

        except Exception as exc:
            raise RuntimeError(
                "Supabase SELECT failed; refusing to ingest without "
                "a reliable duplicate check"
            ) from exc
        finally:
            if response is not None:
                response.close()


def discover_candidates(session):
    queries = (
        'mediatype:movies AND (title:action OR subject:action)',
        'mediatype:movies AND (title:stunt OR subject:stunt)',
        'mediatype:movies AND (title:martial OR subject:martial)',
        'mediatype:movies AND (title:chase OR subject:chase)',
    )
    seen_identifiers = set()

    for query in queries:
        response = None
        try:
            response = checked_request(
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
            LOG.warning("Discovery request failed: %s", exc)
            continue
        finally:
            if response is not None:
                response.close()

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
                subjects_text = " ".join(map(str, subjects))
            else:
                subjects_text = str(subjects or "")

            searchable = f"{title} {description} {subjects_text}".lower()
            terms = (
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

            if not title or not any(term in searchable for term in terms):
                continue

            response = None
            try:
                response = checked_request(
                    session,
                    "GET",
                    ARCHIVE_METADATA_URL.format(
                        quote(identifier, safe="")
                    ),
                )
                metadata = response.json()
                files = metadata.get("files", [])
                if not isinstance(files, list):
                    continue

            except (
                requests.RequestException,
                ValueError,
                TypeError,
                AttributeError,
            ) as exc:
                LOG.warning("Metadata request failed: %s", exc)
                continue
            finally:
                if response is not None:
                    response.close()

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

                yield {
                    "video_id": f"archive:{identifier}:{filename}",
                    "title": title,
                    "video_url": (
                        "https://archive.org/download/"
                        f"{quote(identifier, safe='')}/"
                        f"{quote(filename, safe='/')}"
                    ),
                    "state": "pending_upload",
                }
                break


def validate_mp4_url(session, url):
    response = None
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
                stream=True,
                allow_redirects=True,
                timeout=REQUEST_TIMEOUT,
            )

        if response.status_code != 200:
            return False

        content_type = response.headers.get("Content-Type", "").lower()
        final_url = response.url.lower().split("?", 1)[0]

        return (
            content_type.startswith("video/")
            or final_url.endswith(".mp4")
        )

    except requests.RequestException as exc:
        LOG.warning("Media URL validation failed: %s", exc)
        return False
    finally:
        if response is not None:
            response.close()


def insert_candidate(session, base_url, headers, candidate):
    response = None
    try:
        response = checked_request(
            session,
            "POST",
            f"{base_url}/rest/v1/payment_events",
            headers={
                **headers,
                "Prefer": "return=minimal",
            },
            json={
                "provider": PROVIDER,
                "provider_payment_id": compact_json(candidate),
            },
        )
    finally:
        if response is not None:
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
        # A failed SELECT must stop ingestion; an empty result is not
        # assumed when the database request itself has failed.
        existing_ids = load_existing_ids(session, base_url, headers)

        for candidate in discover_candidates(session):
            if len(candidates) >= MAX_RECORDS_PER_RUN:
                break
            if candidate["video_id"] in existing_ids:
                continue
            if not validate_mp4_url(session, candidate["video_url"]):
                continue
            candidates.append(candidate)

        for candidate in candidates:
            if dry_run:
                LOG.info(
                    "DRY_RUN: candidate validated; no insert performed: %s",
                    candidate["video_id"],
                )
                continue

            insert_candidate(
                session,
                base_url,
                headers,
                candidate,
            )
            existing_ids.add(candidate["video_id"])
            LOG.info("Candidate inserted: %s", candidate["video_id"])

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
    except Exception as exc:
        LOG.warning("Trend agent stopped safely: %s", exc)
        sys.exit(0)
