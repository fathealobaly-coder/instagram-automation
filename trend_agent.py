import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote

import requests

# وضع آمن: لا كتابة فعلية إلى Supabase.
DRY_RUN = True

MAX_VIDEOS_PER_DAY = 3
REQUEST_TIMEOUT = 15
MAX_SEARCH_RESULTS = 50
MAX_DB_ROWS = 1000

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{}"
DOWNLOAD_URL = "https://archive.org/download/{}/{}"

ACTION_TERMS = (
    "action",
    "stunt",
    "chase",
    "martial arts",
    "fight",
    "adventure",
    "thriller",
    "car chase",
    "kung fu",
    "boxing",
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "AutonomousTrendIngestionAgent/15.0",
})


def required_env(name):
    value = os.getenv(name, "").strip()

    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )

    if name == "SUPABASE_URL":
        return value.rstrip("/")

    return value


def supabase_headers(key, prefer=None):
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }

    if prefer:
        headers["Prefer"] = prefer

    return headers


def is_action_title(title):
    normalized = str(title or "").lower()

    return any(term in normalized for term in ACTION_TERMS)


def verify_mp4_url(identifier, filename):
    """
    فحص أولي لرابط ملف MP4:
    - حالة HTTP مقبولة.
    - نوع محتوى فيديو مناسب.
    - وجود علامة ftyp في بداية الملف.
    """
    safe_id = quote(identifier, safe="")
    safe_name = quote(filename, safe="")

    url = DOWNLOAD_URL.format(safe_id, safe_name)

    try:
        with SESSION.get(
            url,
            headers={"Range": "bytes=0-1023"},
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
        ) as response:

            if response.status_code not in (200, 206):
                print(
                    f"[URL_REJECT] HTTP {response.status_code}: {url}"
                )
                return None

            content_type = response.headers.get(
                "Content-Type", ""
            ).split(";")[0].strip().lower()

            if content_type not in (
                "video/mp4",
                "application/octet-stream",
            ):
                print(
                    f"[URL_REJECT] Unsupported content type: "
                    f"{content_type}"
                )
                return None

            first_bytes = next(
                response.iter_content(chunk_size=32),
                b"",
            )

            if len(first_bytes) < 8 or first_bytes[4:8] != b"ftyp":
                print("[URL_REJECT] MP4 signature not found.")
                return None

            return response.url

    except requests.RequestException as exc:
        print(f"[URL_ERROR] {identifier}/{filename}: {exc}")
        return None


def discover_videos():
    query = (
        "mediatype:movies AND "
        "(subject:action OR subject:thriller OR "
        "subject:stunts OR subject:chase OR "
        'subject:"martial arts") AND language:eng'
    )

    try:
        response = SESSION.get(
            SEARCH_URL,
            params=[
                ("q", query),
                ("fl[]", "identifier"),
                ("fl[]", "title"),
                ("rows", str(MAX_SEARCH_RESULTS)),
                ("output", "json"),
                ("page", "1"),
            ],
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        docs = data.get("response", {}).get("docs", [])

        if not isinstance(docs, list):
            raise ValueError("Unexpected Internet Archive response")

    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"[SEARCH_ERROR] {exc}")
        return []

    found = []
    seen_ids = set()
    seen_urls = set()

    for doc in docs:
        if not isinstance(doc, dict):
            continue

        identifier = doc.get("identifier")
        title = str(doc.get("title") or "").strip()

        if not isinstance(identifier, str) or not identifier.strip():
            continue

        identifier = identifier.strip()

        if identifier in seen_ids:
            continue

        if not is_action_title(title):
            continue

        seen_ids.add(identifier)

        try:
            metadata_response = SESSION.get(
                METADATA_URL.format(quote(identifier, safe="")),
                timeout=REQUEST_TIMEOUT,
            )
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            files = metadata.get("files", [])

        except (requests.RequestException, ValueError, TypeError) as exc:
            print(f"[METADATA_ERROR] {identifier}: {exc}")
            continue

        if not isinstance(files, list):
            continue

        for item in files:
            if not isinstance(item, dict):
                continue

            filename = item.get("name")

            if not isinstance(filename, str):
                continue

            if not filename.lower().endswith(".mp4"):
                continue

            try:
                size = int(item.get("size", 0))
            except (ValueError, TypeError):
                continue

            if size <= 0:
                continue

            video_url = verify_mp4_url(identifier, filename)

            if not video_url or video_url in seen_urls:
                continue

            seen_urls.add(video_url)

            found.append({
                "video_url": video_url,
                "caption": title[:2200],
            })

            # جمع مرشحين محدود، مع ترك تطبيق الحد اليومي لـ main().
            if len(found) >= MAX_VIDEOS_PER_DAY:
                print(
                    f"[DISCOVERY] Candidates found: {len(found)}"
                )
                return found

    print(f"[DISCOVERY] Candidates found: {len(found)}")
    return found


def get_today_rows(base_url, key):
    today = datetime.now(timezone.utc).date()

    start = datetime.combine(
        today,
        datetime.min.time(),
        tzinfo=timezone.utc,
    ).isoformat()

    end = datetime.combine(
        today,
        datetime.max.time(),
        tzinfo=timezone.utc,
    ).isoformat()

    response = SESSION.get(
        f"{base_url}/rest/v1/generated_videos",
        headers=supabase_headers(key),
        params={
            "select": "id,video_url,created_at",
            "created_at": f"gte.{start}",
            "and": f"(created_at.lte.{end})",
            "limit": str(MAX_DB_ROWS),
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    rows = response.json()

    if not isinstance(rows, list):
        raise ValueError("Invalid generated_videos response")

    if len(rows) >= MAX_DB_ROWS:
        raise RuntimeError(
            "Daily count may be incomplete; refusing to proceed safely."
        )

    return rows


def video_exists(base_url, key, video_url):
    response = SESSION.get(
        f"{base_url}/rest/v1/generated_videos",
        headers=supabase_headers(key),
        params={
            "select": "id",
            "video_url": f"eq.{video_url}",
            "limit": "1",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    rows = response.json()

    if not isinstance(rows, list):
        raise ValueError("Invalid duplicate-check response")

    return bool(rows)


def insert_video(base_url, key, video):
    if DRY_RUN:
        raise RuntimeError("Safety block: insert attempted in DRY_RUN")

    response = SESSION.post(
        f"{base_url}/rest/v1/generated_videos",
        headers=supabase_headers(
            key,
            "return=representation",
        ),
        json={
            "video_url": video["video_url"],
            "caption": video["caption"],
            "status": "ready_to_publish",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    result = response.json()

    if not isinstance(result, list) or not result:
        raise RuntimeError(
            "Insert response did not confirm a returned row."
        )

    return result


def main():
    base_url = required_env("SUPABASE_URL")
    key = required_env("SUPABASE_SERVICE_ROLE_KEY")

    print(
        f"[START] Trend Agent V15 | DRY_RUN={DRY_RUN}"
    )

    try:
        today_rows = get_today_rows(base_url, key)
        daily_count = len(today_rows)

        print(
            f"[DAILY_COUNT] rows_today={daily_count}"
        )

        if daily_count >= MAX_VIDEOS_PER_DAY:
            print("[LIMIT] Daily limit reached.")
            return 0

        remaining = MAX_VIDEOS_PER_DAY - daily_count
        candidates = discover_videos()

        if not candidates:
            print("[INFO] No qualifying candidates found.")
            return 0

        processed = 0

        for video in candidates:
            if processed >= remaining:
                break

            url = video["video_url"]

            if video_exists(base_url, key, url):
                print("[DUPLICATE] Skipping existing URL.")
                continue

            if DRY_RUN:
                print("[DRY_RUN_PREVIEW] Would insert:", url)
                processed += 1
                continue

            insert_video(base_url, key, video)
            print("[INSERTED]", url)
            processed += 1

        print(
            f"[SUMMARY] processed={processed}; "
            f"database_writes={0 if DRY_RUN else processed}"
        )

        return 0

    except (
        requests.RequestException,
        RuntimeError,
        ValueError,
        KeyError,
    ) as exc:
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        return 1

    finally:
        SESSION.close()


if __name__ == "__main__":
    sys.exit(main())
