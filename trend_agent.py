# -*- coding: utf-8 -*-

"""
trend_agent.py
Multi-platform-ready video discovery prototype using official Supabase client.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any
from urllib.parse import quote, urlparse

import requests
from supabase import create_client, Client

AGENT_NAME = "instagram_trend_crawler"
DAILY_TARGET = 3

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

API_URL = "https://commons.wikimedia.org/w/api.php"

USER_AGENT = (
    "TrendAgentVideo/1.0 "
    "(public media discovery; configure a valid contact)"
)

SEARCH_TERMS = [
    "action scene video",
    "cinematic action",
    "car chase video",
    "adventure action video",
    "martial arts demonstration video",
    "explosion film special effects",
    "parkour action video",
]

ALLOWED_HOSTS = {"upload.wikimedia.org"}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json, video/*, */*",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger(AGENT_NAME)


def validate_configuration() -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("CRITICAL: Environment variables missing.")


def get_supabase_client() -> Client:
    validate_configuration()
    try:
        return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    except Exception as exc:
        raise RuntimeError(f"Failed to initialize Supabase client: {exc}")


def clean_text(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"[^\w\s.,!?-]", "", text).strip()


def fingerprint(url: str) -> str:
    import hashlib
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def search_commons(term: str, limit: int = 30) -> list[dict[str, Any]]:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": f"filetype:video {term}",
        "gsrnamespace": 6,
        "gsrlimit": min(limit, 50),
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
    }

    response = SESSION.get(API_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    pages = payload.get("query", {}).get("pages", {})
    candidates = []

    for page in pages.values():
        imageinfo = page.get("imageinfo") or []
        if not imageinfo:
            continue

        info = imageinfo[0]
        url = info.get("url", "")
        mime = info.get("mime", "")

        if not url or not mime.startswith("video/"):
            continue

        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            continue

        metadata = info.get("extmetadata", {})

        def meta_value(key: str) -> str:
            value = metadata.get(key, {})
            if isinstance(value, dict):
                return str(value.get("value", ""))
            return ""

        title = page.get("title", "")

        candidates.append({
            "title": clean_text(title),
            "video_url": url,
            "mime": mime,
            "size_bytes": info.get("size"),
            "description": clean_text(meta_value("ImageDescription")),
            "license": meta_value("LicenseShortName"),
            "license_url": meta_value("LicenseUrl"),
            "source_page": (
                "https://commons.wikimedia.org/wiki/"
                + quote(title.replace(" ", "_"), safe=":_")
            ),
        })

    return candidates


def verify_video_url(url: str) -> tuple[bool, dict[str, Any]]:
    try:
        response = SESSION.get(
            url,
            headers={"Range": "bytes=0-1023"},
            stream=True,
            allow_redirects=True,
            timeout=(10, 25),
        )
        try:
            if response.status_code not in (200, 206):
                return False, {
                    "http_status": response.status_code,
                    "reason": "unexpected_http_status",
                }

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";")[0]
                .strip()
                .lower()
            )

            if not (
                content_type.startswith("video/")
                or content_type == "application/octet-stream"
            ):
                return False, {
                    "http_status": response.status_code,
                    "content_type": content_type,
                    "reason": "not_detected_as_video",
                }

            return True, {
                "http_status": response.status_code,
                "content_type": content_type,
            }
        finally:
            response.close()
    except requests.RequestException as exc:
        return False, {"reason": "request_failed", "error": str(exc)[:300]}


def fetch_existing_video_ids(supabase: Client) -> set:
    try:
        response = (
            supabase.table("payment_events")
            .select("provider_payment_id")
            .eq("provider", AGENT_NAME)
            .execute()
        )
        existing_ids = set()
        if response.data:
            for row in response.data:
                try:
                    data_json = json.loads(row.get("provider_payment_id", "{}"))
                    if "video_id" in data_json:
                        existing_ids.add(data_json["video_id"])
                except Exception:
                    continue
        return existing_ids
    except Exception as exc:
        raise RuntimeError(
            f"Supabase query failed: {exc}. Halting process to prevent data contamination."
        )


def run_agent() -> int:
    supabase = get_supabase_client()

    try:
        existing_video_ids = fetch_existing_video_ids(supabase)
    except Exception as exc:
        LOG.critical(exc)
        return 1

    collected_count = 0
    seen_this_run = set()

    for term in SEARCH_TERMS:
        if collected_count >= DAILY_TARGET:
            break

        LOG.info("Searching Commons: %s", term)

        try:
            candidates = search_commons(term)
        except Exception as exc:
            LOG.warning("Search failed for '%s': %s", term, exc)
            continue

        for candidate in candidates:
            if collected_count >= DAILY_TARGET:
                break

            video_id = fingerprint(candidate["video_url"])

            if video_id in seen_this_run or video_id in existing_video_ids:
                continue
            seen_this_run.add(video_id)

            valid, verification = verify_video_url(candidate["video_url"])
            if not valid:
                continue

            payload = {
                "agent": AGENT_NAME,
                "record_type": "video_candidate",
                "video_id": video_id,
                "video_url": candidate["video_url"],
                "title": candidate.get("title"),
                "description": candidate.get("description"),
                "source": "Wikimedia Commons",
                "source_page": candidate.get("source_page"),
                "mime": candidate.get("mime"),
                "size_bytes": candidate.get("size_bytes"),
                "license": candidate.get("license"),
                "license_url": candidate.get("license_url"),
                "link_check": verification,
                "status": "pending_upload",
                "publication_approved": False,
            }

            try:
                supabase.table("payment_events").insert({
                    "provider": AGENT_NAME,
                    "provider_payment_id": json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ),
                }).execute()
                collected_count += 1
                LOG.info(
                    "Recorded %s/%s: %s",
                    collected_count,
                    DAILY_TARGET,
                    candidate["title"],
                )
            except Exception as exc:
                LOG.error("Supabase insert failed: %s", exc)
                continue

    LOG.info("Run complete. New records: %s", collected_count)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_agent())
    except Exception:
        LOG.exception("Agent stopped unexpectedly.")
        sys.exit(1)

