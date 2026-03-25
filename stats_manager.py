from datetime import datetime
from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    print(f"Supabase'ga ulanishda xatolik: {e}")

def register_user(user_id, full_name, username):
    uid = str(user_id)
    try:
        res = supabase.table("users").select("telegram_id").eq("telegram_id", uid).execute()
        if not res.data:
            supabase.table("users").insert({
                "telegram_id": uid,
                "full_name": full_name or "Ismsiz",
                "username": username or "yo'q",
                "joined_at": datetime.now().strftime("%Y-%m-%d %H:%M")
            }).execute()
    except Exception as e:
        print(f"Foydalanuvchini saqlashda xato: {e}")

def get_all_users():
    try:
        res = supabase.table("users").select("*").execute()
        return res.data
    except Exception as e:
        return []

def get_top_users(limit=10):
    try:
        stats_res = supabase.table("user_stats").select("*").order("total_correct", desc=True).limit(limit).execute()
        result = []
        for s in stats_res.data:
            if s["total_correct"] > 0:
                result.append({"user_id": s["user_id"], "correct": s["total_correct"], "completed": s["tests_completed"]})
        return result
    except Exception as e:
        return []

def get_user_stats(user_id):
    uid = str(user_id)
    try:
        response = supabase.table("user_stats").select("*").eq("user_id", uid).execute()
        if len(response.data) > 0:
            user_data = response.data[0]
            if not user_data.get("history"): user_data["history"] = []
            return user_data
        else:
            return {"user_id": uid, "tests_completed": 0, "total_correct": 0, "total_wrong": 0, "history": []}
    except Exception as e:
        return {"tests_completed": 0, "total_correct": 0, "total_wrong": 0, "history": []}

def update_user_stats(user_id, correct, wrong, subject_key, test_id, mistakes):
    uid = str(user_id)
    stats = get_user_stats(uid)
    stats["tests_completed"] += 1
    stats["total_correct"] += correct
    stats["total_wrong"] += wrong
    
    history_entry = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "subject": subject_key,
        "test_id": test_id,
        "correct": correct,
        "wrong": wrong,
        "mistakes": mistakes
    }
    
    history = stats.get("history", [])
    if not isinstance(history, list): history = []
    history.insert(0, history_entry)
    stats["history"] = history[:15]
    
    try: supabase.table("user_stats").upsert(stats).execute()
    except Exception as e: print(f"Stats yozishda xato: {e}")

def save_user_test(creator_id, subject, block_name, questions):
    try:
        res = supabase.table("user_tests").insert({
            "creator_id": str(creator_id),
            "subject": subject,
            "block_name": block_name,
            "questions": questions,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }).execute()
        if res.data: return res.data[0]['id']
        return None
    except Exception as e:
        return None

def get_user_test(test_id):
    try:
        res = supabase.table("user_tests").select("*").eq("id", int(test_id)).execute()
        if res.data: return res.data[0]
        return None
    except Exception as e:
        return None

# ⚠️ YANGI QO'SHILGAN FUNKSIYALAR (Testlarni boshqarish uchun):
def get_user_created_tests(creator_id):
    """Foydalanuvchi yaratgan barcha testlarni bazadan oladi."""
    try:
        res = supabase.table("user_tests").select("id, subject, block_name, created_at").eq("creator_id", str(creator_id)).order("id", desc=True).execute()
        return res.data
    except Exception as e:
        print(f"Testlarni olishda xato: {e}")
        return []

def delete_user_test(test_id, creator_id):
    """Foydalanuvchiga o'zining testini o'chirishga ruxsat beradi."""
    try:
        supabase.table("user_tests").delete().eq("id", int(test_id)).eq("creator_id", str(creator_id)).execute()
        return True
    except Exception as e:
        print(f"Testni o'chirishda xato: {e}")
        return False
def get_user_rank(user_id):
    """Foydalanuvchining umumiy reytingdagi o'rnini aniqlaydi."""
    try:
        # Kichik va o'rta (10-15k gacha) bazalar uchun bu usul juda tez ishlaydi.
        res = supabase.table("user_stats").select("user_id, total_correct").order("total_correct", desc=True).execute()
        for index, stat in enumerate(res.data):
            if stat["user_id"] == str(user_id):
                return index + 1
        return "N/A"
    except Exception as e:
        print(f"Reytingni aniqlashda xato: {e}")
        return "N/A"
