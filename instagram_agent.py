import os
import sys
import json
import logging
import time
import requests
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AUDIT_MONITOR] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)

TABLE_NAME = "generated_videos"

def main() -> int:
    logging.info("LAUNCHING CRITICAL INSTAGRAM GRAPH API REELS PUBLISHING ENGINE...")
    
    # 1. سحب أوراق الاعتماد الحقيقية المشفرة في السيرفر
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    instagram_id = os.getenv("INSTAGRAM_ACCOUNT_ID", "").strip()
    access_token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
    
    if not all([supabase_url, supabase_key, instagram_id, access_token]):
        logging.error("Fatal startup error: Instagram API or Supabase credentials missing from Repository Secrets.")
        return 2

    try:
        # 2. الاتصال بـ Supabase لسحب آخر مقطع أكشن جاهز للنشر ولم ينشر بعد
        supabase: Client = create_client(supabase_url, supabase_key)
        response = supabase.table(TABLE_NAME).select("*").eq("status", "ready_to_publish").limit(1).execute()
        rows = getattr(response, "data", [])
        
        if not rows:
            logging.info("No 'ready_to_publish' video records found in database. Exiting smoothly.")
            return 0
            
        video_record = rows[0]
        row_id = video_record["id"]
        video_url = video_record["video_url"]
        caption = video_record["caption"]
        
        logging.info(f"Targeting Video Record ID {row_id} for live platform publishing. URL: {video_url}")

        # 3. خطوة ميتا الرسمية 1: إنشاء حاوية ميديا للـ Reel (Create Container)
        container_url = f"https://facebook.com{instagram_id}/media"
        container_payload = {
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": access_token
        }
        
        logging.info("Step 1: Submitting Reels media container creation request to Meta Graph API...")
        container_res = requests.post(container_url, data=container_payload, timeout=30)
        container_data = container_res.json()
        
        if "id" not in container_data:
            logging.error(f"Meta Graph Container Creation Failed: {json.dumps(container_data)}")
            supabase.table(TABLE_NAME).update({"error_message": f"Container Failed: {container_data}"}).eq("id", row_id).execute()
            return 1
            
        creation_id = container_data["id"]
        logging.info(f"Reels Container successfully generated. Creation ID: {creation_id}")
        
        # الانتظار الإجباري ثوانٍ لتجهيز ومعالجة الفيديو في سيرفرات ميتا قبل النشر
        logging.info("Waiting for Meta servers to index and process video assets...")
        time.sleep(15)

        # 4. خطوة ميتا الرسمية 2: إطلاق ونشر الـ Reel علناً للملايين (Publish Container)
        publish_url = f"https://facebook.com{instagram_id}/media_publish"
        publish_payload = {
            "creation_id": creation_id,
            "access_token": access_token
        }
        
        logging.info("Step 2: Commencing public structural dissemination on Instagram Reels...")
        publish_res = requests.post(publish_url, data=publish_payload, timeout=30)
        publish_data = publish_res.json()
        
        if "id" not in publish_data:
            logging.error(f"Meta Graph Live Publishing Failed: {json.dumps(publish_data)}")
            supabase.table(TABLE_NAME).update({"error_message": f"Publish Failed: {publish_data}"}).eq("id", row_id).execute()
            return 1

        # 5. تحديث قاعدة البيانات بنجاح النشر الفعلي وإغلاق الدورة
        logging.info(f"INSTAGRAM REELS PUBLISHED SUCCESSFULLY! Live Media ID: {publish_data['id']}")
        supabase.table(TABLE_NAME).update({
            "status": "published",
            "published_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", row_id).execute()
        
        logging.info("Supabase repository tracking state updated to 'published'. Exit Code 0.")
        return 0

    except Exception as e:
        logging.error(f"FATAL: Instagram Agent execution crash encountered: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
