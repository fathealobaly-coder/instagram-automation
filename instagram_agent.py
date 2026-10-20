# instagram_agent.py
# Python 3.11
# Supabase table: public.payment_events
# Required secrets:
# SUPABASE_URL
# SUPABASE_SERVICE_ROLE_KEY
#
# Isolated simulation mode:
# No Meta credentials, Instagram credentials, or external publishing
# credentials are read or required.
# This agent validates and simulates queue processing only.
# DRY_RUN defaults to true.

import gc
import json
import logging
import os
import re
import sys

import requests

PROVIDER = "instagram_trend_crawler"
PAGE_SIZE = 100
REQUEST_TIMEOUT = (10, 30)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("instagram_agent")


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


def parse_payload(raw_payload):
    if not isinstance(raw_payload, str) or not raw_payload.strip():
        return None

    try:
        payload = json.loads(raw_payload)

        if not isinstance(payload, dict):
            return None

        for key in ("video_id", "title", "video_url", "state"):
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                return None

        return payload

    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def build_caption(title):
    normalized = re.sub(
        r"\s+",
        " ",
        str(title or ""),
    ).strip()

    hashtags = (
        "#Action #Cinematic #Stunts "
        "#Adventure #ActionMovies"
    )

    return f"{normalized}\n\n{hashtags}"[:2200]


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


def fetch_queue(session, base_url, headers):
    records = []
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
                    "select": "id,provider,provider_payment_id",
                    "provider": f"eq.{PROVIDER}",
                    "limit": str(PAGE_SIZE),
                    "offset": str(offset),
                    "order": "id.asc",
                },
            )

            rows = response.json()

            if not isinstance(rows, list):
                raise ValueError(
                    "Supabase returned an invalid queue response"
                )

            records.extend(rows)

            if len(rows) < PAGE_SIZE:
                return records

            offset += PAGE_SIZE

        except Exception as exc:
            raise RuntimeError(
                "Supabase queue retrieval failed"
            ) from exc

        finally:
            if response is not None:
                response.close()


def validate_record(record):
    if not isinstance(record, dict):
        LOG.warning("Skipping malformed database row")
        return None

    record_id = record.get("id")

    if record_id is None:
        LOG.warning("Skipping row without an ID")
        return None

    if record.get("provider") != PROVIDER:
        return None

    payload = parse_payload(
        record.get("provider_payment_id")
    )

    if payload is None:
        LOG.warning(
            "Skipping malformed payload for row ID %s",
            record_id,
        )
        return None

    if payload.get("state") != "pending_upload":
        return None

    video_url = payload.get("video_url", "")

    if not video_url.startswith("https://"):
        LOG.warning(
            "Skipping non-HTTPS media URL for row ID %s",
            record_id,
        )
        return None

    return {
        "record_id": record_id,
        "payload": payload,
        "caption": build_caption(payload.get("title", "")),
    }


def simulate_record(record):
    payload = record["payload"]

    # Simulation does not publish content, modify database state,
    # or claim that a remote operation succeeded.
    result = {
        "record_id": record["record_id"],
        "video_id": payload["video_id"],
        "state_before": payload["state"],
        "state_after": payload["state"],
        "publish_attempted": False,
        "database_updated": False,
        "simulation": True,
    }

    LOG.info(
        "SIMULATION: %s",
        compact_json(result),
    )


def main():
    base_url = required_env("SUPABASE_URL").rstrip("/")
    service_key = required_env("SUPABASE_SERVICE_ROLE_KEY")

    # Publishing is deliberately disabled in this isolated version.
    # Even DRY_RUN=false does not activate an external publishing API.
    dry_run = True

    if not dry_run_enabled():
        LOG.warning(
            "External publishing remains disabled; "
            "processing will remain simulation-only"
        )

    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
    }

    session = requests.Session()
    records = []
    validated_records = []

    try:
        records = fetch_queue(
            session,
            base_url,
            headers,
        )

        if not records:
            LOG.info("Queue is empty; no work required")
            return

        for record in records:
            try:
                validated = validate_record(record)

                if validated is None:
                    continue

                validated_records.append(validated)

                if dry_run:
                    simulate_record(validated)

            except Exception as exc:
                LOG.warning(
                    "Record validation failed safely: %s",
                    exc,
                )

        LOG.info(
            "Simulation completed: total_rows=%d valid_pending=%d",
            len(records),
            len(validated_records),
        )

    finally:
        validated_records.clear()
        records.clear()
        session.close()

        del validated_records
        del records

        gc.collect()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOG.warning(
            "Instagram agent stopped safely: %s",
            exc,
        )
        sys.exit(0)
