import os
import requests
import random
from supabase import create_client, Client

# إعداد المتغيرات البرمجية الصارمة للمستودع الجديد
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY")

# قوالب الوصف الاحترافي لـ "الهندسة الاجتماعية" لجمع المشاهدات والمتابعين بسرعة
CAPTIONS_POOL = [
    "🔥 لا تجرب هذا في المنزل! هل تعتقد أنك تمتلك الشجاعة الكافية؟ تابع الحساب للمزيد من الحماس اليومي. 👇 #Action #Trending #Viral",
    "🎬 اللقطة الأكثر إثارة لهذا الأسبوع! إذا كنت من عشاق الأكشن اضغط زر المتابعة فوراً. 🚀 #Reels #Adventure",
    "🎮 عشاق الإثارة والحماس فقط! مقاطع يومية متفجرة، لا تفوت القادم واشترك في الحساب الآن 📥 #Gaming #Combat"
]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def fetch_viral_action_content():
    # البحث عن فيديوهات الأكشن الأكثر طلباً (Combat, Parkour, Extreme Sports)
    keywords = ["action", "combat", "extreme sports", "tactical"]
    selected_keyword = random.choice(keywords)
    
    url = f"https://pexels.com{selected_keyword}&per_page=3&orientation=portrait"
    headers = {"Authorization": PEXELS_API_KEY}
    
    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            videos = response.json().get("videos", [])
            for video in videos:
                # استخراج الرابط عالي الجودة HD لضمان رفع الجودة في إنستغرام
                video_files = video.get("video_files", [])
                hd_videos = [f for f in video_files if f.get("quality") == "hd" or f.get("height", 0) >= 1080]
                
                if hd_videos:
                    video_url = hd_videos[0].get("link")
                else:
                    video_url = video_files[0].get("link") if video_files else None
                
                if not video_url:
                    continue
                
                chosen_caption = random.choice(CAPTIONS_POOL)
                
                # إدراج البيانات الاحترافية في Supabase لتغذية خوارزمية النشر التلقائي بحالة pending
                data, count = supabase.table("videos").insert({
                    "video_url": video_url,
                    "caption": chosen_caption,
                    "status": "pending"
                }).execute()
                
                print(f"✅ تم بنجاح جلب وحقن فيديو ترند: {video_url[:50]}...")
        else:
            print(f"❌ خطأ في الاتصال بالمنصة: {response.status_code}")
    except Exception as e:
        print(f"❌ حدث خطأ غير متوقع: {str(e)}")

if __name__ == "__main__":
    fetch_viral_action_content()
