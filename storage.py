import json
import os
from pathlib import Path
from config import DATA_DIR, SUBJECTS

def init_storage() -> dict:
    """
    Barcha fanlar papkalaridan JSON fayllarni o'qib, xotiraga yuklaydi.
    Natija formati: {"korporativ": {1: {...}, 2: {...}}, "moliyaviy": {...}}
    """
    memory_db = {}
    total_tests_loaded = 0

    for subject_key in SUBJECTS.keys():
        memory_db[subject_key] = {}
        subject_dir = Path(DATA_DIR) / subject_key
        
        # Agar papka ichida fayllar bo'lsa, ularni o'qiymiz
        if subject_dir.exists():
            for filename in os.listdir(subject_dir):
                if filename.startswith("test_") and filename.endswith(".json"):
                    filepath = subject_dir / filename
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                            # Fayldan test ID sini olib, fanning lug'atiga saqlaymiz
                            # Agar data ichida test_id bo'lmasa, fayl nomidan oladi (test_1.json -> 1)
                            test_id = data.get("test_id", int(filename.split("_")[1].split(".")[0]))
                            
                            # range (savollar oraliqi) xususiyati bo'lmasa, qo'shib qo'yamiz
                            if "range" not in data:
                                q_count = len(data.get("questions", []))
                                data["range"] = f"1-{q_count}"
                                
                            memory_db[subject_key][test_id] = data
                            total_tests_loaded += 1
                    except Exception as e:
                        print(f"Xato: {filepath} faylini o'qib bo'lmadi. Sabab: {e}")

    print(f"Muvaffaqiyatli! Jami {len(SUBJECTS)} ta fandan {total_tests_loaded} ta test bloki yuklandi.")
    return memory_db