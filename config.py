"""
Configuration settings for the Quiz Bot system.
"""
import os
from pathlib import Path

# Telegram Bot Token - BotFather'dan olingan tokeningiz
BOT_TOKEN = "8764719096:AAHqT0lrIM9o_uE23ZtqGCWe7R8jzJYp0e4"

# Data storage
DATA_DIR = Path(__file__).parent / "data"

# Dasturda mavjud fanlar va ularning papka nomlari
SUBJECTS = {
    "korporativ": "🎓 Korporativ Boshqaruv",
    "moliyaviy": "💰 Moliyaviy Hisob",
    "ekonometrika": "📈 Ekonometrika"
}

QUESTIONS_PER_TEST = 25

# Barcha fanlar uchun avtomatik papkalarni yaratib qoyish
DATA_DIR.mkdir(exist_ok=True)
for subj_folder in SUBJECTS.keys():
    (DATA_DIR / subj_folder).mkdir(exist_ok=True)

# --- SUPABASE SOZLAMALARI ---
SUPABASE_URL = "https://wsvzggnhotzhmvugeyil.supabase.co" # O'zingiznikini qo'ying
SUPABASE_KEY = "sb_secret_nZQcnSza0XFCGy_4k1o8rw_zmJ_ISgM" # O'zingiznikini qo'ying
ADMIN_ID = 2014973670
