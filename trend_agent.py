#!/usr/bin/env python3
"""
Trend & Ingestion Agent
Python 3.10+

وظائف:
- البحث عن مقاطع أكشن أجنبية باستخدام Pexels API.
- اختيار 3 مقاطع يومياً كحد أقصى.
- تنظيف الروابط والتحقق من بنيتها.
- منع التكرار محلياً وبالاستعلام عن Supabase.
- تخزين بيانات الفيديو كـ JSON داخل provider_payment_id.
- استخدام provider مخصص لتمييز سجلات الفيديو.

ملاحظة:
استخدام payment_events لتخزين الفيديو حل مؤقت غير مثالي.
لا يكتب السكربت فوق سجلات الدفع الموجودة.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)

import requests


# =========================================================
# CONFIGURATION
# =========================================================

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY", ""
).strip()

TABLE = "payment_events"

# علامة مستقلة حتى لا تختلط سجلات الوكيل بسجلات الدفع.
PROVIDER_NAME = "trend_agent_video_v1"

DAILY_LIMIT = 3
SEARCH_PER_PAGE = 30
MAX_PAGES = 3
REQUEST_TIMEOUT = 25

STATE_FILE = Path(
    os.getenv("TREND_AGENT_STATE_FILE", "trend_agent_state.json")
)

PEXELS_SEARCH_URL = "https://api.pexels.com/videos/search"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "TrendIngestionAgent/1.0"
})

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("trend_agent")


# =========================================================
# SEARCH SCOPE
# =========================================================

SEARCH_QUERIES = [
    "cinematic action",
    "action movie cinematic",
    "high speed car chase",
    "martial arts action",
    "extreme sports action",
    "cinematic stunt",
]

# كلمات استبعاد للمحتوى الذي لا يطابق نطاق البحث.
EXCLUDE_TERMS = {
    "tutorial", "course", "lecture", "podcast",
    "product review", "advertisement", "unboxing",
    "news report", "political speech",
}

# كلمات داعمة لتصنيف المحتوى.
ACTION_TERMS = {
    "action", "cinematic", "chase", "stunt",
    "martial arts", "fight scene", "adrenaline",
    "extreme sports", "car chase", "parkour",
    "explosion", "thriller", "high speed",
}


# =========================================================
# VALIDATION
# =========================================================

def validate_config() -> None:
    missing = []

    if not PEXELS_API_KEY:
        missing.append("PEXELS_API_KEY")

    if not SUPABASE_URL:
        missing.append("SUPABASE_URL")

    if not SUPABASE_SERVICE_ROLE_KEY:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")

    if missing:
        raise RuntimeError(
            "متغيرات البيئة الناقصة: " + ", ".join(missing)
        )

    parsed = urlsplit(SUPABASE_URL)

    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(
            "SUPABASE_URL يجب أن يكون عنوان HTTPS صالحاً."
        )


def clean_url(url: str) -> str | None:
    """
    تنظيف الرابط دون الادعاء أن التنظيف يصلح رابطاً معطوباً.
    لا نتبع التحويلات تلقائياً هنا.
    """
    if not isinstance(url, str):
        return None

    url = url.strip()

    if not url or any(ord(c) < 32 for c in url):
        return None

    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    if parts.scheme.lower() != "https":
        return None

    if not parts.hostname:
        return None

    # لا نقبل بيانات دخول مضمنة داخل الرابط.
    if parts.username or parts.password:
        return None

    host = parts.hostname.lower()

    # لا نقبل إلا نطاقات Pexels المعروفة للفيديو.
    allowed = (
        host == "pexels.com"
        or host.endswith(".pexels.com")
    )

    if not allowed:
        return None

    # إزالة معاملات التتبع فقط.
    tracking_keys = {
        "utm_source", "utm_medium", "utm_campaign",
        "utm_term", "utm_content", "fbclid", "gclid",
    }

    query = [
        (k, v)
        for k, v in parse_qsl(
            parts.query,
            keep_blank_values=True,
        )
        if k.lower() not in tracking_keys
    ]

    return urlunsplit((
        "https",
        parts.netloc.lower(),
        parts.path.rstrip("/"),
        urlencode(query),
        "",
    ))


def is_action_candidate(video: dict) -> bool:
    """
    يعتمد على البيانات الوصفية المتاحة.
    هذا تصنيف نصي أولي وليس فهماً بصرياً مضموناً للمقطع.
    """
    text = " ".join([
        str(video.get("url", "")),
        str(video.get("user", {}).get("name", "")),
        str(video.get("alt", "")),
    ]).lower()

    if any(term in text for term in EXCLUDE_TERMS):
        return False

    return any(term in text for term in ACTION_TERMS)


def video_id(video: dict, clean_page_url: str) -> str:
    """
    استخدام معرف Pexels الأصلي متى توفر.
    """
    original_id = video.get("id")

    if original_id is not None:
        return f"pexels:{original_id}"

    digest = hashlib.sha256(
        clean_page_url.encode("utf-8")
    ).hexdigest()

    return f"pexels:url:{digest}"


# =========================================================
# LOCAL STATE
# =========================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "date": date.today().isoformat(),
            "accepted_ids": [],
        }

    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(state, dict):
            raise ValueError("Invalid state")

        return state

    except (OSError, ValueError, json.JSONDecodeError):
        log.warning(
            "تعذر قراءة ملف الحالة؛ ستتم إعادة بنائه."
        )

        return {
            "date": date.today().isoformat(),
            "accepted_ids": [],
        }


def save_state(state: dict) -> None:
    temp_file = STATE_FILE.with_suffix(".tmp")

    temp_file.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp_file.replace(STATE_FILE)


# =========================================================
# PEXELS SEARCH
# =========================================================

def search_pexels(query: str, page: int) -> list[dict]:
    headers = {
        "Authorization": PEXELS_API_KEY,
    }

    params = {
        "query": query,
        "per_page": SEARCH_PER_PAGE,
        "page": page,
        "locale": "en-US",
    }

    response = SESSION.get(
        PEXELS_SEARCH_URL,
        headers=headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code == 429:
        raise RuntimeError(
            "Pexels rate limit: أوقف التشغيل لتجنب تجاوز الحدود."
        )

    response.raise_for_status()

    payload = response.json()
    videos = payload.get("videos", [])

    if not isinstance(videos, list):
        raise RuntimeError("استجابة Pexels غير متوقعة.")

    return videos


def build_candidates() -> list[dict]:
    candidates = []
    seen = set()

    for query in SEARCH_QUERIES:
        for page in range(1, MAX_PAGES + 1):
            try:
                results = search_pexels(query, page)
            except requests.RequestException as exc:
                log.error(
                    "فشل البحث عن %r: %s",
                    query,
                    exc,
                )
                break

            for video in results:
                page_url = clean_url(video.get("url", ""))

                if not page_url:
                    continue

                if not is_action_candidate(video):
                    continue

                identifier = video_id(video, page_url)

                if identifier in seen:
                    continue

                seen.add(identifier)

                files = video.get("video_files", [])
                valid_files = [
                    f for f in files
                    if isinstance(f, dict)
                    and clean_url(f.get("link", ""))
                ]

                # لا نقبل مقطعاً لا يحتوي على ملف فيديو صالح.
                if not valid_files:
                    continue

                # اختيار ملف ذي دقة معروفة إن أمكن.
                valid_files.sort(
                    key=lambda f: (
                        int(f.get("width") or 0),
                        int(f.get("height") or 0),
                    ),
                    reverse=True,
                )

                best_file = valid_files[0]
                file_url = clean_url(best_file.get("link", ""))

                if not file_url:
                    continue

                candidates.append({
                    "video_id": identifier,
                    "source": "pexels",
                    "page_url": page_url,
                    "video_url": file_url,
                    "caption": (
                        str(video.get("alt") or "").strip()
                        or f"Cinematic action video from Pexels"
                    ),
                    "duration_seconds": video.get("duration"),
                    "width": best_file.get("width"),
                    "height": best_file.get("height"),
                    "creator": (
                        video.get("user", {}).get("name")
                    ),
                    "discovered_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                })

            # لا نواصل الصفحات إذا لم تعد هناك نتائج.
            if not results or len(results) < SEARCH_PER_PAGE:
                break

            time.sleep(0.25)

    return candidates


# =========================================================
# SUPABASE
# =========================================================

def supabase_headers(prefer: str | None = None) -> dict:
    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": (
            f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"
        ),
        "Content-Type": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    return headers


def supabase_get_existing_ids() -> set[str]:
    """
    قراءة سجلات الوكيل فقط.
    لا يقرأ جميع سجلات الدفع ولا يعدّلها.
    """
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}"

    params = {
        "select": "provider_payment_id",
        "provider": f"eq.{PROVIDER_NAME}",
        "limit": "1000",
    }

    response = SESSION.get(
        url,
        headers=supabase_headers(),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    records = response.json()

    existing = set()

    for row in records:
        raw = row.get("provider_payment_id")

        if not isinstance(raw, str):
            continue

        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue

        if isinstance(payload, dict):
            identifier = payload.get("video_id")

            if identifier:
                existing.add(str(identifier))

    return existing


def supabase_insert_video(video: dict) -> bool:
    """
    لا يستخدم upsert حتى لا يستبدل سجلاً سابقاً.
    يفترض أن id يُولّد تلقائياً في قاعدة البيانات.
    """
    url = f"{SUPABASE_URL}/rest/v1/{TABLE}"

    serialized = json.dumps(
        video,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    record = {
        "provider": PROVIDER_NAME,
        "provider_payment_id": serialized,
    }

    response = SESSION.post(
        url,
        headers=supabase_headers("return=minimal"),
        json=record,
        timeout=REQUEST_TIMEOUT,
    )

    if response.status_code in (200, 201, 204):
        return True

    # لا نعتبر أي خطأ نجاحاً، ولا نكرر الإدراج تلقائياً.
    log.error(
        "فشل إدراج %s: HTTP %s — %s",
        video.get("video_id"),
        response.status_code,
        response.text[:500],
    )

    response.raise_for_status()
    return False


# =========================================================
# MAIN DAILY RUN
# =========================================================

def run_agent() -> int:
    validate_config()

    today = date.today().isoformat()
    state = load_state()

    # عدّاد يومي محلي؛ لا يُعدّ بديلاً عن قفل ذري مشترك.
    if state.get("date") != today:
        state = {
            "date": today,
            "accepted_ids": [],
        }

    accepted_ids = set(state.get("accepted_ids", []))

    if len(accepted_ids) >= DAILY_LIMIT:
        log.info(
            "اكتمل الحد اليومي: %s/%s",
            len(accepted_ids),
            DAILY_LIMIT,
        )
        return 0

    existing_ids = supabase_get_existing_ids()

    # نحتسب السجلات المسجلة اليوم محلياً فقط.
    # إذا حُذف ملف الحالة، لا يمكن ضمان عدّ تاريخ الإدراج
    # بدقة لأن الجدول لا يحتوي على created_at في المخطط المعلن.
    remaining = DAILY_LIMIT - len(accepted_ids)

    candidates = build_candidates()

    inserted = 0

    for candidate in candidates:
        identifier = candidate["video_id"]

        if identifier in existing_ids:
            continue

        if identifier in accepted_ids:
            continue

        if inserted >= remaining:
            break

        try:
            success = supabase_insert_video(candidate)
        except requests.RequestException as exc:
            log.error(
                "توقف الإدراج عند %s: %s",
                identifier,
                exc,
            )
            break

        if success:
            accepted_ids.add(identifier)
            existing_ids.add(identifier)
            inserted += 1

            state = {
                "date": today,
                "accepted_ids": sorted(accepted_ids),
            }
            save_state(state)

            log.info(
                "تم الإدراج: %s",
                identifier,
            )

    log.info(
        "انتهى التشغيل: أُدرج %s، والحد اليومي %s.",
        inserted,
        DAILY_LIMIT,
    )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(run_agent())
    except Exception as exc:
        log.exception("فشل الوكيل: %s", exc)
        sys.exit(1)
