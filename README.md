# instagram-automation
# Instagram Reels Automation Pipeline 🚀

نظام متطور لأتمتة جلب ونشر فيديوهات الريلز على إنستغرام بشكل تلقائي باستخدام Python، وSupabase، وGitHub Actions.

## 🛠️ مكونات المشروع:
* **trend_agent.py**: جلب ومراقبة الفيديوهات والتحقق من الروابط وعدم التكرار عبر قاعدة البيانات.
* **instagram_agent.py**: معالجة طابور النشر والتواصل مع Meta Graph API v21.0 لإدارة ونشر الريلز بثبات وأمان.

## ⚙️ المتطلبات والأسرار (GitHub Secrets):
يعتمد النظام على الأسرار التالية لضمان الاتصال الناجح:
* `SUPABASE_URL`
* `SUPABASE_SERVICE_ROLE_KEY`
* `INSTAGRAM_ACCESS_TOKEN`
* `INSTAGRAM_ACCOUNT_ID`
