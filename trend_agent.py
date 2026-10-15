import os
import requests
import random
from supabase import create_client, Client

# إعداد المتغيرات البرمجية الصارمة للمستودع النظيف
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# قوالب الوصف الاحترافي لـ "الهندسة الاجتماعية" لجمع المشاهدات والمتابعين بسرعة
CAPTIONS_POOL = [
    "🔥 لا تجرب هذا في المنزل! هل تعتقد أنك تمتلك الشجاعة الكافية؟ تابع الحساب للمزيد من الحماس اليومي. 👇 #Action #Trending #Viral",
    "🎬 اللقطة الأكثر إثارة لهذا الأسبوع! إذا كنت من عشاق الأكشن اضغط زر المتابعة فوراً. 🚀 #Reels #Adventure",
    "🎮 عشاق الإثارة والحماس فقط! مقاطع يومية متفجرة، لا تفوت القادم واشترك في الحساب الآن 📥 #Gaming #Combat"
]

# قائمة روابط لفيديوهات أكشن عمودية عالية الجودة مجهزة مسبقاً للعمل الفوري كمصدر بديل مستقر
FALLBACK_VIDEOS = [
    "https://vimeo.com",
    "https://vimeo.com",
    "https://vimeo.com"
]

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def fetch_viral_action_content():
    try:
        # اختيار فيديو أكشن حماسي بشكل عشوائي ذكي من المصدر البديل النظيف
        video_url = random.choice(FALLBACK_VIDEOS)
        chosen_caption = random.choice(CAPTIONS_POOL)
        
        # إدراج البيانات الاحترافية في Supabase لتغذية خوارزمية النشر التلقائي بحالة pending
        data, count = supabase.table("videos").insert({
            "video_url": video_url,
            "caption": chosen_caption,
            "status": "pending"
        }).execute()
        
        print(f"✅ تم بنجاح جلب وحقن فيديو ترند من المصدر المستقر: {video_url[:50]}...")
    except Exception as e:
        print(f"❌ حدث خطأ غير متوقع في الاتصال: {str(e)}")

if __name__ == "__main__":
    fetch_viral_action_content()
