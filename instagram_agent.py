#!/usr/bin/env python3
"""Instagram Reels publisher using only the official Meta Graph API.

Reads JSON records from public.payment_events.provider_payment_id. The table schema
is intentionally limited to id, provider, provider_payment_id. Publishing is disabled
unless INSTAGRAM_DRY_RUN=false and all required credentials are supplied.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from urllib.parse import quote

import requests

AGENT_NAME = "instagram_agent_video_v1"
SOURCE_AGENT = "trend_agent_video_v1"
REQUEST_TIMEOUT = (8, 30)
PAGE_SIZE = 200

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
INSTAGRAM_ACCOUNT_ID = os.environ.get("INSTAGRAM_ACCOUNT_ID", "").strip()
INSTAGRAM_ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
META_GRAPH_API_VERSION = os.environ.get("META_GRAPH_API_VERSION", "").strip()
INSTAGRAM_DRY_RUN = os.environ.get("INSTAGRAM_DRY_RUN", "true").strip().lower() not in {"0", "false", "no"}
SESSION = requests.Session()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(AGENT_NAME)


class AgentStop(RuntimeError):
    pass


def db_headers(prefer: str | None = None) -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise AgentStop("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def request_or_stop(response: requests.Response, operation: str) -> requests.Response:
    if response.status_code in (401, 403, 429):
        raise AgentStop(f"{operation} stopped on HTTP {response.status_code}; no retry attempted")
    response.raise_for_status()
    return response


def read_queue(session: requests.Session | None = None) -> list[dict[str, Any]]:
    client = session or SESSION
    records: list[dict[str, Any]] = []
    offset = 0
    while True:
        headers = db_headers()
        headers["Range-Unit"] = "items"
        headers["Range"] = f"{offset}-{offset + PAGE_SIZE - 1}"
        response = request_or_stop(client.get(
            f"{SUPABASE_URL}/rest/v1/payment_events",
            headers=headers,
            params={"provider": f"eq.{SOURCE_AGENT}", "select": "id,provider_payment_id", "limit": str(PAGE_SIZE)},
            timeout=REQUEST_TIMEOUT,
        ), "Supabase queue read")
        rows = response.json()
        if not isinstance(rows, list):
            raise AgentStop("Supabase queue response was not a JSON array")
        for row in rows:
            try:
                payload = json.loads(row.get("provider_payment_id", ""))
            except (TypeError, json.JSONDecodeError):
                LOG.warning("Skipping malformed queue record id=%s", row.get("id"))
                continue
            if isinstance(payload, dict) and payload.get("record_type") == "video_candidate":
                records.append({"id": str(row.get("id", "")), "payload": payload})
        if len(rows) < PAGE_SIZE:
            return records
        offset += len(rows)


def update_record(record_id: str, payload: dict[str, Any], session: requests.Session | None = None) -> None:
    client = session or SESSION
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    response = request_or_stop(client.patch(
        f"{SUPABASE_URL}/rest/v1/payment_events",
        headers=db_headers("return=minimal"),
        params={"id": f"eq.{record_id}", "provider": f"eq.{SOURCE_AGENT}"},
        json={"provider_payment_id": text},
        timeout=REQUEST_TIMEOUT,
    ), "Supabase record update")
    # PostgREST PATCH with return=minimal returns an empty body on success.
    del response


def meta_post(path: str, data: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    client = session or SESSION
    if not META_GRAPH_API_VERSION:
        raise AgentStop("META_GRAPH_API_VERSION must be set to a version enabled for your Meta app")
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{path.lstrip('/')}"
    response = client.post(url, data=data, timeout=REQUEST_TIMEOUT)
    request_or_stop(response, "Meta Graph API POST")
    result = response.json()
    if not isinstance(result, dict) or result.get("error"):
        raise AgentStop("Meta Graph API returned an error response")
    return result


def meta_get(path: str, params: dict[str, Any], session: requests.Session | None = None) -> dict[str, Any]:
    client = session or SESSION
    if not META_GRAPH_API_VERSION:
        raise AgentStop("META_GRAPH_API_VERSION must be set to a version enabled for your Meta app")
    url = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}/{path.lstrip('/')}"
    response = client.get(url, params=params, timeout=REQUEST_TIMEOUT)
    request_or_stop(response, "Meta Graph API GET")
    result = response.json()
    if not isinstance(result, dict) or result.get("error"):
        raise AgentStop("Meta Graph API returned an error response")
    return result


def publish_reel(video_url: str, caption: str, session: requests.Session | None = None) -> dict[str, Any]:
    if not INSTAGRAM_ACCOUNT_ID or not INSTAGRAM_ACCESS_TOKEN:
        raise AgentStop("Missing INSTAGRAM_ACCOUNT_ID or INSTAGRAM_ACCESS_TOKEN")
    client = session or SESSION
    created = meta_post(f"{INSTAGRAM_ACCOUNT_ID}/media", {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption[:2200],
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    }, client)
    creation_id = str(created.get("id", ""))
    if not creation_id:
        raise AgentStop("Meta did not return a media container id")

    # Poll processing status, with a hard deadline. Never publish before FINISHED.
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        status = meta_get(creation_id, {"fields": "status_code,status", "access_token": INSTAGRAM_ACCESS_TOKEN}, client)
        code = str(status.get("status_code", "")).upper()
        if code == "FINISHED":
            break
        if code in {"ERROR", "EXPIRED"}:
            raise AgentStop(f"Instagram container processing stopped: {code}")
        time.sleep(5)
    else:
        raise AgentStop("Instagram container processing timed out; not published")

    published = meta_post(f"{INSTAGRAM_ACCOUNT_ID}/media_publish", {
        "creation_id": creation_id,
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    }, client)
    media_id = str(published.get("id", ""))
    if not media_id:
        raise AgentStop("Meta publish response did not include a media id")
    return {"creation_id": creation_id, "instagram_media_id": media_id, "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def eligible(payload: dict[str, Any]) -> tuple[bool, str]:
    if payload.get("record_type") != "video_candidate":
        return False, "not_video_candidate"
    if payload.get("status") != "DISCOVERED":
        return False, "status_not_DISCOVERED"
    if payload.get("publication_approved") is not True:
        return False, "publication_not_approved"
    if not payload.get("rights_verified"):
        return False, "rights_not_verified"
    if not payload.get("video_url") or payload.get("mime") != "video/mp4":
        return False, "missing_direct_mp4"
    if not payload.get("link_check", {}).get("ok"):
        return False, "link_not_verified"
    if not payload.get("caption"):
        return False, "caption_missing"
    return True, "eligible"


def run_agent() -> dict[str, Any]:
    records = read_queue()
    eligible_rows = []
    for row in records:
        ok, reason = eligible(row["payload"])
        if ok:
            eligible_rows.append(row)
        else:
            LOG.info("Skipping row %s: %s", row["id"], reason)
    if not eligible_rows:
        return {"status": "NO_APPROVED_RECORDS", "queue_records": len(records), "published": 0, "dry_run": INSTAGRAM_DRY_RUN}

    if INSTAGRAM_DRY_RUN:
        return {"status": "DRY_RUN", "queue_records": len(records), "eligible": len(eligible_rows), "published": 0, "dry_run": True}

    if not META_GRAPH_API_VERSION:
        raise AgentStop("Set META_GRAPH_API_VERSION before enabling publishing")

    published_count = 0
    for row in eligible_rows:
        record_id, payload = row["id"], row["payload"]
        # Mark PROCESSING before external side effects. No automatic retries on ambiguous outcomes.
        payload = dict(payload)
        payload["status"] = "PROCESSING"
        payload["processing_started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        update_record(record_id, payload)
        try:
            result = publish_reel(payload["video_url"], payload["caption"])
        except Exception as exc:
            payload["status"] = "PUBLISH_ERROR"
            payload["publish_error_type"] = type(exc).__name__
            payload["publish_error"] = str(exc)[:500]
            update_record(record_id, payload)
            LOG.error("Publishing stopped for row %s (%s)", record_id, type(exc).__name__)
            # Do not continue after an API or publish failure: avoid quota burn/repeated failures.
            raise
        payload["status"] = "PUBLISHED"
        payload["publish_result"] = result
        update_record(record_id, payload)
        published_count += 1
        LOG.info("Published Instagram media id %s", result["instagram_media_id"])
    return {"status": "PUBLISHED", "queue_records": len(records), "published": published_count, "dry_run": False}


if __name__ == "__main__":
    try:
        result = run_agent()
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        LOG.error("Agent stopped: %s: %s", type(exc).__name__, exc)
        raise SystemExit(1)
