import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote

import requests

# حاجز الأمان: لا كتابة فعلية أثناء الاختبارات.
DRY_RUN = True

MAX_VIDEOS_PER_DAY = 3
REQUEST_TIMEOUT = 15

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
    "User-Agent": "AutonomousTrendIngestionAgent/14.0"
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

    return any(
        term in normalized
        for term in ACTION_TERMS
    )


def verify_mp4_url(identifier, filename):
    safe_id = quote(identifier, safe="")
    safe_name = quote(filename, safe="")

    url = DOWNLOAD_URL.format(
        safe_id,
        safe_name
    )

    try:
        with SESSION.get(
            url,
            headers={"Range": "bytes=0-63"},
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=True,
        ) as response:

            if response.status_code not in (200, 206):
                return None

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if not (
                content_type.startswith("video/")
                or content_type == "application/octet-stream"
            ):
                return None

            first_bytes = next(
                response.iter_content(chunk_size=64),
                b""
            )

            # فحص أولي لعلامة MP4.
            if b"ftyp" not in first_bytes[:32]:
                return None

            return url

    except requests.RequestException as exc:
        print(
            f"[URL_REJECT] {identifier}/{filename}: {exc}"
        )
        return None


def discover_videos():
    query = (
        'mediatype:movies AND '
        '(subject:action OR subject:thriller OR '
        'subject:stunts OR subject:chase OR '
        'subject:"martial arts") AND language:eng'
    )

    try:
        response = SESSION.get(
            SEARCH_URL,
            params={
                "q": query,
                "fl[]": ["identifier", "title"],
                "rows": 50,
                "output": "json",
                "page": 1,
            },
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()
        data = response.json()

        docs = data.get(
            "response", {}
        ).get("docs", [])

        if not isinstance(docs, list):
            raise ValueError(
                "Unexpected Internet Archive search format"
            )

    except (
        requests.RequestException,
        ValueError,
        TypeError,
    ) as exc:
        print(f"[SEARCH_ERROR] {exc}")
        return []

    found = []
    seen_ids = set()

    for doc in docs:
        if not isinstance(doc, dict):
            continue

        identifier = doc.get("identifier")
        title = str(
            doc.get("title") or "Untitled"
        ).strip()

        if (
            not isinstance(identifier, str)
            or not identifier.strip()
        ):
            continue

        identifier = identifier.strip()

        if identifier in seen_ids:
            continue

        # فلترة أولية بالعنوان، وليست ضمانًا لمحتوى الفيديو.
        if not is_action_title(title):
            continue

        seen_ids.add(identifier)

        try:
            metadata_response = SESSION.get(
                METADATA_URL.format(
                    quote(identifier, safe="")
                ),
                timeout=REQUEST_TIMEOUT,
            )

            metadata_response.raise_for_status()

            files = metadata_response.json().get(
                "files", []
            )

        except (
            requests.RequestException,
            ValueError,
            TypeError,
        ) as exc:
            print(
                f"[METADATA_REJECT] {identifier}: {exc}"
            )
            continue

        if not isinstance(files, list):
            continue

        candidates = []

        for item in files:
            if not isinstance(item, dict):
                continue

            filename = item.get("name")

            if (
                not isinstance(filename, str)
                or not filename.lower().endswith(".mp4")
            ):
                continue

            try:
                size = int(item.get("size", 0))

                if size <= 0:
                    continue

            except (ValueError, TypeError):
                continue

            candidates.append(filename)

        for filename in candidates:
            verified_url = verify_mp4_url(
                identifier,
                filename
            )

            if verified_url:
                found.append({
                    "video_url": verified_url,
                    "caption": title,
                })
                break

        if len(found) >= MAX_VIDEOS_PER_DAY:
            break

    print(f"[DISCOVERY] Candidates found: {len(found)}")

    return found


def get_today_rows(base_url, key):
    today_utc = (
        datetime.now(timezone.utc)
        .date()
        .isoformat()
        + "T00:00:00+00:00"
    )

    response = SESSION.get(
        f"{base_url}/rest/v1/generated_videos",
        headers=supabase_headers(key),
        params={
            "select": "video_url,created_at",
            "created_at": f"gte.{today_utc}",
            "order": "created_at.desc",
            "limit": "1000",
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()
    rows = response.json()

    if not isinstance(rows, list):
        raise ValueError(
            "Invalid generated_videos response"
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
        raise ValueError(
            "Invalid duplicate-check response"
        )

    return bool(rows)


def insert_video(base_url, key, video):
    # لا يُستدعى هذا الإجراء أثناء DRY_RUN.
    response = SESSION.post(
        f"{base_url}/rest/v1/generated_videos",
        headers=supabase_headers(
            key,
            "return=representation"
        ),
        json={
            "video_url": video["video_url"],
            "caption": video["caption"][:2200],
            "status": "ready_to_publish",
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    return response.json()


def main():
    base_url = required_env("SUPABASE_URL")
    key = required_env("SUPABASE_SERVICE_ROLE_KEY")

    print(
        f"[START] Trend Agent V14 | DRY_RUN={DRY_RUN}"
    )

    try:
        today_rows = get_today_rows(
            base_url,
            key
        )

        daily_count = len(today_rows)

        print(
            f"[DAILY_COUNT] rows_today={daily_count}"
        )

        if daily_count >= MAX_VIDEOS_PER_DAY:
            print(
                "[LIMIT] Three videos already recorded today."
            )
            return 0

        candidates = discover_videos()

        if not candidates:
            print(
                "[INFO] No qualifying candidates found."
            )
            return 0

        processed = 0

        for video in candidates:
            if daily_count + processed >= MAX_VIDEOS_PER_DAY:
                break

            url = video["video_url"]

            if video_exists(base_url, key, url):
                print("[DUPLICATE] Skipping existing URL.")
                continue

            if DRY_RUN:
                print(
                    "[DRY_RUN_PREVIEW] Would insert:",
                    url
                )
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
        print(
            f"[ERROR] {type(exc).__name__}: {exc}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
