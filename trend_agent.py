import os
import sys
import gc
import urllib.parse
import requests

from supabase import create_client, Client

# حاجز الأمان: يمنع الكتابة إلى قاعدة البيانات أثناء الاختبارات.
DRY_RUN = True

ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata/{}"
ARCHIVE_DOWNLOAD_URL = "https://archive.org/download/{}/{}"

MAX_VIDEOS_PER_RUN = 3
REQUEST_TIMEOUT = 15
PROVIDER_NAME = "archive_real_action_v13"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "AutonomousTrendIngestionAgent/13.0"
})


def get_verified_mp4_url(identifier: str):
    """
    العثور على ملف MP4 في Internet Archive والتحقق من استجابة الرابط.
    التحقق هنا لا يضمن سلامة الفيديو بالكامل أو حقوق إعادة نشره.
    """
    if not isinstance(identifier, str) or not identifier.strip():
        print("[REJECT] معرف فارغ أو غير صالح.")
        return None

    identifier = identifier.strip()
    safe_identifier = urllib.parse.quote(identifier, safe="")

    metadata_url = ARCHIVE_METADATA_URL.format(safe_identifier)

    try:
        response = SESSION.get(
            metadata_url,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            print(
                f"[REJECT] Metadata HTTP {response.status_code}: "
                f"{identifier}"
            )
            return None

        try:
            metadata = response.json()
        except ValueError:
            print(f"[REJECT] Metadata ليست JSON صالحًا: {identifier}")
            return None

        files = metadata.get("files", [])

        if not isinstance(files, list):
            print(f"[REJECT] بنية قائمة الملفات غير صالحة: {identifier}")
            return None

        candidates = []

        for item in files:
            if not isinstance(item, dict):
                continue

            filename = item.get("name")

            if not isinstance(filename, str):
                continue

            # قبول الملفات ذات امتداد MP4 فقط.
            if not filename.lower().endswith(".mp4"):
                continue

            # استبعاد الملفات التي لا تحتوي على حجم صالح.
            size = item.get("size")

            try:
                if size is None or int(size) <= 0:
                    continue
            except (ValueError, TypeError):
                continue

            format_type = str(item.get("format", "")).lower()

            # تفضيل ملفات الفيديو التي تشير بياناتها إلى MP4 أو H.264.
            score = 1

            if "mp4" in format_type:
                score += 3
            elif "h.264" in format_type:
                score += 2
            elif "mpeg4" in format_type:
                score += 2

            candidates.append((score, filename))

        if not candidates:
            print(f"[REJECT] لم يُعثر على ملف MP4 مناسب: {identifier}")
            return None

        candidates.sort(key=lambda candidate: candidate[0], reverse=True)

        for _, filename in candidates:
            safe_filename = urllib.parse.quote(filename, safe="")

            video_url = ARCHIVE_DOWNLOAD_URL.format(
                safe_identifier,
                safe_filename
            )

            if _check_video_response(video_url, identifier):
                return video_url

        print(f"[REJECT] لم ينجح فحص أي رابط فيديو: {identifier}")
        return None

    except requests.RequestException as error:
        print(f"[NETWORK_ERROR] فشل طلب metadata: {error}")
        return None

    except (TypeError, KeyError) as error:
        print(f"[DATA_ERROR] بيانات غير متوقعة للمعرف {identifier}: {error}")
        return None


def _check_video_response(video_url: str, identifier: str) -> bool:
    """
    فحص ترويسات HEAD، ثم تجربة GET بنطاق جزئي عند الحاجة.
    لا ينزّل الفيديو كاملًا ولا يثبت قابليته للتشغيل الكامل.
    """

    try:
        with SESSION.head(
            video_url,
            timeout=8,
            allow_redirects=True
        ) as response:

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if (
                response.status_code == 200
                and (
                    content_type.startswith("video/")
                    or content_type == "application/octet-stream"
                )
            ):
                print(f"[HTTP_CHECK_OK] {identifier}")
                return True

    except requests.RequestException as error:
        print(f"[HEAD_WARNING] {identifier}: {error}")

    try:
        with SESSION.get(
            video_url,
            headers={"Range": "bytes=0-1023"},
            timeout=8,
            stream=True
        ) as response:

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            accepted_type = (
                content_type.startswith("video/")
                or content_type == "application/octet-stream"
            )

            if response.status_code == 206 and accepted_type:
                print(f"[RANGE_CHECK_OK] {identifier}")
                return True

            # بعض الخوادم تتجاهل Range وتعيد الملف كاملًا برمز 200.
            if response.status_code == 200 and accepted_type:
                print(f"[GET_CHECK_OK] {identifier}")
                return True

            print(
                f"[HTTP_REJECT] {identifier}: "
                f"status={response.status_code}, "
                f"type={content_type}"
            )

    except requests.RequestException as error:
        print(f"[GET_WARNING] {identifier}: {error}")

    return False


def fetch_real_action_videos() -> list:
    """
    البحث عن مقاطع أجنبية مصنفة ضمن الأكشن والإثارة والمطاردات.
    الكلمات المفتاحية لا تضمن أن كل نتيجة مناسبة فعليًا.
    """
    query = (
        "mediatype:movies AND "
        "(subject:action OR subject:thriller OR "
        "subject:stunts OR subject:chase) AND "
        "language:eng"
    )

    params = {
        "q": query,
        "fl[]": ["identifier", "title"],
        "rows": 30,
        "output": "json",
        "page": 1,
    }

    valid_videos = []
    seen_identifiers = set()

    try:
        print("[FETCH] البحث في Internet Archive...")

        response = SESSION.get(
            ARCHIVE_SEARCH_URL,
            params=params,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            print(f"[FETCH_ERROR] HTTP {response.status_code}")
            return []

        try:
            search_data = response.json()
        except ValueError:
            print("[FETCH_ERROR] استجابة البحث ليست JSON صالحًا.")
            return []

        docs = search_data.get("response", {}).get("docs", [])

        if not isinstance(docs, list):
            print("[FETCH_ERROR] بنية نتائج البحث غير صالحة.")
            return []

        for document in docs:
            if not isinstance(document, dict):
                continue

            identifier = document.get("identifier")

            if not isinstance(identifier, str) or not identifier.strip():
                continue

            if identifier in seen_identifiers:
                continue

            seen_identifiers.add(identifier)

            verified_url = get_verified_mp4_url(identifier)

            if not verified_url:
                continue

            valid_videos.append({
                "id": identifier,
                "title": document.get("title", "Untitled"),
                "url": verified_url,
            })

            if len(valid_videos) >= MAX_VIDEOS_PER_RUN:
                break

        print(
            f"[FETCH_SUMMARY] عدد الروابط المقبولة: "
            f"{len(valid_videos)}"
        )

        return valid_videos

    except requests.RequestException as error:
        print(f"[FETCH_NETWORK_ERROR] {error}")
        return []


def run_secure_agent():
    """
    اكتشاف الفيديوهات وفحص وجود المعرّف في payment_events.
    وضع DRY_RUN يمنع الإدخال والتعديل.
    """

    discovered_videos = []

    print(
        f"[AGENT_START] Trend Agent V13 | "
        f"DRY_RUN={DRY_RUN}"
    )

    try:
        supabase_url = os.environ.get("SUPABASE_URL")
        supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

        if not supabase_url or not supabase_key:
            print(
                "[CRITICAL] متغيرات Supabase المطلوبة غير موجودة."
            )
            return 1

        discovered_videos = fetch_real_action_videos()

        if not discovered_videos:
            print("[INFO] لم يُعثر على فيديوهات مقبولة في هذه الدورة.")
            return 1

        # اختيار الفيديو الأول فقط للفحص.
        target_video = discovered_videos[0]

        print(f"[TARGET] {target_video['title']}")
        print(f"[TARGET_URL] {target_video['url']}")

        # عميل Supabase للقراءة فقط في المسار الحالي.
        supabase_client: Client = create_client(
            supabase_url,
            supabase_key
        )

        print(
            f"[DB_CHECK] فحص المعرّف: "
            f"{target_video['id']}"
        )

        result = (
            supabase_client
            .table("payment_events")
            .select("id")
            .eq("provider_payment_id", target_video["id"])
            .limit(1)
            .execute()
        )

        is_duplicate = bool(result.data)

        if is_duplicate:
            print("[DB_RESULT] المعرّف موجود مسبقًا.")
        else:
            print("[DB_RESULT] لم يُعثر على المعرّف في نتيجة الفحص.")

        # حاجز الكتابة: يجب ألا يحدث أي INSERT أو UPDATE أو DELETE هنا.
        if DRY_RUN:
            print("[DRY_RUN] الاختبار انتهى دون كتابة إلى قاعدة البيانات.")
            return 0

        # مسار كتابة معطل عمدًا في هذه النسخة.
        # لا تُفعّل الكتابة قبل تصميم تخزين البيانات واختبارها.
        print(
            "[BLOCKED] الكتابة الحقيقية غير مفعّلة في هذه النسخة."
        )
        return 2

    except Exception as error:
        print(f"[RUNTIME_ERROR] {type(error).__name__}: {error}")
        return 1

    finally:
        discovered_videos.clear()
        gc.collect()
        print("[CLEANUP] اكتملت عملية التنظيف.")


if __name__ == "__main__":
    sys.exit(run_secure_agent())
