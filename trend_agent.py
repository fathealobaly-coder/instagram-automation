import os
import sys
import time
import uuid
import requests
from datetime import datetime, timezone

# 1. Environment Credentials Verification
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
INSTAGRAM_ACCOUNT_ID = os.getenv("INSTAGRAM_ACCOUNT_ID")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")

missing_vars = []
if not SUPABASE_URL: missing_vars.append("SUPABASE_URL")
if not SUPABASE_SERVICE_ROLE_KEY: missing_vars.append("SUPABASE_SERVICE_ROLE_KEY")
if not INSTAGRAM_ACCOUNT_ID: missing_vars.append("INSTAGRAM_ACCOUNT_ID")
if not INSTAGRAM_ACCESS_TOKEN: missing_vars.append("INSTAGRAM_ACCESS_TOKEN")

if missing_vars:
    print(f'{{"error": "Missing required environment variables", "missing": {missing_vars}}}', file=sys.stderr)
    sys.exit(1)

# 2. Payload and Unique Identifier Generation
unique_suffix = uuid.uuid4().hex
video_url = f"https://upload.wikimedia.org/wikipedia/commons/1/1b/Big_Buck_Bunny_Trailer_400p.ogv.360p.mp4?v={unique_suffix}"
caption = f"Automated Trend Broadcast - {unique_suffix}"

# 3. Supabase Direct Row Insertion ('ready_to_publish')
supabase_endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/generated_videos"
supabase_headers = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}
insert_payload = {
    "video_url": video_url,
    "caption": caption,
    "status": "ready_to_publish"
}

insert_response = requests.post(supabase_endpoint, headers=supabase_headers, json=insert_payload)
if insert_response.status_code not in (200, 201):
    print(insert_response.text, file=sys.stderr)
    sys.exit(1)

inserted_data = insert_response.json()
row_record = inserted_data[0] if isinstance(inserted_data, list) and len(inserted_data) > 0 else inserted_data
row_id = row_record.get("id")

if not row_id:
    print(f'{{"error": "Failed to retrieve row ID from Supabase insert response", "response": {insert_response.text}}}', file=sys.stderr)
    sys.exit(1)

# 4. Meta Graph API: Create Reel Media Container
meta_media_url = f"https://graph.facebook.com/v19.0/{INSTAGRAM_ACCOUNT_ID}/media"
container_payload = {
    "media_type": "REELS",
    "video_url": video_url,
    "caption": caption,
    "access_token": INSTAGRAM_ACCESS_TOKEN
}

container_response = requests.post(meta_media_url, data=container_payload)
if container_response.status_code != 200:
    print(container_response.text, file=sys.stderr)
    sys.exit(1)

container_data = container_response.json()
container_id = container_data.get("id")

if not container_id:
    print(f'{{"error": "Meta container ID missing in response", "response": {container_response.text}}}', file=sys.stderr)
    sys.exit(1)

# 5. Wait exactly 20 seconds for processing
time.sleep(20)

# 6. Meta Graph API: Media Publish Handshake
meta_publish_url = f"https://graph.facebook.com/v19.0/{INSTAGRAM_ACCOUNT_ID}/media_publish"
publish_payload = {
    "creation_id": container_id,
    "access_token": INSTAGRAM_ACCESS_TOKEN
}

publish_response = requests.post(meta_publish_url, data=publish_payload)
if publish_response.status_code != 200:
    print(publish_response.text, file=sys.stderr)
    sys.exit(1)

publish_data = publish_response.json()
post_id = publish_data.get("id")

if not post_id:
    print(f'{{"error": "Meta publish ID missing in response", "response": {publish_response.text}}}', file=sys.stderr)
    sys.exit(1)

# 7. Update Supabase Row Status to 'published'
update_endpoint = f"{SUPABASE_URL.rstrip('/')}/rest/v1/generated_videos?id=eq.{row_id}"
update_payload = {
    "status": "published",
    "published_at": datetime.now(timezone.utc).isoformat()
}

update_response = requests.patch(update_endpoint, headers=supabase_headers, json=update_payload)
if update_response.status_code not in (200, 204):
    print(update_response.text, file=sys.stderr)
    sys.exit(1)

print(f'{{"status": "success", "row_id": "{row_id}", "post_id": "{post_id}"}}')
sys.exit(0)
