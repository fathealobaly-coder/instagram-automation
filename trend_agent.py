#!/usr/bin/env python3
"""trend_agent_video_v1: discover openly licensed action videos and record candidates.

This agent records metadata in public.payment_events.provider_payment_id as JSON text.
It does not download video binaries or publish to Instagram.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests

AGENT_NAME = "trend_agent_video_v1"
DAILY_TARGET = 3
STATE_FILE = Path(__file__).with_name("trend_agent_state.json")
REQUEST_TIMEOUT = (8, 25)
USER_AGENT = "trend-agent-video-v1/2.0 (open-media discovery; contact configured by operator)"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# Restrict discovery to fictional animation/CGI adventure content; do not seek real-world stunts.
SEARCH_TERMS = (
    "public domain animated adventure film",
    "animated fantasy adventure scene",
    "CGI fictional adventure sequence",
    "public domain cartoon adventure",
)

# Conservative allow-list: source APIs and their official media hosts only.
ALLOWED_MEDIA_HOSTS = {
    "archive.org",
    "ia800100.us.archive.org",  # checked again by host suffix rule below
    "upload.wikimedia.org",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(AGENT_NAME)


class SourceAPIStop(RuntimeError):
    """Fail closed when a discovery source denies access or rate-limits us."""


def check_source_response(response: requests.Response, source: str) -> None:
    if response.status_code in (401, 403, 429):
        raise SourceAPIStop(f"{source} stopped on HTTP {response.status_code}; no retry attempted")
    response.raise_for_status()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_day() -> str:
    return utc_now().date().isoformat()


def clean_text(value: Any, limit: int = 1200) -> str:
    """Normalize untrusted source metadata into plain, bounded text."""
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit]


def stable_video_id(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("state root is not an object")
    except FileNotFoundError:
        state = {}
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        LOG.warning("State file unreadable; starting a fresh local state: %s", exc)
        state = {}

    if state.get("day") != utc_day():
        state = {"day": utc_day(), "daily_count": 0, "processed_ids": []}
    state.setdefault("daily_count", 0)
    state.setdefault("processed_ids", [])
    if not isinstance(state["processed_ids"], list):
        state["processed_ids"] = []
    return state


def save_state(state: dict[str, Any]) -> None:
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temp.replace(STATE_FILE)


def license_is_open(license_url: str, license_text: str = "") -> bool:
    """Accept only clearly identified public-domain or Creative Commons licenses."""
    value = f"{license_url} {license_text}".lower()
    allowed_markers = (
        "creativecommons.org/publicdomain/zero",
        "creativecommons.org/publicdomain/mark",
        "creativecommons.org/licenses/by/",
        "creativecommons.org/licenses/by-sa/",
        "creativecommons.org/licenses/by/4.0",
        "creativecommons.org/licenses/by-sa/4.0",
        "creativecommons.org/licenses/by/3.0",
        "creativecommons.org/licenses/by-sa/3.0",
        "public domain",
        "publicdomain",
    )
    return any(marker in value for marker in allowed_markers)


def is_allowed_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        trusted_host = host in ALLOWED_MEDIA_HOSTS or host.endswith(".archive.org")
        return parsed.scheme == "https" and trusted_host and parsed.path.lower().endswith(".mp4")
    except (TypeError, ValueError):
        return False


def check_video_url(url: str, session: requests.Session | None = None) -> dict[str, Any]:
    """Probe a direct MP4 with a ranged GET; require a successful response and video type."""
    client = session or SESSION
    if not is_allowed_media_url(url):
        return {"ok": False, "reason": "URL host, scheme, or extension is not allowed"}
    try:
        with client.get(
            url,
            headers={"Range": "bytes=0-2047"},
            stream=True,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        ) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
            valid_status = response.status_code in (200, 206)
            valid_type = content_type in ("video/mp4", "application/octet-stream")
            # Reject redirects to hosts outside the allow-list, even if the first URL was trusted.
            final_url = response.url
            valid_final_url = is_allowed_media_url(final_url)
            ok = valid_status and valid_type and valid_final_url
            return {
                "ok": ok,
                "status_code": response.status_code,
                "content_type": content_type,
                "final_url": final_url,
                "reason": "ok" if ok else "HTTP status/content type/final URL check failed",
            }
    except requests.RequestException as exc:
        return {"ok": False, "reason": f"request error: {type(exc).__name__}"}


def search_internet_archive(term: str, rows: int = 20, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """Search Internet Archive and return only MP4s with explicit open-license metadata."""
    client = session or SESSION
    query = f'mediatype:movies AND (title:"{term}" OR description:"{term}")'
    try:
        response = client.get(
            "https://archive.org/advancedsearch.php",
            params={"q": query, "fl[]": ["identifier", "title", "description", "licenseurl"], "rows": rows, "page": 1, "output": "json"},
            timeout=REQUEST_TIMEOUT,
        )
        check_source_response(response, "Internet Archive search")
        docs = response.json().get("response", {}).get("docs", [])
    except (requests.RequestException, ValueError, AttributeError) as exc:
        LOG.warning("Internet Archive search failed for %r: %s", term, type(exc).__name__)
        return []

    results: list[dict[str, Any]] = []
    for doc in docs:
        identifier = clean_text(doc.get("identifier"), 200)
        if not identifier:
            continue
        license_url = clean_text(doc.get("licenseurl"), 500)
        if not license_is_open(license_url):
            continue
        try:
            meta_response = client.get(
                f"https://archive.org/metadata/{quote(identifier, safe='')}", timeout=REQUEST_TIMEOUT
            )
            check_source_response(meta_response, "Internet Archive metadata")
            metadata = meta_response.json()
        except (requests.RequestException, ValueError, AttributeError) as exc:
            LOG.debug("Archive metadata unavailable for %s: %s", identifier, type(exc).__name__)
            continue

        meta = metadata.get("metadata", {})
        actual_license = clean_text(meta.get("licenseurl") or license_url, 500)
        license_name = clean_text(meta.get("license") or "", 300)
        if not license_is_open(actual_license, license_name):
            continue
        files = metadata.get("files", [])
        for file_info in files:
            name = clean_text(file_info.get("name"), 500)
            if not name.lower().endswith(".mp4") or file_info.get("private") == "true":
                continue
            media_url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name, safe='') }"
            if not is_allowed_media_url(media_url):
                continue
            results.append({
                "title": clean_text(meta.get("title") or doc.get("title") or name, 500),
                "description": clean_text(meta.get("description") or doc.get("description") or "", 1200),
                "video_url": media_url,
                "license": license_name or actual_license,
                "license_url": actual_license,
                "source": "Internet Archive",
                "source_page": f"https://archive.org/details/{quote(identifier, safe='')}",
                "mime": "video/mp4",
                "size_bytes": int(file_info.get("size", 0) or 0),
            })
            break  # one MP4 per archive item
    return results


def search_wikimedia_commons(term: str, limit: int = 30, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """Search Commons; accept direct MP4 only and require explicit open license metadata."""
    client = session or SESSION
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f'filetype:video "{term}"', "gsrnamespace": 6,
        "gsrlimit": min(max(limit, 1), 50), "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
    }
    try:
        response = client.get("https://commons.wikimedia.org/w/api.php", params=params, timeout=REQUEST_TIMEOUT)
        check_source_response(response, "Wikimedia Commons search")
        pages = response.json().get("query", {}).get("pages", {})
    except (requests.RequestException, ValueError, AttributeError) as exc:
        LOG.warning("Wikimedia search failed for %r: %s", term, type(exc).__name__)
        return []

    results: list[dict[str, Any]] = []
    for page in pages.values():
        info_list = page.get("imageinfo") or []
        if not info_list:
            continue
        info = info_list[0]
        media_url = clean_text(info.get("url"), 2000)
        if info.get("mime") != "video/mp4" or not is_allowed_media_url(media_url):
            continue
        ext = info.get("extmetadata") or {}
        license_name = clean_text((ext.get("LicenseShortName") or {}).get("value", ""), 200)
        license_url = clean_text((ext.get("LicenseUrl") or {}).get("value", ""), 500)
        if not license_is_open(license_url, license_name):
            continue
        results.append({
            "title": clean_text(page.get("title", ""), 500),
            "description": clean_text((ext.get("ImageDescription") or {}).get("value", ""), 1200),
            "video_url": media_url,
            "license": license_name,
            "license_url": license_url,
            "source": "Wikimedia Commons",
            "source_page": f"https://commons.wikimedia.org/wiki/{quote(clean_text(page.get('title', '').replace(' ', '_'), 500), safe='')}",
            "mime": "video/mp4",
            "size_bytes": int(info.get("size", 0) or 0),
        })
    return results


def make_caption(title: str, description: str) -> str:
    """Create a truthful, adaptable caption without claiming visual facts not in metadata."""
    basis = clean_text(title, 180) or "Action moment"
    context = clean_text(description, 220)
    caption = f"{basis} — a cinematic moment worth watching."
    if context and context.lower() not in basis.lower():
        caption += f" Context: {context}"
    caption += " #Action #Cinematic #Adventure #Reels"
    return caption[:900]


def license_allows_automatic_publication(license_url: str, license_text: str = "") -> bool:
    value = f"{license_url} {license_text}".lower()
    return any(marker in value for marker in (
        "creativecommons.org/publicdomain/zero/1.0",
        "creativecommons.org/publicdomain/mark/1.0",
        "public domain",
    ))


def make_record(candidate: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    url = candidate["video_url"]
    video_id = stable_video_id(url)
    auto_approved = license_allows_automatic_publication(
        clean_text(candidate.get("license_url", ""), 500),
        clean_text(candidate.get("license", ""), 300),
    )
    return {
        "agent": AGENT_NAME,
        "record_type": "video_candidate",
        "video_id": video_id,
        "video_url": url,
        "title": clean_text(candidate.get("title", ""), 500),
        "description": clean_text(candidate.get("description", ""), 1200),
        "caption": make_caption(candidate.get("title", ""), candidate.get("description", "")),
        "source": clean_text(candidate.get("source", ""), 100),
        "source_page": clean_text(candidate.get("source_page", ""), 2000),
        "mime": candidate.get("mime", "video/mp4"),
        "size_bytes": int(candidate.get("size_bytes", 0) or 0),
        "license": clean_text(candidate.get("license", ""), 300),
        "license_url": clean_text(candidate.get("license_url", ""), 500),
        "link_check": probe,
        "discovered_at": utc_now().isoformat(),
        "status": "DISCOVERED",
        "rights_verified": bool(candidate.get("license_url") or "public domain" in clean_text(candidate.get("license", ""), 300).lower()),
        "publication_approved": auto_approved,
    }


def supabase_headers() -> dict[str, str]:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY before running")
    return {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }


def database_snapshot(session: requests.Session | None = None) -> tuple[set[str], int]:
    """Read all agent records in pages; derive today's count from JSON, not runner-local state.

    The approved payment_events schema has no timestamp column, so discovered_at inside
    provider_payment_id is the only available date source. Missing/invalid timestamps do
    not count toward today's limit, but their video IDs still participate in deduplication.
    """
    client = session or SESSION
    headers = supabase_headers()
    headers["Range-Unit"] = "items"
    ids: set[str] = set()
    today_count = 0
    offset = 0
    page_size = 500
    today = utc_day()

    while True:
        headers["Range"] = f"{offset}-{offset + page_size - 1}"
        response = client.get(
            f"{SUPABASE_URL}/rest/v1/payment_events",
            headers=headers,
            params={"provider": f"eq.{AGENT_NAME}", "select": "provider_payment_id", "limit": str(page_size)},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("Supabase response must be a JSON array")

        for row in rows:
            try:
                payload = json.loads(row.get("provider_payment_id", ""))
                if not isinstance(payload, dict):
                    continue
                if payload.get("video_id"):
                    ids.add(str(payload["video_id"]))
                discovered_at = str(payload.get("discovered_at", ""))
                if discovered_at[:10] == today:
                    today_count += 1
            except (TypeError, json.JSONDecodeError):
                LOG.warning("Ignoring malformed prior provider_payment_id JSON")

        if len(rows) < page_size:
            break
        offset += len(rows)

    return ids, today_count


def recorded_video_ids(session: requests.Session | None = None) -> set[str]:
    """Compatibility wrapper for callers needing only the known video IDs."""
    ids, _ = database_snapshot(session)
    return ids


def insert_record(record: dict[str, Any], session: requests.Session | None = None) -> None:
    client = session or SESSION
    payload_text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # Validate serialization/parsing before the network write.
    parsed = json.loads(payload_text)
    if parsed.get("video_id") != record.get("video_id"):
        raise ValueError("JSON round-trip validation failed")
    response = client.post(
        f"{SUPABASE_URL}/rest/v1/payment_events",
        headers=supabase_headers(),
        json={"provider": AGENT_NAME, "provider_payment_id": payload_text},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()


def discover_candidates() -> Iterable[dict[str, Any]]:
    seen_urls: set[str] = set()
    for term in SEARCH_TERMS:
        for candidate in search_internet_archive(term):
            if candidate["video_url"] not in seen_urls:
                seen_urls.add(candidate["video_url"])
                yield candidate
        for candidate in search_wikimedia_commons(term):
            if candidate["video_url"] not in seen_urls:
                seen_urls.add(candidate["video_url"])
                yield candidate


def run_agent() -> dict[str, Any]:
    """Find up to three verified, nonduplicate candidates; never fake a daily quota."""
    headers = supabase_headers()  # fail fast if credentials are absent
    del headers
    state = load_state()
    db_ids, count = database_snapshot()
    processed = set(str(value) for value in state["processed_ids"])
    # The database is authoritative; GitHub-hosted runners are ephemeral and local state
    # must never reset or inflate the daily quota.
    processed.update(db_ids)
    added = 0

    if count >= DAILY_TARGET:
        LOG.info("Daily target already reached (%s/%s)", count, DAILY_TARGET)
        return {"status": "TARGET_ALREADY_REACHED", "added": 0, "daily_count": count, "target": DAILY_TARGET}

    for candidate in discover_candidates():
        if count >= DAILY_TARGET:
            break
        url = candidate.get("video_url", "")
        if not is_allowed_media_url(url):
            continue
        video_id = stable_video_id(url)
        if video_id in processed or video_id in db_ids:
            continue
        # Confirm the URL at the time of discovery before writing to Supabase.
        probe = check_video_url(url)
        if not probe.get("ok"):
            LOG.info("Skipping failed media URL (%s): %s", url, probe.get("reason"))
            continue
        record = make_record(candidate, probe)
        try:
            insert_record(record)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            LOG.error("Supabase insert failed for video %s: %s; stopping to avoid silent data loss", video_id[:12], type(exc).__name__)
            raise
        processed.add(video_id)
        db_ids.add(video_id)
        count += 1
        added += 1
        state.update({"day": utc_day(), "daily_count": count, "processed_ids": sorted(processed)})
        save_state(state)
        LOG.info("Recorded verified candidate %s/%s: %s", count, DAILY_TARGET, clean_text(candidate.get("title", ""), 100))
        time.sleep(0.5)

    state.update({"day": utc_day(), "daily_count": count, "processed_ids": sorted(processed)})
    save_state(state)
    status = "TARGET_REACHED" if count >= DAILY_TARGET else "TARGET_NOT_REACHED"
    report = {"status": status, "added": added, "daily_count": count, "target": DAILY_TARGET}
    if status != "TARGET_REACHED":
        LOG.warning("Daily target not reached: %s/%s verified records; no placeholders were inserted", count, DAILY_TARGET)
    return report


if __name__ == "__main__":
    try:
        result = run_agent()
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        LOG.error("Agent stopped: %s: %s", type(exc).__name__, exc)
        raise SystemExit(1)
