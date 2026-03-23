from datetime import datetime
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY

# Supabase bazasiga ulanish
try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase'ga ulanishda xatolik: {e}")

def get_user_stats(user_id):
    """Supabase'dan foydalanuvchi statistikasini o'qib keladi."""
    uid = str(user_id)
    try:
        response = supabase.table("user_stats").select("*").eq("user_id", uid).execute()
        # Agar foydalanuvchi bazada bor bo'lsa
        if len(response.data) > 0:
            user_data = response.data[0]
            # Agar history bo'sh (None) bo'lsa, uni bo'sh ro'yxatga aylantiramiz
            if not user_data.get("history"):
                user_data["history"] = []
            return user_data
        else:
            # Yangi foydalanuvchi uchun standart qolip
            return {
                "user_id": uid,
                "tests_completed": 0,
                "total_correct": 0,
                "total_wrong": 0,
                "history": []
            }
    except Exception as e:
        print(f"Ma'lumotni o'qishda xatolik: {e}")
        return {"tests_completed": 0, "total_correct": 0, "total_wrong": 0, "history": []}

def update_user_stats(user_id, correct, wrong, subject_key, test_id, mistakes):
    """Natijalarni va test tarixini Supabase bazasiga yozadi (Upsert)."""
    uid = str(user_id)
    stats = get_user_stats(uid)
    
    # Asosiy ko'rsatkichlarni yangilash
    stats["tests_completed"] += 1
    stats["total_correct"] += correct
    stats["total_wrong"] += wrong
    
    # Test tarixini yaratish
    history_entry = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "subject": subject_key,
        "test_id": test_id,
        "correct": correct,
        "wrong": wrong,
        "mistakes": mistakes
    }
    
    # Yangi tarixni eng boshiga qo'shamiz
    history = stats.get("history", [])
    if not isinstance(history, list):
        history = []
        
    history.insert(0, history_entry)
    
    # Baza to'lib ketmasligi uchun faqat oxirgi 15 ta test tarixini saqlaymiz
    stats["history"] = history[:15]
    
    # Bazaga saqlash (Upsert - foydalanuvchi bo'lsa yangilaydi, yo'q bo'lsa yangi yaratadi)
    try:
        supabase.table("user_stats").upsert(stats).execute()
    except Exception as e:
        print(f"Supabase'ga yozishda xatolik: {e}")