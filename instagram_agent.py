# instagram_agent.py
# Instagram Reels Publishing Agent
# Supabase queue: public.generated_videos
# Meta Graph API: official endpoint structure
# DRY_RUN defaults to True.

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from supabase import Client, create_client
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURATION
# ============================================================

AGENT_VERSION = "2.1.0"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY", ""
).strip()

INSTAGRAM_ACCOUNT_ID = os.getenv(
    "INSTAGRAM_ACCOUNT_ID", ""
).strip()

INSTAGRAM_ACCESS_TOKEN = os.getenv(
    "INSTAGRAM_ACCESS_TOKEN", ""
).strip()

GRAPH_API_VERSION = os.getenv(
    "GRAPH_API_VERSION", "v23.0"
).strip()

DRY_RUN = os.getenv(
    "DRY_RUN", "true"
).strip().lower() in {"1", "true", "yes", "on"}

MAX_RECORDS_PER_RUN = int(
    os.getenv("MAX_RECORDS_PER_RUN", "3")
)

REQUEST_TIMEOUT = 30
MAX_PROCESSING_WAIT_SECONDS = 180
PROCESSING_POLL_INTERVAL_SECONDS = 5

TABLE_NAME = "generated_videos"

STATUS_READY = "ready_to_publish"
STATUS_PUBLISHING = "publishing"
STATUS_PUBLISHED = "published"
STATUS_FAILED_VALIDATION = "failed_validation"
STATUS_FAILED_PUBLISHING = "failed_publishing"

VALID_STATUSES = {
    STATUS_READY,
    STATUS_PUBLISHING,
    STATUS_PUBLISHED,
    STATUS_FAILED_VALIDATION,
    STATUS_FAILED_PUBLISHING,
}

# Official Meta Graph API URL structure.
# The account ID belongs after the API version and slash.
GRAPH_API_BASE = (
    f"https://graph.facebook.com/{GRAPH_API_VERSION}"
)

MEDIA_CONTAINER_URL = (
    f"https://graph.facebook.com/"
    f"{GRAPH_API_VERSION}/"
    f"{INSTAGRAM_ACCOUNT_ID}/media"
)

MEDIA_PUBLISH_URL = (
    f"https://graph.facebook.com/"
    f"{GRAPH_API_VERSION}/"
    f"{INSTAGRAM_ACCOUNT_ID}/media_publish"
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("instagram_agent")


# ============================================================
# HTTP SESSION
# ============================================================

def create_http_session() -> requests.Session:
    session = requests.Session()

    retry_policy = Retry(
        total=3,
        connect=3,
        read=2,
        status=2,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry_policy)

    session.mount("https://", adapter)

    session.headers.update({
        "User-Agent": f"InstagramReelsAgent/{AGENT_VERSION}",
        "Accept": "application/json",
    })

    return session


HTTP = create_http_session()


# ============================================================
# CONFIGURATION VALIDATION
# ============================================================

def validate_configuration() -> None:
    if MAX_RECORDS_PER_RUN < 1:
        raise ValueError(
            "MAX_RECORDS_PER_RUN must be greater than zero."
        )

    if not GRAPH_API_VERSION.startswith("v"):
        raise ValueError(
            "GRAPH_API_VERSION must start with 'v'."
        )

    if not DRY_RUN:
        required_variables = {
            "SUPABASE_URL": SUPABASE_URL,
            "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_SERVICE_ROLE_KEY,
            "INSTAGRAM_ACCOUNT_ID": INSTAGRAM_ACCOUNT_ID,
            "INSTAGRAM_ACCESS_TOKEN": INSTAGRAM_ACCESS_TOKEN,
        }

        missing = [
            name
            for name, value in required_variables.items()
            if not value
        ]

        if missing:
            raise RuntimeError(
                "Missing required environment variables: "
                + ", ".join(missing)
            )

    if not DRY_RUN:
        if not INSTAGRAM_ACCOUNT_ID.isdigit():
            raise ValueError(
                "INSTAGRAM_ACCOUNT_ID must be a numeric account ID."
            )


# ============================================================
# SUPABASE
# ============================================================

def initialize_supabase() -> Client:
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is missing.")

    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is missing."
        )

    return create_client(
        SUPABASE_URL,
        SUPABASE_SERVICE_ROLE_KEY,
    )


def fetch_ready_videos(
    supabase: Client,
) -> list[Dict[str, Any]]:
    response = (
        supabase.table(TABLE_NAME)
        .select(
            "id,video_url,caption,status,created_at"
        )
        .eq("status", STATUS_READY)
        .order("created_at", desc=False)
        .limit(MAX_RECORDS_PER_RUN)
        .execute()
    )

    records = response.data

    if not isinstance(records, list):
        raise RuntimeError(
            "Supabase returned an invalid queue response."
        )

    return records


def update_video_status(
    supabase: Client,
    video_id: str,
    status: str,
    error_message: Optional[str] = None,
    published_at: Optional[str] = None,
) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid video status: {status}"
        )

    payload: Dict[str, Any] = {
        "status": status,
        "error_message": error_message,
    }

    if published_at is not None:
        payload["published_at"] = published_at

    response = (
        supabase.table(TABLE_NAME)
        .update(payload)
        .eq("id", video_id)
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "Supabase did not confirm the status update "
            f"for video {video_id}."
        )


# ============================================================
# API ERROR HANDLING
# ============================================================

def parse_response_json(
    response: requests.Response,
) -> Dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {
            "raw_response": response.text[:10000]
        }

    if not isinstance(body, dict):
        raise RuntimeError(
            "API returned a response that is not a JSON object."
        )

    return body


def graph_api_request(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    response = HTTP.request(
        method=method,
        url=url,
        params=params,
        data=payload,
        timeout=REQUEST_TIMEOUT,
    )

    body = parse_response_json(response)

    if not response.ok:
        logger.error(
            "Meta API HTTP failure: status=%s body=%s",
            response.status_code,
            body,
        )

        raise RuntimeError(
            f"Meta API HTTP {response.status_code}: {body}"
        )

    if "error" in body:
        logger.error("Meta API error response: %s", body)

        raise RuntimeError(
            f"Meta API returned an error: {body}"
        )

    return body


# ============================================================
# RECORD VALIDATION
# ============================================================

def validate_video_record(
    record: Dict[str, Any],
) -> Optional[str]:
    video_url = record.get("video_url")
    caption = record.get("caption")

    if not isinstance(video_url, str) or not video_url.strip():
        return "Missing video_url."

    video_url = video_url.strip()

    if not video_url.startswith("https://"):
        return "video_url must use HTTPS."

    if any(character.isspace() for character in video_url):
        return "video_url contains whitespace."

    if caption is not None and not isinstance(caption, str):
        return "caption must be a string or null."

    if not record.get("id"):
        return "Missing database record ID."

    if record.get("status") != STATUS_READY:
        return "Record is not ready to publish."

    return None


# ============================================================
# MEDIA CONTAINER CREATION
# ============================================================

def create_media_container(
    video_url: str,
    caption: str,
) -> str:
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true",
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    }

    result = graph_api_request(
        "POST",
        MEDIA_CONTAINER_URL,
        payload=payload,
    )

    container_id = result.get("id")

    if not container_id:
        raise RuntimeError(
            "Meta did not return a media container ID: "
            f"{result}"
        )

    return str(container_id)


# ============================================================
# MEDIA PROCESSING STATUS
# ============================================================

def wait_for_media_processing(
    container_id: str,
) -> None:
    deadline = (
        time.monotonic()
        + MAX_PROCESSING_WAIT_SECONDS
    )

    while time.monotonic() < deadline:
        result = graph_api_request(
            "GET",
            f"{GRAPH_API_BASE}/{container_id}",
            params={
                "fields": "status_code,status",
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
        )

        status_code = result.get("status_code")

        if status_code == "FINISHED":
            logger.info(
                "Media container %s is ready.",
                container_id,
            )
            return

        if status_code in {"ERROR", "EXPIRED"}:
            raise RuntimeError(
                "Media processing failed: "
                f"{result}"
            )

        logger.info(
            "Container %s processing status: %s",
            container_id,
            status_code,
        )

        time.sleep(PROCESSING_POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        "Timed out waiting for media processing: "
        f"{container_id}"
    )


# ============================================================
# LIVE PUBLICATION
# ============================================================

def publish_media_container(
    container_id: str,
) -> str:
    payload = {
        "creation_id": container_id,
        "access_token": INSTAGRAM_ACCESS_TOKEN,
    }

    result = graph_api_request(
        "POST",
        MEDIA_PUBLISH_URL,
        payload=payload,
    )

    published_media_id = result.get("id")

    if not published_media_id:
        raise RuntimeError(
            "Meta did not return a published media ID: "
            f"{result}"
        )

    return str(published_media_id)


# ============================================================
# SINGLE RECORD WORKFLOW
# ============================================================

def process_video(
    supabase: Client,
    record: Dict[str, Any],
) -> None:
    video_id = record.get("id")

    if not video_id:
        raise RuntimeError(
            f"Queue record has no ID: {record}"
        )

    validation_error = validate_video_record(record)

    if validation_error:
        logger.error(
            "Video %s failed validation: %s",
            video_id,
            validation_error,
        )

        if not DRY_RUN:
            update_video_status(
                supabase,
                str(video_id),
                STATUS_FAILED_VALIDATION,
                error_message=validation_error,
            )

        return

    video_url = record["video_url"].strip()
    caption = (record.get("caption") or "").strip()

    if DRY_RUN:
        logger.info(
            "DRY_RUN: record=%s passed basic validation. "
            "No API publication or database updates performed.",
            video_id,
        )
        return

    update_video_status(
        supabase,
        str(video_id),
        STATUS_PUBLISHING,
        error_message=None,
    )

    container_id = create_media_container(
        video_url,
        caption,
    )

    logger.info(
        "Media container created: record=%s container=%s",
        video_id,
        container_id,
    )

    wait_for_media_processing(container_id)

    published_media_id = publish_media_container(
        container_id
    )

    published_at = datetime.now(
        timezone.utc
    ).isoformat()

    update_video_status(
        supabase,
        str(video_id),
        STATUS_PUBLISHED,
        error_message=None,
        published_at=published_at,
    )

    logger.info(
        "Publication confirmed by Meta API: "
        "record=%s media_id=%s published_at=%s",
        video_id,
        published_media_id,
        published_at,
    )


# ============================================================
# MAIN EXECUTION
# ============================================================

def main() -> int:
    logger.info(
        "Instagram Agent %s starting. DRY_RUN=%s",
        AGENT_VERSION,
        DRY_RUN,
    )

    try:
        validate_configuration()

        supabase = initialize_supabase()

        records = fetch_ready_videos(supabase)

        logger.info(
            "Queue fetched successfully: %s records.",
            len(records),
        )

        for record in records:
            process_video(supabase, record)

        logger.info("Instagram Agent finished successfully.")
        return 0

    except Exception:
        logger.exception(
            "FATAL: Instagram Agent execution failed."
        )
        return 1

    finally:
        HTTP.close()


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)

