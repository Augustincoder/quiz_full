import os
import time
import random
import asyncio
import logging
from collections import defaultdict
from docx import Document
from aiogram import Bot, Router, F
from aiogram.types import Message, CallbackQuery, PollAnswer, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import stats_manager
from config import SUBJECTS, ADMIN_ID

logger = logging.getLogger(__name__)

router = Router()

active_tests: dict = {}
waiting_rooms: dict = {}
poll_chat_map: dict = {}

# ⚠️ Bot yonganda bazadagi rasmiy testlarni yuklab olamiz
memory_db: dict = stats_manager.load_all_official_tests()
ITEMS_PER_PAGE = 5

_group_answer_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

def _get_group_lock(chat_id: int) -> asyncio.Lock:
    return _group_answer_locks[chat_id]

_leaderboard_cache: dict = {"text": None, "ts": 0.0}
_LEADERBOARD_TTL = 120   # 2 daqiqa kesh (avval 60 soniya edi)
_BROADCAST_SEMAPHORE = asyncio.Semaphore(25)

# Foydalanuvchi ismlari keshi — bot.get_chat() chaqiruvlarini kamaytiradi
_user_name_cache: dict[int, str] = {}

def _parse_suffix(data: str, prefix: str) -> str:
    return data[len(prefix):]

# ============================================================
# YORDAMCHI FUNKSIYALAR
# ============================================================

async def _safe_edit(message: Message, text: str, reply_markup=None, parse_mode="Markdown") -> bool:
    """Xavfsiz edit — xato bo'lsa yangi xabar yuboradi."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except TelegramBadRequest:
        try:
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception as e:
            logger.warning(f"_safe_edit failed completely: {e}")
    return False

async def _safe_delete(message: Message):
    """Xavfsiz o'chirish — xato bo'lsa e'tibor bermaydi."""
    try:
        await message.delete()
    except Exception:
        pass

async def _get_user_name(bot: Bot, user_id: int) -> str:
    """
    Foydalanuvchi ismini AVVAL keshdan oladi, yo'q bo'lsa stats_manager'dan,
    oxirgi chora sifatida bot.get_chat() ishlatadi.
    Bu funksiya bot qotib qolishining asosiy sababini bartaraf etadi.
    """
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]

    # Stats managerdan tezroq olishga harakat
    try:
        all_users = stats_manager.get_all_users()
        for u in all_users:
            if u.get("telegram_id") == user_id:
                name = u.get("full_name") or "Sirli Talaba"
                _user_name_cache[user_id] = name
                return name
    except Exception:
        pass

    # Oxirgi chora: API chaqiruvi
    try:
        chat_info = await asyncio.wait_for(bot.get_chat(user_id), timeout=5.0)
        name = chat_info.full_name or "Sirli Talaba"
        _user_name_cache[user_id] = name
        return name
    except Exception:
        return "Sirli Talaba"

def _progress_bar(current: int, total: int, length: int = 15) -> str:
    if total == 0:
        return "░" * length
    filled = int(length * current / total)
    return "▓" * filled + "░" * (length - filled)

# ============================================================
# FSM HOLATLARI
# ============================================================

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

# ============================================================
# SAVOLLARNI TAYYORLASH
# ============================================================

def prepare_shuffled_questions(raw_questions: list) -> list:
    shuffled_q = random.sample(raw_questions, len(raw_questions))
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

# ============================================================
# KLAVIATURALAR
# ============================================================

def get_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Rasmiy Testlar", callback_data="official_tests")],
        [
            InlineKeyboardButton(text="📝 Test Yaratish", callback_data="create_test"),
            InlineKeyboardButton(text="📂 Mening Testlarim", callback_data="my_tests"),
        ],
        [
            InlineKeyboardButton(text="📊 Statistikam", callback_data="show_stats"),
            InlineKeyboardButton(text="🏆 Reyting", callback_data="show_leaderboard"),
        ],
        [InlineKeyboardButton(text="💬 Adminga Murojaat", callback_data="contact_admin")],
    ])

_blocks_kb_cache: dict = {}

def invalidate_blocks_cache(subject_key: str | None = None):
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
        buttons.append([InlineKeyboardButton(text="📭 Bu fanda hozircha test yo'q", callback_data="ignore")])
    else:
        for t_id in current_tests:
            q_count = len(subject_tests[t_id].get("questions", []))
            buttons.append([InlineKeyboardButton(
                text=f"📘 {t_id}-Blok  •  {q_count} ta savol",
                callback_data=f"start_test_{subject_key}_{t_id}"
            )])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"page_{subject_key}_{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"page_{subject_key}_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton(text="🎲 Aralash (Mock Exam)", callback_data=f"mock_{subject_key}")])
    buttons.append([InlineKeyboardButton(text="🔙 Fanlarga qaytish", callback_data="official_tests")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    _blocks_kb_cache[cache_key] = kb
    return kb

def back_to_main_kb(extra_buttons: list = None) -> InlineKeyboardMarkup:
    """Asosiy menyuga qaytish tugmasi bilan klaviatura."""
    buttons = extra_buttons or []
    buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ============================================================
# 1. ASOSIY BUYRUQLAR
# ============================================================

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    chat_id = message.chat.id

    stats_manager.register_user(
        message.from_user.id,
        message.from_user.full_name,
        message.from_user.username
    )
    # Ismni keshga olish
    _user_name_cache[message.from_user.id] = message.from_user.full_name or "Foydalanuvchi"

    # Eski testlarni tozalash
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
        _group_answer_locks.pop(chat_id, None)
        del active_tests[chat_id]
        cleared = True
    if cleared:
        await message.answer("🔄 Tugallanmagan test tozalandi. Yangi boshlashingiz mumkin!")

    # Deep-link ishlov
    args = message.text.split()
    if len(args) > 1:
        if args[1].startswith("s_"):
            ref_id = args[1][2:]
            test_data_db = stats_manager.get_user_test(ref_id)
            if not test_data_db:
                return await message.answer(
                    "❌ Bu fan topilmadi yoki egasi tomonidan o'chirilgan.\n\n"
                    "Asosiy menyuga qaytish uchun /start bosing.",
                    reply_markup=back_to_main_kb()
                )
            return await show_ugc_subject_blocks(message, test_data_db["creator_id"], test_data_db["subject"])
        elif args[1].startswith("t_"):
            test_data_db = stats_manager.get_user_test(args[1][2:])
            if test_data_db:
                return await start_ugc_test(message, test_data_db, bot)
            else:
                return await message.answer(
                    "❌ Bu blok topilmadi yoki egasi tomonidan o'chirilgan.\n\n"
                    "Asosiy menyuga qaytish uchun /start bosing.",
                    reply_markup=back_to_main_kb()
                )

    first_name = message.from_user.first_name or "Talaba"
    await message.answer(
        f"👋 Assalomu alaykum, *{first_name}*!\n\n"
        f"🏛 *Talabalar Imtihon Trenajyori*ga xush kelibsiz!\n\n"
        f"📌 Nima qilishingiz mumkin:\n"
        f"• 📚 Rasmiy testlar — Admin tomonidan tayyorlangan bloklar\n"
        f"• 📝 Test yaratish — O'z testingizni tuzing va ulashing\n"
        f"• 📊 Statistika — Natijalaringizni kuzating\n"
        f"• 🏆 Reyting — Top 10 talabalar\n\n"
        f"⬇️ Kerakli bo'limni tanlang:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await _safe_edit(
        callback.message,
        "🏛 *Talabalar Imtihon Trenajyori*\n\nKerakli bo'limni tanlang:",
        reply_markup=get_main_keyboard()
    )

@router.message(Command("stop"))
async def cmd_stop(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    chat_id = message.chat.id
    user_id = message.from_user.id

    if chat_id in waiting_rooms:
        room = waiting_rooms[chat_id]
        if user_id == room["initiator_id"] or message.chat.type == "private":
            del waiting_rooms[chat_id]
            await message.answer(
                "🛑 Test bekor qilindi.",
                reply_markup=back_to_main_kb()
            )
        else:
            await message.answer("⚠️ Faqat testni boshlagan kishi bekor qila oladi!")
        return

    if chat_id in active_tests:
        if message.chat.type != "private" and user_id != active_tests[chat_id].get("initiator_id"):
            return await message.answer("⚠️ Faqat testni boshlagan kishi to'xtatа oladi!")
        await message.answer("🛑 *Test to'xtatildi!*\nNatijalar hisoblanmoqda...", parse_mode="Markdown")
        await finish_test(chat_id, bot)
    else:
        await message.answer(
            "ℹ️ Hozir faol test yo'q.\n\nAsosiy menyuga qaytish uchun tugmani bosing:",
            reply_markup=back_to_main_kb()
        )

@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    """Istalgan vaqtda asosiy menyuni chiqarish."""
    await state.clear()
    await message.answer(
        "🏛 *Asosiy Menyu*",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

# ============================================================
# 2. TEST YARATISH (UGC)
# ============================================================

@router.callback_query(F.data == "create_test")
async def create_test_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()

    tests = stats_manager.get_user_created_tests(callback.from_user.id)
    subjects = {}
    for t in tests:
        subjects.setdefault(t["subject"], t["id"])

    buttons = []
    if subjects:
        buttons.append([InlineKeyboardButton(text="── Mavjud fanlaringiz ──", callback_data="ignore")])
        for subj, ref_id in subjects.items():
            subj_tests = [t for t in tests if t["subject"] == subj]
            buttons.append([InlineKeyboardButton(
                text=f"📁 {subj}  •  {len(subj_tests)} ta blok",
                callback_data=f"ct_exist_{ref_id}"
            )])
        buttons.append([InlineKeyboardButton(text="➕ Yangi fan yaratish", callback_data="ct_new")])
    else:
        buttons.append([InlineKeyboardButton(text="➕ Birinchi fanimni yarataman", callback_data="ct_new")])

    buttons.append([InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")])

    text = (
        "📝 *Test Yaratish*\n\n"
        "Bu bo'limda o'z testlaringizni yaratib, do'stlaringiz bilan ulashishingiz mumkin.\n\n"
        "📌 *Qo'llanma:*\n"
        "1️⃣ Fan tanlang (mavjud yoki yangi)\n"
        "2️⃣ Blok nomi bering\n"
        "3️⃣ Savollarni yuboring\n"
        "4️⃣ Havolani do'stlaringizga yuboring\n\n"
        "👇 Qaysi fanga yangi blok qo'shmoqchisiz?"
    )
    await _safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "ct_new")
async def ct_new_subject(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(CreateTestStates.waiting_for_subject)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")]
    ])
    await _safe_edit(
        callback.message,
        "📝 *Yangi Fan Yaratish — 1-qadam*\n\n"
        "Fan nomini yozing:\n\n"
        "_Masalan: Anatomiya, Fizika 1-kurs, Tarix (2024)_\n\n"
        "⌨️ Pastga yozing:",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("ct_exist_"))
async def ct_exist_subject(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    ref_id = _parse_suffix(callback.data, "ct_exist_")
    test_data = stats_manager.get_user_test(ref_id)
    if not test_data:
        return await callback.answer("❌ Fan topilmadi!", show_alert=True)
    await state.update_data(subject=test_data["subject"])
    await state.set_state(CreateTestStates.waiting_for_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")]
    ])
    await _safe_edit(
        callback.message,
        f"✅ Fan tanlandi: *{test_data['subject']}*\n\n"
        f"📝 *Yangi Blok Yaratish — 2-qadam*\n\n"
        f"Yangi blok nomini yozing:\n\n"
        f"_Masalan: 1-Mavzu, 5-Bob, Yakuniy imtihon_\n\n"
        f"⌨️ Pastga yozing:",
        reply_markup=kb
    )

@router.message(CreateTestStates.waiting_for_subject)
async def create_test_subject(message: Message, state: FSMContext):
    subject = message.text.strip()
    if not subject or len(subject) < 2:
        return await message.answer("⚠️ Fan nomi kamida 2 ta harfdan iborat bo'lishi kerak.")
    if len(subject) > 50:
        return await message.answer("⚠️ Fan nomi 50 ta belgidan ko'p bo'lmasligi kerak.")

    await state.update_data(subject=subject)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")]
    ])
    await message.answer(
        f"✅ Fan yaratildi: *{subject}*\n\n"
        f"📝 *2-qadam: Blok nomi*\n\n"
        f"Bu fanning birinchi bloki nomini yozing:\n"
        f"_Masalan: 1-Mavzu, 1-Blok, Kirish_\n\n"
        f"⌨️ Pastga yozing:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(CreateTestStates.waiting_for_name)

@router.message(CreateTestStates.waiting_for_name)
async def create_test_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) < 1:
        return await message.answer("⚠️ Blok nomini kiriting.")
    if len(name) > 60:
        return await message.answer("⚠️ Blok nomi 60 ta belgidan ko'p bo'lmasligi kerak.")

    await state.update_data(block_name=name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Telegram Quiz (Eng qulay)", callback_data="fmt_quiz")],
        [InlineKeyboardButton(text="📝 Matn ko'rinishida", callback_data="fmt_text")],
        [InlineKeyboardButton(text="📄 Word fayl (.docx)", callback_data="fmt_docx")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
    ])
    await message.answer(
        f"✅ Blok nomi: *{name}*\n\n"
        f"📝 *3-qadam: Format tanlang*\n\n"
        f"Savollarni qanday formatda yubormoqchisiz?\n\n"
        f"📊 *Quiz* — Telegram viktorina sifatida (eng qulay)\n"
        f"📝 *Matn* — Oddiy matn formatida yozing\n"
        f"📄 *Word* — .docx fayl yuklang\n\n"
        f"⬇️ Formatni tanlang:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(CreateTestStates.waiting_for_format)

@router.callback_query(CreateTestStates.waiting_for_format, F.data.startswith("fmt_"))
async def create_test_format(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    fmt = _parse_suffix(callback.data, "fmt_")
    await state.update_data(format=fmt, questions=[])

    if fmt == "quiz":
        text = (
            "📊 *Telegram Quiz Formati*\n\n"
            "Telegram'ning standart *Viktorina (Quiz)* funksiyasidan foydalanib savollarni bittadan yuboring.\n\n"
            "📌 *Qanday yaratiladi?*\n"
            "1. Paperclip (📎) belgisini bosing\n"
            "2. *Poll* ni tanlang\n"
            "3. *Quiz* turini belgilang\n"
            "4. Savol va javoblarni kiriting\n"
            "5. To'g'ri javobni belgilang\n"
            "6. Yuboring\n\n"
            "✅ Barcha savollar yuborilgach — *Yakunlash* tugmasini bosing."
        )
    elif fmt == "text":
        text = (
            "📝 *Matn Formati*\n\n"
            "Savollarni quyidagi ko'rinishda yuboring:\n\n"
            "```\n"
            "O'zbekiston poytaxti qaysi shahar?\n"
            "#Toshkent\n"
            "Samarqand\n"
            "Buxoro\n"
            "Namangan\n"
            "```\n\n"
            "📌 *Qoidalar:*\n"
            "• To'g'ri javob oldiga `#` qo'ying\n"
            "• Savollar orasida bo'sh qator bo'lsin\n"
            "• Bir xabarda ko'p savol yuboring mumkin\n\n"
            "✅ Barcha savollar yuborilgach — *Yakunlash* tugmasini bosing."
        )
    else:
        text = (
            "📄 *Word Fayl (.docx) Formati*\n\n"
            "Faylni quyidagi tartibda tayyorlang:\n\n"
            "```\n"
            "O'zbekiston poytaxti?\n"
            "#Toshkent\n"
            "Samarqand\n"
            "Buxoro\n"
            "\n"
            "Ikkinchi savol matni?\n"
            "#To'g'ri javob\n"
            "Xato javob\n"
            "Xato javob\n"
            "```\n\n"
            "📌 *Qoidalar:*\n"
            "• To'g'ri javob oldiga `#` qo'ying\n"
            "• Savollar orasida *1 ta bo'sh qator* bo'lsin\n"
            "• Bir nechta fayl ketma-ket yuborishingiz mumkin\n\n"
            "✅ Barcha fayllar yuborilgach — *Yakunlash* tugmasini bosing."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yakunlash va Saqlash", callback_data="finish_test_creation")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
    ])
    await _safe_edit(callback.message, text, reply_markup=kb)
    await state.set_state(CreateTestStates.waiting_for_questions)

# --- Docx parsing ---
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

def _questions_summary_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yakunlash va Saqlash", callback_data="finish_test_creation")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_creation")],
    ])

@router.message(CreateTestStates.waiting_for_questions, F.document)
async def receive_docx_file(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if data.get("format") != "docx":
        return await message.answer(
            "⚠️ Hozir *Quiz* yoki *Matn* formati tanlangan.\n"
            "Word fayl yuklash uchun avval format o'zgartiring.",
            parse_mode="Markdown"
        )
    if not message.document.file_name.endswith(".docx"):
        return await message.answer("⚠️ Faqat `.docx` formatdagi fayl qabul qilinadi.")

    msg = await message.answer("⏳ Fayl o'qilmoqda, biroz kuting...")
    file_path = f"temp_{message.from_user.id}_{int(time.time())}.docx"
    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, file_path)
        new_qs = _parse_docx_questions(file_path)

        if not new_qs:
            await msg.edit_text(
                "❌ Fayldan savol topilmadi!\n\n"
                "Fayl tuzilishi to'g'riligini tekshiring:\n"
                "```\nSavol matni\n#To'g'ri javob\nXato javob\nXato javob\n```\n"
                "_Savollar orasida bo'sh qator bo'lsin._",
                parse_mode="Markdown"
            )
            return

        questions = data.get("questions", []) + new_qs
        await state.update_data(questions=questions)
        bar = _progress_bar(min(len(questions), 50), 50)
        await msg.edit_text(
            f"✅ *Fayl muvaffaqiyatli o'qildi!*\n\n"
            f"📊 Bu fayldan: *{len(new_qs)} ta* savol\n"
            f"📊 Jami yig'ildi: *{len(questions)} ta* savol\n"
            f"{bar}\n\n"
            f"Yana fayl yuborishingiz yoki yakunlashingiz mumkin.",
            reply_markup=_questions_summary_kb(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"docx parse error: {e}")
        await msg.edit_text(
            "❌ Faylni o'qishda xatolik yuz berdi.\n"
            "Fayl buzilgan bo'lishi mumkin. Qayta urinib ko'ring."
        )
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
            return await message.answer(
                "⚠️ Iltimos, Telegram *Quiz (Viktorina)* yuboring!\n\n"
                "📎 → Poll → Quiz turini tanlang.",
                parse_mode="Markdown"
            )
        questions.append({
            "question": message.poll.question,
            "options": [o.text for o in message.poll.options],
            "correct_index": message.poll.correct_option_id,
        })
    elif fmt == "text":
        if not message.text:
            return await message.answer("⚠️ Iltimos, matn formatida savollar yuboring.")
        added = _parse_text_questions(message.text)
        if not added:
            return await message.answer(
                "⚠️ Savol topilmadi!\n\n"
                "To'g'ri format:\n"
                "```\nSavol matni?\n#To'g'ri javob\nXato javob 1\nXato javob 2\n```\n"
                "To'g'ri javob oldiga `#` qo'yishni unutmang.",
                parse_mode="Markdown"
            )
        questions.extend(added)
    elif fmt == "docx":
        return await message.answer(
            "⚠️ Hozir *Word fayl* formati tanlangan.\n"
            "Iltimos, `.docx` fayl yuboring.",
            parse_mode="Markdown"
        )

    await state.update_data(questions=questions)
    bar = _progress_bar(min(len(questions), 50), 50)
    await message.answer(
        f"✅ Qabul qilindi!\n\n"
        f"📊 Jami savollar: *{len(questions)} ta*\n"
        f"{bar}\n\n"
        f"Davom etishingiz yoki yakunlashingiz mumkin.",
        reply_markup=_questions_summary_kb(),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "finish_test_creation")
async def finish_creation(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.answer()
    data = await state.get_data()
    questions = data.get("questions", [])
    if not questions:
        return await callback.answer("❌ Hech qanday savol yo'q! Avval savol yuboring.", show_alert=True)

    test_id = stats_manager.save_user_test(
        callback.from_user.id,
        data["subject"],
        data["block_name"],
        questions
    )
    if not test_id:
        return await callback.message.answer(
            "❌ Saqlashda xatolik yuz berdi. Qayta urinib ko'ring."
        )

    bot_info = await bot.get_me()
    link_block = f"https://t.me/{bot_info.username}?start=t_{test_id}"
    link_subject = f"https://t.me/{bot_info.username}?start=s_{test_id}"

    text = (
        f"🎉 *Blok muvaffaqiyatli saqlandi!*\n\n"
        f"📚 Fan: *{data['subject']}*\n"
        f"📝 Blok: *{data['block_name']}*\n"
        f"🔢 Savollar: *{len(questions)} ta*\n\n"
        f"🔗 *Faqat shu blok havolasi:*\n`{link_block}`\n\n"
        f"🔗 *Butun fan havolasi:*\n`{link_subject}`\n\n"
        f"_Havolani nusxalab do'stlaringizga yuboring!_"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Yana blok qo'shish", callback_data="create_test")],
        [InlineKeyboardButton(text="📂 Mening Testlarim", callback_data="my_tests")],
        [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")],
    ])
    await _safe_edit(callback.message, text, reply_markup=kb)
    await state.clear()

@router.callback_query(F.data == "cancel_creation")
async def cancel_creation_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await _safe_edit(
        callback.message,
        "❌ Test yaratish bekor qilindi.",
        reply_markup=back_to_main_kb([
            [InlineKeyboardButton(text="📝 Qayta yaratish", callback_data="create_test")]
        ])
    )

# ============================================================
# 3. MENING TESTLARIM
# ============================================================

@router.callback_query(F.data == "my_tests")
async def my_tests_handler(callback: CallbackQuery):
    await callback.answer()
    tests = stats_manager.get_user_created_tests(callback.from_user.id)
    if not tests:
        return await _safe_edit(
            callback.message,
            "📂 *Mening Testlarim*\n\n"
            "Siz hali hech qanday test yaratmagansiz.\n\n"
            "Test yaratish uchun quyidagi tugmani bosing:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Test Yaratish", callback_data="create_test")],
                [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")],
            ])
        )

    subjects: dict[str, list] = {}
    for t in tests:
        subjects.setdefault(t["subject"], []).append(t)

    total_blocks = len(tests)
    total_questions = sum(len(t.get("questions", [])) for t in tests)

    buttons = []
    for subj, subj_tests in subjects.items():
        q_count = sum(len(t.get("questions", [])) for t in subj_tests)
        buttons.append([InlineKeyboardButton(
            text=f"📁 {subj}  •  {len(subj_tests)} blok, {q_count} savol",
            callback_data=f"manage_subj_{subj_tests[0]['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Yangi fan/blok yaratish", callback_data="create_test")])
    buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")])

    await _safe_edit(
        callback.message,
        f"📂 *Mening Fanlarim*\n\n"
        f"📊 Jami: {len(subjects)} ta fan, {total_blocks} ta blok, {total_questions} ta savol\n\n"
        f"Boshqarish uchun fanni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith("manage_subj_"))
async def manage_subj_handler(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    ref_id = _parse_suffix(callback.data, "manage_subj_")
    test_data = stats_manager.get_user_test(ref_id)
    if not test_data:
        return await callback.answer("❌ Topilmadi!", show_alert=True)

    tests = stats_manager.get_user_created_tests(callback.from_user.id)
    subj_tests = [t for t in tests if t["subject"] == test_data["subject"]]

    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=s_{ref_id}"

    buttons = []
    for t in subj_tests:
        q_count = len(t.get("questions", []))
        buttons.append([InlineKeyboardButton(
            text=f"📝 {t['block_name']}  •  {q_count} ta savol",
            callback_data=f"manage_test_{t['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="➕ Bu fanga blok qo'shish", callback_data=f"ct_exist_{ref_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Mening Testlarimga", callback_data="my_tests")])

    await _safe_edit(
        callback.message,
        f"📚 *Fan:* {test_data['subject']}\n\n"
        f"🔗 *Fan havolasi (barcha bloklar):*\n`{link}`\n"
        f"_Do'stlaringiz shu havola orqali barcha bloklaringizni ko'ra oladi_\n\n"
        f"📋 Bloklar ro'yxati:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith("manage_test_"))
async def manage_test_handler(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    test_id = _parse_suffix(callback.data, "manage_test_")
    test_data = stats_manager.get_user_test(test_id)

    if not test_data or str(test_data["creator_id"]) != str(callback.from_user.id):
        return await callback.answer("❌ Test topilmadi yoki ruxsat yo'q!", show_alert=True)

    bot_info = await bot.get_me()
    link_block = f"https://t.me/{bot_info.username}?start=t_{test_id}"
    q_count = len(test_data.get("questions", []))

    text = (
        f"📝 *Blok Ma'lumotlari*\n\n"
        f"📚 Fan: {test_data['subject']}\n"
        f"🔖 Blok: *{test_data['block_name']}*\n"
        f"🔢 Savollar: *{q_count} ta*\n"
        f"📅 Yaratilgan: {test_data['created_at'][:10]}\n\n"
        f"🔗 *Blok havolasi:*\n`{link_block}`"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Testni boshlash", callback_data=f"ugc_start_{test_id}")],
        [InlineKeyboardButton(text="🗑 Blokni o'chirish", callback_data=f"delete_test_{test_id}")],
        [InlineKeyboardButton(text="🔙 Fanga qaytish", callback_data=f"manage_subj_{test_id}")],
    ])
    await _safe_edit(callback.message, text, reply_markup=kb)

@router.callback_query(F.data.startswith("delete_test_"))
async def delete_test_confirm(callback: CallbackQuery):
    """O'chirishdan oldin tasdiqlash."""
    await callback.answer()
    test_id = _parse_suffix(callback.data, "delete_test_")
    test_data = stats_manager.get_user_test(test_id)
    if not test_data:
        return await callback.answer("❌ Test topilmadi!", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Ha, o'chiraman", callback_data=f"confirm_delete_{test_id}")],
        [InlineKeyboardButton(text="❌ Yo'q, bekor qilish", callback_data=f"manage_test_{test_id}")],
    ])
    await _safe_edit(
        callback.message,
        f"⚠️ *Ishonchingiz komilmi?*\n\n"
        f"🔖 Blok: *{test_data['block_name']}*\n"
        f"🔢 {len(test_data.get('questions', []))} ta savol o'chib ketadi!\n\n"
        f"Bu amalni qaytarib bo'lmaydi.",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_handler(callback: CallbackQuery):
    await callback.answer()
    test_id = _parse_suffix(callback.data, "confirm_delete_")
    success = stats_manager.delete_user_test(test_id, callback.from_user.id)
    if success:
        await _safe_edit(
            callback.message,
            "✅ Blok muvaffaqiyatli o'chirildi.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📂 Mening Testlarim", callback_data="my_tests")],
                [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")],
            ])
        )
    else:
        await callback.answer("❌ O'chirishda xatolik yuz berdi.", show_alert=True)

async def show_ugc_subject_blocks(message: Message, creator_id: str, subject: str):
    tests = stats_manager.get_user_created_tests(creator_id)
    subj_tests = [t for t in tests if t["subject"] == subject]
    if not subj_tests:
        return await message.answer(
            "❌ Bu fanda bloklar topilmadi.",
            reply_markup=back_to_main_kb()
        )

    buttons = [
        [InlineKeyboardButton(
            text=f"📘 {t['block_name']}  •  {len(t.get('questions', []))} ta savol",
            callback_data=f"ugc_start_{t['id']}"
        )]
        for t in subj_tests
    ]
    buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")])
    await message.answer(
        f"📚 *Fan:* {subject}\n\n"
        f"📋 Jami {len(subj_tests)} ta blok mavjud.\n"
        f"Boshlash uchun blokni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )

async def start_ugc_test(message: Message, test_data_db: dict, bot: Bot):
    chat_id = message.chat.id
    if chat_id in active_tests or chat_id in waiting_rooms:
        return await message.answer(
            "⚠️ Bu chatda tugallanmagan test mavjud.\n"
            "Avval to'xtating: /stop"
        )

    session_q = prepare_shuffled_questions(test_data_db["questions"])
    active_tests[chat_id] = {
        "chat_type": "private",
        "initiator_id": message.from_user.id,
        "subject_key": test_data_db["subject"],
        "test_id": f"ugc_{test_data_db['id']}",
        "block_name": test_data_db.get("block_name", ""),
        "session_questions": session_q,
        "q_idx": 0,
        "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [],
        "consecutive_timeouts": 0, "group_scores": {},
    }
    await message.answer(
        f"🚀 *Test Boshlandi!*\n\n"
        f"📚 Fan: {test_data_db['subject']}\n"
        f"📝 Blok: {test_data_db.get('block_name', '')}\n"
        f"🔢 Jami savollar: {len(session_q)} ta\n"
        f"⏱ Har bir savolga 30 soniya vaqt beriladi\n\n"
        f"_Test davomida /stop yozsangiz test to'xtatiladi_",
        parse_mode="Markdown"
    )
    await send_next_question(chat_id, bot)

@router.callback_query(F.data.startswith("ugc_start_"))
async def restart_ugc_test(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await _safe_delete(callback.message)
    test_id = _parse_suffix(callback.data, "ugc_start_")
    test_data_db = stats_manager.get_user_test(test_id)
    if test_data_db:
        await start_ugc_test(callback.message, test_data_db, bot)
    else:
        await callback.message.answer(
            "❌ Test topilmadi yoki o'chirilgan.",
            reply_markup=back_to_main_kb()
        )

# ============================================================
# 4. RASMIY TESTLAR
# ============================================================

@router.callback_query(F.data == "official_tests")
async def show_official_tests(callback: CallbackQuery):
    await callback.answer()
    buttons = []
    for subj_key, subj_name in SUBJECTS.items():
        block_count = len(memory_db.get(subj_key, {}))
        q_count = sum(len(v.get("questions", [])) for v in memory_db.get(subj_key, {}).values())
        buttons.append([InlineKeyboardButton(
            text=f"📘 {subj_name}  •  {block_count} blok, {q_count} savol",
            callback_data=f"subj_{subj_key}"
        )])
    buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")])
    await _safe_edit(
        callback.message,
        "📚 *Rasmiy Testlar*\n\n"
        "Admin tomonidan tayyorlangan testlar.\n\n"
        "Fan tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith("subj_"))
async def process_subject_selection(callback: CallbackQuery):
    await callback.answer()
    subject_key = _parse_suffix(callback.data, "subj_")
    subj_name = SUBJECTS.get(subject_key, "Fan")
    await _safe_edit(
        callback.message,
        f"📚 *{subj_name}*\n\n"
        f"Blokdan birini tanlang yoki barcha savollar aralashtirilgan Mock Exam'ni yechib ko'ring:",
        reply_markup=get_blocks_keyboard(subject_key, 0)
    )

@router.callback_query(F.data.startswith("page_"))
async def process_page(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.rsplit("_", 1)
    page = int(parts[1])
    subject_key = _parse_suffix(parts[0], "page_")
    try:
        await callback.message.edit_reply_markup(reply_markup=get_blocks_keyboard(subject_key, page))
    except TelegramBadRequest:
        pass

# ============================================================
# 5. ADMIN PANELI
# ============================================================

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("⛔ Siz admin emassiz!")
    await show_admin_panel(message, is_callback=False)

@router.callback_query(F.data == "admin_panel_main")
async def cb_admin_panel_main(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    await callback.answer()
    await show_admin_panel(callback.message, is_callback=True)

async def show_admin_panel(message: Message, is_callback=False):
    users = stats_manager.get_all_users()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Barchaga xabar yuborish", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar ro'yxati", callback_data="admin_users_page_0")],
        [InlineKeyboardButton(text="➕ Rasmiy test qo'shish/tahrirlash", callback_data="admin_add_test")],
        [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")],
    ])
    text = (
        f"👨‍💻 *ADMIN PANEL*\n\n"
        f"👥 Jami foydalanuvchilar: *{len(users)} ta*\n\n"
        f"Kerakli bo'limni tanlang:"
    )
    if is_callback:
        await _safe_edit(message, text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data.startswith("admin_users_page_"))
async def admin_users_list_paginated(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    await callback.answer()

    page = int(_parse_suffix(callback.data, "admin_users_page_"))
    users = stats_manager.get_all_users()
    if not users:
        return await callback.message.answer("Hozircha foydalanuvchilar yo'q.")

    per_page = 15
    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page
    current_users = users[start_idx:start_idx + per_page]

    def _uname(u):
        un = u.get("username")
        return f" (@{un})" if un and un != "yo'q" else ""

    lines = [
        f"*{i}.* [{u.get('full_name') or 'Ismsiz'}](tg://user?id={u.get('telegram_id')}){_uname(u)}"
        for i, u in enumerate(current_users, start_idx + 1)
    ]
    text = f"👥 *Barcha foydalanuvchilar ({page + 1}/{total_pages}):*\n\n" + "\n".join(lines)

    buttons, nav = [], []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_users_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_users_page_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_panel_main")])

    await _safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    await callback.answer()
    await state.set_state(AdminStates.waiting_for_broadcast)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_cancel")]
    ])
    await callback.message.answer(
        "📢 *Ommaviy xabar yuborish*\n\n"
        "Barcha foydalanuvchilarga yuboriladigan xabar matnini yozing:\n\n"
        "_(Bekor qilish uchun quyidagi tugmani bosing)_",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@router.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    users = stats_manager.get_all_users()
    status_msg = await message.answer(f"⏳ {len(users)} ta foydalanuvchiga xabar yuborilmoqda...")

    async def _send(user):
        async with _BROADCAST_SEMAPHORE:
            try:
                await bot.send_message(chat_id=user["telegram_id"], text=message.text)
                return True
            except (TelegramForbiddenError, TelegramBadRequest):
                return False
            except Exception as e:
                logger.warning(f"Broadcast error for {user['telegram_id']}: {e}")
                return False

    results = await asyncio.gather(*[_send(u) for u in users])
    success = sum(results)
    await status_msg.edit_text(
        f"✅ *Ommaviy xabar yakunlandi!*\n\n"
        f"🟢 Yetib bordi: {success} ta\n"
        f"🔴 Bloklaganlar: {len(results) - success} ta",
        parse_mode="Markdown"
    )

# --- ADMIN RASMIY TEST QO'SHISH ---

def _save_official_test(subj: str, t_id: int, questions: list):
    success = stats_manager.save_official_test(subj, t_id, questions)
    if success:
        file_data = {"test_id": t_id, "range": f"1-{len(questions)}", "questions": questions}
        if subj not in memory_db:
            memory_db[subj] = {}
        memory_db[subj][t_id] = file_data
        invalidate_blocks_cache(subj)
        return file_data
    else:
        raise Exception("Supabase'ga saqlashda xatolik yuz berdi.")

def _admin_controls_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Matn", callback_data="adm_switch_text"),
            InlineKeyboardButton(text="📄 Word (.docx)", callback_data="adm_switch_docx")
        ],
        [InlineKeyboardButton(text="👁 Ko'rib chiqish", callback_data="adm_preview")],
        [InlineKeyboardButton(text="✅ Saqlash", callback_data="adm_finish")],
        [InlineKeyboardButton(text="🗑 Savollarni tozalash", callback_data="adm_reset")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_cancel")],
    ])

async def _admin_show_input_prompt(message: Message, fmt: str, total: int, edit: bool = False):
    fmt_label = "📝 Matn" if fmt == "text" else "📄 Word (.docx)"
    bar = _progress_bar(min(total, 30), 30)

    if fmt == "text":
        hint = (
            "📝 *Matn formatida savollar yuboring:*\n\n"
            "```\nSavol matni?\n#To'g'ri javob\nXato javob 1\nXato javob 2\n```\n\n"
            "_Savollar orasida bo'sh qator bo'lsin. Bir xabarda ko'p savol mumkin._"
        )
    else:
        hint = (
            "📄 *Word (.docx) fayl yuboring:*\n\n"
            "Fayl tuzilishi:\n"
            "```\nSavol matni\n#To'g'ri javob\nXato javob\nXato javob\n```\n\n"
            "_Savollar orasida bo'sh qator bo'lsin. Bir nechta fayl ketma-ket mumkin._"
        )

    text = (
        f"➕ *Rasmiy test qo'shish*\n\n"
        f"📌 Format: {fmt_label}\n"
        f"📊 Yig'ilgan: *{total} ta savol*\n"
        f"{bar}\n\n"
        f"{hint}\n\n"
        f"💡 Format o'zgartirish, ko'rish yoki saqlash uchun pastdagi tugmalar:"
    )
    if edit:
        await _safe_edit(message, text, reply_markup=_admin_controls_kb())
    else:
        await message.answer(text, reply_markup=_admin_controls_kb(), parse_mode="Markdown")

@router.callback_query(F.data == "admin_add_test")
async def admin_add_test_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.answer()
    buttons = [[InlineKeyboardButton(text=v, callback_data=f"adm_subj_{k}")] for k, v in SUBJECTS.items()]
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_cancel")])
    await _safe_edit(
        callback.message,
        "📂 *Rasmiy test qo'shish*\n\nQaysi fanga test qo'shmoqchi (yoki tahrirlamoqchi)siz?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await state.set_state(AdminCreateTest.waiting_for_subject)

@router.callback_query(F.data == "admin_cancel")
async def admin_cancel_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await show_admin_panel(callback.message, is_callback=True)

@router.callback_query(AdminCreateTest.waiting_for_subject, F.data.startswith("adm_subj_"))
async def admin_add_test_subj(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    subj = _parse_suffix(callback.data, "adm_subj_")
    await state.update_data(subject=subj)
    existing = memory_db.get(subj, {})
    existing_info = f"\n\n📋 Hozirda mavjud bloklar: {list(existing.keys())}" if existing else ""
    await _safe_edit(
        callback.message,
        f"✅ Fan: *{SUBJECTS.get(subj, subj)}*\n\n"
        f"🔢 Blok raqamini kiriting:\n"
        f"_(Masalan: 1, 2, 3... Agar bu raqam mavjud bo'lsa, u yangilanadi)_{existing_info}\n\n"
        f"⌨️ Raqam yozing:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_cancel")]
        ])
    )
    await state.set_state(AdminCreateTest.waiting_for_test_id)

@router.message(AdminCreateTest.waiting_for_test_id)
async def admin_add_test_id(message: Message, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("⚠️ Iltimos, faqat raqam kiriting (Masalan: 1, 2, 15)")
    await state.update_data(test_id=int(message.text.strip()))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Matn formatida", callback_data="adm_fmt_text")],
        [InlineKeyboardButton(text="📄 Word fayl (.docx)", callback_data="adm_fmt_docx")],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_cancel")],
    ])
    await message.answer(
        f"✅ Blok raqami: *{message.text.strip()}*\n\n"
        f"Savollarni qaysi formatda yuborasiz?",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(AdminCreateTest.waiting_for_format)

@router.callback_query(AdminCreateTest.waiting_for_format, F.data.startswith("adm_fmt_"))
async def admin_add_test_fmt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    fmt = _parse_suffix(callback.data, "adm_fmt_")
    await state.update_data(format=fmt, questions=[])
    await _admin_show_input_prompt(callback.message, fmt, total=0, edit=True)
    await state.set_state(AdminCreateTest.waiting_for_content)

@router.callback_query(AdminCreateTest.waiting_for_content, F.data.startswith("adm_switch_"))
async def admin_switch_format(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    fmt = _parse_suffix(callback.data, "adm_switch_")
    data = await state.get_data()
    await state.update_data(format=fmt)
    await _admin_show_input_prompt(callback.message, fmt, total=len(data.get("questions", [])), edit=True)

@router.callback_query(AdminCreateTest.waiting_for_content, F.data == "adm_preview")
async def admin_preview(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    questions = data.get("questions", [])
    if not questions:
        return await callback.answer("❌ Hali hech qanday savol yo'q!", show_alert=True)

    lines = [
        f"*{i}.* {q['question']}\n✅ {q['options'][q['correct_index']]}"
        for i, q in enumerate(questions, 1)
    ]
    text = f"👁 *Preview — {len(questions)} ta savol:*\n\n" + "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + f"\n\n_...va yana {len(questions) - 20} ta savol_"
    await callback.message.answer(text, parse_mode="Markdown")

@router.callback_query(AdminCreateTest.waiting_for_content, F.data == "adm_reset")
async def admin_reset(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(questions=[])
    await callback.answer("✅ Barcha savollar o'chirildi!", show_alert=True)
    await _admin_show_input_prompt(callback.message, data.get("format", "text"), total=0, edit=True)

@router.message(AdminCreateTest.waiting_for_content, F.text)
async def admin_receive_text(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("format") != "text":
        await message.answer(
            "⚠️ Hozir *Word* formati tanlangan.\n"
            "Matn yubormoqchi bo'lsangiz — *📝 Matn* tugmasini bosing.",
            parse_mode="Markdown"
        )
        await _admin_show_input_prompt(message, data.get("format", "docx"), total=len(data.get("questions", [])))
        return

    new_qs = _parse_text_questions(message.text)
    if not new_qs:
        return await message.answer(
            "⚠️ Savol topilmadi!\n\n"
            "Format:\n```\nSavol?\n#To'g'ri javob\nXato 1\nXato 2\n```\n"
            "To'g'ri javob oldiga `#` qo'yishni unutmang.",
            parse_mode="Markdown"
        )
    questions = data.get("questions", []) + new_qs
    await state.update_data(questions=questions)
    await _admin_show_input_prompt(message, "text", total=len(questions))

@router.message(AdminCreateTest.waiting_for_content, F.document)
async def admin_receive_docx(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if data.get("format") != "docx":
        await message.answer(
            "⚠️ Hozir *Matn* formati tanlangan.\n"
            "Word fayl yubormoqchi bo'lsangiz — *📄 Word* tugmasini bosing.",
            parse_mode="Markdown"
        )
        await _admin_show_input_prompt(message, data.get("format", "text"), total=len(data.get("questions", [])))
        return

    if not message.document.file_name.endswith(".docx"):
        return await message.answer("⚠️ Faqat `.docx` kengaytmali fayl qabul qilinadi.")

    msg = await message.answer("⏳ Fayl o'qilmoqda...")
    file_path = f"admin_temp_{message.from_user.id}_{int(time.time())}.docx"
    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, file_path)
        new_qs = _parse_docx_questions(file_path)
        if not new_qs:
            return await msg.edit_text(
                "❌ Fayldan savol topilmadi.\n\n"
                "Fayl tuzilishi:\n"
                "```\nSavol matni\n#To'g'ri javob\nXato javob\nXato javob\n```\n"
                "_Savollar orasida bo'sh qator bo'lsin._",
                parse_mode="Markdown"
            )
        questions = data.get("questions", []) + new_qs
        await state.update_data(questions=questions)
        await msg.delete()
        await _admin_show_input_prompt(message, "docx", total=len(questions))
    except Exception as e:
        logger.error(f"admin docx error: {e}")
        await msg.edit_text(f"❌ Xatolik: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

@router.callback_query(AdminCreateTest.waiting_for_content, F.data == "adm_finish")
async def admin_finish_creation(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    questions = data.get("questions", [])
    if not questions:
        return await callback.answer("⚠️ Hali hech qanday savol yo'q!", show_alert=True)

    subj = data["subject"]
    t_id = data["test_id"]
    try:
        _save_official_test(subj, t_id, questions)
        await _safe_edit(
            callback.message,
            f"✅ *Rasmiy test saqlandi!*\n\n"
            f"📚 Fan: *{SUBJECTS.get(subj, subj)}*\n"
            f"🔖 Blok ID: *{t_id}*\n"
            f"🔢 Savollar: *{len(questions)} ta*",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Yana blok qo'shish", callback_data="admin_add_test")],
                [InlineKeyboardButton(text="🔙 Admin panel", callback_data="admin_panel_main")],
            ])
        )
        await state.clear()
    except Exception as e:
        await callback.answer(f"❌ Xatolik: {e}", show_alert=True)

# --- ADMIN MULOQOT ---

@router.callback_query(F.data == "contact_admin")
async def cb_contact_admin(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(UserStates.waiting_for_message)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_contact")]
    ])
    await callback.message.answer(
        "💬 *Adminga Murojaat*\n\n"
        "Savol, taklif yoki muammongizni yozing.\n"
        "Admin iloji boricha tez javob beradi:\n\n"
        "⌨️ Xabaringizni yozing:",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "cancel_contact")
async def cancel_contact(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer(
        "❌ Murojaat bekor qilindi.",
        reply_markup=back_to_main_kb()
    )

@router.message(UserStates.waiting_for_message)
async def send_to_admin(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    text = (
        f"📨 *YANGI MUROJAAT!*\n\n"
        f"👤 [{message.from_user.full_name}](tg://user?id={message.from_user.id})\n"
        f"🆔 `{message.from_user.id}`\n"
        f"💬 Xabar:\n\n{message.text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Javob berish", callback_data=f"reply_{message.from_user.id}")]
    ])
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="Markdown")
        await message.answer(
            "✅ Xabaringiz adminga yuborildi!\n\n"
            "Tez orada javob beriladi.",
            reply_markup=back_to_main_kb()
        )
    except Exception:
        await message.answer(
            "❌ Xabar yuborishda xatolik yuz berdi. Qayta urinib ko'ring.",
            reply_markup=back_to_main_kb()
        )

@router.callback_query(F.data.startswith("reply_"))
async def admin_reply_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    await callback.answer()
    await state.update_data(target_id=_parse_suffix(callback.data, "reply_"))
    await state.set_state(AdminStates.waiting_for_reply)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_cancel")]
    ])
    await callback.message.answer("✍️ Foydalanuvchiga javobingizni yozing:", reply_markup=kb)

@router.message(AdminStates.waiting_for_reply)
async def admin_reply_send(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    try:
        await bot.send_message(
            data.get("target_id"),
            f"📩 *Admin javobi:*\n\n{message.text}",
            parse_mode="Markdown"
        )
        await message.answer("✅ Javob yuborildi.")
    except Exception:
        await message.answer("❌ Foydalanuvchiga xabar yuborib bo'lmadi.")

# ============================================================
# 6. STATISTIKA VA REYTING
# ============================================================

@router.callback_query(F.data == "show_leaderboard")
async def show_leaderboard_handler(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    now = time.time()

    # Keshdan foydalanish (qotish oldini olish)
    if _leaderboard_cache["text"] and now - _leaderboard_cache["ts"] < _LEADERBOARD_TTL:
        await _safe_edit(
            callback.message,
            _leaderboard_cache["text"],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]
            ])
        )
        return

    await _safe_edit(callback.message, "⏳ Reyting yuklanmoqda...")
    top_users = stats_manager.get_top_users(10)

    if not top_users:
        text = "🏆 *GLOBAL REYTING*\n\nHozircha reytingda hech kim yo'q.\n\nTest ishlang va birinchi o'ringa chiqing!"
    else:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        # Parallel ravishda ismlarni olish, timeout bilan
        names = await asyncio.gather(
            *[_get_user_name(bot, u["user_id"]) for u in top_users],
            return_exceptions=True
        )
        lines = []
        for i, (user, name) in enumerate(zip(top_users, names)):
            if isinstance(name, Exception):
                name = "Sirli Talaba"
            pct = round(user["correct"] / max(user["correct"] + user.get("wrong", 0), 1) * 100, 1)
            lines.append(
                f"{medals[i] if i < 10 else '🔸'} *{name}*\n"
                f"      ✅ {user['correct']} to'g'ri  •  🎯 {pct}%  •  📝 {user['completed']} test"
            )
        text = "🏆 *TOP 10 TALABALAR REYTINGI*\n\n" + "\n\n".join(lines)

    _leaderboard_cache["text"] = text
    _leaderboard_cache["ts"] = now

    await _safe_edit(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Asosiy Menyu", callback_data="back_to_main")]
        ])
    )

@router.callback_query(F.data == "show_stats")
async def show_stats_handler(callback: CallbackQuery):
    await callback.answer()
    if callback.message.chat.type != "private":
        return await callback.answer("📊 Statistika faqat shaxsiy chatda ko'rsatiladi!", show_alert=True)

    stats = stats_manager.get_user_stats(callback.from_user.id)
    rank = stats_manager.get_user_rank(callback.from_user.id)

    total = stats["total_correct"] + stats["total_wrong"]
    percent = (stats["total_correct"] / total * 100) if total > 0 else 0
    bar = _progress_bar(int(percent), 100, 20)

    history_count = len(stats.get("history", []))

    text = (
        f"📊 *Shaxsiy Statistika*\n\n"
        f"🏆 Umumiy reyting: *{rank}-o'rin*\n\n"
        f"✅ To'g'ri javoblar: *{stats['total_correct']} ta*\n"
        f"❌ Xato javoblar: *{stats['total_wrong']} ta*\n"
        f"📝 Ishlangan testlar: *{history_count} ta*\n\n"
        f"🎯 O'zlashtirish: *{percent:.1f}%*\n"
        f"{bar}"
    )

    buttons = []
    if stats.get("history"):
        buttons.append([InlineKeyboardButton(text="📜 Test tarixim", callback_data="hist_page_0")])
    buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")])

    await _safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("hist_page_"))
async def show_history_page(callback: CallbackQuery):
    await callback.answer()
    page = int(_parse_suffix(callback.data, "hist_page_"))
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if not history:
        return await _safe_edit(
            callback.message,
            "📜 Tarix bo'sh.\n\nHali hech qanday test ishlamadingiz.",
            reply_markup=back_to_main_kb()
        )

    total_pages = max(1, (len(history) + 4) // 5)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * 5

    buttons = []
    for i, item in enumerate(history[start_idx:start_idx + 5]):
        t_id = item["test_id"]
        if str(t_id) == "mock":
            label = "🎲 Aralash"
        elif str(t_id).startswith("ugc_"):
            label = "📝 Maxsus"
        else:
            label = f"{t_id}-Blok"
        subj_label = SUBJECTS.get(item["subject"], item["subject"])
        total_q = item["correct"] + item.get("wrong", 0)
        pct = round(item["correct"] / max(total_q, 1) * 100)
        abs_idx = start_idx + i
        buttons.append([InlineKeyboardButton(
            text=f"{item['date'][:10]} | {subj_label} {label} | {item['correct']}/{total_q} ({pct}%)",
            callback_data=f"hist_det_{abs_idx}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"hist_page_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"hist_page_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 Statistikaga", callback_data="show_stats")])

    await _safe_edit(
        callback.message,
        f"📜 *Test Tarixi* ({page + 1}/{total_pages})\n\nBatafsil ko'rish uchun testni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith("hist_det_"))
async def show_history_detail(callback: CallbackQuery):
    await callback.answer()
    idx = int(_parse_suffix(callback.data, "hist_det_"))
    history = stats_manager.get_user_stats(callback.from_user.id).get("history", [])
    if idx >= len(history):
        return await callback.answer("❌ Ma'lumot topilmadi!", show_alert=True)

    item = history[idx]
    t_id = str(item["test_id"])
    if t_id == "mock":
        t_label = "🎲 Aralash Test"
    elif t_id.startswith("ugc_"):
        t_label = "📝 Maxsus Test"
    else:
        t_label = f"{t_id}-Blok"

    total_q = item["correct"] + item.get("wrong", 0)
    pct = round(item["correct"] / max(total_q, 1) * 100, 1)
    bar = _progress_bar(int(pct), 100)

    parts = [
        f"📅 *Sana:* {item['date'][:10]}\n"
        f"📚 *Fan:* {SUBJECTS.get(item['subject'], item['subject'])}\n"
        f"📝 *Test:* {t_label}\n\n"
        f"✅ To'g'ri: *{item['correct']}*  ❌ Xato: *{item.get('wrong', 0)}*\n"
        f"🎯 Natija: *{pct}%*\n{bar}\n"
    ]

    mistakes = item.get("mistakes", [])
    if not mistakes:
        parts.append("\n🎉 *Ajoyib! Xato qilmadingiz!*")
    else:
        parts.append(f"\n📑 *Xatolar ({len(mistakes)} ta):*\n")
        for i, m in enumerate(mistakes[:15], 1):
            parts.append(f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}")
        if len(mistakes) > 15:
            parts.append(f"\n_...va yana {len(mistakes) - 15} ta xato_")

    text = "\n".join(parts)
    if len(text) > 4000:
        text = text[:3900] + "\n\n_(Matn kesildi)_"

    # Qaysi sahifaga qaytish — indeksdan hisoblaymiz
    back_page = idx // 5
    await _safe_edit(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Tarixga qaytish", callback_data=f"hist_page_{back_page}")]
        ])
    )

# ============================================================
# 7. TEST O'YINI
# ============================================================

@router.callback_query(F.data.startswith("start_test_") | F.data.startswith("mock_"))
async def start_test_handler(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    chat_id = callback.message.chat.id

    if chat_id in active_tests or chat_id in waiting_rooms:
        return await callback.answer(
            "⚠️ Bu chatda faol test mavjud!\n"
            "Avval to'xtating: /stop",
            show_alert=True
        )

    is_mock = callback.data.startswith("mock_")
    if is_mock:
        subject_key = _parse_suffix(callback.data, "mock_")
        all_q = [q for test in memory_db.get(subject_key, {}).values() for q in test["questions"]]
        if not all_q:
            return await callback.answer("❌ Bu fanda savollar yo'q!", show_alert=True)
        test_data = {"questions": random.sample(all_q, min(25, len(all_q))), "block_name": "Aralash Test"}
        test_id = "mock"
    else:
        parts = _parse_suffix(callback.data, "start_test_").rsplit("_", 1)
        subject_key, test_id = parts[0], int(parts[1])
        test_data = memory_db.get(subject_key, {}).get(test_id)
        if not test_data:
            return await callback.answer("❌ Test topilmadi!", show_alert=True)

    chat_type = callback.message.chat.type
    if chat_type != "private":
        waiting_rooms[chat_id] = {
            "subject_key": subject_key,
            "test_id": test_id,
            "test_data": test_data,
            "ready_users": set(),
            "initiator_id": callback.from_user.id
        }
        await _safe_delete(callback.message)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Tayyorman! (0)", callback_data="room_ready")],
            [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")],
        ])
        t_label = "Aralash" if test_id == "mock" else f"{test_id}-Blok"
        await bot.send_message(
            chat_id,
            f"👥 *Guruh Rejimi!*\n\n"
            f"📚 {SUBJECTS.get(subject_key, 'Fan')} | {t_label}\n"
            f"🔢 Savollar: {len(test_data['questions'])} ta\n\n"
            f"⬇️ Kamida 2 kishi tayyor bo'lgach boshlanadi.\n"
            f"Tayyor bo'lsangiz — quyidagi tugmani bosing:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return

    session_q = prepare_shuffled_questions(test_data["questions"])
    active_tests[chat_id] = {
        "chat_type": "private",
        "initiator_id": callback.from_user.id,
        "subject_key": subject_key,
        "test_id": test_id,
        "block_name": test_data.get("block_name", ""),
        "session_questions": session_q,
        "q_idx": 0,
        "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [],
        "consecutive_timeouts": 0, "group_scores": {},
    }
    await _safe_delete(callback.message)
    t_label = "Aralash Test" if test_id == "mock" else f"{test_id}-Blok"
    await bot.send_message(
        chat_id,
        f"🚀 *Test Boshlandi!*\n\n"
        f"📚 Fan: {SUBJECTS.get(subject_key, subject_key)}\n"
        f"📝 Blok: {t_label}\n"
        f"🔢 Jami savollar: {len(session_q)} ta\n"
        f"⏱ Har bir savolga: 30 soniya\n\n"
        f"_/stop — testni to'xtatish_",
        parse_mode="Markdown"
    )
    await send_next_question(chat_id, bot)

# --- GURUH KUTISH ZALI ---

@router.callback_query(F.data == "room_ready")
async def room_ready_handler(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms:
        return await callback.answer("Kutish zali yopilgan!", show_alert=True)
    room = waiting_rooms[chat_id]
    if callback.from_user.id in room["ready_users"]:
        return await callback.answer("✅ Siz allaqachon tayyorsiz!", show_alert=True)

    room["ready_users"].add(callback.from_user.id)
    count = len(room["ready_users"])
    buttons = [[InlineKeyboardButton(text=f"✅ Tayyorman ({count})", callback_data="room_ready")]]
    if count >= 2:
        buttons.append([InlineKeyboardButton(text="🚀 Testni Boshlash!", callback_data="room_start")])
    buttons.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="room_cancel")])
    try:
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except TelegramBadRequest:
        pass
    await callback.answer(f"✅ Tayyor! Jami tayyor: {count} kishi")

@router.callback_query(F.data == "room_start")
async def room_start_handler(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms:
        return
    room = waiting_rooms[chat_id]
    if len(room["ready_users"]) < 2:
        return await callback.answer("⚠️ Kamida 2 kishi tayyor bo'lishi kerak!", show_alert=True)

    session_q = prepare_shuffled_questions(room["test_data"]["questions"])
    active_tests[chat_id] = {
        "chat_type": "group",
        "initiator_id": room["initiator_id"],
        "subject_key": room["subject_key"],
        "test_id": room["test_id"],
        "block_name": room["test_data"].get("block_name", ""),
        "session_questions": session_q,
        "q_idx": 0,
        "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [],
        "consecutive_timeouts": 0, "group_scores": {},
    }
    del waiting_rooms[chat_id]
    await _safe_delete(callback.message)
    await bot.send_message(
        chat_id,
        f"🚀 *Test Boshlandi!*\n\n"
        f"👥 {len(room['ready_users'])} kishi qatnashmoqda\n"
        f"🔢 Jami savollar: {len(session_q)} ta\n\n"
        f"_/stop — testni to'xtatish_",
        parse_mode="Markdown"
    )
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "room_cancel")
async def room_cancel_handler(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in waiting_rooms:
        return await callback.answer("Kutish zali allaqachon yopilgan.", show_alert=True)
    if callback.from_user.id != waiting_rooms[chat_id]["initiator_id"]:
        return await callback.answer("⚠️ Faqat testni boshlagan kishi bekor qila oladi!", show_alert=True)
    del waiting_rooms[chat_id]
    await _safe_delete(callback.message)
    await callback.answer("Test bekor qilindi.", show_alert=True)
    await callback.message.answer("❌ Test bekor qilindi.")

# --- SAVOL YUBORISH VA TIMEOUT ---

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
        session["mistakes"].append({
            "question": q_data["question"],
            "correct_ans": q_data["correct_text"],
            "wrong_ans": "⏳ Vaqt tugadi"
        })
        if session["consecutive_timeouts"] >= 2 and session["q_idx"] < len(session["session_questions"]):
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Davom etaman", callback_data="resume_test")],
                [InlineKeyboardButton(text="🏁 Testni yakunlash", callback_data="force_finish")],
            ])
            await bot.send_message(
                chat_id,
                "⏸ *Test to'xtatildi!*\n\n"
                "Ketma-ket 2 ta savolga javob bermadingiz.\n\n"
                "Davom etmoqchimisiz?",
                reply_markup=kb,
                parse_mode="Markdown"
            )
        else:
            await send_next_question(chat_id, bot)
    else:
        await send_next_question(chat_id, bot)

@router.callback_query(F.data == "resume_test")
async def resume_test_handler(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    chat_id = callback.message.chat.id
    if chat_id not in active_tests:
        return await callback.answer("❌ Test topilmadi.", show_alert=True)
    active_tests[chat_id]["consecutive_timeouts"] = 0
    await _safe_delete(callback.message)
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "force_finish")
async def force_finish_handler(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    await _safe_delete(callback.message)
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
    progress = f"[{q_idx + 1}/{len(questions)}]"
    q_text_full = f"{progress} {q['question']}"

    needs_text = len(q_text_full) > 255 or any(len(opt) > 100 for opt in q["options"])

    if needs_text:
        labels = ["A", "B", "C", "D", "E", "F"]
        text_msg = (
            f"📑 *Savol {progress}*\n\n"
            f"{q['question']}\n\n"
            + "\n".join(f"*{labels[i]})* {opt}" for i, opt in enumerate(q["options"]))
        )
        if len(text_msg) > 4000:
            text_msg = text_msg[:3900] + "\n_(Matn kesildi)_"
        await bot.send_message(chat_id, text_msg, parse_mode="Markdown")
        poll_q = f"{progress} To'g'ri variantni belgilang:"
        poll_opts = [f"{labels[i]} varianti" for i in range(len(q["options"]))]
    else:
        poll_q, poll_opts = q_text_full, q["options"]

    try:
        msg = await bot.send_poll(
            chat_id=chat_id,
            question=poll_q,
            options=poll_opts,
            type="quiz",
            correct_option_id=q["correct_index"],
            is_anonymous=False,
            open_period=30,
        )
    except Exception as e:
        logger.error(f"send_poll error for chat {chat_id}: {e}")
        return

    session["poll_id"] = msg.poll.id
    session["msg_id"] = msg.message_id
    poll_chat_map[msg.poll.id] = chat_id

    if session.get("timer_task"):
        session["timer_task"].cancel()
    session["timer_task"] = asyncio.create_task(
        question_timeout_task(chat_id, q_idx, msg.poll.id, bot)
    )

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

        # Kesh uchun ismni saqlash
        _user_name_cache[poll_answer.user.id] = poll_answer.user.full_name or "Foydalanuvchi"

        await send_next_question(chat_id, bot)
    else:
        lock = _get_group_lock(chat_id)
        async with lock:
            u_id = poll_answer.user.id
            _user_name_cache[u_id] = poll_answer.user.full_name or "Foydalanuvchi"
            if u_id not in session["group_scores"]:
                session["group_scores"][u_id] = {
                    "name": poll_answer.user.full_name or "Foydalanuvchi",
                    "correct": 0, "wrong": 0, "mistakes": []
                }
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

# ============================================================
# 8. TEST YAKUNLASH
# ============================================================

async def finish_test(chat_id: int, bot: Bot):
    session = active_tests.get(chat_id)
    if not session:
        return
    if session.get("timer_task"):
        session["timer_task"].cancel()

    t_id = session["test_id"]
    if str(t_id) == "mock":
        t_name = "🎲 Aralash Test"
    elif str(t_id).startswith("ugc_"):
        t_name = f"📝 {session.get('block_name', 'Maxsus Test')}"
    else:
        t_name = f"{t_id}-Blok"

    mins, secs = divmod(int(time.time() - session["start_time"]), 60)
    subj_name = SUBJECTS.get(session["subject_key"], session["subject_key"])
    buttons = []

    if session["chat_type"] == "private":
        stats_manager.update_user_stats(
            chat_id,
            session["correct"],
            session["wrong"],
            session["subject_key"],
            session["test_id"],
            session["mistakes"]
        )
        total_q = session["correct"] + session["wrong"]
        answered = total_q
        skipped = len(session["session_questions"]) - total_q
        percent = round(session["correct"] / total_q * 100, 1) if total_q > 0 else 0
        bar = _progress_bar(int(percent), 100)

        # Baho
        if percent >= 90:
            grade = "🏆 A'lo!"
        elif percent >= 75:
            grade = "👍 Yaxshi!"
        elif percent >= 60:
            grade = "📈 Qoniqarli"
        else:
            grade = "📚 Mashq kerak"

        text = (
            f"🏁 *Test Yakunlandi!*\n\n"
            f"📚 {subj_name} | {t_name}\n\n"
            f"✅ To'g'ri: *{session['correct']} ta*\n"
            f"❌ Xato: *{session['wrong']} ta*\n"
            f"⏭ O'tkazildi: *{skipped} ta*\n\n"
            f"🎯 Natija: *{percent}%* — {grade}\n"
            f"{bar}\n\n"
            f"⏱ Sarflangan vaqt: *{mins:02d}:{secs:02d}*"
        )

        if session.get("mistakes"):
            buttons.append([InlineKeyboardButton(text="❌ Xatolarni ko'rish", callback_data="review_mistakes")])

        if str(t_id).startswith("ugc_"):
            buttons.append([InlineKeyboardButton(
                text="🔁 Qayta ishlash",
                callback_data=f"ugc_start_{str(t_id).replace('ugc_', '')}"
            )])
            buttons.append([InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")])
        elif t_id == "mock":
            buttons.append([InlineKeyboardButton(
                text="🎲 Yana aralash test",
                callback_data=f"mock_{session['subject_key']}"
            )])
            buttons.extend([
                [InlineKeyboardButton(text="🔙 Fan menyusiga", callback_data=f"post_subj_{session['subject_key']}")],
                [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")],
            ])
        else:
            buttons.append([InlineKeyboardButton(
                text="🔁 Qayta ishlash",
                callback_data=f"post_start_{session['subject_key']}_{t_id}"
            )])
            if t_id + 1 in memory_db.get(session["subject_key"], {}):
                buttons.append([InlineKeyboardButton(
                    text=f"➡️ Keyingi blok ({t_id + 1}-Blok)",
                    callback_data=f"post_start_{session['subject_key']}_{t_id + 1}"
                )])
            buttons.extend([
                [InlineKeyboardButton(text="🔙 Fan menyusiga", callback_data=f"post_subj_{session['subject_key']}")],
                [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")],
            ])
    else:
        # Guruh rejimi
        await asyncio.gather(*[
            asyncio.to_thread(
                stats_manager.update_user_stats,
                u_id, scores["correct"], scores["wrong"],
                session["subject_key"], session["test_id"], scores["mistakes"]
            )
            for u_id, scores in session["group_scores"].items()
        ])

        if not session["group_scores"]:
            body = "😔 Hech kim javob bermadi."
        else:
            medals = ["🥇", "🥈", "🥉"]
            sorted_scores = sorted(session["group_scores"].values(), key=lambda x: x["correct"], reverse=True)
            body = "\n".join(
                f"{medals[i] if i < 3 else '🔸'} *{s['name']}*: {s['correct']} to'g'ri, {s['wrong']} xato"
                for i, s in enumerate(sorted_scores)
            )
        text = (
            f"🏁 *Test Yakunlandi!*\n\n"
            f"📚 {subj_name} | {t_name}\n"
            f"⏱ Vaqt: *{mins:02d}:{secs:02d}*\n\n"
            f"🏆 *NATIJALAR:*\n{body}"
        )
        buttons.extend([
            [InlineKeyboardButton(text="🔙 Fan menyusiga", callback_data=f"post_subj_{session['subject_key']}")],
            [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="post_main")],
        ])

    try:
        await bot.send_message(
            chat_id,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"finish_test send error: {e}")

    poll_chat_map.pop(session.get("poll_id"), None)
    _group_answer_locks.pop(chat_id, None)
    del active_tests[chat_id]

# --- POST-TEST NAVIGATSIYA ---

@router.callback_query(F.data == "post_main")
async def post_main_handler(callback: CallbackQuery):
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "🏛 *Asosiy Menyu*\n\nKerakli bo'limni tanlang:",
        reply_markup=get_main_keyboard(),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("post_subj_"))
async def post_subj_handler(callback: CallbackQuery):
    await callback.answer()
    subject_key = _parse_suffix(callback.data, "post_subj_")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        f"📚 *{SUBJECTS.get(subject_key, 'Fan')}*\n\n"
        f"Bloklardan birini tanlang:",
        reply_markup=get_blocks_keyboard(subject_key, 0),
        parse_mode="Markdown"
    )

@router.callback_query(F.data.startswith("post_start_"))
async def post_start_handler(callback: CallbackQuery, bot: Bot):
    await callback.answer()
    suffix = _parse_suffix(callback.data, "post_start_")
    parts = suffix.rsplit("_", 1)
    subject_key, test_id = parts[0], int(parts[1])
    test_data = memory_db.get(subject_key, {}).get(test_id)
    if not test_data:
        return await callback.answer("❌ Test topilmadi!", show_alert=True)

    chat_id = callback.message.chat.id
    if chat_id in active_tests or chat_id in waiting_rooms:
        return await callback.answer("⚠️ Avval joriy testni to'xtating: /stop", show_alert=True)

    session_q = prepare_shuffled_questions(test_data["questions"])
    active_tests[chat_id] = {
        "chat_type": "private",
        "initiator_id": callback.from_user.id,
        "subject_key": subject_key,
        "test_id": test_id,
        "block_name": test_data.get("block_name", f"{test_id}-Blok"),
        "session_questions": session_q,
        "q_idx": 0,
        "start_time": time.time(),
        "poll_id": None, "msg_id": None, "timer_task": None,
        "correct": 0, "wrong": 0, "mistakes": [],
        "consecutive_timeouts": 0, "group_scores": {},
    }
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await bot.send_message(
        chat_id,
        f"🚀 *{test_id}-Blok boshlandi!*\n\n"
        f"🔢 Jami savollar: {len(session_q)} ta\n"
        f"⏱ Har bir savolga: 30 soniya",
        parse_mode="Markdown"
    )
    await send_next_question(chat_id, bot)

@router.callback_query(F.data == "review_mistakes")
async def review_mistakes_handler(callback: CallbackQuery):
    await callback.answer()
    stats = stats_manager.get_user_stats(callback.from_user.id)
    history = stats.get("history", [])
    if not history:
        return await callback.answer("❌ Xatolar topilmadi!", show_alert=True)

    mistakes = history[0].get("mistakes", [])
    if not mistakes:
        return await callback.answer("🎉 Bu testda xato yo'q edi!", show_alert=True)

    parts = [f"📑 *So'nggi testdagi xatolar ({len(mistakes)} ta):*\n"]
    for i, m in enumerate(mistakes[:20], 1):
        parts.append(f"*{i}.* {m['question']}\n❌ {m['wrong_ans']}\n✅ {m['correct_ans']}")
    if len(mistakes) > 20:
        parts.append(f"\n_...va yana {len(mistakes) - 20} ta xato_")

    text = "\n\n".join(parts)
    if len(text) > 4000:
        text = text[:3900] + "\n\n_(Matn kesildi)_"

    await _safe_edit(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Asosiy Menyu", callback_data="back_to_main")]
        ])
    )

# --- YORDAMCHI HANDLERLAR ---

@router.callback_query(F.data == "ignore")
async def ignore_handler(callback: CallbackQuery):
    await callback.answer()
