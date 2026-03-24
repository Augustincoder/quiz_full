import time
import random
import asyncio
from aiogram import Bot, Router, F
from aiogram.types import Message, CallbackQuery, PollAnswer, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import stats_manager
from config import SUBJECTS, ADMIN_ID

router = Router()

active_tests = {}
waiting_rooms = {}  
poll_chat_map = {} 
memory_db = {}
ITEMS_PER_PAGE = 5  

# Muloqot uchun holatlar
class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_reply = State()

class UserStates(StatesGroup):
    waiting_for_message = State()

# Savollar va variantlarni aralashtirish
def prepare_shuffled_questions(raw_questions):
    shuffled_q = list(raw_questions)
    random.shuffle(shuffled_q)
    session_questions = []
    for q in shuffled_q:
        options = list(q["options"])
        correct_text = options[q["correct_index"]]
        random.shuffle(options)
        new_correct_idx = options.index(correct_text)
        session_questions.append({
            "question": q["question"], "options": options,
            "correct_index": new_correct_idx, "correct_text": correct_text  
        })
    return session_questions

def get_subjects_keyboard():
    buttons = []
    for subj_key, subj_name in SUBJECTS.items():
        block_count = len(memory_db.get(subj_key, {}))
        buttons.append([InlineKeyboardButton(text=f"{subj_name} ({block_count} ta blok)", callback_data=f"subj_{subj_key}")])
    buttons.append([InlineKeyboardButton(text="📊 Statistikam", callback_data="show_stats"),
                    InlineKeyboardButton(text="🏆 Reyting", callback_data="show_leaderboard")])
    buttons.append([InlineKeyboardButton(text="💬 Adminga xabar yozish", callback_data="contact_admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==========================================
# 1. ASOSIY BUYRUQLAR (Start, Stop)
# ==========================================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    chat_id = message.chat.id
    stats_manager.register_user(message.from_user.id, message.from_user.full_name, message.from_user.username)

    cleared = False
    if chat_id in waiting_rooms:
        del waiting_rooms[chat_id]
        cleared = True
    if chat_id in active_tests:
        if active_tests[chat_id].get("timer_task"): active_tests[chat_id]["timer_task"].cancel()
        del active_tests[chat_id]
        cleared = True
        
    if cleared: await message.answer("🔄 Eski tugallanmagan testlaringiz tozalandi.")
    await message.answer("🏛 *Talabalar Imtihon Trenajyori*\n\nAssalomu alaykum! Fanni tanlang:", reply_markup=get_subjects_keyboard(), parse_mode="Markdown")

@router.message(Command("stop"))
async def cmd_stop(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    chat_id = message.chat.id
    user_id = message.from_user.id

    if chat_id in waiting_rooms:
        if user_id == waiting_rooms[chat_id]["initiator_id"] or message.chat.type == "private":
            del waiting_rooms[chat_id]
            await message.answer("🛑 Test bekor qilindi.")
        else: await message.answer("⚠️ Faqat testni boshlagan odam uni bekor qila oladi!")
        return

    if chat_id in active_tests:
        if message.chat.type != "private" and user_id != active_tests[chat_id].get("initiator_id"):
            return await message.answer("⚠️ Faqat testni boshlagan odam uni to'xtata oladi!")
        await message.answer("🛑 *Test to'xtatildi!*\nNatijalar hisoblanmoqda...", parse_mode="Markdown")
        await finish_test(chat_id, bot)
    else: await message.answer("ℹ️ Hozir bu chatda hech qanday test yo'q.")

# ==========================================
# 2. ADMIN PANEL VA MULOQOT TIZIMI
# ==========================================
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID: return await message.answer("⛔ Siz admin emassiz!")
    users = stats_manager.get_all_users()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Barchaga xabar yuborish", callback_data="admin_broadcast")]])
    await message.answer(f"👨‍💻 *ADMIN PANEL*\n\n👥 Jami foydalanuvchilar: {len(users)} ta", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.message.answer("📝 Barchaga yuboriladigan xabar matnini yozing.\n(Bekor qilish uchun /start)")
    await callback.answer()

@router.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = stats_manager.get_all_users()
    success, fail = 0, 0
    await message.answer("⏳ Xabar yuborilmoqda...")
    for u in users:
        try:
            await bot.send_message(chat_id=u["telegram_id"], text=message.text)
            success += 1
            await asyncio.sleep(0.05)
        except: fail += 1
    await message.answer(f"✅ Ommaviy xabar yakunlandi!\n🟢 Yetib bordi: {success}\n🔴 Bloklaganlar: {fail}")

@router.message(Command("message"))
async def cmd_message(message: Message, state: FSMContext):
    await state.set_state(UserStates.waiting_for_message)
    await message.answer("✍️ Adminga o'z savol yoki taklifingizni yozing:")

@router.callback_query(F.data == "contact_admin")
async def cb_contact_admin(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_message)
    await callback.message.answer("✍️ Adminga xabaringizni yozing:")
    await callback.answer()

@router.message(UserStates.waiting_for_message)
async def send_to_admin(message: Message, state: FSMContext, bot: Bot):
    text = f"📨 *YANGI XABAR!*\n\n👤 {message.from_user.full_name}\nID: `{message.from_user.id}`\n💬 Matn:\n{message.text}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Javob berish", callback_data=f"reply_{message.from_user.id}")]])
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="Markdown")
        await message.answer("✅ Xabaringiz adminga yetkazildi!")
    except: await message.answer("Xatolik yuz berdi.")
    await state.clear()

@router.callback_query(F.data.startswith("reply_"))
async def admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await state.update_data(target_id=callback.data.split("_")[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await callback.message.answer("✍️ Foydalanuvchiga javobingizni yozing:")
    await callback.answer()

@router.message(AdminStates.waiting_for_reply)
async def admin_reply_send(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    text = f"👨‍💻 *Admin tomonidan xabar:*\n\n{message.text}"
    try:
        await bot.send_message(data.get("target_id"), text, parse_mode="Markdown")
        await message.answer("✅ Javob yuborildi.")
    except: await message.answer("❌ Xatolik!")
    await state.clear()

# ==========================================
# 3. REYTING VA TEST MENYULARI
# ==========================================
@router.callback_query(F.data == "show_leaderboard")
async def show_leaderboard_handler(callback: CallbackQuery):
    await callback.message.edit_text("⏳ Reyting yuklanmoqda...", parse_mode="Markdown")
    top_users = stats_manager.get_top_users(10)
    
    if not top_users: text = "🏆 *GLOBAL REYTING*\n\nHozircha reytingda hech kim yo'q."
    else:
        text = "🏆 *TOP 10 TALABALAR REYTINGI*\n\n"
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        for i, user in enumerate(top_users):
            text += f"{medals[i] if i<10 else '🔸'} *{user['name']}*\n      ✅ {user['correct']} ta to'g'ri | 📝 {user['completed']} ta test\n\n"
            
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]]), parse_mode="Markdown")

def get_blocks_keyboard(subject_key: str, page: int = 0):
    buttons = []
    subject_tests = memory_db.get(subject_key, {})
    test_ids = sorted(subject_tests.keys())
    
    total_pages = (len(test_ids) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start_idx = page * ITEMS_PER_PAGE
    current_tests = test_ids[start_idx:start_idx + ITEMS_PER_PAGE]

    if not test_ids: buttons.append([InlineKeyboardButton(text="Testlar yo'q", callback_data="ignore")])
    else:
        for t_id in current_tests:
            buttons.append([InlineKeyboardButton(text=f"📘 {t_id}-Blok ({subject_tests[t_id].get('range', '?')})", callback_data=f"start_test_{subject_key}_{t_id}")])
            
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"page_{subject_key}_{page-1}"))
        if page < total_pages - 1: nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"page_{subject_key}_{page+1}"))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="🎲 Aralash Test (Mock Exam)", callback_data=f"mock_{subject_key}")])
    buttons.append([InlineKeyboardButton(text="🔙 Fanlarga", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    try: await callback.message.edit_text("🏛 *Talabalar Imtihon Trenajyori*\n\nFanni tanlang:", reply_markup=get_subjects_keyboard(), parse_mode="Markdown")
    except: pass

@router.callback_query(F.data.startswith("subj_"))
async def process_subject_selection(callback: CallbackQuery):
    subject_key = callback.data.split("_")[1]
    await callback.message.edit_text(f"📚 *{SUBJECTS.get(subject_key, 'Fan')}*\n\nBloklardan birini tanlang:", reply_markup=get_blocks_keyboard(subject_key, 0), parse_mode="Markdown")

@router.callback_query(F.data.startswith("page_"))
async def process_page(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=get_blocks_keyboard(callback.data.split("_")[1], int(callback.data.split("_")[2])))

@router.callback_query(F.data == "show_stats")
async def show_stats_handler(callback: CallbackQuery):
    if callback.message.chat.type != "private": return await callback.answer("Faqat botning shaxsiy chatida ko'rish mumkin!", show_alert=True)
    stats = stats_manager.get_user_stats(callback.from_user.id)
    total = stats['total_correct'] + stats['total_wrong']
    percent = (stats['total_correct'] / total * 100) if total > 0 else 0
    text = f"📊 *Shaxsiy statistika:*\n\n✅ To'g'ri: {stats['total_correct']}\n❌ Xato: {stats['total_wrong']}\n🎯 O'zlashtirish: {percent:.1f}%"
    
    buttons = []
    if stats.get("history"): buttons.append([InlineKeyboardButton(text="📜 Tarix va xatolar", callback_data="hist_page_0")])
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_to_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@router.callback_query(F.data.startswith("hist_page_"))
async def show_history_page(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if not history: return await callback.answer("Tarix bo'sh!", show_alert=True)
    
    start_idx = page * 5
    buttons = []
    for i, item in enumerate(history[start_idx:start_idx + 5]):
        t_id = "Aralash" if str(item['test_id']) == "mock" else f"{item['test_id']}-B"
        buttons.append([InlineKeyboardButton(text=f"📅 {item['date'][:10]} | {SUBJECTS.get(item['subject'], 'Fan')} ({t_id}) | ✅ {item['correct']}", callback_data=f"hist_det_{start_idx + i}")])
        
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"hist_page_{page-1}"))
    if start_idx + 5 < len(history): nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"hist_page_{page+1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="show_stats")])
    
    await callback.message.edit_text("📜 *Oxirgi ishlangan testlar:*\nBatafsil ko'rish uchun tanlang.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@router.callback_query(F.data.startswith("hist_det_"))
async def show_history_detail(callback: CallbackQuery):
    idx = int(callback.data.split("_")[2])
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if idx >= len(history): return await callback.answer("Xatolik!", show_alert=True)
        
    item = history[idx]
    t_id = "Aralash Test" if str(item['test_id']) == "mock" else f"{item['test_id']}-Blok"
    text = f"📅 {item['date']}\n📚 {SUBJECTS.get(item['subject'], 'Fan')} | {t_id}\n📊 ✅ {item['correct']}, ❌ {item['wrong']}\n\n"
    
    if not item.get("mistakes"): text += "🎉 *Xato qilinmagan!*"
    else:
        text += "📑 *XATOLAR:*\n\n"
        for i, m in enumerate(item["mistakes"], 1): text += f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}\n\n"

    if len(text) > 4000: text = text[:4000] + "\n... (kesildi)."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="hist_page_0")]]), parse_mode="Markdown")

# ==========================================
# 4. TEST JARAYONI VA TAYMER
# ==========================================
@router.callback_query(F.data.startswith("start_test_") | F.data.startswith("mock_"))
async def start_test_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id in active_tests or chat_id in waiting_rooms:
        return await callback.answer("⚠️ Bu chatda tugallanmagan yoki kutilayotgan test bor.\nUni to'xtatish uchun /stop yozing!", show_alert=True)

    parts = callback.data.split("_")
    if parts[0] == "mock":
        subject_key = parts[1]
        all_q = [q for test in memory_db.get(subject_key, {}).values() for q in test["questions"]]
        if not all_q: return await callback.answer("Savollar yo'q!", show_alert=True)
        test_data = {"questions": random.sample(all_q, min(25, len(all_q)))}
        test_id = "mock"
    else:
        subject_key, test_id = parts[2], int(parts[3])
        test_data = memory_db.get(subject_key, {}).get(test_id)

    chat_type = callback.message.chat.type
    if chat_type != "private":
        waiting_rooms[chat_id] = {"subject_key": subject_key, "test_id": test_id, "test_data": test_data, "ready_users": set(), "initiator_id": callback.from_user.id}
        try: await callback.message.delete()
        except: pass
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tayyorman (0)", callback_data="room_ready")], [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")]])
        await bot.send_message(chat_id, f"👥 *Guruh rejimi!*\n📚 {SUBJECTS.get(subject_key, 'Fan')} | {'Aralash Test' if str(test_id)=='mock' else f'{test_id}-Blok'}\n\nTestni boshlash uchun kamida 2 kishi tayyor bo'lishi kerak!", reply_markup=kb, parse_mode="Markdown")
        return

    session_q = prepare_shuffled_questions(test_data["questions"])
    active_tests[chat_id] = {
        "chat_type": chat_type, "initiator_id": callback.from_user.id, "subject_key": subject_key, "test_id": test_id,
        "session_questions": session_q, "q_idx": 0, "start_time": time.time(), "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {} 
    }
    try: await callback.message.delete()
    except: pass
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "room_ready")
async def room_ready_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms: return await callback.answer("Kutish zali yopilgan!", show_alert=True)
    room = waiting_rooms[chat_id]
    if callback.from_user.id in room["ready_users"]: return await callback.answer("Siz tayyorsiz!", show_alert=True)
        
    room["ready_users"].add(callback.from_user.id)
    count = len(room["ready_users"])
    buttons = [[InlineKeyboardButton(text=f"✅ Tayyorman ({count})", callback_data="room_ready")]]
    if count >= 2: buttons.append([InlineKeyboardButton(text="🚀 Boshlash", callback_data="room_start")])
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "room_start")
async def room_start_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms: return
    room = waiting_rooms[chat_id]
    if len(room["ready_users"]) < 2: return await callback.answer("Kamida 2 kishi tayyor bo'lishi kerak!", show_alert=True)
        
    session_q = prepare_shuffled_questions(room["test_data"]["questions"])
    active_tests[chat_id] = {
        "chat_type": "group", "initiator_id": room["initiator_id"], "subject_key": room["subject_key"], "test_id": room["test_id"],
        "session_questions": session_q, "q_idx": 0, "start_time": time.time(), "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {} 
    }
    del waiting_rooms[chat_id]
    await callback.message.delete()
    await bot.send_message(chat_id, "🚀 *Test boshlandi!*", parse_mode="Markdown")
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "room_cancel")
async def room_cancel_handler(callback: CallbackQuery):
    if callback.message.chat.id in waiting_rooms:
        if callback.from_user.id == waiting_rooms[callback.message.chat.id]["initiator_id"]:
            del waiting_rooms[callback.message.chat.id]
            await callback.message.delete()
            await callback.message.answer("Test bekor qilindi.")
        else: await callback.answer("Faqat tanlagan odam bekor qila oladi!", show_alert=True)

async def question_timeout_task(chat_id: int, expected_q_idx: int, poll_id: str, bot: Bot):
    try: await asyncio.sleep(30) 
    except asyncio.CancelledError: return
    
    session = active_tests.get(chat_id)
    if not session or session["q_idx"] != expected_q_idx or session["poll_id"] != poll_id: return
    try: await bot.stop_poll(chat_id=chat_id, message_id=session["msg_id"])
    except: pass 

    q_data = session["session_questions"][expected_q_idx]
    if session["chat_type"] == "private":
        session["wrong"] += 1
        session["consecutive_timeouts"] += 1 
        session["mistakes"].append({"question": q_data["question"], "correct_ans": q_data["correct_text"], "wrong_ans": "⏳ Vaqt tugadi"})
        session["q_idx"] += 1
        if session["consecutive_timeouts"] >= 2 and session["q_idx"] < len(session["session_questions"]):
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Davom etish", callback_data="resume_test")], [InlineKeyboardButton(text="🛑 Yakunlash", callback_data="force_finish")]])
            await bot.send_message(chat_id, "⏸ *Test to'xtatildi!*\nKetma-ket 2 ta savolga javob bermadingiz.", reply_markup=kb, parse_mode="Markdown")
        else: await send_next_question(chat_id, bot)
    else:
        session["q_idx"] += 1
        await send_next_question(chat_id, bot)

@router.callback_query(F.data == "resume_test")
async def resume_test_handler(callback: CallbackQuery, bot: Bot):
    if callback.message.chat.id in active_tests:
        active_tests[callback.message.chat.id]["consecutive_timeouts"] = 0 
        await callback.message.delete()
        await send_next_question(callback.message.chat.id, bot)

@router.callback_query(F.data == "force_finish")
async def force_finish_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.delete()
    await finish_test(callback.message.chat.id, bot)

# ⚠️ UZUN MATNLAR UCHUN HIMOYA SHU YERDA!
async def send_next_question(chat_id: int, bot: Bot):
    session = active_tests.get(chat_id)
    if not session: return

    questions = session["session_questions"]
    q_idx = session["q_idx"]
    if q_idx >= len(questions): return await finish_test(chat_id, bot)

    q = questions[q_idx]
    q_text_full = f"[{q_idx + 1}/{len(questions)}] {q['question']}"
    
    needs_text = len(q_text_full) > 255 or any(len(opt) > 100 for opt in q["options"])
    
    if needs_text:
        labels = ["A", "B", "C", "D", "E", "F"]
        text_msg = f"📑 Savol [{q_idx + 1}/{len(questions)}]\n\n{q['question']}\n\nVariantlar:\n"
        for i, opt in enumerate(q['options']): text_msg += f"{labels[i]}) {opt}\n"
        if len(text_msg) > 4000: text_msg = text_msg[:4000] + "...\n(Xabar kesildi)"
        await bot.send_message(chat_id, text_msg)
        
        poll_q = f"[{q_idx + 1}/{len(questions)}] To'g'ri variantni belgilang:"
        poll_opts = [f"{labels[i]} varianti" for i in range(len(q['options']))]
    else:
        poll_q, poll_opts = q_text_full, q["options"]

    msg = await bot.send_poll(chat_id=chat_id, question=poll_q, options=poll_opts, type="quiz", correct_option_id=q["correct_index"], is_anonymous=False, open_period=30)
    session["poll_id"] = msg.poll.id
    session["msg_id"] = msg.message_id
    poll_chat_map[msg.poll.id] = chat_id
    if session.get("timer_task"): session["timer_task"].cancel()
    session["timer_task"] = asyncio.create_task(question_timeout_task(chat_id, q_idx, msg.poll.id, bot))

@router.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer, bot: Bot):
    chat_id = poll_chat_map.get(poll_answer.poll_id)
    if not chat_id or chat_id not in active_tests: return
    session = active_tests[chat_id]
    if session["poll_id"] != poll_answer.poll_id: return

    q_data = session["session_questions"][session["q_idx"]]
    is_correct = (poll_answer.option_ids[0] == q_data["correct_index"])

    if session["chat_type"] == "private":
        session["consecutive_timeouts"] = 0
        if session.get("timer_task"): session["timer_task"].cancel()
        try: await bot.stop_poll(chat_id=chat_id, message_id=session["msg_id"])
        except: pass 

        if is_correct: session["correct"] += 1
        else:
            session["wrong"] += 1
            session["mistakes"].append({"question": q_data["question"], "correct_ans": q_data["correct_text"], "wrong_ans": q_data["options"][poll_answer.option_ids[0]]})
        session["q_idx"] += 1
        await send_next_question(chat_id, bot)
    else:
        u_id = poll_answer.user.id
        if u_id not in session["group_scores"]: session["group_scores"][u_id] = {"name": poll_answer.user.full_name, "correct": 0, "wrong": 0, "mistakes": []}
        if is_correct: session["group_scores"][u_id]["correct"] += 1
        else:
            session["group_scores"][u_id]["wrong"] += 1
            session["group_scores"][u_id]["mistakes"].append({"question": q_data["question"], "correct_ans": q_data["correct_text"], "wrong_ans": q_data["options"][poll_answer.option_ids[0]]})

async def finish_test(chat_id: int, bot: Bot):
    session = active_tests.get(chat_id)
    if not session: return
    if session.get("timer_task"): session["timer_task"].cancel()
    
    mins, secs = divmod(int(time.time() - session["start_time"]), 60)
    title = f"{SUBJECTS.get(session['subject_key'], 'Fan')} | " + ("Aralash Test" if session['test_id'] == 'mock' else f"{session['test_id']}-Blok")
    buttons = []
    
    if session["chat_type"] == "private":
        stats_manager.update_user_stats(chat_id, session["correct"], session["wrong"], session["subject_key"], session["test_id"], session["mistakes"])
        text = f"🏁 *{title} Yakunlandi!*\n\n🟢 To'g'ri: {session['correct']}\n🔴 Xato: {session['wrong']}\n⏱ Vaqt: {mins:02d}:{secs:02d}"
        if session.get("mistakes"): buttons.append([InlineKeyboardButton(text="❌ Xatolar ustida ishlash", callback_data="review_mistakes")])
        if session['test_id'] != 'mock':
            buttons.append([InlineKeyboardButton(text="🔁 Qayta ishlash", callback_data=f"post_start_{session['subject_key']}_{session['test_id']}")])
            if session['test_id'] + 1 in memory_db.get(session['subject_key'], {}):
                buttons.append([InlineKeyboardButton(text="➡️ Keyingi Blok", callback_data=f"post_start_{session['subject_key']}_{session['test_id'] + 1}")])
    else:
        for u_id, scores in session["group_scores"].items():
            stats_manager.update_user_stats(u_id, scores["correct"], scores["wrong"], session["subject_key"], session["test_id"], scores["mistakes"])
        text = f"🏁 *{title} yakunlandi!*\n⏱ Vaqt: {mins:02d}:{secs:02d}\n\n🏆 *NATIJALAR:*\n"
        if not session["group_scores"]: text += "Hech kim qatnashmadi 😔"
        else:
            for i, score in enumerate(sorted(session["group_scores"].values(), key=lambda x: x["correct"], reverse=True)):
                text += f"{['🥇', '🥈', '🥉'][i] if i < 3 else '🔸'} {score['name']}: {score['correct']} ta to'g'ri\n"
                
    buttons.extend([[InlineKeyboardButton(text="🔙 Fan menyusiga", callback_data=f"post_subj_{session['subject_key']}")], [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")]])
    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    if session["poll_id"] in poll_chat_map: del poll_chat_map[session["poll_id"]]
    del active_tests[chat_id] 

@router.callback_query(F.data.startswith("post_start_"))
async def post_start_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_reply_markup(reply_markup=None) 
    callback.data = f"start_test_{callback.data.split('_')[2]}_{callback.data.split('_')[3]}"
    await start_test_handler(callback, bot)

@router.callback_query(F.data.startswith("post_subj_"))
async def post_subj_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_reply_markup(reply_markup=None)
    k = callback.data.split("_")[2]
    await bot.send_message(callback.message.chat.id, f"📚 *{SUBJECTS.get(k, 'Fan')}*\n\nBloklardan birini tanlang:", reply_markup=get_blocks_keyboard(k, 0), parse_mode="Markdown")

@router.callback_query(F.data == "post_main")
async def post_main_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_reply_markup(reply_markup=None)
    await bot.send_message(callback.message.chat.id, "🏛 *Talabalar Imtihon Trenajyori*\n\nFanni tanlang:", reply_markup=get_subjects_keyboard(), parse_mode="Markdown")

@router.callback_query(F.data == "review_mistakes")
async def review_mistakes_handler(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if not history or not history[0].get("mistakes"): return await callback.message.answer("Xatolar topilmadi.")
    text = "📑 *XATOLAR USTIDA ISHLASH*\n\n"
    for i, m in enumerate(history[0]["mistakes"], 1): text += f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}\n\n"
    if len(text) > 4000: text = text[:4000] + "\n... (qolgani kesildi)."
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy", callback_data="post_main")]]), parse_mode="Markdown")
