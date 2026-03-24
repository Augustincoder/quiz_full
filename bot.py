import os
import time
import random
import asyncio
import logging
from docx import Document
from aiogram import Bot, Router, F
from aiogram.types import Message, CallbackQuery, PollAnswer, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import stats_manager
from config import SUBJECTS, ADMIN_ID

logger = logging.getLogger(__name__)

router = Router()

active_tests: dict = {}
waiting_rooms: dict = {}
poll_chat_map: dict = {}
memory_db: dict = {}
ITEMS_PER_PAGE = 5

_group_answer_locks: dict[int, asyncio.Lock] = {}

def _get_group_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in _group_answer_locks:
        _group_answer_locks[chat_id] = asyncio.Lock()
    return _group_answer_locks[chat_id]

def _parse_suffix(data: str, prefix: str) -> str:
    return data[len(prefix):]

class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_reply = State()

class UserStates(StatesGroup):
    waiting_for_message = State()

class CreateTestStates(StatesGroup):
    waiting_for_subject = State()
    waiting_for_name = State()
    waiting_for_format = State()
    waiting_for_questions = State()

def prepare_shuffled_questions(raw_questions: list) -> list:
    shuffled_q = list(raw_questions)
    random.shuffle(shuffled_q)
    session_questions = []
    for q in shuffled_q:
        options = list(q["options"])
        correct_text = options[q["correct_index"]]
        random.shuffle(options)
        new_correct_idx = options.index(correct_text)
        session_questions.append({
            "question": q["question"],
            "options": options,
            "correct_index": new_correct_idx,
            "correct_text": correct_text,
        })
    return session_questions

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Rasmiy (Admin) Testlar", callback_data="official_tests")],
        [
            InlineKeyboardButton(text="📝 Test Yaratish", callback_data="create_test"),
            InlineKeyboardButton(text="📂 Mening Testlarim", callback_data="my_tests"),
        ],
        [
            InlineKeyboardButton(text="📊 Statistikam", callback_data="show_stats"),
            InlineKeyboardButton(text="🏆 Reyting", callback_data="show_leaderboard"),
        ],
        [InlineKeyboardButton(text="💬 Adminga xabar yozish", callback_data="contact_admin")],
    ])

# ==========================================
# 1. ASOSIY BUYRUQLAR VA DEEP-LINK
# ==========================================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    chat_id = message.chat.id
    stats_manager.register_user(
        message.from_user.id, message.from_user.full_name, message.from_user.username
    )

    cleared = False
    if chat_id in waiting_rooms:
        del waiting_rooms[chat_id]
        cleared = True
    if chat_id in active_tests:
        task = active_tests[chat_id].get("timer_task")
        if task: task.cancel()
        old_poll_id = active_tests[chat_id].get("poll_id")
        if old_poll_id and old_poll_id in poll_chat_map:
            del poll_chat_map[old_poll_id]
        del active_tests[chat_id]
        cleared = True
    if cleared:
        await message.answer("🔄 Eski tugallanmagan testlaringiz tozalandi.")

    args = message.text.split()
    if len(args) > 1:
        # Fan bo'yicha link (Deep-link) kelgan bo'lsa
        if args[1].startswith("s_"):
            ref_id = args[1][2:]
            test_data_db = stats_manager.get_user_test(ref_id)
            if not test_data_db:
                return await message.answer("❌ Bu fan topilmadi yoki muallif tomonidan o'chirilgan.")
            
            creator_id = test_data_db["creator_id"]
            subject = test_data_db["subject"]
            return await show_ugc_subject_blocks(message, creator_id, subject)
            
        # Aniq bitta blok bo'yicha link kelgan bo'lsa
        elif args[1].startswith("t_"):
            test_id = args[1][2:] 
            test_data_db = stats_manager.get_user_test(test_id)
            if test_data_db:
                return await start_ugc_test(message, test_data_db, bot)
            else:
                return await message.answer("❌ Bu blok topilmadi yoki muallif tomonidan o'chirilgan.")

    await message.answer(
        "🏛 *Talabalar Imtihon Trenajyori*\n\nAssalomu alaykum! Kerakli bo'limni tanlang:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown",
    )

async def show_ugc_subject_blocks(message: Message, creator_id: str, subject: str):
    """Deep-link orqali kirganda Fan ichidagi bloklarni ko'rsatish"""
    tests = stats_manager.get_user_created_tests(creator_id)
    subj_tests = [t for t in tests if t["subject"] == subject]
    
    if not subj_tests:
        return await message.answer("❌ Bu fanda bloklar topilmadi.")

    buttons = []
    for t in subj_tests:
        buttons.append([InlineKeyboardButton(text=f"📘 {t['block_name']}", callback_data=f"ugc_start_{t['id']}")])
    buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")])

    await message.answer(
        f"📚 *Fan:* {subject}\n\nQuyidagi test bloklaridan birini tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    try:
        await callback.message.edit_text(
            "🏛 *Talabalar Imtihon Trenajyori*\n\nKerakli bo'limni tanlang:",
            reply_markup=get_main_keyboard(),
            parse_mode="Markdown",
        )
    except Exception: pass
    await callback.answer()

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
            return await message.answer("⚠️ Faqat testni boshlagan odam uni to'xtatа oladi!")
        await message.answer("🛑 *Test to'xtatildi!*\nNatijalar hisoblanmoqda...", parse_mode="Markdown")
        await finish_test(chat_id, bot)
    else:
        await message.answer("ℹ️ Hozir bu chatda hech qanday test yo'q.")


# ==========================================
# 2. TEST YARATISH (UGC) - Yangi Mantiq
# ==========================================

@router.callback_query(F.data == "create_test")
async def create_test_start(callback: CallbackQuery, state: FSMContext):
    tests = stats_manager.get_user_created_tests(callback.from_user.id)
    
    # Foydalanuvchining avvalgi fanlarini guruhlash
    subjects = {}
    for t in tests:
        if t["subject"] not in subjects:
            subjects[t["subject"]] = t["id"] # Fanni tanlash uchun reference ID

    buttons = []
    if subjects:
        for subj, ref_id in subjects.items():
            buttons.append([InlineKeyboardButton(text=f"📁 {subj}", callback_data=f"ct_exist_{ref_id}")])
            
    buttons.append([InlineKeyboardButton(text="➕ Yangi fan yaratish", callback_data="ct_new")])
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")])

    text = (
        "📝 *Test Yaratish Bo'limi*\n\n"
        "O'zingizning shaxsiy fanlaringizni yarating va ularga bloklar qo'shib boring!\n\n"
        "👉 *Qaysi fanga yangi blok qo'shmoqchisiz?* Mavjud faningizni tanlang yoki yangisini yarating:"
    )
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "ct_new")
async def ct_new_subject(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CreateTestStates.waiting_for_subject)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")]])
    await callback.message.edit_text(
        "👉 *Yangi fanning nomini kiriting:*\n_(Masalan: Anatomiya, Fizika 1-qism, Karantin savollari)_",
        reply_markup=kb, parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("ct_exist_"))
async def ct_exist_subject(callback: CallbackQuery, state: FSMContext):
    ref_id = _parse_suffix(callback.data, "ct_exist_")
    test_data = stats_manager.get_user_test(ref_id)
    if not test_data:
        return await callback.answer("Xatolik! Fan topilmadi.", show_alert=True)
        
    await state.update_data(subject=test_data["subject"])
    await state.set_state(CreateTestStates.waiting_for_name)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")]])
    await callback.message.edit_text(
        f"✅ Fan tanlandi: *{test_data['subject']}*\n\n👉 Endi ushbu fan uchun *yangi blok nomini* kiriting:\n_(Masalan: 1-Mavzu, Yakuniy imtihon)_",
        reply_markup=kb, parse_mode="Markdown"
    )

@router.message(CreateTestStates.waiting_for_subject)
async def create_test_subject(message: Message, state: FSMContext):
    subject = message.text.strip()
    if not subject: return await message.answer("⚠️ Iltimos, fan nomini kiriting.")
    await state.update_data(subject=subject)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")]])
    await message.answer(
        f"✅ Fan yaratildi: *{subject}*\n\n👉 Endi ushbu fan ichidagi *birinchi blok uchun nom* bering:\n_(Masalan: 1-Mavzu, 1-Blok)_",
        reply_markup=kb, parse_mode="Markdown"
    )
    await state.set_state(CreateTestStates.waiting_for_name)

@router.message(CreateTestStates.waiting_for_name)
async def create_test_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name: return await message.answer("⚠️ Iltimos, blok nomini kiriting.")
    await state.update_data(block_name=name)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Telegram Quiz", callback_data="fmt_quiz")],
        [InlineKeyboardButton(text="📝 Matn (Text)", callback_data="fmt_text")],
        [InlineKeyboardButton(text="📄 Word fayl (.docx)", callback_data="fmt_docx")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
    ])
    await message.answer(f"Ajoyib! Endi *{name}* bloki uchun savollarni qaysi formatda yuborishni tanlang:", reply_markup=kb, parse_mode="Markdown")
    await state.set_state(CreateTestStates.waiting_for_format)

@router.callback_query(CreateTestStates.waiting_for_format, F.data.startswith("fmt_"))
async def create_test_format(callback: CallbackQuery, state: FSMContext):
    fmt = _parse_suffix(callback.data, "fmt_")
    await state.update_data(format=fmt, questions=[])

    if fmt == "quiz":
        text = ("📊 *Telegram Quiz Formati:*\nMenga Telegram'ning standart Quiz (Viktorina) funksiyasidan foydalanib savollarni bittadan yuboring.\n\n⚠️ *Muhim:* Savollar tugagach, pastdagi *Yakunlash* tugmasini bosing.")
    elif fmt == "text":
        text = ("📝 *Matn Formati:*\nSavollarni ushbu qolipda yuboring:\n\n`O'zbekiston poytaxti?\n#Toshkent\nSamarqand\nBuxoro`\n\n*(To'g'ri javob oldida # bo'lishi shart. Bir nechta savolni probel bilan ajratib yuborish mumkin).*")
    else:
        text = ("📄 *Word Fayl (.docx):*\nFayl tayyorlang. Tuzilishi:\n\n1-Savol matni\n#To'g'ri javob\nXato javob\nXato javob\n\n(Savollar orasida bitta bo'sh qator bo'lsin). Tayyor faylni shu yerga yuboring.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yakunlash (Saqlash)", callback_data="finish_test_creation")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await state.set_state(CreateTestStates.waiting_for_questions)
    await callback.answer()

@router.message(CreateTestStates.waiting_for_questions, F.document)
async def receive_docx_file(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if data.get("format") != "docx": return await message.answer("⚠️ Tanlangan format Word emas.")
    if not message.document.file_name.endswith(".docx"): return await message.answer("⚠️ Faqat `.docx` qabul qilinadi.")

    msg = await message.answer("⏳ Fayl o'qilmoqda...")
    file_path = f"temp_{message.from_user.id}.docx"
    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, file_path)

        questions = data.get("questions", [])
        added = 0
        doc = Document(file_path)
        current_q: list[str] = []
        all_blocks = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                if current_q:
                    all_blocks.append(current_q)
                    current_q = []
            else: current_q.append(text)
        if current_q: all_blocks.append(current_q)

        for lines in all_blocks:
            if len(lines) < 3: continue
            q_text = lines[0]
            opts, corr = [], -1
            for i, line in enumerate(lines[1:]):
                if line.startswith("#"):
                    corr = i
                    opts.append(line[1:].strip())
                else: opts.append(line)
            if corr != -1 and len(opts) >= 2:
                questions.append({"question": q_text, "options": opts, "correct_index": corr})
                added += 1

        await state.update_data(questions=questions)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yakunlash (Saqlash)", callback_data="finish_test_creation")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
        ])
        await msg.edit_text(f"✅ Fayl o'qildi! *{added} ta* savol topildi. Jami: {len(questions)} ta.\n\nYana fayl yuboring yoki Yakunlashni bosing.", reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text("❌ Faylni o'qishda xatolik yuz berdi.")
    finally:
        if os.path.exists(file_path): os.remove(file_path)

@router.message(CreateTestStates.waiting_for_questions)
async def receive_question(message: Message, state: FSMContext):
    data = await state.get_data()
    questions = data.get("questions", [])
    fmt = data.get("format")

    if fmt == "quiz":
        if not message.poll or message.poll.type != "quiz":
            return await message.answer("⚠️ Iltimos, Telegram Quiz yuboring!")
        questions.append({
            "question": message.poll.question,
            "options": [o.text for o in message.poll.options],
            "correct_index": message.poll.correct_option_id,
        })
    elif fmt == "text":
        if not message.text: return await message.answer("⚠️ Iltimos, matn yuboring!")
        blocks = message.text.split("\n\n")
        added = 0
        for block in blocks:
            lines = [l.strip() for l in block.split("\n") if l.strip()]
            if len(lines) < 3: continue
            q_text = lines[0]
            opts, corr = [], -1
            for i, line in enumerate(lines[1:]):
                if line.startswith("#"):
                    corr = i
                    opts.append(line[1:].strip())
                else: opts.append(line)
            if corr != -1 and len(opts) >= 2:
                questions.append({"question": q_text, "options": opts, "correct_index": corr})
                added += 1
        if added == 0: return await message.answer("⚠️ Xato! To'g'ri javob oldiga # qo'yishni unutmang.")
    
    await state.update_data(questions=questions)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yakunlash (Saqlash)", callback_data="finish_test_creation")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
    ])
    await message.answer(f"✅ Qabul qilindi! Jami: {len(questions)} ta.\nYana yuboring yoki yakunlang.", reply_markup=kb)

@router.callback_query(F.data == "finish_test_creation")
async def finish_creation(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    questions = data.get("questions", [])
    if not questions:
        return await callback.answer("Hech qanday savol yo'q!", show_alert=True)

    test_id = stats_manager.save_user_test(callback.from_user.id, data["subject"], data["block_name"], questions)
    if not test_id:
        return await callback.message.answer("Bazaga saqlashda xatolik yuz berdi.")

    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=t_{test_id}"
    
    text = (
        f"🎉 *Blok muvaffaqiyatli saqlandi!*\n\n"
        f"📚 Fan: {data['subject']}\n📝 Blok: {data['block_name']}\n🔢 Savollar: {len(questions)} ta\n\n"
        f"🔗 *Faqat shu blokka to'g'ridan-to'g'ri kirish uchun havola:*\n`{link}`\n\n"
        f"_(Mening Testlarim bo'limiga kirsangiz, butun boshli Fanni ulashish ssilkasini ham olishingiz mumkin)_"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📂 Mening Testlarim", callback_data="my_tests")]])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "cancel_creation")
async def cancel_creation_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Yaratish bekor qilindi.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]]))
    await callback.answer()

# ==========================================
# 3. MENING TESTLARIM (Fanlar Ierarxiyasi)
# ==========================================

@router.callback_query(F.data == "my_tests")
async def my_tests_handler(callback: CallbackQuery):
    tests = stats_manager.get_user_created_tests(callback.from_user.id)
    if not tests:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]])
        return await callback.message.edit_text("📂 *Mening Testlarim*\n\nSiz hali hech qanday test yaratmagansiz.", reply_markup=kb, parse_mode="Markdown")

    # Fanlar bo'yicha guruhlash
    subjects = {}
    for t in tests:
        if t["subject"] not in subjects:
            subjects[t["subject"]] = []
        subjects[t["subject"]].append(t)

    buttons = []
    for subj, subj_tests in subjects.items():
        ref_id = subj_tests[0]["id"] # O'sha fan guruhining 1-test idsi
        buttons.append([InlineKeyboardButton(text=f"📁 {subj} ({len(subj_tests)} ta blok)", callback_data=f"manage_subj_{ref_id}")])

    buttons.append([InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")])
    await callback.message.edit_text("📂 *Mening Fanlarim*\n\nBoshqarish va ulashish uchun kerakli fanni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("manage_subj_"))
async def manage_subj_handler(callback: CallbackQuery, bot: Bot):
    ref_id = _parse_suffix(callback.data, "manage_subj_")
    test_data = stats_manager.get_user_test(ref_id)
    if not test_data: return await callback.answer("Topilmadi", show_alert=True)
    
    # Barcha testlarni olib, faqat shu fanga tegishlilarini saralaymiz
    tests = stats_manager.get_user_created_tests(callback.from_user.id)
    subj_tests = [t for t in tests if t["subject"] == test_data["subject"]]

    bot_info = await bot.get_me()
    # Fanni ulashish havolasi s_ bilan boshlanadi
    link = f"https://t.me/{bot_info.username}?start=s_{ref_id}"

    text = (f"📚 *Fan:* {test_data['subject']}\n\n"
            f"🔗 *Ushbu fanni to'liq ulashish (Ssilka):*\n`{link}`\n"
            f"_(Do'stlaringiz shu orqali kirsa, pastdagi barcha bloklarni ko'ra oladi)_\n\n"
            f"Boshqarish uchun blokni tanlang:")

    buttons = []
    for t in subj_tests:
        buttons.append([InlineKeyboardButton(text=f"🔖 {t['block_name']}", callback_data=f"manage_test_{t['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Fanlar ro'yxatiga", callback_data="my_tests")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@router.callback_query(F.data.startswith("manage_test_"))
async def manage_test_handler(callback: CallbackQuery):
    test_id = _parse_suffix(callback.data, "manage_test_")
    test_data = stats_manager.get_user_test(test_id)

    if not test_data or str(test_data["creator_id"]) != str(callback.from_user.id):
        return await callback.answer("Test topilmadi!", show_alert=True)

    text = (f"📝 *Blok Ma'lumotlari*\n\n"
            f"📚 Fan: {test_data['subject']}\n"
            f"🔖 Blok: *{test_data['block_name']}*\n"
            f"🔢 Savollar: {len(test_data['questions'])} ta\n"
            f"📅 Yaratilgan: {test_data['created_at'][:10]}")
            
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Blokni o'chirish", callback_data=f"delete_test_{test_id}")],
        [InlineKeyboardButton(text="🔙 Ortga", callback_data=f"manage_subj_{test_id}")], # Shunchaki fanga qaytish
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("delete_test_"))
async def delete_test_handler(callback: CallbackQuery):
    test_id = _parse_suffix(callback.data, "delete_test_")
    success = stats_manager.delete_user_test(test_id, callback.from_user.id)
    if success:
        await callback.answer("✅ Blok o'chirildi!", show_alert=True)
        await my_tests_handler(callback)
    else:
        await callback.answer("❌ Xatolik yuz berdi.", show_alert=True)

async def start_ugc_test(message: Message, test_data_db: dict, bot: Bot):
    chat_id = message.chat.id
    if chat_id in active_tests or chat_id in waiting_rooms:
        return await message.answer("⚠️ Bu chatda tugallanmagan test bor. /stop yozing.")

    subject_key = test_data_db["subject"]
    test_id = f"ugc_{test_data_db['id']}"
    session_q = prepare_shuffled_questions(test_data_db["questions"])

    active_tests[chat_id] = {
        "chat_type": "private", "initiator_id": message.from_user.id, "subject_key": subject_key,
        "test_id": test_id, "block_name": test_data_db.get("block_name", ""),
        "session_questions": session_q, "q_idx": 0, "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {},
    }

    await message.answer(f"🚀 *Test Boshlandi!*\n\n📚 Fan: {subject_key}\n📝 Blok: {test_data_db.get('block_name', '')}\n🔢 Savollar: {len(session_q)} ta", parse_mode="Markdown")
    await send_next_question(chat_id, bot)

@router.callback_query(F.data.startswith("ugc_start_"))
async def restart_ugc_test(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_reply_markup(reply_markup=None)
    test_id = _parse_suffix(callback.data, "ugc_start_")
    test_data_db = stats_manager.get_user_test(test_id)
    if test_data_db: await start_ugc_test(callback.message, test_data_db, bot)
    else: await callback.answer("Test topilmadi", show_alert=True)

# ==========================================
# 4. ADMIN TESTLARI (Rasmiy) VA ADMIN PANELI
# ==========================================

@router.callback_query(F.data == "official_tests")
async def show_official_tests(callback: CallbackQuery):
    buttons = []
    for subj_key, subj_name in SUBJECTS.items():
        block_count = len(memory_db.get(subj_key, {}))
        buttons.append([InlineKeyboardButton(text=f"📘 {subj_name} ({block_count} ta blok)", callback_data=f"subj_{subj_key}")])
    buttons.append([InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")])
    await callback.message.edit_text("📚 *Rasmiy (Admin) Testlar*\n\nFanlardan birini tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID: return await message.answer("⛔ Siz admin emassiz!")
    users = stats_manager.get_all_users()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Barchaga xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar ro'yxati", callback_data="admin_users_list")],
    ])
    await message.answer(f"👨‍💻 *ADMIN PANEL*\n\n👥 Jami foydalanuvchilar: {len(users)} ta", reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "admin_users_list")
async def admin_users_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    users = stats_manager.get_all_users()
    if not users: return await callback.message.answer("Hozircha foydalanuvchilar yo'q.")

    await callback.message.answer("⏳ Foydalanuvchilar ro'yxati yuklanmoqda...")
    text = "👥 *Barcha foydalanuvchilar:*\n\n"
    for i, u in enumerate(users, 1):
        name = u.get("full_name") or "Ismsiz"
        uid = u.get("telegram_id")
        username = f" (@{u.get('username')})" if u.get("username") and u.get("username") != "yo'q" else ""
        sana = u.get("joined_at", "")[:10]
        line = f"*{i}.* [{name}](tg://user?id={uid}){username} | 📅 {sana}\n"
        if len(text) + len(line) > 4000:
            await callback.message.answer(text, parse_mode="Markdown")
            text = ""
        text += line
    if text: await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.message.answer("📝 Barchaga yuboriladigan xabar matnini yozing.\n(Bekor qilish uchun /start)")
    await callback.answer()

@router.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = stats_manager.get_all_users()
    success, fail = 0, 0
    status_msg = await message.answer("⏳ Xabar yuborilmoqda...")
    for u in users:
        try:
            await bot.send_message(chat_id=u["telegram_id"], text=message.text)
            success += 1
        except Exception: fail += 1
        await asyncio.sleep(0.05)
    await status_msg.edit_text(f"✅ Ommaviy xabar yakunlandi!\n🟢 Yetib bordi: {success}\n🔴 Bloklaganlar: {fail}")

@router.callback_query(F.data == "contact_admin")
async def cb_contact_admin(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_message)
    await callback.message.answer("✍️ Adminga o'z savol yoki taklifingizni yozing:")
    await callback.answer()

@router.message(UserStates.waiting_for_message)
async def send_to_admin(message: Message, state: FSMContext, bot: Bot):
    text = f"📨 *YANGI XABAR!*\n\n👤 [{message.from_user.full_name}](tg://user?id={message.from_user.id})\nID: `{message.from_user.id}`\n💬 Matn:\n{message.text}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Javob berish", callback_data=f"reply_{message.from_user.id}")]])
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="Markdown")
        await message.answer("✅ Xabaringiz adminga yetkazildi!")
    except Exception: await message.answer("Xatolik yuz berdi.")
    await state.clear()

@router.callback_query(F.data.startswith("reply_"))
async def admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    await state.update_data(target_id=_parse_suffix(callback.data, "reply_"))
    await state.set_state(AdminStates.waiting_for_reply)
    await callback.message.answer("✍️ Foydalanuvchiga javobingizni yozing:")
    await callback.answer()

@router.message(AdminStates.waiting_for_reply)
async def admin_reply_send(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    try:
        await bot.send_message(data.get("target_id"), f"👨‍💻 *Admin tomonidan xabar:*\n\n{message.text}", parse_mode="Markdown")
        await message.answer("✅ Javob yuborildi.")
    except Exception: await message.answer("❌ Xatolik!")
    await state.clear()


# ==========================================
# 5. REYTING VA STATISTIKA
# ==========================================

@router.callback_query(F.data == "show_leaderboard")
async def show_leaderboard_handler(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_text("⏳ Reyting yuklanmoqda...", parse_mode="Markdown")
    top_users = stats_manager.get_top_users(10)
    if not top_users: text = "🏆 *GLOBAL REYTING*\n\nHozircha reytingda hech kim yo'q."
    else:
        text = "🏆 *TOP 10 TALABALAR REYTINGI*\n\n"
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        for i, user in enumerate(top_users):
            try:
                chat_info = await bot.get_chat(user["user_id"])
                name = chat_info.full_name or "Sirli Talaba"
            except Exception: name = "Sirli Talaba"
            text += f"{medals[i] if i < 10 else '🔸'} *{name}*\n      ✅ {user['correct']} ta to'g'ri | 📝 {user['completed']} ta test\n\n"
            await asyncio.sleep(0.05)
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]]), parse_mode="Markdown")
    await callback.answer()

def get_blocks_keyboard(subject_key: str, page: int = 0) -> InlineKeyboardMarkup:
    buttons = []
    subject_tests = memory_db.get(subject_key, {})
    test_ids = sorted(subject_tests.keys())
    total_pages = max(1, (len(test_ids) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    start_idx = page * ITEMS_PER_PAGE
    current_tests = test_ids[start_idx:start_idx + ITEMS_PER_PAGE]

    if not test_ids: buttons.append([InlineKeyboardButton(text="Testlar yo'q", callback_data="ignore")])
    else:
        for t_id in current_tests:
            buttons.append([InlineKeyboardButton(text=f"📘 {t_id}-Blok ({subject_tests[t_id].get('range', '?')})", callback_data=f"start_test_{subject_key}_{t_id}")])
        nav = []
        if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"page_{subject_key}_{page - 1}"))
        if page < total_pages - 1: nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"page_{subject_key}_{page + 1}"))
        if nav: buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="🎲 Aralash Test (Mock Exam)", callback_data=f"mock_{subject_key}")])
    buttons.append([InlineKeyboardButton(text="🔙 Fanlarga", callback_data="official_tests")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.callback_query(F.data.startswith("subj_"))
async def process_subject_selection(callback: CallbackQuery):
    subject_key = _parse_suffix(callback.data, "subj_")
    await callback.message.edit_text(f"📚 *{SUBJECTS.get(subject_key, 'Fan')}*\n\nBloklardan birini tanlang:", reply_markup=get_blocks_keyboard(subject_key, 0), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("page_"))
async def process_page(callback: CallbackQuery):
    parts = callback.data.rsplit("_", 1)
    page = int(parts[1])
    subject_key = _parse_suffix(parts[0], "page_")
    await callback.message.edit_reply_markup(reply_markup=get_blocks_keyboard(subject_key, page))
    await callback.answer()

@router.callback_query(F.data == "show_stats")
async def show_stats_handler(callback: CallbackQuery):
    if callback.message.chat.type != "private": return await callback.answer("Faqat shaxsiy chatda!", show_alert=True)
    stats = stats_manager.get_user_stats(callback.from_user.id)
    total = stats["total_correct"] + stats["total_wrong"]
    percent = (stats["total_correct"] / total * 100) if total > 0 else 0
    text = f"📊 *Shaxsiy statistika:*\n\n✅ To'g'ri: {stats['total_correct']}\n❌ Xato: {stats['total_wrong']}\n🎯 O'zlashtirish: {percent:.1f}%"
    buttons = []
    if stats.get("history"): buttons.append([InlineKeyboardButton(text="📜 Tarix va xatolar", callback_data="hist_page_0")])
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_to_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("hist_page_"))
async def show_history_page(callback: CallbackQuery):
    page = int(_parse_suffix(callback.data, "hist_page_"))
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if not history: return await callback.answer("Tarix bo'sh!", show_alert=True)

    start_idx = page * 5
    buttons = []
    for i, item in enumerate(history[start_idx:start_idx + 5]):
        t_id = item["test_id"]
        label = "Aralash" if str(t_id) == "mock" else (str(t_id).replace("ugc_", "") if str(t_id).startswith("ugc_") else f"{t_id}-B")
        subj_label = SUBJECTS.get(item["subject"], item["subject"])
        buttons.append([InlineKeyboardButton(text=f"📅 {item['date'][:10]} | {subj_label} ({label}) | ✅ {item['correct']}", callback_data=f"hist_det_{start_idx + i}")])

    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"hist_page_{page - 1}"))
    if start_idx + 5 < len(history): nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"hist_page_{page + 1}"))
    if nav: buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="show_stats")])
    await callback.message.edit_text("📜 *Oxirgi ishlangan testlar:*\nBatafsil ko'rish uchun tanlang.", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("hist_det_"))
async def show_history_detail(callback: CallbackQuery):
    idx = int(_parse_suffix(callback.data, "hist_det_"))
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if idx >= len(history): return await callback.answer("Xatolik!", show_alert=True)

    item = history[idx]
    t_id = str(item["test_id"])
    t_label = "Aralash Test" if t_id == "mock" else ("Maxsus Test" if t_id.startswith("ugc_") else f"{t_id}-Blok")

    text = f"📅 {item['date']}\n📚 {SUBJECTS.get(item['subject'], item['subject'])} | {t_label}\n📊 ✅ {item['correct']}, ❌ {item['wrong']}\n\n"
    if not item.get("mistakes"): text += "🎉 *Xato qilinmagan!*"
    else:
        text += "📑 *XATOLAR:*\n\n"
        for i, m in enumerate(item["mistakes"], 1): text += f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}\n\n"
    if len(text) > 4000: text = text[:4000] + "\n... (kesildi)."

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="hist_page_0")]]), parse_mode="Markdown")
    await callback.answer()


# ==========================================
# 6. TEST O'YINI (Guruh va Taymer)
# ==========================================

@router.callback_query(F.data.startswith("start_test_") | F.data.startswith("mock_"))
async def start_test_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id in active_tests or chat_id in waiting_rooms: return await callback.answer("⚠️ Avval joriy testni to'xtating! /stop", show_alert=True)

    is_mock = callback.data.startswith("mock_")
    if is_mock:
        subject_key = _parse_suffix(callback.data, "mock_")
        all_q = [q for test in memory_db.get(subject_key, {}).values() for q in test["questions"]]
        if not all_q: return await callback.answer("Savollar yo'q!", show_alert=True)
        test_data = {"questions": random.sample(all_q, min(25, len(all_q))), "block_name": "Aralash Test"}
        test_id = "mock"
    else:
        parts = _parse_suffix(callback.data, "start_test_").rsplit("_", 1)
        subject_key, test_id = parts[0], int(parts[1])
        test_data = memory_db.get(subject_key, {}).get(test_id)
        if not test_data: return await callback.answer("Test topilmadi!", show_alert=True)

    chat_type = callback.message.chat.type
    if chat_type != "private":
        waiting_rooms[chat_id] = {"subject_key": subject_key, "test_id": test_id, "test_data": test_data, "ready_users": set(), "initiator_id": callback.from_user.id}
        try: await callback.message.delete()
        except: pass
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tayyorman (0)", callback_data="room_ready")], [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")]])
        await bot.send_message(chat_id, f"👥 *Guruh rejimi!*\n📚 {SUBJECTS.get(subject_key, 'Fan')} | {'Aralash' if test_id == 'mock' else f'{test_id}-Blok'}\n\nKamida 2 kishi tayyor bo'lishi kerak!", reply_markup=kb, parse_mode="Markdown")
        return await callback.answer()

    session_q = prepare_shuffled_questions(test_data["questions"])
    active_tests[chat_id] = {
        "chat_type": chat_type, "initiator_id": callback.from_user.id, "subject_key": subject_key, "test_id": test_id,
        "block_name": test_data.get("block_name", ""), "session_questions": session_q, "q_idx": 0, "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None, "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {},
    }
    try: await callback.message.delete()
    except: pass
    await callback.answer()
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "room_ready")
async def room_ready_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms: return await callback.answer("Kutish zali yopilgan!", show_alert=True)
    room = waiting_rooms[chat_id]
    if callback.from_user.id in room["ready_users"]: return await callback.answer("Siz allaqachon tayyorsiz!", show_alert=True)

    room["ready_users"].add(callback.from_user.id)
    count = len(room["ready_users"])
    buttons = [[InlineKeyboardButton(text=f"✅ Tayyorman ({count})", callback_data="room_ready")]]
    if count >= 2: buttons.append([InlineKeyboardButton(text="🚀 Boshlash", callback_data="room_start")])
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer(f"✅ Tayyor! ({count} kishi)")

@router.callback_query(F.data == "room_start")
async def room_start_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms: return await callback.answer()
    room = waiting_rooms[chat_id]
    if len(room["ready_users"]) < 2: return await callback.answer("Kamida 2 kishi!", show_alert=True)

    session_q = prepare_shuffled_questions(room["test_data"]["questions"])
    active_tests[chat_id] = {
        "chat_type": "group", "initiator_id": room["initiator_id"], "subject_key": room["subject_key"], "test_id": room["test_id"],
        "block_name": room["test_data"].get("block_name", ""), "session_questions": session_q, "q_idx": 0, "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None, "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {},
    }
    del waiting_rooms[chat_id]
    try: await callback.message.delete()
    except: pass
    await bot.send_message(chat_id, "🚀 *Test boshlandi!*", parse_mode="Markdown")
    await callback.answer()
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "room_cancel")
async def room_cancel_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms: return await callback.answer("Kutish zali yopilgan.", show_alert=True)
    if callback.from_user.id != waiting_rooms[chat_id]["initiator_id"]: return await callback.answer("Faqat testni boshlagan odam bekor qila oladi!", show_alert=True)
    del waiting_rooms[chat_id]
    try: await callback.message.delete()
    except: pass
    await callback.answer("Test bekor qilindi.", show_alert=True)

async def question_timeout_task(chat_id: int, expected_q_idx: int, poll_id: str, bot: Bot):
    try: await asyncio.sleep(30)
    except asyncio.CancelledError: return
    session = active_tests.get(chat_id)
    if not session or session["q_idx"] != expected_q_idx or session["poll_id"] != poll_id: return
    try: await bot.stop_poll(chat_id=chat_id, message_id=session["msg_id"])
    except: pass

    q_data = session["session_questions"][expected_q_idx]
    session["q_idx"] += 1

    if session["chat_type"] == "private":
        session["wrong"] += 1
        session["consecutive_timeouts"] += 1
        session["mistakes"].append({"question": q_data["question"], "correct_ans": q_data["correct_text"], "wrong_ans": "⏳ Vaqt tugadi"})
        if session["consecutive_timeouts"] >= 2 and session["q_idx"] < len(session["session_questions"]):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Davom etish", callback_data="resume_test")],
                [InlineKeyboardButton(text="🛑 Yakunlash", callback_data="force_finish")]
            ])
            await bot.send_message(chat_id, "⏸ *Test to'xtatildi!*\nKetma-ket 2 marta javob bermadingiz.", reply_markup=kb, parse_mode="Markdown")
        else: await send_next_question(chat_id, bot)
    else: await send_next_question(chat_id, bot)

@router.callback_query(F.data == "resume_test")
async def resume_test_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id not in active_tests: return await callback.answer("Test topilmadi.", show_alert=True)
    active_tests[chat_id]["consecutive_timeouts"] = 0
    try: await callback.message.delete()
    except: pass
    await callback.answer()
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "force_finish")
async def force_finish_handler(callback: CallbackQuery, bot: Bot):
    try: await callback.message.delete()
    except: pass
    await callback.answer()
    await finish_test(callback.message.chat.id, bot)

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
        for i, opt in enumerate(q["options"]): text_msg += f"{labels[i]}) {opt}\n"
        if len(text_msg) > 4000: text_msg = text_msg[:4000] + "...\n(Xabar kesildi)"
        await bot.send_message(chat_id, text_msg)
        poll_q = f"[{q_idx + 1}/{len(questions)}] To'g'ri variantni belgilang:"
        poll_opts = [f"{labels[i]} varianti" for i in range(len(q["options"]))]
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
    is_correct = poll_answer.option_ids[0] == q_data["correct_index"]

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
        lock = _get_group_lock(chat_id)
        async with lock:
            u_id = poll_answer.user.id
            if u_id not in session["group_scores"]:
                session["group_scores"][u_id] = {"name": poll_answer.user.full_name, "correct": 0, "wrong": 0, "mistakes": []}
            if is_correct: session["group_scores"][u_id]["correct"] += 1
            else:
                session["group_scores"][u_id]["wrong"] += 1
                session["group_scores"][u_id]["mistakes"].append({"question": q_data["question"], "correct_ans": q_data["correct_text"], "wrong_ans": q_data["options"][poll_answer.option_ids[0]]})

# ==========================================
# 9. TEST YAKUNLASH VA NAVIGATSIYA
# ==========================================

async def finish_test(chat_id: int, bot: Bot):
    session = active_tests.get(chat_id)
    if not session: return
    if session.get("timer_task"): session["timer_task"].cancel()

    t_id = session["test_id"]
    if str(t_id) == "mock": t_name = "Aralash Test"
    elif str(t_id).startswith("ugc_"): t_name = f"📝 {session.get('block_name', 'Maxsus Test')}"
    else: t_name = f"{t_id}-Blok"

    mins, secs = divmod(int(time.time() - session["start_time"]), 60)
    title = f"{SUBJECTS.get(session['subject_key'], session['subject_key'])} | {t_name}"
    buttons = []

    if session["chat_type"] == "private":
        stats_manager.update_user_stats(chat_id, session["correct"], session["wrong"], session["subject_key"], session["test_id"], session["mistakes"])
        percent = 0
        total_q = session["correct"] + session["wrong"]
        if total_q > 0: percent = round(session["correct"] / total_q * 100, 1)
        
        text = f"🏁 *{title} Yakunlandi!*\n\n🟢 To'g'ri: {session['correct']}\n🔴 Xato: {session['wrong']}\n🎯 O'zlashtirish: {percent}%\n⏱ Vaqt: {mins:02d}:{secs:02d}"
        if session.get("mistakes"): buttons.append([InlineKeyboardButton(text="❌ Xatolar ustida ishlash", callback_data="review_mistakes")])

        if str(t_id).startswith("ugc_"):
            buttons.append([InlineKeyboardButton(text="🔁 Qayta ishlash", callback_data=f"ugc_start_{str(t_id).replace('ugc_', '')}")])
        elif t_id != "mock":
            buttons.append([InlineKeyboardButton(text="🔁 Qayta ishlash", callback_data=f"post_start_{session['subject_key']}_{t_id}")])
            if t_id + 1 in memory_db.get(session["subject_key"], {}):
                buttons.append([InlineKeyboardButton(text="➡️ Keyingi Blok", callback_data=f"post_start_{session['subject_key']}_{t_id + 1}")])
    else:
        for u_id, scores in session["group_scores"].items():
            stats_manager.update_user_stats(u_id, scores["correct"], scores["wrong"], session["subject_key"], session["test_id"], scores["mistakes"])
        text = f"🏁 *{title} yakunlandi!*\n⏱ Vaqt: {mins:02d}:{secs:02d}\n\n🏆 *NATIJALAR:*\n"
        if not session["group_scores"]: text += "Hech kim qatnashmadi 😔"
        else:
            sorted_scores = sorted(session["group_scores"].values(), key=lambda x: x["correct"], reverse=True)
            for i, score in enumerate(sorted_scores):
                medal = ["🥇", "🥈", "🥉"][i] if i < 3 else "🔸"
                text += f"{medal} {score['name']}: {score['correct']} ta to'g'ri\n"

    if str(t_id).startswith("ugc_"):
        buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")])
    else:
        buttons.extend([
            [InlineKeyboardButton(text="🔙 Fan menyusiga", callback_data=f"post_subj_{session['subject_key']}")],
            [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")]
        ])

    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

    old_poll = session.get("poll_id")
    if old_poll and old_poll in poll_chat_map: del poll_chat_map[old_poll]
    _group_answer_locks.pop(chat_id, None)
    del active_tests[chat_id]

@router.callback_query(F.data == "post_main")
async def post_main_handler(callback: CallbackQuery):
    try: await callback.message.edit_reply_markup(reply_markup=None)
    except: pass
    await callback.message.answer("🏛 *Talabalar Imtihon Trenajyori*\n\nKerakli bo'limni tanlang:", reply_markup=get_main_keyboard(), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("post_subj_"))
async def post_subj_handler(callback: CallbackQuery):
    subject_key = _parse_suffix(callback.data, "post_subj_")
    try: await callback.message.edit_reply_markup(reply_markup=None)
    except: pass
    await callback.message.answer(f"📚 *{SUBJECTS.get(subject_key, 'Fan')}*\n\nBloklardan birini tanlang:", reply_markup=get_blocks_keyboard(subject_key, 0), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("post_start_"))
async def post_start_handler(callback: CallbackQuery, bot: Bot):
    suffix = _parse_suffix(callback.data, "post_start_")
    parts = suffix.rsplit("_", 1)
    subject_key, test_id = parts[0], int(parts[1])
    test_data = memory_db.get(subject_key, {}).get(test_id)
    if not test_data: return await callback.answer("Test topilmadi!", show_alert=True)
    
    chat_id = callback.message.chat.id
    if chat_id in active_tests or chat_id in waiting_rooms: return await callback.answer("⚠️ Avval joriy testni to'xtating! /stop", show_alert=True)
    
    session_q = prepare_shuffled_questions(test_data["questions"])
    active_tests[chat_id] = {
        "chat_type": "private", "initiator_id": callback.from_user.id, "subject_key": subject_key, "test_id": test_id,
        "block_name": test_data.get("block_name", f"{test_id}-Blok"), "session_questions": session_q, "q_idx": 0, "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None, "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {},
    }
    try: await callback.message.edit_reply_markup(reply_markup=None)
    except: pass
    await callback.answer()
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "review_mistakes")
async def review_mistakes_handler(callback: CallbackQuery):
    stats = stats_manager.get_user_stats(callback.from_user.id)
    history = stats.get("history", [])
    if not history: return await callback.answer("Xatolar topilmadi!", show_alert=True)

    mistakes = history[0].get("mistakes", [])
    if not mistakes: return await callback.answer("Bu testda xato yo'q edi! 🎉", show_alert=True)

    text = "📑 *So'nggi test xatolari:*\n\n"
    for i, m in enumerate(mistakes, 1): text += f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}\n\n"
    if len(text) > 4000: text = text[:4000] + "\n... (kesildi)."

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]]), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data == "ignore")
async def ignore_handler(callback: CallbackQuery):
    await callback.answer()
