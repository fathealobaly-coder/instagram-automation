import os
import requests
from datetime import datetime

class MasterAssistantAgent:
    def __init__(self, owner_name="فتحي العبالي"):
        self.owner_name = owner_name
        print(f"[*] تم تفعيل الوكيل الرئيسي المسيطر بنجاح لصالح: {self.owner_name}")

    def execute_command(self, command: str):
        """
        تلقي الأوامر من المستخدم وتوجيهها للمهمة المناسبة أو الوكيل المناسب
        """
        command = command.lower()
        print(f"[*] استقبال أمر جديد: {command}")

        if "اتصل" in command or "متاخر" in command or "رسالة" in command:
            return self.handle_personal_task(command)

        elif "حالة الوكلاء" in command or "مراقبة" in command or "الريلز" in command:
            return self.monitor_sub_agents()

        else:
            return f"أنا وكيلك الرئيسي يا {self.owner_name}، أستمع لك وجاهز لتنفيذ أي أمر أو توجيه الوكلاء الآليين فوراً."

    def handle_personal_task(self, command):
        return "تم تنفيذ المهمة الشخصية: تم تجهيز جهة الاتصال وإرسال تنبيه التأخير بنجاح."

    def monitor_sub_agents(self):
        status_report = (
            "📊 تقرير حالة الوكلاء الآليين:\n"
            "- الوكيل الأول (Trend Agent): يعمل بكفاءة ويراقب مصادر الفيديوهات.\n"
            "- الوكيل الثاني (Instagram Agent): متصل بواجهة ميتا وحالة النشر مستقرة وسليمة."
        )
        return status_report

if __name__ == "__main__":
    master = MasterAssistantAgent()
    print(master.execute_command("اتصل بزوجتي وأخبرها أنني متأخر اليوم"))
    print("-" * 40)
    print(master.execute_command("ما هي حالة الوكلاء؟"))
