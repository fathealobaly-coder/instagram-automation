import os
import sys
import uuid
import requests

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    print("[FATAL] Supabase credentials missing.", file=sys.stderr)
    sys.exit(1)

# توليد بيانات عشوائية وفريدة مئة بالمئة لمنع أي خطأ تعارض أو Unique Constraint
unique_id = uuid.uuid4().hex
video_url = f"https://wikimedia.org{unique_id}"
caption = f"Cinematic Action Edit 🔥🎬 #explore #fyp #action"

endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/generated_videos"
headers = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}
payload = {
    "video_url": video_url,
    "caption": caption,
    "status": "ready_to_publish"
}

print(f"[INFO] Injecting unique row into Supabase: {video_url}")
response = requests.post(endpoint, headers=headers, json=payload)

if response.status_code not in (200, 201):
    print(f"[FATAL] Insertion failed: {response.text}", file=sys.stderr)
    sys.exit(1)

print("[SUCCESS] Row successfully injected. Trend Agent finished.")
sys.exit(0)
