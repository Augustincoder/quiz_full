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
        print(f"Barcha foydalanuvchilarni olishda xato: {e}")
        return []

def get_top_users(limit=10):
    try:
        stats_res = supabase.table("user_stats").select("*").order("total_correct", desc=True).limit(limit).execute()
        result = []
        for s in stats_res.data:
            u_res = supabase.table("users").select("full_name").eq("telegram_id", s["user_id"]).execute()
            name = u_res.data[0]["full_name"] if u_res.data else "Ismsiz Talaba"
            if s["total_correct"] > 0:
                result.append({"name": name, "correct": s["total_correct"], "completed": s["tests_completed"]})
        return result
    except Exception as e:
        print(f"Reytingni olishda xato: {e}")
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

# ⚠️ YANGI QO'SHILGAN FUNKSIYALAR:
def save_user_test(creator_id, subject, block_name, questions):
    """Foydalanuvchi yaratgan testni saqlaydi va uning ID sini qaytaradi."""
    try:
        res = supabase.table("user_tests").insert({
            "creator_id": str(creator_id),
            "subject": subject,
            "block_name": block_name,
            "questions": questions,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }).execute()
        if res.data:
            return res.data[0]['id']
        return None
    except Exception as e:
        print(f"Testni saqlashda xato: {e}")
        return None

def get_user_test(test_id):
    """Deep-link orqali kirilganda bazadan testni o'qib keladi."""
    try:
        res = supabase.table("user_tests").select("*").eq("id", int(test_id)).execute()
        if res.data:
            return res.data[0]
        return None
    except Exception as e:
        print(f"Testni o'qishda xatolik: {e}")
        return None
