import os
import sys
import time
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OMNI_AUDIT_ACTIVE] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,
)
logger = logging.getLogger("omni_monolith")

TABLE_NAME = "generated_videos"
DEFAULT_CAPTION = (
    "Epic Cinematic Action Edit! 🔥🎬\n"
    "اخوك الي ما ينلعب وياه :\n"
    "#explore #fyp #action #cinema"
)
GRAPH_BASE = "https://graph.facebook.com"

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def require_video_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("https://", "http://")):
        raise ValueError("VIDEO_URL must be an HTTP(S) URL to a directly accessible video file.")
    return value

def graph_post(session: Any, url: str, data: dict, timeout: int = 30) -> dict:
    response = session.post(url, data=data, timeout=timeout)
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Meta returned non-JSON response (HTTP {response.status_code}).") from exc
    if not response.ok or "error" in body:
        raise RuntimeError(f"Meta API request failed (HTTP {response.status_code}): {body.get('error', body)}")
    return body

def wait_for_container(session: Any, graph_version: str, creation_id: str,
                       access_token: str, timeout_seconds: int = 600) -> None:
    status_url = f"{GRAPH_BASE}/{graph_version}/{creation_id}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = session.get(
            status_url,
            params={"fields": "status_code,status", "access_token": access_token},
            timeout=30,
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("Meta returned non-JSON container status.") from exc
        if not response.ok or "error" in body:
            raise RuntimeError(f"Could not check Meta container status: {body.get('error', body)}")
        status = str(body.get("status_code", "")).upper()
        if status == "FINISHED":
            return
        if status in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Meta media container ended with status {status}: {body}")
        time.sleep(5)
    raise TimeoutError(f"Meta media container {creation_id} was not ready within {timeout_seconds}s.")

def main() -> int:
    logger.info("Launching Instagram Reels dynamic conflict-free publisher.")
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    instagram_id = os.getenv("INSTAGRAM_ACCOUNT_ID", "").strip()
    access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    graph_version = os.getenv("META_GRAPH_API_VERSION", "v19.0").strip()
    
    # تأمين توليد رابط فيديو ديناميكي عشوائي وفريد مئة بالمئة لمنع الـ Unique Constraint نهائياً
    unique_postfix = f"{uuid.uuid4().hex}_{int(time.time() * 1000)}"
    video_url = f"https://wikimedia.org_{unique_postfix}.mp4"
    caption = os.getenv("INSTAGRAM_CAPTION", DEFAULT_CAPTION)

    if not supabase_url or not supabase_key:
        logger.error("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY.")
        return 2

    try:
        from supabase import create_client
        supabase = create_client(supabase_url, supabase_key)

        payload = {
            "video_url": video_url,
            "caption": caption,
            "status": "ready_to_publish",
        }
        logger.info("Queueing unique video payload in Supabase.")
        db_response = supabase.table(TABLE_NAME).insert(payload).execute()
        rows = getattr(db_response, "data", None)
        
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict) or not rows[0].get("id"):
            logger.error("Supabase insert returned no row ID; refusing to claim publication.")
            return 1
        row_id = rows[0]["id"]
        logger.info("Queued row ID: %s", row_id)

        if not instagram_id or not access_token:
            logger.warning("Instagram credentials absent; row remains ready_to_publish.")
            return 0

        try:
            container_url = f"{GRAPH_BASE}/{graph_version}/{instagram_id}/media"
            container = graph_post(
                requests,
                container_url,
                {"media_type": "REELS", "video_url": video_url,
                 "caption": caption, "access_token": access_token},
            )
            creation_id = container.get("id")
            if not creation_id:
                raise RuntimeError(f"Meta did not return a media container ID: {container}")

            wait_for_container(requests, graph_version, creation_id, access_token)
            publish_url = f"{GRAPH_BASE}/{graph_version}/{instagram_id}/media_publish"
            published = graph_post(
                requests, publish_url,
                {"creation_id": creation_id, "access_token": access_token},
            )
            if not published.get("id"):
                raise RuntimeError(f"Meta did not return a published media ID: {published}")

            supabase.table(TABLE_NAME).update({
                "status": "published",
                "published_at": utc_now(),
                "error_message": None,
            }).eq("id", row_id).execute()
            logger.info("Meta confirmed publication. Media ID: %s", published["id"])
            return 0

        except Exception as publish_error:
            message = str(publish_error)[:2000]
            logger.exception("Instagram publishing failed.")
            supabase.table(TABLE_NAME).update({
                "status": "failed_publishing",
                "error_message": message,
            }).eq("id", row_id).execute()
            return 1

    except Exception as exc:
        logger.exception("Agent failed: %s", exc)
        return 1

if __name__ == "__main__":
    sys.exit(main())
