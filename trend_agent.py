import logging
import re
from urllib.parse import unquote, urlparse
import requests

# إعداد السجلات (Logs) لتتبع العمل بدقة كأنه نظام Google SRE
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WIKIMEDIA_API_URL = "https://wikimedia.org"
ALLOWED_VIDEO_HOSTS = {"upload.wikimedia.org"}

# =====================================================================
# 1. نظام جدار الحماية الصارم للتراخيص التجارية
# =====================================================================
def license_allows_commercial_use(
    license_url: str = "", license_text: str = ""
) -> bool:
    """
    سياسة صارمة ومحصنة لقبول التراخيص التجارية فقط (CC0, CC BY, CC BY-SA).
    تتعامل بمرونة مع لاحقات اللغات والدول وتمنع التلاعب أو التناقض.
    """
    url = (license_url or "").strip()
    text = (license_text or "").strip().lower()

    # 1. طرد القيود الصريحة فوراً
    restrictive_patterns = (
        r"\bnon[\s-]?commercial\b",
        r"\bno[\s-]?derivatives\b",
        r"\ball rights reserved\b",
        r"\bcc\s*by[\s-]?nc(?:[\s-]?sa)?\b",
        r"\bcc\s*by[\s-]?nd\b",
    )
    if any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for pattern in restrictive_patterns
    ):
        return False

    if not url:
        return False

    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    # 2. التحقق من أمان النطاق الرسمي وطرد الروابط الملغومة
    if parsed.scheme.lower() != "https":
        return False
    if (parsed.hostname or "").lower() != "creativecommons.org":
        return False
    if parsed.username or parsed.password or parsed.port:
        return False

    # 3. تنظيف المسار والتعامل مع لاحقة اللغة (مثل deed.ar أو deed.fr)
    path = unquote(parsed.path).lower()
    path = re.sub(r"/+", "/", path).strip("/")

    language_suffix_pattern = r"/deed(?:\.[a-z]{2,3}(?:-[a-z]{2})?)?$"
    path_without_language = re.sub(language_suffix_pattern, "", path).strip("/")
    path_without_language = path_without_language.rstrip("/")

    # 4. قائمة التراخيص التجارية المستقرة والمعتمدة حتمياً
    accepted_license_paths = {
        "publicdomain/zero/1.0",
        "licenses/by/1.0",
        "licenses/by/2.0",
        "licenses/by/3.0",
        "licenses/by/4.0",
        "licenses/by-sa/1.0",
        "licenses/by-sa/2.0",
        "licenses/by-sa/3.0",
        "licenses/by-sa/4.0",
    }

    if path_without_language not in accepted_license_paths:
        return False

    if parsed.query or parsed.fragment:
        return False

    # 5. كشف التناقضات بين النص والرابط المرفق
    if text:
        if "by-sa" in path_without_language and (
            "by" in text
            and "by-sa" not in text
            and "attribution-sharealike" not in text
        ):
            return False
        if path_without_language.endswith("/zero/1.0"):
            if "cc0" not in text and "public domain" not in text:
                return False

    return True


# =====================================================================
# 2. نظام تشريح وتصنيف روابط ومضيفي الوسائط
# =====================================================================
def classify_wikimedia_media_url(media_url: str) -> str | None:
    """يفحص بنية الرابط ويعيد سبب الرفض بالتفصيل، أو None إذا كان الرابط سليماً."""
    if not media_url:
        return "missing_media_url"

    try:
        parsed = urlparse(media_url)
    except ValueError:
        return "malformed_media_url"

    if parsed.scheme.lower() != "https":
        return "non_https_url"

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_VIDEO_HOSTS:
        return "untrusted_media_host"

    if not parsed.path.lower().endswith(".mp4"):
        return "unsupported_file_extension"

    return None


# =====================================================================
# 3. دالة البحث التفتيشية المتقدمة لـ Wikimedia Commons
# =====================================================================
def search_wikimedia_commons(
    term: str, limit: int = 30, session: requests.Session | None = None
) -> list[dict]:
    """تبحث عن الفيديوهات وتفضح سبب رفض كل ملف في السجلات بشكل تفصيلي."""
    client = session or requests.Session()
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrnamespace": 6,
        "gsrsearch": f"{term} filetype:video",
        "gsrlimit": min(max(int(limit), 1), 50),
        "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata",
    }

    try:
        response = client.get(WIKIMEDIA_API_URL, params=params, timeout=25)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("[COMMONS_API_ERROR] term=%r error=%s", term, exc)
        return []

    pages = payload.get("query", {}).get("pages", {})
    if not isinstance(pages, dict):
        logger.warning(
            "[COMMONS_INVALID_RESPONSE] term=%r pages_field_not_dict", term
        )
        return []

    accepted = []
    rejection_counts = {}

    def reject(reason: str, title: str, url: str = "", detail: str = ""):
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        logger.warning(
            "[COMMONS_REJECT] reason=%s title=%r url=%r detail=%s",
            reason,
            title,
            url,
            detail,
        )

    for page in pages.values():
        title = page.get("title", "<unknown>")
        imageinfo = page.get("imageinfo") or []

        if not imageinfo:
            reject("missing_imageinfo", title)
            continue

        info = imageinfo[0]
        media_url = info.get("url", "")
        mime = (info.get("mime") or "").strip().lower()
        size = info.get("size")

        # تفكيك الفحص الأعمى إلى فحص بنيوي وفحص MIME
        url_reason = classify_wikimedia_media_url(media_url)
        if url_reason:
            reject(url_reason, title, media_url)
            continue

        if mime != "video/mp4":
            reject(
                "mime_mismatch",
                title,
                media_url,
                f"actual_mime={mime!r}; expected='video/mp4'",
            )
            continue

        # استخراج بيانات الترخيص الوصفية المتقدمة
        metadata = info.get("extmetadata") or {}
        license_url = (
            metadata.get("LicenseUrl", {}).get("value", "") or ""
        ).strip()
        license_text = (
            metadata.get("LicenseShortName", {}).get("value", "") or ""
        ).strip()
        usage_terms = (
            metadata.get("UsageTerms", {}).get("value", "") or ""
        ).strip()

        if not license_allows_commercial_use(
            license_url=license_url, license_text=license_text
        ):
            reject(
                "license_not_approved",
                title,
                media_url,
                f"license_url={license_url!r}; license_text={license_text!r}",
            )
            continue

        # حفظ البيانات النظيفة والمثبتة تجارياً فقط
        accepted.append(
            {
                "video_url": media_url,
                "source": "wikimedia_commons",
                "source_title": title,
                "mime_type": mime,
                "size_bytes": size,
                "license_url": license_url,
                "license_text": license_text,
                "usage_terms": usage_terms,
                "rights_verified": False,
            }
        )

    logger.info(
        "[COMMONS_SUMMARY] term=%r total_pages=%d accepted=%d rejected=%d reasons=%s",
        term,
        len(pages),
        len(accepted),
        sum(rejection_counts.values()),
        rejection_counts,
    )
    return accepted


# =====================================================================
# 4. محرك تشغيل الوكيل الذكي (State-Tracking & Deduplication)
# =====================================================================
def run_agent(term: str, daily_target: int = 5, dry_run: bool = True):
    """
    الدالة القائدة لتشغيل الوكيل وتتبع الحالات محلياً
    لمنع التكرار اللانهائي أثناء التشغيل.
    """
    logger.info(
        "[START_RUN] term=%r target=%d dry_run=%s",
        term,
        daily_target,
        dry_run,
    )

    # محاكاة سجلات الـ معرفات القديمة المستخرجة من Supabase (فارغة الآن)
    processed_ids = set()

    # العدادات المحلية الحتمية التي فرضناها على الشات لمنع الدوران المفرغ
    local_processed_count = 0
    local_processed_ids = set()

    # استدعاء دالة البحث المحدثة والمطهرة
    candidates = search_wikimedia_commons(term=term, limit=30)

    for candidate in candidates:
        if local_processed_count >= daily_target:
            logger.info("[TARGET_REACHED] Stop processing. Daily target met.")
            break

        video_url = str(candidate.get("video_url") or "").strip()
        if not video_url:
            logger.warning("[LOCAL_SKIP] reason=missing_video_url")
            continue

        dedupe_key = video_url  # استخدام الرابط كمفتاح فريد لمنع التكرار

        # كشف التكرار في المعاينة الفورية الحالية
        if dedupe_key in local_processed_ids:
            logger.info("[LOCAL_SKIP] reason=duplicate_in_this_run")
            continue

        if dedupe_key in processed_ids:
            logger.info("[LOCAL_SKIP] reason=already_processed_in_database")
            continue

        # تسجيل وتتبع الحالة محلياً خطوة بخطوة
        local_processed_ids.add(dedupe_key)
        local_processed_count += 1

        logger.info(
            "[DRY_RUN_PREVIEW] item#%d accepted_for_preview=%s",
            local_processed_count,
            video_url,
        )

    logger.info(
        "[RUN_SUMMARY] processed_this_run=%d dry_run=%s",
        local_processed_count,
        dry_run,
    )
    return {"local_processed_count": local_processed_count, "dry_run": dry_run}


# نقطة تشغيل تجريبية حية لاختبار المنظومة فوراً
if __name__ == "__main__":
