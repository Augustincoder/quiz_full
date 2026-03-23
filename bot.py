import time
import random
import asyncio
from aiogram import Bot, Router, F
from aiogram.types import Message, CallbackQuery, PollAnswer, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
import stats_manager
from config import SUBJECTS

router = Router()

# --- Holatlarni saqlash (State Management) ---
active_tests = {}
waiting_rooms = {}  # Guruhlar uchun kutish zali
poll_chat_map = {} 
memory_db = {}
ITEMS_PER_PAGE = 5  

def truncate_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit-3] + "..."

# --- 1. ASOSIY MENYU ---
def get_subjects_keyboard():
    buttons = []
    for subj_key, subj_name in SUBJECTS.items():
        block_count = len(memory_db.get(subj_key, {}))
        btn_text = f"{subj_name} ({block_count} ta blok)"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"subj_{subj_key}")])
    
    buttons.append([InlineKeyboardButton(text="📊 Mening Statistikam", callback_data="show_stats")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.message(Command("start"))
async def cmd_start(message: Message):
    kb = get_subjects_keyboard()
    text = (
        "🏛 *Talabalar Imtihon Trenajyori*\n\n"
        "Assalomu alaykum! Tayyorgarlik ko'rmoqchi bo'lgan fanni tanlang:"
    )
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    kb = get_subjects_keyboard()
    try:
        await callback.message.edit_text("🏛 *Talabalar Imtihon Trenajyori*\n\nFanni tanlang:", reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.delete()
        await callback.message.answer("🏛 *Talabalar Imtihon Trenajyori*\n\nFanni tanlang:", reply_markup=kb, parse_mode="Markdown")

# --- 2. BLOKLAR MENYUSI ---
def get_blocks_keyboard(subject_key: str, page: int = 0):
    buttons = []
    subject_tests = memory_db.get(subject_key, {})
    test_ids = sorted(subject_tests.keys())
    
    total_pages = (len(test_ids) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    current_tests = test_ids[start_idx:end_idx]

    if not test_ids:
        buttons.append([InlineKeyboardButton(text="Hozircha testlar yo'q", callback_data="ignore")])
    else:
        for t_id in current_tests:
            data = subject_tests[t_id]
            btn = InlineKeyboardButton(
                text=f"📘 {t_id}-Blok (Savollar: {data.get('range', '?')})", 
                callback_data=f"start_test_{subject_key}_{t_id}"
            )
            buttons.append([btn])
            
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"page_{subject_key}_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"page_{subject_key}_{page+1}"))
        if nav_buttons:
            buttons.append(nav_buttons)

        buttons.append([InlineKeyboardButton(text="🎲 Aralash Test (Mock Exam)", callback_data=f"mock_{subject_key}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Fanlar ro'yxatiga", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.callback_query(F.data.startswith("subj_"))
async def process_subject_selection(callback: CallbackQuery):
    subject_key = callback.data.split("_")[1]
    kb = get_blocks_keyboard(subject_key, page=0)
    subj_name = SUBJECTS.get(subject_key, "Fan")
    await callback.message.edit_text(f"📚 *{subj_name}*\n\nBloklardan birini tanlang:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("page_"))
async def process_page(callback: CallbackQuery):
    parts = callback.data.split("_")
    subject_key = parts[1]
    page = int(parts[2])
    kb = get_blocks_keyboard(subject_key, page=page)
    await callback.message.edit_reply_markup(reply_markup=kb)

# --- 3. STATISTIKA VA TARIX ---
@router.callback_query(F.data == "show_stats")
async def show_stats_handler(callback: CallbackQuery):
    if callback.message.chat.type != "private":
        await callback.answer("Statistikani faqat botning shaxsiy xabarida ko'rish mumkin!", show_alert=True)
        return

    stats = stats_manager.get_user_stats(callback.from_user.id)
    total = stats['total_correct'] + stats['total_wrong']
    percent = (stats['total_correct'] / total * 100) if total > 0 else 0
    
    text = (
        "📊 *Sizning shaxsiy statistikangiz:*\n\n"
        f"✅ *To'g'ri javoblar:* {stats['total_correct']}\n"
        f"❌ *Xato/O'tkazib yuborilgan:* {stats['total_wrong']}\n"
        f"📝 *Tugatilgan bloklar:* {stats['tests_completed']}\n"
        f"🎯 *O'zlashtirish darajasi:* {percent:.1f}%\n"
    )
    
    buttons = []
    if stats.get("history"):
        buttons.append([InlineKeyboardButton(text="📜 Tarix va xatolarni ko'rish", callback_data="hist_page_0")])
    buttons.append([InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("hist_page_"))
async def show_history_page(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    stats = stats_manager.get_user_stats(callback.from_user.id)
    history = stats.get("history", [])
    
    if not history:
        await callback.answer("Tarix bo'sh!", show_alert=True)
        return

    start_idx = page * 5
    end_idx = start_idx + 5
    current_items = history[start_idx:end_idx]
    
    text = "📜 *Oxirgi ishlangan testlar tarixi:*\nBatafsil ko'rish uchun tugmalardan birini tanlang."
    buttons = []
    
    for i, item in enumerate(current_items):
        actual_idx = start_idx + i
        subj_name = SUBJECTS.get(item['subject'], "Fan")
        t_id = "Aralash" if str(item['test_id']) == "mock" else f"{item['test_id']}-Blok"
        btn_text = f"📅 {item['date']} | {subj_name} ({t_id}) | ✅ {item['correct']}"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"hist_det_{actual_idx}")])
        
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"hist_page_{page-1}"))
    if end_idx < len(history):
        nav_buttons.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"hist_page_{page+1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
        
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="show_stats")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("hist_det_"))
async def show_history_detail(callback: CallbackQuery):
    idx = int(callback.data.split("_")[2])
    stats = stats_manager.get_user_stats(callback.from_user.id)
    history = stats.get("history", [])
    
    if idx >= len(history):
        await callback.answer("Xatolik!", show_alert=True)
        return
        
    item = history[idx]
    subj_name = SUBJECTS.get(item['subject'], "Fan")
    t_id = "Aralash Test" if str(item['test_id']) == "mock" else f"{item['test_id']}-Blok"
    
    text = (
        f"📅 *Sana:* {item['date']}\n"
        f"📚 *Fan:* {subj_name} | {t_id}\n"
        f"📊 *Natija:* ✅ {item['correct']} ta to'g'ri, ❌ {item['wrong']} ta xato\n\n"
    )
    
    if not item.get("mistakes"):
        text += "🎉 *Siz bu testda umuman xato qilmagansiz!*"
    else:
        text += "📑 *XATOLAR ROYXATI:*\n\n"
        for i, m in enumerate(item["mistakes"], 1):
            text += f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}\n\n"

    if len(text) > 4000: text = text[:4000] + "\n... (xabar juda uzun, qolgani kesildi)."
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Ro'yxatga qaytish", callback_data="hist_page_0")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# --- 4. GURUHDA KUTISH ZALI ---
@router.callback_query(F.data == "room_ready")
async def room_ready_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms:
        await callback.answer("Kutish zali topilmadi yoxud test allaqachon boshlangan!", show_alert=True)
        return
        
    room = waiting_rooms[chat_id]
    user_id = callback.from_user.id
    
    if user_id in room["ready_users"]:
        await callback.answer("Siz allaqachon tayyorsiz!", show_alert=True)
        return
        
    room["ready_users"].add(user_id)
    count = len(room["ready_users"])
    
    buttons = [
        [InlineKeyboardButton(text=f"✅ Tayyorman ({count})", callback_data="room_ready")]
    ]
    
    # 2 kishidan oshganda Boshlash tugmasi chiqadi
    if count >= 2:
        buttons.append([InlineKeyboardButton(text="🚀 Boshlash", callback_data="room_start")])
        
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer("Siz testga tayyorsiz!")

@router.callback_query(F.data == "room_start")
async def room_start_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms: return
        
    room = waiting_rooms[chat_id]
    
    if len(room["ready_users"]) < 2:
        await callback.answer("Kamida 2 kishi tayyor bo'lishi kerak!", show_alert=True)
        return
        
    # Active testga o'tkazish
    active_tests[chat_id] = {
        "chat_type": "group",
        "subject_key": room["subject_key"],
        "test_id": room["test_id"],
        "test_data": room["test_data"],
        "q_idx": 0,
        "start_time": time.time(),
        "poll_id": None,
        "msg_id": None,
        "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [], 
        "consecutive_timeouts": 0,
        "group_scores": {} 
    }
    
    del waiting_rooms[chat_id]
    
    await callback.message.delete()
    await bot.send_message(chat_id, "🚀 *Test boshlandi!* Barcha ishtirokchilar javob belgilashlari mumkin.", parse_mode="Markdown")
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "room_cancel")
async def room_cancel_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id in waiting_rooms:
        room = waiting_rooms[chat_id]
        if callback.from_user.id == room["initiator_id"]:
            del waiting_rooms[chat_id]
            await callback.message.delete()
            await callback.message.answer("Test bekor qilindi.")
        else:
            await callback.answer("Faqat testni tanlagan odam bekor qila oladi!", show_alert=True)

# --- 5. TESTNI BOSHLASH VA TAYMER ---
@router.callback_query(F.data.startswith("start_test_") | F.data.startswith("mock_"))
async def start_test_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    
    if chat_id in active_tests:
        await callback.answer("Bu chatda allaqachon test ketmoqda!", show_alert=True)
        return
    if chat_id in waiting_rooms:
        await callback.answer("Bu chatda test boshlanishi kutilmoqda!", show_alert=True)
        return

    parts = callback.data.split("_")
    
    if parts[0] == "mock":
        subject_key = parts[1]
        subject_tests = memory_db.get(subject_key, {})
        all_q = [q for test in subject_tests.values() for q in test["questions"]]
        if not all_q:
            await callback.answer("Bu fanda savollar yo'q!", show_alert=True)
            return
        selected_questions = random.sample(all_q, min(25, len(all_q)))
        test_id = "mock"
        test_data = {"questions": selected_questions}
    else:
        subject_key = parts[2]
        test_id = int(parts[3])
        test_data = memory_db.get(subject_key, {}).get(test_id)
        if not test_data:
            await callback.answer("Test topilmadi!", show_alert=True)
            return

    chat_type = callback.message.chat.type

    # Guruhlar uchun avval kutish zali ochiladi
    if chat_type != "private":
        waiting_rooms[chat_id] = {
            "subject_key": subject_key,
            "test_id": test_id,
            "test_data": test_data,
            "ready_users": set(),
            "initiator_id": callback.from_user.id
        }
        
        try:
            await callback.message.delete()
        except Exception:
            pass
            
        t_name = "Aralash Test" if str(test_id) == "mock" else f"{test_id}-Blok"
        subj_name = SUBJECTS.get(subject_key, "Fan")
        
        text = (
            f"👥 *Guruh test rejimi faollashdi!*\n"
            f"📚 *Fan:* {subj_name} | {t_name}\n\n"
            f"Testni boshlash uchun kamida 2 kishi tayyor bo'lishi kerak!"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Tayyorman (0)", callback_data="room_ready")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")]
        ])
        
        msg = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="Markdown")
        waiting_rooms[chat_id]["msg_id"] = msg.message_id
        return

    # Shaxsiy rejim uchun darhol boshlanadi
    active_tests[chat_id] = {
        "chat_type": chat_type,
        "subject_key": subject_key,
        "test_id": test_id,
        "test_data": test_data,
        "q_idx": 0,
        "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [], 
        "consecutive_timeouts": 0,
        "group_scores": {} 
    }
    
    try:
        await callback.message.delete()
    except Exception:
        pass
        
    await send_next_question(chat_id, bot)

async def question_timeout_task(chat_id: int, expected_q_idx: int, poll_id: str, bot: Bot):
    try:
        await asyncio.sleep(30) 
    except asyncio.CancelledError:
        return

    session = active_tests.get(chat_id)
    if not session or session["q_idx"] != expected_q_idx or session["poll_id"] != poll_id:
        return

    try:
        await bot.stop_poll(chat_id=chat_id, message_id=session["msg_id"])
    except Exception:
        pass 

    q_data = session["test_data"]["questions"][expected_q_idx]
    correct_idx = q_data["correct_index"]

    if session["chat_type"] == "private":
        session["wrong"] += 1
        session["consecutive_timeouts"] += 1 
        session["mistakes"].append({
            "question": q_data["question"],
            "correct_ans": q_data["options"][correct_idx],
            "wrong_ans": "⏳ Vaqt tugadi"
        })
        session["q_idx"] += 1
        if session["consecutive_timeouts"] >= 2 and session["q_idx"] < len(session["test_data"]["questions"]):
            await show_pause_menu(chat_id, bot)
        else:
            await send_next_question(chat_id, bot)
    else:
        # Guruhda vaqt tugagach darhol keyingi savolga o'tiladi
        session["q_idx"] += 1
        await send_next_question(chat_id, bot)

async def show_pause_menu(chat_id: int, bot: Bot):
    session = active_tests.get(chat_id)
    if not session: return

    text = "⏸ *Test to'xtatildi!*\nKetma-ket 2 ta savolga javob bermadingiz."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Davom etish", callback_data="resume_test")],
        [InlineKeyboardButton(text="🛑 Yakunlash", callback_data="force_finish")]
    ])
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "resume_test")
async def resume_test_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id in active_tests:
        active_tests[chat_id]["consecutive_timeouts"] = 0 
        await callback.message.delete()
        await send_next_question(chat_id, bot)

@router.callback_query(F.data == "force_finish")
async def force_finish_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.delete()
    await finish_test(callback.message.chat.id, bot)

async def send_next_question(chat_id: int, bot: Bot):
    session = active_tests.get(chat_id)
    if not session: return

    test_data = session["test_data"]
    q_idx = session["q_idx"]

    if q_idx >= len(test_data["questions"]):
        await finish_test(chat_id, bot)
        return

    q = test_data["questions"][q_idx]
    question_text = truncate_text(f"[{q_idx + 1}/{len(test_data['questions'])}] {q['question']}", 300)
    options = [truncate_text(opt, 100) for opt in q["options"]]

    msg = await bot.send_poll(
        chat_id=chat_id, question=question_text, options=options,
        type="quiz", correct_option_id=q["correct_index"], is_anonymous=False, open_period=30 
    )

    session["poll_id"] = msg.poll.id
    session["msg_id"] = msg.message_id
    poll_chat_map[msg.poll.id] = chat_id

    if session.get("timer_task"):
        session["timer_task"].cancel()
    session["timer_task"] = asyncio.create_task(question_timeout_task(chat_id, q_idx, msg.poll.id, bot))

@router.poll_answer()
async def handle_poll_answer(poll_answer: PollAnswer, bot: Bot):
    chat_id = poll_chat_map.get(poll_answer.poll_id)
    if not chat_id or chat_id not in active_tests: return

    session = active_tests[chat_id]
    if session["poll_id"] != poll_answer.poll_id: return

    q_idx = session["q_idx"]
    q_data = session["test_data"]["questions"][q_idx]
    correct_idx = q_data["correct_index"]
    user_choice = poll_answer.option_ids[0]
    is_correct = (user_choice == correct_idx)

    # SHAXSIY REJIM (Darhol to'xtaydi)
    if session["chat_type"] == "private":
        session["consecutive_timeouts"] = 0
        if session.get("timer_task"):
            session["timer_task"].cancel()
            session["timer_task"] = None

        try:
            await bot.stop_poll(chat_id=chat_id, message_id=session["msg_id"])
        except Exception:
            pass 

        if is_correct:
            session["correct"] += 1
        else:
            session["wrong"] += 1
            session["mistakes"].append({
                "question": q_data["question"],
                "correct_ans": q_data["options"][correct_idx],
                "wrong_ans": q_data["options"][user_choice]
            })

        session["q_idx"] += 1
        await send_next_question(chat_id, bot)
        
    # GURUH REJIMI (Vaqt tugashini kutadi va hamma a'zolar xatolarini yozib boradi)
    else:
        user_id = poll_answer.user.id
        name = poll_answer.user.full_name
        
        if user_id not in session["group_scores"]:
            session["group_scores"][user_id] = {"name": name, "correct": 0, "wrong": 0, "mistakes": []}
            
        if is_correct:
            session["group_scores"][user_id]["correct"] += 1
        else:
            session["group_scores"][user_id]["wrong"] += 1
            session["group_scores"][user_id]["mistakes"].append({
                "question": q_data["question"],
                "correct_ans": q_data["options"][correct_idx],
                "wrong_ans": q_data["options"][user_choice]
            })

# --- 6. YAKUNLASH VA NATIJALAR ---
async def finish_test(chat_id: int, bot: Bot):
    session = active_tests[chat_id]
    if session.get("timer_task"): session["timer_task"].cancel()
    
    elapsed = int(time.time() - session["start_time"])
    mins, secs = divmod(elapsed, 60)
    subj_name = SUBJECTS.get(session['subject_key'], "Fan")
    title = f"{subj_name} | Aralash Test" if session['test_id'] == 'mock' else f"{subj_name} | {session['test_id']}-Blok"
    
    buttons = []
    
    # SHAXSIY REJIM YAKUNI
    if session["chat_type"] == "private":
        stats_manager.update_user_stats(chat_id, session["correct"], session["wrong"], session["subject_key"], session["test_id"], session["mistakes"])
        
        text = (f"🏁 *{title} Yakunlandi!*\n\n"
                f"🟢 *To'g'ri:* {session['correct']}\n🔴 *Xato/O'tkazilgan:* {session['wrong']}\n⏱ *Vaqt:* {mins:02d}:{secs:02d}")

        if session.get("mistakes"):
            buttons.append([InlineKeyboardButton(text="❌ Xatolar ustida ishlash", callback_data="review_mistakes")])
            
        if session['test_id'] != 'mock':
            buttons.append([InlineKeyboardButton(text="🔁 Qayta ishlash", callback_data=f"post_start_{session['subject_key']}_{session['test_id']}")])
            next_test_id = session['test_id'] + 1
            if next_test_id in memory_db.get(session['subject_key'], {}):
                buttons.append([InlineKeyboardButton(text="➡️ Keyingi Blok", callback_data=f"post_start_{session['subject_key']}_{next_test_id}")])
                
        buttons.append([InlineKeyboardButton(text="🔙 Fan menyusiga", callback_data=f"post_subj_{session['subject_key']}")])
        buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")])
        
    # GURUH REJIMI YAKUNI (Leaderboard tuzish)
    else:
        # Guruh ishtirokchilarining shaxsiy bazasiga natijalarni yozish
        for u_id, scores in session["group_scores"].items():
            stats_manager.update_user_stats(
                user_id=u_id, 
                correct=scores["correct"], 
                wrong=scores["wrong"], 
                subject_key=session["subject_key"], 
                test_id=session["test_id"], 
                mistakes=scores["mistakes"]
            )
            
        text = f"🏁 *{title} yakunlandi!*\n⏱ *Sarflangan vaqt:* {mins:02d}:{secs:02d}\n\n🏆 *GURUH NATIJALARI:*\n"
        
        if not session["group_scores"]:
            text += "Hech kim javob bermadi 😔"
        else:
            sorted_scores = sorted(session["group_scores"].values(), key=lambda x: x["correct"], reverse=True)
            medals = ["🥇", "🥈", "🥉"]
            for i, score in enumerate(sorted_scores):
                medal = medals[i] if i < 3 else "🔸"
                text += f"{medal} {score['name']}: {score['correct']} ta to'g'ri\n"
                
        buttons.append([InlineKeyboardButton(text="🔙 Fan menyusiga", callback_data=f"post_subj_{session['subject_key']}")])
        buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="Markdown")
    
    if session["poll_id"] in poll_chat_map: del poll_chat_map[session["poll_id"]]
    del active_tests[chat_id] 


# --- 7. TARIXNI SAQLAB YANGI XABARGA O'TISH ("post_" harakatlari) ---
@router.callback_query(F.data.startswith("post_start_"))
async def post_start_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_reply_markup(reply_markup=None) # Eski tugmalarni o'chiramiz
    parts = callback.data.split("_")
    subject_key = parts[2]
    test_id = parts[3]
    callback.data = f"start_test_{subject_key}_{test_id}"
    await start_test_handler(callback, bot)

@router.callback_query(F.data.startswith("post_subj_"))
async def post_subj_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_reply_markup(reply_markup=None)
    subject_key = callback.data.split("_")[2]
    kb = get_blocks_keyboard(subject_key, page=0)
    subj_name = SUBJECTS.get(subject_key, "Fan")
    await bot.send_message(callback.message.chat.id, f"📚 *{subj_name}*\n\nBloklardan birini tanlang:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "post_main")
async def post_main_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_reply_markup(reply_markup=None)
    kb = get_subjects_keyboard()
    await bot.send_message(callback.message.chat.id, "🏛 *Talabalar Imtihon Trenajyori*\n\nFanni tanlang:", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "review_mistakes")
async def review_mistakes_handler(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    stats = stats_manager.get_user_stats(callback.from_user.id)
    history = stats.get("history", [])
    if not history or not history[0].get("mistakes"):
        await callback.message.answer("Xatolar topilmadi.")
        return

    text = "📑 *XATOLAR USTIDA ISHLASH*\n\n"
    for i, m in enumerate(history[0]["mistakes"], 1):
        text += f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}\n\n"

    if len(text) > 4000: text = text[:4000] + "\n... (qolgani kesildi)."
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy menyu", callback_data="post_main")]])
    await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown") 
