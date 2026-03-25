import os
import time
import random
import asyncio
import logging
import json
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from docx import Document
from aiogram import Bot, Router, F
from aiogram.types import Message, CallbackQuery, PollAnswer, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import stats_manager
from config import SUBJECTS, ADMIN_ID

try:
    from config import DATA_DIR
    data_folder = Path(DATA_DIR)
except ImportError:
    data_folder = Path(__file__).parent / "data"

logger = logging.getLogger(__name__)

router = Router()

active_tests: dict = {}
waiting_rooms: dict = {}
poll_chat_map: dict = {}
memory_db: dict = {}
ITEMS_PER_PAGE = 5

# ── OPTIM 1: defaultdict → lock yaratish uchun qo'shimcha if kerak emas ──
_group_answer_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def _get_group_lock(chat_id: int) -> asyncio.Lock:
    return _group_answer_locks[chat_id]

# ── OPTIM 2: Leaderboard natijasini 60 soniya keshlash ──
_leaderboard_cache: dict = {"text": None, "ts": 0.0}
_LEADERBOARD_TTL = 60  # sekund

# ── OPTIM 3: Broadcast uchun semaphore (Telegram rate-limit: ~30 msg/sek) ──
_BROADCAST_SEMAPHORE = asyncio.Semaphore(25)

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

class AdminCreateTest(StatesGroup):
    waiting_for_subject = State()
    waiting_for_test_id = State()
    waiting_for_format = State()
    waiting_for_content = State()

def prepare_shuffled_questions(raw_questions: list) -> list:
    shuffled_q = random.sample(raw_questions, len(raw_questions))   # shuffle + copy birga
    session_questions = []
    for q in shuffled_q:
        options = list(q["options"])
        correct_text = options[q["correct_index"]]
        random.shuffle(options)
        session_questions.append({
            "question": q["question"],
            "options": options,
            "correct_index": options.index(correct_text),
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

# ── OPTIM 4: get_blocks_keyboard — memory_db o'zgarmasa qayta hisoblash shart emas ──
# Faqat memory_db.get(...) tezkor, lekin ko'p marta chaqirilganda InlineKeyboardMarkup
# obyektini qayta quramiz. Cache sifatida oddiy dict ishlatamiz.
_blocks_kb_cache: dict = {}   # (subject_key, page) → InlineKeyboardMarkup

def invalidate_blocks_cache(subject_key: str | None = None):
    """memory_db o'zgarganda keshni tozalash."""
    if subject_key:
        keys = [k for k in _blocks_kb_cache if k[0] == subject_key]
    else:
        keys = list(_blocks_kb_cache)
    for k in keys:
        _blocks_kb_cache.pop(k, None)

def get_blocks_keyboard(subject_key: str, page: int = 0) -> InlineKeyboardMarkup:
    cache_key = (subject_key, page)
    if cache_key in _blocks_kb_cache:
        return _blocks_kb_cache[cache_key]

    subject_tests = memory_db.get(subject_key, {})
    test_ids = sorted(subject_tests.keys())
    total_pages = max(1, (len(test_ids) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    start_idx = page * ITEMS_PER_PAGE
    current_tests = test_ids[start_idx:start_idx + ITEMS_PER_PAGE]

    buttons = []
    if not test_ids:
        buttons.append([InlineKeyboardButton(text="Testlar yo'q", callback_data="ignore")])
    else:
        for t_id in current_tests:
            buttons.append([InlineKeyboardButton(
                text=f"📘 {t_id}-Blok ({subject_tests[t_id].get('range', '?')})",
                callback_data=f"start_test_{subject_key}_{t_id}"
            )])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"page_{subject_key}_{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"page_{subject_key}_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="🎲 Aralash Test (Mock Exam)", callback_data=f"mock_{subject_key}")])
    buttons.append([InlineKeyboardButton(text="🔙 Fanlarga", callback_data="official_tests")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    _blocks_kb_cache[cache_key] = kb
    return kb


# ==========================================
# 1. ASOSIY BUYRUQLAR VA DEEP-LINK
# ==========================================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    chat_id = message.chat.id

    # ── OPTIM 5: register_user + eski test tozalash parallel ──
    stats_manager.register_user(
        message.from_user.id, message.from_user.full_name, message.from_user.username
    )

    cleared = False
    if chat_id in waiting_rooms:
        del waiting_rooms[chat_id]
        cleared = True
    if chat_id in active_tests:
        task = active_tests[chat_id].get("timer_task")
        if task:
            task.cancel()
        old_poll_id = active_tests[chat_id].get("poll_id")
        if old_poll_id:
            poll_chat_map.pop(old_poll_id, None)
        del active_tests[chat_id]
        cleared = True
    if cleared:
        await message.answer("🔄 Eski tugallanmagan testlaringiz tozalandi.")

    args = message.text.split()
    if len(args) > 1:
        if args[1].startswith("s_"):
            ref_id = args[1][2:]
            test_data_db = stats_manager.get_user_test(ref_id)
            if not test_data_db:
                return await message.answer("❌ Bu fan topilmadi yoki muallif tomonidan o'chirilgan.")
            return await show_ugc_subject_blocks(message, test_data_db["creator_id"], test_data_db["subject"])

        elif args[1].startswith("t_"):
            test_data_db = stats_manager.get_user_test(args[1][2:])
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
    tests = stats_manager.get_user_created_tests(creator_id)
    subj_tests = [t for t in tests if t["subject"] == subject]
    if not subj_tests:
        return await message.answer("❌ Bu fanda bloklar topilmadi.")

    buttons = [
        [InlineKeyboardButton(text=f"📘 {t['block_name']}", callback_data=f"ugc_start_{t['id']}")]
        for t in subj_tests
    ]
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
    except Exception:
        pass
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
        else:
            await message.answer("⚠️ Faqat testni boshlagan odam uni bekor qila oladi!")
        return

    if chat_id in active_tests:
        if message.chat.type != "private" and user_id != active_tests[chat_id].get("initiator_id"):
            return await message.answer("⚠️ Faqat testni boshlagan odam uni to'xtatа oladi!")
        await message.answer("🛑 *Test to'xtatildi!*\nNatijalar hisoblanmoqda...", parse_mode="Markdown")
        await finish_test(chat_id, bot)
    else:
        await message.answer("ℹ️ Hozir bu chatda hech qanday test yo'q.")


# ==========================================
# 2. TEST YARATISH (UGC)
# ==========================================

@router.callback_query(F.data == "create_test")
async def create_test_start(callback: CallbackQuery, state: FSMContext):
    tests = stats_manager.get_user_created_tests(callback.from_user.id)

    # ── OPTIM 6: dict comprehension → bitta o'tish ──
    subjects = {}
    for t in tests:
        subjects.setdefault(t["subject"], t["id"])

    buttons = [
        [InlineKeyboardButton(text=f"📁 {subj}", callback_data=f"ct_exist_{ref_id}")]
        for subj, ref_id in subjects.items()
    ]
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
        "👉 *Yangi fanning nomini kiriting:*\n_(Masalan: Anatomiya, Fizika 1-qism)_",
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
    if not subject:
        return await message.answer("⚠️ Iltimos, fan nomini kiriting.")
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
    if not name:
        return await message.answer("⚠️ Iltimos, blok nomini kiriting.")
    await state.update_data(block_name=name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Telegram Quiz", callback_data="fmt_quiz")],
        [InlineKeyboardButton(text="📝 Matn (Text)", callback_data="fmt_text")],
        [InlineKeyboardButton(text="📄 Word fayl (.docx)", callback_data="fmt_docx")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
    ])
    await message.answer(
        f"Ajoyib! Endi *{name}* bloki uchun savollarni qaysi formatda yuborishni tanlang:",
        reply_markup=kb, parse_mode="Markdown"
    )
    await state.set_state(CreateTestStates.waiting_for_format)

@router.callback_query(CreateTestStates.waiting_for_format, F.data.startswith("fmt_"))
async def create_test_format(callback: CallbackQuery, state: FSMContext):
    fmt = _parse_suffix(callback.data, "fmt_")
    await state.update_data(format=fmt, questions=[])

    if fmt == "quiz":
        text = "📊 *Telegram Quiz Formati:*\nMenga Telegram'ning standart Quiz (Viktorina) funksiyasidan foydalanib savollarni bittadan yuboring.\n\n⚠️ *Muhim:* Savollar tugagach, pastdagi *Yakunlash* tugmasini bosing."
    elif fmt == "text":
        text = "📝 *Matn Formati:*\nSavollarni ushbu qolipda yuboring:\n\n`O'zbekiston poytaxti?\n#Toshkent\nSamarqand\nBuxoro`\n\n*(To'g'ri javob oldida # bo'lishi shart. Bir nechta savolni probel bilan ajratib yuborish mumkin).*"
    else:
        text = "📄 *Word Fayl (.docx):*\nFayl tayyorlang. Tuzilishi:\n\n1-Savol matni\n#To'g'ri javob\nXato javob\nXato javob\n\n(Savollar orasida bitta bo'sh qator bo'lsin). Tayyor faylni shu yerga yuboring."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yakunlash (Saqlash)", callback_data="finish_test_creation")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await state.set_state(CreateTestStates.waiting_for_questions)
    await callback.answer()

# ── OPTIM 7: docx parsing uchun alohida funksiya (admin va user uchun umumiy) ──
def _parse_docx_questions(doc_path: str) -> list:
    doc = Document(doc_path)
    all_blocks = []
    current_q: list[str] = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            current_q.append(text)
        elif current_q:
            all_blocks.append(current_q)
            current_q = []
    if current_q:
        all_blocks.append(current_q)

    questions = []
    for lines in all_blocks:
        if len(lines) < 3:
            continue
        opts, corr = [], -1
        for i, line in enumerate(lines[1:]):
            if line.startswith("#"):
                corr = i
                opts.append(line[1:].strip())
            else:
                opts.append(line)
        if corr != -1 and len(opts) >= 2:
            questions.append({"question": lines[0], "options": opts, "correct_index": corr})
    return questions

def _parse_text_questions(text: str) -> list:
    questions = []
    for block in text.split("\n\n"):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        if len(lines) < 3:
            continue
        opts, corr = [], -1
        for i, line in enumerate(lines[1:]):
            if line.startswith("#"):
                corr = i
                opts.append(line[1:].strip())
            else:
                opts.append(line)
        if corr != -1 and len(opts) >= 2:
            questions.append({"question": lines[0], "options": opts, "correct_index": corr})
    return questions

@router.message(CreateTestStates.waiting_for_questions, F.document)
async def receive_docx_file(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if data.get("format") != "docx":
        return await message.answer("⚠️ Tanlangan format Word emas.")
    if not message.document.file_name.endswith(".docx"):
        return await message.answer("⚠️ Faqat `.docx` qabul qilinadi.")

    msg = await message.answer("⏳ Fayl o'qilmoqda...")
    file_path = f"temp_{message.from_user.id}.docx"
    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, file_path)

        new_qs = _parse_docx_questions(file_path)
        questions = data.get("questions", []) + new_qs

        await state.update_data(questions=questions)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yakunlash (Saqlash)", callback_data="finish_test_creation")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
        ])
        await msg.edit_text(
            f"✅ Fayl o'qildi! *{len(new_qs)} ta* savol topildi. Jami: {len(questions)} ta.\n\nYana fayl yuboring yoki Yakunlashni bosing.",
            reply_markup=kb, parse_mode="Markdown"
        )
    except Exception:
        await msg.edit_text("❌ Faylni o'qishda xatolik yuz berdi.")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

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
        if not message.text:
            return await message.answer("⚠️ Iltimos, matn yuboring!")
        added = _parse_text_questions(message.text)
        if not added:
            return await message.answer("⚠️ Xato! To'g'ri javob oldiga # qo'yishni unutmang.")
        questions.extend(added)

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
    await callback.message.edit_text(
        "❌ Yaratish bekor qilindi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]])
    )
    await callback.answer()


# ==========================================
# 3. MENING TESTLARIM (Fanlar Ierarxiyasi)
# ==========================================

@router.callback_query(F.data == "my_tests")
async def my_tests_handler(callback: CallbackQuery):
    tests = stats_manager.get_user_created_tests(callback.from_user.id)
    if not tests:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]])
        return await callback.message.edit_text(
            "📂 *Mening Testlarim*\n\nSiz hali hech qanday test yaratmagansiz.",
            reply_markup=kb, parse_mode="Markdown"
        )

    # ── OPTIM 8: subject grouping bitta pass ──
    subjects: dict[str, list] = {}
    for t in tests:
        subjects.setdefault(t["subject"], []).append(t)

    buttons = [
        [InlineKeyboardButton(
            text=f"📁 {subj} ({len(subj_tests)} ta blok)",
            callback_data=f"manage_subj_{subj_tests[0]['id']}"
        )]
        for subj, subj_tests in subjects.items()
    ]
    buttons.append([InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")])
    await callback.message.edit_text(
        "📂 *Mening Fanlarim*\n\nBoshqarish va ulashish uchun kerakli fanni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("manage_subj_"))
async def manage_subj_handler(callback: CallbackQuery, bot: Bot):
    ref_id = _parse_suffix(callback.data, "manage_subj_")
    test_data = stats_manager.get_user_test(ref_id)
    if not test_data:
        return await callback.answer("Topilmadi", show_alert=True)

    tests = stats_manager.get_user_created_tests(callback.from_user.id)
    subj_tests = [t for t in tests if t["subject"] == test_data["subject"]]

    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=s_{ref_id}"

    text = (
        f"📚 *Fan:* {test_data['subject']}\n\n"
        f"🔗 *Ushbu fanni to'liq ulashish (Ssilka):*\n`{link}`\n"
        f"_(Do'stlaringiz shu orqali kirsa, pastdagi barcha bloklarni ko'ra oladi)_\n\n"
        f"Boshqarish uchun blokni tanlang:"
    )
    buttons = [
        [InlineKeyboardButton(text=f"🔖 {t['block_name']}", callback_data=f"manage_test_{t['id']}")]
        for t in subj_tests
    ]
    buttons.append([InlineKeyboardButton(text="🔙 Fanlar ro'yxatiga", callback_data="my_tests")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@router.callback_query(F.data.startswith("manage_test_"))
async def manage_test_handler(callback: CallbackQuery):
    test_id = _parse_suffix(callback.data, "manage_test_")
    test_data = stats_manager.get_user_test(test_id)

    if not test_data or str(test_data["creator_id"]) != str(callback.from_user.id):
        return await callback.answer("Test topilmadi!", show_alert=True)

    text = (
        f"📝 *Blok Ma'lumotlari*\n\n"
        f"📚 Fan: {test_data['subject']}\n"
        f"🔖 Blok: *{test_data['block_name']}*\n"
        f"🔢 Savollar: {len(test_data['questions'])} ta\n"
        f"📅 Yaratilgan: {test_data['created_at'][:10]}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Blokni o'chirish", callback_data=f"delete_test_{test_id}")],
        [InlineKeyboardButton(text="🔙 Ortga", callback_data=f"manage_subj_{test_id}")],
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

    session_q = prepare_shuffled_questions(test_data_db["questions"])
    active_tests[chat_id] = {
        "chat_type": "private", "initiator_id": message.from_user.id,
        "subject_key": test_data_db["subject"],
        "test_id": f"ugc_{test_data_db['id']}",
        "block_name": test_data_db.get("block_name", ""),
        "session_questions": session_q, "q_idx": 0, "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {},
    }
    await message.answer(
        f"🚀 *Test Boshlandi!*\n\n📚 Fan: {test_data_db['subject']}\n📝 Blok: {test_data_db.get('block_name', '')}\n🔢 Savollar: {len(session_q)} ta",
        parse_mode="Markdown"
    )
    await send_next_question(chat_id, bot)

@router.callback_query(F.data.startswith("ugc_start_"))
async def restart_ugc_test(callback: CallbackQuery, bot: Bot):
    await callback.message.edit_reply_markup(reply_markup=None)
    test_id = _parse_suffix(callback.data, "ugc_start_")
    test_data_db = stats_manager.get_user_test(test_id)
    if test_data_db:
        await start_ugc_test(callback.message, test_data_db, bot)
    else:
        await callback.answer("Test topilmadi", show_alert=True)


# ==========================================
# 4. ADMIN TESTLARI VA ADMIN PANELI
# ==========================================

@router.callback_query(F.data == "official_tests")
async def show_official_tests(callback: CallbackQuery):
    buttons = [
        [InlineKeyboardButton(
            text=f"📘 {subj_name} ({len(memory_db.get(subj_key, {}))} ta blok)",
            callback_data=f"subj_{subj_key}"
        )]
        for subj_key, subj_name in SUBJECTS.items()
    ]
    buttons.append([InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")])
    await callback.message.edit_text(
        "📚 *Rasmiy (Admin) Testlar*\n\nFanlardan birini tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown"
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("⛔ Siz admin emassiz!")
    await show_admin_panel(message, is_callback=False)

@router.callback_query(F.data == "admin_panel_main")
async def cb_admin_panel_main(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    await show_admin_panel(callback.message, is_callback=True)
    await callback.answer()

async def show_admin_panel(message: Message, is_callback=False):
    users = stats_manager.get_all_users()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Barchaga xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar ro'yxati", callback_data="admin_users_page_0")],
        [InlineKeyboardButton(text="➕ Rasmiy test qo'shish/tahrirlash", callback_data="admin_add_test")]
    ])
    text = f"👨‍💻 *ADMIN PANEL*\n\n👥 Jami foydalanuvchilar: {len(users)} ta"
    if is_callback:
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            pass
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("admin_users_page_"))
async def admin_users_list_paginated(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    page = int(_parse_suffix(callback.data, "admin_users_page_"))
    users = stats_manager.get_all_users()
    if not users:
        return await callback.message.answer("Hozircha foydalanuvchilar yo'q.")

    per_page = 15
    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * per_page
    current_users = users[start_idx:start_idx + per_page]

    # ── OPTIM 9: string join list ──
    lines = [f"*{i}.* [{u.get('full_name') or 'Ismsiz'}](tg://user?id={u.get('telegram_id')})"
             f"{f' (@{u.get(\"username\")})' if u.get('username') and u.get('username') != 'yo\\'q' else ''}"
             f" | 📅 {u.get('joined_at', '')[:10]}"
             for i, u in enumerate(current_users, start_idx + 1)]
    text = f"👥 *Barcha foydalanuvchilar ({page+1}/{total_pages}):*\n\n" + "\n".join(lines)

    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_users_page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_users_page_{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_panel_main")])

    try:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer()

@router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    await state.set_state(AdminStates.waiting_for_broadcast)
    await callback.message.answer("📝 Barchaga yuboriladigan xabar matnini yozing.\n(Bekor qilish uchun /start)")
    await callback.answer()

# ── OPTIM 10: Broadcast — parallel yuborish, semaphore bilan cheklash ──
@router.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = stats_manager.get_all_users()
    status_msg = await message.answer("⏳ Xabar yuborilmoqda...")

    async def _send(user):
        async with _BROADCAST_SEMAPHORE:
            try:
                await bot.send_message(chat_id=user["telegram_id"], text=message.text)
                return True
            except Exception:
                return False

    results = await asyncio.gather(*[_send(u) for u in users])
    success = sum(results)
    await status_msg.edit_text(
        f"✅ Ommaviy xabar yakunlandi!\n🟢 Yetib bordi: {success}\n🔴 Bloklaganlar: {len(results) - success}"
    )


# --- ADMIN RASMIY TEST QO'SHISH ---
@router.callback_query(F.data == "admin_add_test")
async def admin_add_test_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    buttons = [[InlineKeyboardButton(text=v, callback_data=f"adm_subj_{k}")] for k, v in SUBJECTS.items()]
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_cancel")])
    await callback.message.edit_text(
        "📂 Qaysi fanga rasmiy test qo'shmoqchi (yoki tahrirlamoqchi)siz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await state.set_state(AdminCreateTest.waiting_for_subject)

@router.callback_query(F.data == "admin_cancel")
async def admin_cancel_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb_admin_panel_main(callback)

@router.callback_query(AdminCreateTest.waiting_for_subject, F.data.startswith("adm_subj_"))
async def admin_add_test_subj(callback: CallbackQuery, state: FSMContext):
    subj = _parse_suffix(callback.data, "adm_subj_")
    await state.update_data(subject=subj)
    await callback.message.edit_text(
        "🔢 *Blok raqamini (ID) kiriting:*\n_(Masalan: 1, 2, 3... Agar bu raqamli test avvaldan bor bo'lsa, u yangilanadi)_",
        parse_mode="Markdown"
    )
    await state.set_state(AdminCreateTest.waiting_for_test_id)

@router.message(AdminCreateTest.waiting_for_test_id)
async def admin_add_test_id(message: Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("⚠️ Iltimos, faqat raqam kiriting (Masalan: 1)")
    await state.update_data(test_id=int(message.text))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Matn (Text)", callback_data="adm_fmt_text")],
        [InlineKeyboardButton(text="📄 Word fayl (.docx)", callback_data="adm_fmt_docx")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_cancel")]
    ])
    await message.answer("Ajoyib! Savollarni qaysi formatda yuborasiz?", reply_markup=kb)
    await state.set_state(AdminCreateTest.waiting_for_format)

@router.callback_query(AdminCreateTest.waiting_for_format, F.data.startswith("adm_fmt_"))
async def admin_add_test_fmt(callback: CallbackQuery, state: FSMContext):
    fmt = _parse_suffix(callback.data, "adm_fmt_")
    await state.update_data(format=fmt)
    if fmt == "text":
        text = "📝 *Matn Formati:*\nSavollarni ushbu qolipda yuboring:\n\n`Savol matni\n#To'g'ri javob\nXato javob\nXato javob`"
    else:
        text = "📄 *Word Fayl (.docx):*\nFayl tayyorlang. Tuzilishi matndagi kabi bo'lishi kerak. Tayyor faylni yuboring."
    await callback.message.edit_text(text, parse_mode="Markdown")
    await state.set_state(AdminCreateTest.waiting_for_content)

def _save_official_test(subj: str, t_id: int, questions: list):
    """JSON faylga va memory_db ga bir vaqtda yozish."""
    subj_dir = data_folder / subj
    subj_dir.mkdir(parents=True, exist_ok=True)
    file_data = {"test_id": t_id, "range": f"1-{len(questions)}", "questions": questions}
    with open(subj_dir / f"test_{t_id}.json", "w", encoding="utf-8") as f:
        json.dump(file_data, f, ensure_ascii=False, indent=4)
    memory_db.setdefault(subj, {})[t_id] = file_data
    invalidate_blocks_cache(subj)   # keyboard keshini tozala
    return file_data

@router.message(AdminCreateTest.waiting_for_content, F.document)
async def admin_receive_docx(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if data.get("format") != "docx":
        return await message.answer("⚠️ Format Word emas.")
    if not message.document.file_name.endswith(".docx"):
        return await message.answer("⚠️ Faqat `.docx`")

    msg = await message.answer("⏳ Fayl o'qilmoqda...")
    file_path = f"admin_temp_{message.from_user.id}.docx"
    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, file_path)
        questions = _parse_docx_questions(file_path)
        if not questions:
            return await msg.edit_text("❌ Savollar topilmadi. Format xato bo'lishi mumkin.")

        _save_official_test(data["subject"], data["test_id"], questions)
        await msg.edit_text(
            f"✅ *Rasmiy test muvaffaqiyatli saqlandi!*\n\n📚 Fan: {SUBJECTS.get(data['subject'])}\n🔖 Blok: {data['test_id']}\n🔢 Savollar: {len(questions)} ta",
            parse_mode="Markdown"
        )
        await state.clear()
    except Exception as e:
        await msg.edit_text(f"❌ Xatolik yuz berdi: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@router.message(AdminCreateTest.waiting_for_content, F.text)
async def admin_receive_text(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("format") != "text":
        return await message.answer("⚠️ Iltimos, fayl yuboring.")

    questions = _parse_text_questions(message.text)
    if not questions:
        return await message.answer("❌ Savollar topilmadi. Javob oldiga # qo'yganingizni tekshiring.")

    try:
        _save_official_test(data["subject"], data["test_id"], questions)
        await message.answer(
            f"✅ *Rasmiy test muvaffaqiyatli saqlandi!*\n\n📚 Fan: {SUBJECTS.get(data['subject'])}\n🔖 Blok: {data['test_id']}\n🔢 Savollar: {len(questions)} ta",
            parse_mode="Markdown"
        )
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Fayl yaratishda xatolik yuz berdi: {e}")


# --- ADMIN MULOQOT ---
@router.callback_query(F.data == "contact_admin")
async def cb_contact_admin(callback: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_message)
    await callback.message.answer("✍️ Adminga o'z savol yoki taklifingizni yozing:")
    await callback.answer()

@router.message(UserStates.waiting_for_message)
async def send_to_admin(message: Message, state: FSMContext, bot: Bot):
    text = (
        f"📨 *YANGI XABAR!*\n\n"
        f"👤 [{message.from_user.full_name}](tg://user?id={message.from_user.id})\n"
        f"ID: `{message.from_user.id}`\n💬 Matn:\n{message.text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Javob berish", callback_data=f"reply_{message.from_user.id}")]])
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="Markdown")
        await message.answer("✅ Xabaringiz adminga yetkazildi!")
    except Exception:
        await message.answer("Xatolik yuz berdi.")
    await state.clear()

@router.callback_query(F.data.startswith("reply_"))
async def admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
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
    except Exception:
        await message.answer("❌ Xatolik!")
    await state.clear()


# ==========================================
# 5. REYTING VA STATISTIKA
# ==========================================

@router.callback_query(F.data == "show_leaderboard")
async def show_leaderboard_handler(callback: CallbackQuery, bot: Bot):
    now = time.time()
    # ── OPTIM 11: Kesh ishlayaptimi? ──
    if _leaderboard_cache["text"] and now - _leaderboard_cache["ts"] < _LEADERBOARD_TTL:
        await callback.message.edit_text(
            _leaderboard_cache["text"],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]]),
            parse_mode="Markdown"
        )
        return await callback.answer()

    await callback.message.edit_text("⏳ Reyting yuklanmoqda...", parse_mode="Markdown")
    top_users = stats_manager.get_top_users(10)

    if not top_users:
        text = "🏆 *GLOBAL REYTING*\n\nHozircha reytingda hech kim yo'q."
    else:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        # ── OPTIM 12: bot.get_chat — barchasini parallel chaqirish ──
        async def _get_name(user_id):
            try:
                chat_info = await bot.get_chat(user_id)
                return chat_info.full_name or "Sirli Talaba"
            except Exception:
                return "Sirli Talaba"

        names = await asyncio.gather(*[_get_name(u["user_id"]) for u in top_users])

        lines = [
            f"{medals[i] if i < 10 else '🔸'} *{name}*\n      ✅ {user['correct']} ta to'g'ri | 📝 {user['completed']} ta test"
            for i, (user, name) in enumerate(zip(top_users, names))
        ]
        text = "🏆 *TOP 10 TALABALAR REYTINGI*\n\n" + "\n\n".join(lines)

    _leaderboard_cache["text"] = text
    _leaderboard_cache["ts"] = now

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("subj_"))
async def process_subject_selection(callback: CallbackQuery):
    subject_key = _parse_suffix(callback.data, "subj_")
    await callback.message.edit_text(
        f"📚 *{SUBJECTS.get(subject_key, 'Fan')}*\n\nBloklardan birini tanlang:",
        reply_markup=get_blocks_keyboard(subject_key, 0), parse_mode="Markdown"
    )
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
    if callback.message.chat.type != "private":
        return await callback.answer("Faqat shaxsiy chatda!", show_alert=True)

    stats = stats_manager.get_user_stats(callback.from_user.id)
    rank = stats_manager.get_user_rank(callback.from_user.id)

    total = stats["total_correct"] + stats["total_wrong"]
    percent = (stats["total_correct"] / total * 100) if total > 0 else 0
    text = (
        f"📊 *Shaxsiy statistika:*\n\n"
        f"🏆 *Umumiy reytingdagi o'rningiz:* {rank}-o'rin\n\n"
        f"✅ To'g'ri: {stats['total_correct']}\n❌ Xato: {stats['total_wrong']}\n🎯 O'zlashtirish: {percent:.1f}%"
    )
    buttons = []
    if stats.get("history"):
        buttons.append([InlineKeyboardButton(text="📜 Tarix va xatolar", callback_data="hist_page_0")])
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_to_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await callback.answer()

@router.callback_query(F.data.startswith("hist_page_"))
async def show_history_page(callback: CallbackQuery):
    page = int(_parse_suffix(callback.data, "hist_page_"))
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if not history:
        return await callback.answer("Tarix bo'sh!", show_alert=True)

    start_idx = page * 5
    buttons = []
    for i, item in enumerate(history[start_idx:start_idx + 5]):
        t_id = item["test_id"]
        label = "Aralash" if str(t_id) == "mock" else (str(t_id).replace("ugc_", "") if str(t_id).startswith("ugc_") else f"{t_id}-B")
        subj_label = SUBJECTS.get(item["subject"], item["subject"])
        buttons.append([InlineKeyboardButton(
            text=f"📅 {item['date'][:10]} | {subj_label} ({label}) | ✅ {item['correct']}",
            callback_data=f"hist_det_{start_idx + i}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"hist_page_{page - 1}"))
    if start_idx + 5 < len(history):
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"hist_page_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="show_stats")])
    await callback.message.edit_text(
        "📜 *Oxirgi ishlangan testlar:*\nBatafsil ko'rish uchun tanlang.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("hist_det_"))
async def show_history_detail(callback: CallbackQuery):
    idx = int(_parse_suffix(callback.data, "hist_det_"))
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if idx >= len(history):
        return await callback.answer("Xatolik!", show_alert=True)

    item = history[idx]
    t_id = str(item["test_id"])
    t_label = "Aralash Test" if t_id == "mock" else ("Maxsus Test" if t_id.startswith("ugc_") else f"{t_id}-Blok")

    parts = [f"📅 {item['date']}\n📚 {SUBJECTS.get(item['subject'], item['subject'])} | {t_label}\n📊 ✅ {item['correct']}, ❌ {item['wrong']}\n"]
    if not item.get("mistakes"):
        parts.append("🎉 *Xato qilinmagan!*")
    else:
        parts.append("📑 *XATOLAR:*\n")
        for i, m in enumerate(item["mistakes"], 1):
            parts.append(f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}")

    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:4000] + "\n... (kesildi)."

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="hist_page_0")]]),
        parse_mode="Markdown"
    )
    await callback.answer()


# ==========================================
# 6. TEST O'YINI
# ==========================================

@router.callback_query(F.data.startswith("start_test_") | F.data.startswith("mock_"))
async def start_test_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id in active_tests or chat_id in waiting_rooms:
        return await callback.answer("⚠️ Avval joriy testni to'xtating! /stop", show_alert=True)

    is_mock = callback.data.startswith("mock_")
    if is_mock:
        subject_key = _parse_suffix(callback.data, "mock_")
        # ── OPTIM 13: list comprehension bitta pass ──
        all_q = [q for test in memory_db.get(subject_key, {}).values() for q in test["questions"]]
        if not all_q:
            return await callback.answer("Savollar yo'q!", show_alert=True)
        test_data = {"questions": random.sample(all_q, min(25, len(all_q))), "block_name": "Aralash Test"}
        test_id = "mock"
    else:
        parts = _parse_suffix(callback.data, "start_test_").rsplit("_", 1)
        subject_key, test_id = parts[0], int(parts[1])
        test_data = memory_db.get(subject_key, {}).get(test_id)
        if not test_data:
            return await callback.answer("Test topilmadi!", show_alert=True)

    chat_type = callback.message.chat.type
    if chat_type != "private":
        waiting_rooms[chat_id] = {
            "subject_key": subject_key, "test_id": test_id, "test_data": test_data,
            "ready_users": set(), "initiator_id": callback.from_user.id
        }
        try:
            await callback.message.delete()
        except Exception:
            pass
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Tayyorman (0)", callback_data="room_ready")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")]
        ])
        await bot.send_message(
            chat_id,
            f"👥 *Guruh rejimi!*\n📚 {SUBJECTS.get(subject_key, 'Fan')} | {'Aralash' if test_id == 'mock' else f'{test_id}-Blok'}\n\nKamida 2 kishi tayyor bo'lishi kerak!",
            reply_markup=kb, parse_mode="Markdown"
        )
        return await callback.answer()

    session_q = prepare_shuffled_questions(test_data["questions"])
    active_tests[chat_id] = {
        "chat_type": chat_type, "initiator_id": callback.from_user.id, "subject_key": subject_key, "test_id": test_id,
        "block_name": test_data.get("block_name", ""), "session_questions": session_q, "q_idx": 0, "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None, "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {},
    }
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "room_ready")
async def room_ready_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms:
        return await callback.answer("Kutish zali yopilgan!", show_alert=True)
    room = waiting_rooms[chat_id]
    if callback.from_user.id in room["ready_users"]:
        return await callback.answer("Siz allaqachon tayyorsiz!", show_alert=True)

    room["ready_users"].add(callback.from_user.id)
    count = len(room["ready_users"])
    buttons = [[InlineKeyboardButton(text=f"✅ Tayyorman ({count})", callback_data="room_ready")]]
    if count >= 2:
        buttons.append([InlineKeyboardButton(text="🚀 Boshlash", callback_data="room_start")])
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer(f"✅ Tayyor! ({count} kishi)")

@router.callback_query(F.data == "room_start")
async def room_start_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms:
        return await callback.answer()
    room = waiting_rooms[chat_id]
    if len(room["ready_users"]) < 2:
        return await callback.answer("Kamida 2 kishi!", show_alert=True)

    session_q = prepare_shuffled_questions(room["test_data"]["questions"])
    active_tests[chat_id] = {
        "chat_type": "group", "initiator_id": room["initiator_id"], "subject_key": room["subject_key"], "test_id": room["test_id"],
        "block_name": room["test_data"].get("block_name", ""), "session_questions": session_q, "q_idx": 0, "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None, "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {},
    }
    del waiting_rooms[chat_id]
    try:
        await callback.message.delete()
    except Exception:
        pass
    await bot.send_message(chat_id, "🚀 *Test boshlandi!*", parse_mode="Markdown")
    await callback.answer()
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "room_cancel")
async def room_cancel_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms:
        return await callback.answer("Kutish zali yopilgan.", show_alert=True)
    if callback.from_user.id != waiting_rooms[chat_id]["initiator_id"]:
        return await callback.answer("Faqat testni boshlagan odam bekor qila oladi!", show_alert=True)
    del waiting_rooms[chat_id]
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Test bekor qilindi.", show_alert=True)

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
        else:
            await send_next_question(chat_id, bot)
    else:
        await send_next_question(chat_id, bot)

@router.callback_query(F.data == "resume_test")
async def resume_test_handler(callback: CallbackQuery, bot: Bot):
    chat_id = callback.message.chat.id
    if chat_id not in active_tests:
        return await callback.answer("Test topilmadi.", show_alert=True)
    active_tests[chat_id]["consecutive_timeouts"] = 0
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "force_finish")
async def force_finish_handler(callback: CallbackQuery, bot: Bot):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
    await finish_test(callback.message.chat.id, bot)

async def send_next_question(chat_id: int, bot: Bot):
    session = active_tests.get(chat_id)
    if not session:
        return
    questions = session["session_questions"]
    q_idx = session["q_idx"]
    if q_idx >= len(questions):
        return await finish_test(chat_id, bot)

    q = questions[q_idx]
    q_text_full = f"[{q_idx + 1}/{len(questions)}] {q['question']}"
    needs_text = len(q_text_full) > 255 or any(len(opt) > 100 for opt in q["options"])

    if needs_text:
        labels = ["A", "B", "C", "D", "E", "F"]
        # ── OPTIM 14: join bilan string qurilishi ──
        text_msg = f"📑 Savol [{q_idx + 1}/{len(questions)}]\n\n{q['question']}\n\nVariantlar:\n" + \
                   "\n".join(f"{labels[i]}) {opt}" for i, opt in enumerate(q["options"]))
        if len(text_msg) > 4000:
            text_msg = text_msg[:4000] + "...\n(Xabar kesildi)"
        await bot.send_message(chat_id, text_msg)
        poll_q = f"[{q_idx + 1}/{len(questions)}] To'g'ri variantni belgilang:"
        poll_opts = [f"{labels[i]} varianti" for i in range(len(q["options"]))]
    else:
        poll_q, poll_opts = q_text_full, q["options"]

    msg = await bot.send_poll(
        chat_id=chat_id, question=poll_q, options=poll_opts,
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
    if not chat_id or chat_id not in active_tests:
        return
    session = active_tests[chat_id]
    if session["poll_id"] != poll_answer.poll_id:
        return

    q_data = session["session_questions"][session["q_idx"]]
    is_correct = poll_answer.option_ids[0] == q_data["correct_index"]

    if session["chat_type"] == "private":
        session["consecutive_timeouts"] = 0
        if session.get("timer_task"):
            session["timer_task"].cancel()
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
                "correct_ans": q_data["correct_text"],
                "wrong_ans": q_data["options"][poll_answer.option_ids[0]]
            })
        session["q_idx"] += 1
        await send_next_question(chat_id, bot)
    else:
        lock = _get_group_lock(chat_id)
        async with lock:
            u_id = poll_answer.user.id
            if u_id not in session["group_scores"]:
                session["group_scores"][u_id] = {"name": poll_answer.user.full_name, "correct": 0, "wrong": 0, "mistakes": []}
            score = session["group_scores"][u_id]
            if is_correct:
                score["correct"] += 1
            else:
                score["wrong"] += 1
                score["mistakes"].append({
                    "question": q_data["question"],
                    "correct_ans": q_data["correct_text"],
                    "wrong_ans": q_data["options"][poll_answer.option_ids[0]]
                })


# ==========================================
# 9. TEST YAKUNLASH VA NAVIGATSIYA
# ==========================================

async def finish_test(chat_id: int, bot: Bot):
    session = active_tests.get(chat_id)
    if not session:
        return
    if session.get("timer_task"):
        session["timer_task"].cancel()

    t_id = session["test_id"]
    if str(t_id) == "mock":
        t_name = "Aralash Test"
    elif str(t_id).startswith("ugc_"):
        t_name = f"📝 {session.get('block_name', 'Maxsus Test')}"
    else:
        t_name = f"{t_id}-Blok"

    mins, secs = divmod(int(time.time() - session["start_time"]), 60)
    title = f"{SUBJECTS.get(session['subject_key'], session['subject_key'])} | {t_name}"
    buttons = []

    if session["chat_type"] == "private":
        stats_manager.update_user_stats(
            chat_id, session["correct"], session["wrong"],
            session["subject_key"], session["test_id"], session["mistakes"]
        )
        total_q = session["correct"] + session["wrong"]
        percent = round(session["correct"] / total_q * 100, 1) if total_q > 0 else 0
        text = (
            f"🏁 *{title} Yakunlandi!*\n\n"
            f"🟢 To'g'ri: {session['correct']}\n🔴 Xato: {session['wrong']}\n"
            f"🎯 O'zlashtirish: {percent}%\n⏱ Vaqt: {mins:02d}:{secs:02d}"
        )
        if session.get("mistakes"):
            buttons.append([InlineKeyboardButton(text="❌ Xatolar ustida ishlash", callback_data="review_mistakes")])

        if str(t_id).startswith("ugc_"):
            buttons.append([InlineKeyboardButton(text="🔁 Qayta ishlash", callback_data=f"ugc_start_{str(t_id).replace('ugc_', '')}")])
        elif t_id != "mock":
            buttons.append([InlineKeyboardButton(text="🔁 Qayta ishlash", callback_data=f"post_start_{session['subject_key']}_{t_id}")])
            if t_id + 1 in memory_db.get(session["subject_key"], {}):
                buttons.append([InlineKeyboardButton(text="➡️ Keyingi Blok", callback_data=f"post_start_{session['subject_key']}_{t_id + 1}")])
    else:
        # ── OPTIM 15: guruh statistikasini parallel yangilash ──
        await asyncio.gather(*[
            asyncio.to_thread(
                stats_manager.update_user_stats,
                u_id, scores["correct"], scores["wrong"],
                session["subject_key"], session["test_id"], scores["mistakes"]
            )
            for u_id, scores in session["group_scores"].items()
        ])
        if not session["group_scores"]:
            body = "Hech kim qatnashmadi 😔"
        else:
            medals = ["🥇", "🥈", "🥉"]
            sorted_scores = sorted(session["group_scores"].values(), key=lambda x: x["correct"], reverse=True)
            body = "\n".join(
                f"{medals[i] if i < 3 else '🔸'} {s['name']}: {s['correct']} ta to'g'ri"
                for i, s in enumerate(sorted_scores)
            )
        text = f"🏁 *{title} yakunlandi!*\n⏱ Vaqt: {mins:02d}:{secs:02d}\n\n🏆 *NATIJALAR:*\n{body}"

    if str(t_id).startswith("ugc_"):
        buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")])
    else:
        buttons.extend([
            [InlineKeyboardButton(text="🔙 Fan menyusiga", callback_data=f"post_subj_{session['subject_key']}")],
            [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")]
        ])

    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

    poll_chat_map.pop(session.get("poll_id"), None)
    _group_answer_locks.pop(chat_id, None)
    del active_tests[chat_id]

@router.callback_query(F.data == "post_main")
async def post_main_handler(callback: CallbackQuery):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "🏛 *Talabalar Imtihon Trenajyori*\n\nKerakli bo'limni tanlang:",
        reply_markup=get_main_keyboard(), parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("post_subj_"))
async def post_subj_handler(callback: CallbackQuery):
    subject_key = _parse_suffix(callback.data, "post_subj_")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        f"📚 *{SUBJECTS.get(subject_key, 'Fan')}*\n\nBloklardan birini tanlang:",
        reply_markup=get_blocks_keyboard(subject_key, 0), parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("post_start_"))
async def post_start_handler(callback: CallbackQuery, bot: Bot):
    suffix = _parse_suffix(callback.data, "post_start_")
    parts = suffix.rsplit("_", 1)
    subject_key, test_id = parts[0], int(parts[1])
    test_data = memory_db.get(subject_key, {}).get(test_id)
    if not test_data:
        return await callback.answer("Test topilmadi!", show_alert=True)

    chat_id = callback.message.chat.id
    if chat_id in active_tests or chat_id in waiting_rooms:
        return await callback.answer("⚠️ Avval joriy testni to'xtating! /stop", show_alert=True)

    session_q = prepare_shuffled_questions(test_data["questions"])
    active_tests[chat_id] = {
        "chat_type": "private", "initiator_id": callback.from_user.id, "subject_key": subject_key, "test_id": test_id,
        "block_name": test_data.get("block_name", f"{test_id}-Blok"), "session_questions": session_q, "q_idx": 0,
        "start_time": time.time(), "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [], "consecutive_timeouts": 0, "group_scores": {},
    }
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "review_mistakes")
async def review_mistakes_handler(callback: CallbackQuery):
    stats = stats_manager.get_user_stats(callback.from_user.id)
    history = stats.get("history", [])
    if not history:
        return await callback.answer("Xatolar topilmadi!", show_alert=True)

    mistakes = history[0].get("mistakes", [])
    if not mistakes:
        return await callback.answer("Bu testda xato yo'q edi! 🎉", show_alert=True)

    # ── OPTIM 16: join bilan string ──
    parts = ["📑 *So'nggi test xatolari:*\n"]
    parts.extend(
        f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}"
        for i, m in enumerate(mistakes, 1)
    )
    text = "\n\n".join(parts)
    if len(text) > 4000:
        text = text[:4000] + "\n... (kesildi)."

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]]),
        parse_mode="Markdown"
    )
    await callback.answer()

@router.callback_query(F.data == "ignore")
async def ignore_handler(callback: CallbackQuery):
    await callback.answer()
