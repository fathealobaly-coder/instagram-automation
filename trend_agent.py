
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
trend_agent.py
Multi-platform-ready video discovery prototype.

Current source: Wikimedia Commons public MediaWiki API.
Destination: public.payment_events in Supabase.

Required environment variables:
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests


AGENT_NAME = "trend_agent_video_v1"
DAILY_TARGET = 3

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

STATE_FILE = Path(
    os.getenv("TREND_AGENT_STATE_FILE", "trend_agent_state.json")
)

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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_day() -> str:
    return utc_now().date().isoformat()


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "day": utc_day(),
            "processed": [],
            "daily_count": 0,
        }

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        if data.get("day") != utc_day():
            return {
                "day": utc_day(),
                "processed": data.get("processed", []),
                "daily_count": 0,
            }

        data.setdefault("processed", [])
        data.setdefault("daily_count", 0)
        return data

    except (OSError, json.JSONDecodeError):
        LOG.warning("State file unreadable; starting fresh.")
        return {
            "day": utc_day(),
            "processed": [],
            "daily_count": 0,
        }


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def fingerprint(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def search_commons(
    term: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Find candidate video files in Wikimedia Commons."""

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

    response = SESSION.get(
        API_URL,
        params=params,
        timeout=30,
    )
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

        if parsed.scheme != "https":
            continue

        if parsed.hostname not in ALLOWED_HOSTS:
            continue

        metadata = info.get("extmetadata", {})

        def meta_value(key: str) -> str:
            value = metadata.get(key, {})
            if isinstance(value, dict):
                return str(value.get("value", ""))
            return ""

        title = page.get("title", "")

        candidates.append({
            "title": title,
            "video_url": url,
            "mime": mime,
            "size_bytes": info.get("size"),
            "description": meta_value("ImageDescription"),
            "license": meta_value("LicenseShortName"),
            "license_url": meta_value("LicenseUrl"),
            "source_page": (
                "https://commons.wikimedia.org/wiki/"
                + quote(title.replace(" ", "_"), safe=":_")
            ),
        })

    return candidates


def verify_video_url(
    url: str,
) -> tuple[bool, dict[str, Any]]:
    """Check remote HTTP availability and video-like content type."""

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
                "final_url": response.url,
                "checked_at": utc_now().isoformat(),
            }

        finally:
            response.close()

    except requests.RequestException as exc:
        return False, {
            "reason": "request_failed",
            "error": str(exc)[:300],
        }


def make_caption(candidate: dict[str, Any]) -> str:
    title = candidate.get("title", "Action video")
    title = title.replace("File:", "").strip()
    title = " ".join(title.split())[:180]

    return (
        f"{title} | Explore the moment. "
        "Check the source and license before publishing."
    )


def make_record(
    candidate: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    return {
        "agent": AGENT_NAME,
        "record_type": "video_candidate",
        "video_id": fingerprint(candidate["video_url"]),
        "video_url": candidate["video_url"],
        "title": candidate.get("title"),
        "description": candidate.get("description"),
        "caption": make_caption(candidate),
        "source": "Wikimedia Commons",
        "source_page": candidate.get("source_page"),
        "mime": candidate.get("mime"),
        "size_bytes": candidate.get("size_bytes"),
        "license": candidate.get("license"),
        "license_url": candidate.get("license_url"),
        "link_check": verification,
        "discovered_at": utc_now().isoformat(),
        "status": "DISCOVERED",
        "publication_approved": False,
    }


def validate_configuration() -> None:
    missing = []

    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")

    if not SUPABASE_SERVICE_ROLE_KEY:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")

    if missing:
        raise RuntimeError(
            "Missing environment variables: "
            + ", ".join(missing)
        )


def supabase_headers() -> dict[str, str]:
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def already_recorded(video_id: str) -> bool:
    """
    Check previously stored JSON records.

    This is not an atomic uniqueness guarantee. It assumes the
    provider_payment_id column contains JSON text.
    """

    endpoint = f"{SUPABASE_URL}/rest/v1/payment_events"

    params = {
        "provider": f"eq.{AGENT_NAME}",
        "select": "provider_payment_id",
        "limit": "1000",
    }

    response = SESSION.get(
        endpoint,
        headers=supabase_headers(),
        params=params,
        timeout=30,
    )
    response.raise_for_status()

    for row in response.json():
        raw = row.get("provider_payment_id")

        if not isinstance(raw, str):
            continue

        try:
            stored = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if stored.get("video_id") == video_id:
            return True

    return False


def insert_record(record: dict[str, Any]) -> None:
    endpoint = f"{SUPABASE_URL}/rest/v1/payment_events"

    payload = {
        "provider": AGENT_NAME,
        "provider_payment_id": json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }

    response = SESSION.post(
        endpoint,
        headers=supabase_headers(),
        json=payload,
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            f"Supabase insert failed: HTTP {response.status_code}; "
            f"{response.text[:1000]}"
        )


def run_agent() -> int:
    validate_configuration()
    state = load_state()

    if state["daily_count"] >= DAILY_TARGET:
        LOG.info("Daily target already reached: %s", DAILY_TARGET)
        return 0

    seen_this_run = set()
    saved_count = 0

    for term in SEARCH_TERMS:
        if state["daily_count"] >= DAILY_TARGET:
            break

        LOG.info("Searching Commons: %s", term)

        try:
            candidates = search_commons(term)
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("Search failed for '%s': %s", term, exc)
            continue

        for candidate in candidates:
            if state["daily_count"] >= DAILY_TARGET:
                break

            video_id = fingerprint(candidate["video_url"])

            if video_id in seen_this_run:
                continue
            seen_this_run.add(video_id)

            if video_id in state["processed"]:
                continue

            try:
                if already_recorded(video_id):
                    state["processed"].append(video_id)
                    save_state(state)
                    continue
            except (
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as exc:
                LOG.error("Supabase duplicate check failed: %s", exc)
                return 1

            valid, verification = verify_video_url(
                candidate["video_url"]
            )

            if not valid:
                LOG.info(
                    "Rejected %s: %s",
                    candidate["title"],
                    verification.get("reason"),
                )
                continue

            record = make_record(candidate, verification)

            try:
                insert_record(record)
            except (
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as exc:
                LOG.error("Supabase insert failed: %s", exc)
                return 1

            state["processed"].append(video_id)
            state["daily_count"] += 1
            save_state(state)
            saved_count += 1

            LOG.info(
                "Recorded %s/%s: %s",
                state["daily_count"],
                DAILY_TARGET,
                candidate["title"],
            )

            time.sleep(1)

    if state["daily_count"] < DAILY_TARGET:
        LOG.warning(
            "Target not reached: %s/%s records today.",
            state["daily_count"],
            DAILY_TARGET,
        )

    LOG.info("Run complete. New records: %s", saved_count)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_agent())
    except Exception:
        LOG.exception("Agent stopped unexpectedly.")
        sys.exit(1)
