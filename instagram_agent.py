# instagram_agent.py
# Python 3.11
# Supabase schema: public.payment_events
# Required environment variables:
# SUPABASE_URL
# SUPABASE_SERVICE_ROLE_KEY
# INSTAGRAM_ACCOUNT_ID
# INSTAGRAM_ACCESS_TOKEN
#
# DRY_RUN=true by default.
# Set DRY_RUN=false to enable publishing.

import json
import logging
import os
import re
import time

import requests

PROVIDER = "instagram_trend_crawler"
GRAPH_VERSION = "v21.0"
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"
REQUEST_TIMEOUT = (10, 45)
MAX_RETRIES = 4
RETRY_BASE_DELAY = 2
CONTAINER_WAIT_SECONDS = 15
RECORD_COOLDOWN_SECONDS = 10
PAGE_SIZE = 100

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

LOG = logging.getLogger("instagram_agent")


def env_value(name):
    return os.getenv(name, "").strip()


def dry_run_enabled():
    return env_value("DRY_RUN").lower() in {
        "1", "true", "yes", "on"
    }


def compact_json(payload):
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def parse_payload(raw_payload):
    if not isinstance(raw_payload, str) or not raw_payload.strip():
        return None

    try:
        payload = json.loads(raw_payload)

        if not isinstance(payload, dict):
            return None

        required_fields = (
            "video_id",
            "title",
            "video_url",
            "state",
        )

        for field in required_fields:
            value = payload.get(field)

            if not isinstance(value, str) or not value.strip():
                return None

        return payload

    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def build_caption(title):
    sanitized = re.sub(
        r"\s+",
        " ",
        str(title or ""),
    ).strip()

    hashtags = (
        "#Action #Cinematic #Stunts "
        "#Adventure #ActionMovies"
    )

    caption = f"{sanitized}\n\n{hashtags}"

    return caption[:2200]


def request_with_backoff(
    session,
    method,
    url,
    *,
    retryable_statuses=None,
    **kwargs,
):
    if retryable_statuses is None:
        retryable_statuses = {
            408,
            425,
            429,
            500,
            502,
            503,
            504,
        }

    last_error = None

    for attempt in range(MAX_RETRIES):
        response = None

        try:
            response = session.request(
                method,
                url,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )

            if (
                response.status_code in retryable_statuses
                and attempt < MAX_RETRIES - 1
            ):
                status = response.status_code
                response.close()
                delay = min(
                    RETRY_BASE_DELAY * (2 ** attempt),
                    30,
                )

                LOG.warning(
                    "Retryable HTTP status %s; retry %d/%d in %ss",
                    status,
                    attempt + 1,
                    MAX_RETRIES - 1,
                    delay,
                )

                time.sleep(delay)
                continue

            response.raise_for_status()
            return response

        except requests.RequestException as exc:
            last_error = exc

            if response is not None:
                response.close()

            if attempt >= MAX_RETRIES - 1:
                break

            delay = min(
                RETRY_BASE_DELAY * (2 ** attempt),
                30,
            )

            LOG.warning(
                "Network request failed; retry %d/%d in %ss",
                attempt + 1,
                MAX_RETRIES - 1,
                delay,
            )

            time.sleep(delay)

    raise RuntimeError(
        f"Request failed after {MAX_RETRIES} attempts: {method} {url}"
    ) from last_error


def graph_post(session, url, token, data):
    response = request_with_backoff(
        session,
        "POST",
        url,
        data={
            **data,
            "access_token": token,
        },
    )

    try:
        result = response.json()

        if not isinstance(result, dict):
            raise RuntimeError(
                "Meta Graph API returned an invalid JSON object"
            )

        if result.get("error"):
            raise RuntimeError(
                "Meta Graph API reported an operation error"
            )

        return result

    except ValueError as exc:
        raise RuntimeError(
            "Meta Graph API returned invalid JSON"
        ) from exc

    finally:
        response.close()


def fetch_queue(session, base_url, headers):
    records = []
    offset = 0

    while True:
        response = request_with_backoff(
            session,
            "GET",
            f"{base_url}/rest/v1/payment_events",
            headers=headers,
            params={
                "select": "id,provider,provider_payment_id",
                "provider": f"eq.{PROVIDER}",
                "limit": str(PAGE_SIZE),
                "offset": str(offset),
                "order": "id.asc",
            },
        )

        try:
            rows = response.json()

            if not isinstance(rows, list):
                raise RuntimeError(
                    "Supabase queue response is not a JSON list"
                )

            records.extend(rows)

            if len(rows) < PAGE_SIZE:
                return records

            offset += PAGE_SIZE

        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "Failed to decode Supabase queue response"
            ) from exc

        finally:
            response.close()


def create_media_container(
    session,
    account_id,
    token,
    video_url,
    caption,
):
    endpoint = f"{GRAPH_BASE_URL}/{account_id}/media"

    result = graph_post(
        session,
        endpoint,
        token,
        {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
        },
    )

    creation_id = result.get("id")

    if not isinstance(creation_id, str) or not creation_id.strip():
        raise RuntimeError(
            "Meta did not return a valid media container ID"
        )

    return creation_id


def publish_media(
    session,
    account_id,
    token,
    creation_id,
):
    time.sleep(CONTAINER_WAIT_SECONDS)

    endpoint = f"{GRAPH_BASE_URL}/{account_id}/media_publish"

    result = graph_post(
        session,
        endpoint,
        token,
        {
            "creation_id": creation_id,
        },
    )

    media_id = result.get("id")

    if not isinstance(media_id, str) or not media_id.strip():
        raise RuntimeError(
            "Meta did not return a valid published media ID"
        )

    return media_id


def update_payload(
    session,
    base_url,
    headers,
    record_id,
    payload,
):
    updated_payload = dict(payload)
    updated_payload["state"] = "published_live"

    serialized = compact_json(updated_payload)

    response = request_with_backoff(
        session,
        "PATCH",
        f"{base_url}/rest/v1/payment_events",
        headers={
            **headers,
            "Prefer": "return=minimal",
        },
        params={
            "id": f"eq.{record_id}",
            "provider": f"eq.{PROVIDER}",
        },
        json={
            "provider_payment_id": serialized,
        },
    )

    response.close()


def process_record(
    session,
    base_url,
    headers,
    record,
    account_id,
    token,
    dry_run,
):
    if not isinstance(record, dict):
        LOG.warning("Skipping malformed database row")
        return

    record_id = record.get("id")

    if record_id is None:
        LOG.warning("Skipping row without a database ID")
        return

    if record.get("provider") != PROVIDER:
        return

    payload = parse_payload(
        record.get("provider_payment_id")
    )

    if payload is None:
        LOG.warning(
            "Skipping malformed payload for row ID %s",
            record_id,
        )
        return

    if payload.get("state") != "pending_upload":
        return

    video_url = payload.get("video_url", "")

    if not video_url.startswith("https://"):
        LOG.warning(
            "Skipping non-HTTPS video URL for row ID %s",
            record_id,
        )
        return

    title = payload.get("title", "")
    caption = build_caption(title)

    if dry_run:
        LOG.info(
            "DRY_RUN: validated row ID %s; publishing and database "
            "updates were not performed",
            record_id,
        )
        return

    creation_id = create_media_container(
        session,
        account_id,
        token,
        video_url,
        caption,
    )

    media_id = publish_media(
        session,
        account_id,
        token,
        creation_id,
    )

    payload["instagram_media_id"] = media_id

    update_payload(
        session,
        base_url,
        headers,
        record_id,
        payload,
    )

    LOG.info(
        "Published and updated database state for row ID %s",
        record_id,
    )


def main():
    base_url = env_value("SUPABASE_URL").rstrip("/")
    service_key = env_value("SUPABASE_SERVICE_ROLE_KEY")
    account_id = env_value("INSTAGRAM_ACCOUNT_ID")
    token = env_value("INSTAGRAM_ACCESS_TOKEN")

    if not base_url or not service_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required"
        )

    dry_run = dry_run_enabled()

    if not account_id or not token:
        if not dry_run:
            raise RuntimeError(
                "Instagram credentials are required when DRY_RUN=false"
            )

        LOG.warning(
            "Instagram credentials unavailable; running in dry-run mode"
        )
        dry_run = True

    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }

    session = requests.Session()
    records = []

    try:
        records = fetch_queue(
            session,
            base_url,
            headers,
        )

        if not records:
            LOG.info("No queue records found")
            return

        for record in records:
            try:
                process_record(
                    session,
                    base_url,
                    headers,
                    record,
                    account_id,
                    token,
                    dry_run,
                )

            except (
                RuntimeError,
                requests.RequestException,
                ValueError,
                TypeError,
                KeyError,
            ):
                LOG.exception(
                    "Record processing failed; continuing to next row"
                )

            finally:
                time.sleep(RECORD_COOLDOWN_SECONDS)

        LOG.info(
            "Queue processing finished; rows=%d dry_run=%s",
            len(records),
            dry_run,
        )

    finally:
        records.clear()
        session.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("Instagram agent terminated")
        raise SystemExit(1)
