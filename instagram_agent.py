import os
import sys
import time
import requests
from datetime import datetime, timezone

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
INSTAGRAM_ACCOUNT_ID = os.getenv("INSTAGRAM_ACCOUNT_ID", "").strip()
ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()

if not all([SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, INSTAGRAM_ACCOUNT_ID, ACCESS_TOKEN]):
    print("[FATAL] Required credentials missing in Instagram Agent.", file=sys.stderr)
    sys.exit(1)

# 1. الاتصال بـ Supabase لجلب المقطع الجاهز للنشر
endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/generated_videos?status=eq.ready_to_publish&limit=1"
headers = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json"
}

response = requests.get(endpoint, headers=headers)
rows = response.json()

if not rows:
    print("[INFO] No videos ready to publish found. Exiting.")
    sys.exit(0)

video_record = rows[0]
row_id = video_record["id"]
video_url = video_record["video_url"]
caption = video_record["caption"]

print(f"[INFO] Found video to publish. Row ID: {row_id}")

# 2. إنشاء حاوية الميديا على إنستغرام (Meta API Step 1)
container_url = f"https://facebook.com{INSTAGRAM_ACCOUNT_ID}/media"
container_payload = {
    "media_type": "REELS",
    "video_url": video_url,
    "caption": caption,
    "access_token": ACCESS_TOKEN
}

res = requests.post(container_url, data=container_payload)
container_data = res.json()
creation_id = container_data.get("id")

if not creation_id:
    print(f"[FATAL] Container creation failed: {res.text}", file=sys.stderr)
    sys.exit(1)

print(f"[INFO] Container created. ID: {creation_id}. Waiting 20 seconds for processing...")
time.sleep(20)

# 3. البث الفعلي والنشر حياً للجمهور (Meta API Step 2)
publish_url = f"https://facebook.com{INSTAGRAM_ACCOUNT_ID}/media_publish"
pub_res = requests.post(publish_url, data={"creation_id": creation_id, "access_token": ACCESS_TOKEN})

if pub_res.status_code != 200:
    print(f"[FATAL] Live publishing failed: {pub_res.text}", file=sys.stderr)
    sys.exit(1)

# 4. تحديث حالة الجدول في Supabase إلى published واكتمال الدورة
update_url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/generated_videos?id=eq.{row_id}"
headers["Prefer"] = "return=representation"
update_payload = {
    "status": "published",
    "published_at": datetime.now(timezone.utc).isoformat()
}
requests.patch(update_url, headers=headers, json=update_payload)

print("[SUCCESS] Video successfully published live to Instagram Reels! Exit 0.")
sys.exit(0)
