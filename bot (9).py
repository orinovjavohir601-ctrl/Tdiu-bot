import asyncio
import contextvars
import csv
import inspect
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram import methods as tg
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


# =========================================================
# SOZLAMALAR (Railway -> Variables bo'limidan olinadi)
# =========================================================
#   BOT_TOKEN  = BotFather bergan token
#   ADMIN_IDS  = 636287377,5640388317,6750843006
#   DB_PATH    = (ixtiyoriy) baza fayli yo'li, masalan /data/universitet.db
#   TEZLIK     = (ixtiyoriy) soniyasiga nechta xabar, standart 28
#
# Railway Volume ulangan bo'lsa, baza avtomatik shu Volume ichiga yoziladi.

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi (Variables yoki .env).")

if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS topilmadi (Variables yoki .env).")


def db_yoli():
    yol = os.getenv("DB_PATH", "").strip()
    if yol:
        return yol

    mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if mount:
        return os.path.join(mount, "universitet.db")

    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
        logging.warning(
            "DIQQAT: Railway'da Volume ulanmagan! Baza vaqtincha papkaga "
            "yoziladi va qayta deploydan keyin O'CHIB KETADI. "
            "Volume qo'shing (Mount path: /data)."
        )

    return "universitet.db"


DB_PATH = db_yoli()

# Serverda qaysi versiya ishlayotganini bilish uchun (/admin panelida ko'rinadi)
VERSIYA = "2026-11-20.2"

MAX_MATN = 2500          # murojaat va javob uchun maksimal uzunlik
KUNLIK_LIMIT = 5         # bir talaba kuniga nechta murojaat yubora oladi
TZ = timezone(timedelta(hours=5))   # O'zbekiston vaqti (UTC+5)

# Yuklama sozlamalari
UMUMIY_TEZLIK = int(os.getenv("TEZLIK", "28"))   # jami xabar/soniya (Telegram ~30)
ADMIN_ORALIQ = 1.0           # bitta admin chatiga kartalar orasidagi pauza (soniya)
MAX_PARALLEL = 300           # bir vaqtda ishlanadigan update'lar soni
MAX_KUTISH = 60              # Telegram "kuting" desa, eng ko'pi necha soniya kutamiz
ZAXIRA_MAX_HAJM = 49 * 1024 * 1024   # Telegram bot fayl chegarasi ~50 MB


# =========================================================
# MA'LUMOTLAR
# =========================================================

# Fakultet -> kurs -> guruhlar ro'yxati guruhlar.json faylidan olinadi.
# Yo'nalish guruh kodining boshidan aniqlanadi: "MNP-81/25" -> "MNP".
# Yangi o'quv yilida faqat guruhlar.json ni almashtirish kifoya.

GURUHLAR_FAYL = os.getenv(
    "GURUHLAR_FAYL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "guruhlar.json")
)


def guruhlar_bazasi():
    try:
        with open(GURUHLAR_FAYL, encoding="utf-8") as f:
            baza = json.load(f)
        jami = sum(len(g) for k in baza.values() for g in k.values())
        logging.info("Guruhlar bazasi: %s bo'lim, %s guruh", len(baza), jami)
        return baza
    except FileNotFoundError:
        logging.warning("guruhlar.json topilmadi — guruhlar qo'lda yoziladi.")
    except Exception:
        logging.exception("guruhlar.json o'qilmadi — guruhlar qo'lda yoziladi.")
    return {}


BAZA = guruhlar_bazasi()

# guruhlar.json bo'lmasa ishlatiladigan zaxira ro'yxat
ZAXIRA_FAKULTETLAR = [
    "Raqamli iqtisodiyot", "Bank ishi", "Moliya", "Buxgalteriya hisobi",
    "Iqtisodiyot", "Menejment", "Turizm", "Soliq",
]

FAKULTETLAR = list(BAZA) if BAZA else ZAXIRA_FAKULTETLAR
COURSES = ["1-kurs", "2-kurs", "3-kurs", "4-kurs", "5-kurs", "Magistratura"]

# Yo'nalish kodlarining to'liq nomlari (ixtiyoriy). Bo'sh bo'lsa, kod ko'rinadi.
# Misol: YONALISH_NOMLARI = {"MNP": "Menejment", "BBA": "Biznes boshqaruvi"}
YONALISH_NOMLARI = {}


def yonalish_kodi(guruh):
    """'MNP-81/25' -> 'MNP', 'MBA25R' -> 'MBA', '5/EK-75/23' -> '5/EK'"""
    bosh = guruh.split("-")[0].strip()
    if any(c.isdigit() for c in bosh):
        harflar = ""
        for c in bosh:
            if c.isalpha() or c == "'":
                harflar += c
            else:
                break
        return harflar or bosh
    return bosh


def yonalish_nomi(kod):
    nom = YONALISH_NOMLARI.get(kod)
    return f"{kod} — {nom}" if nom else kod


def kurslar(fakultet):
    if fakultet in BAZA:
        return list(BAZA[fakultet])
    return COURSES


def yonalishlar(fakultet, kurs):
    """{kod: [guruhlar]} — saytdagi tartibda."""
    natija = {}
    for g in BAZA.get(fakultet, {}).get(kurs, []):
        natija.setdefault(yonalish_kodi(g), []).append(g)
    return natija


def yonalish_bosqichi_kerakmi(fakultet, kurs):
    """Yo'nalish faqat foydali bo'lsa so'raladi. Masalan magistraturada har
    yo'nalishda 1-2 guruh — u yerda to'g'ridan-to'g'ri guruh tanlanadi."""
    y = yonalishlar(fakultet, kurs)
    jami = sum(len(g) for g in y.values())
    if len(y) < 2:
        return False
    if jami > 95:
        return True
    return len(y) <= 40 and jami / len(y) >= 2


def guruh_variantlari(fakultet, kurs, yonalish=None):
    if yonalish:
        return yonalishlar(fakultet, kurs).get(yonalish, [])
    return BAZA.get(fakultet, {}).get(kurs, [])


TOIFALAR = [
    "📚 O‘qish va dars jadvali",
    "💳 Kontrakt / to‘lov",
    "🏠 Yotoqxona",
    "💰 Stipendiya",
    "📄 Hujjat va ma’lumotnoma",
    "💡 Taklif",
    "❓ Boshqa",
]

YANGI = "Yangi"
KORILMOQDA = "Ko‘rilmoqda"
JAVOB_BERILDI = "Javob berildi"
YOPILGAN = "Yopilgan"

HOLAT_BELGI = {
    YANGI: "🆕",
    KORILMOQDA: "👀",
    JAVOB_BERILDI: "✅",
    YOPILGAN: "🔒",
}



# =========================================================
# TILLAR (o'zbek / rus / ingliz)
# Talabaga ko'rinadigan barcha matnlar shu yerda.
# Admin paneli o'zbek tilida qoladi.
# =========================================================

TILLAR = {"uz": "🇺🇿 O‘zbekcha", "ru": "🇷🇺 Русский", "en": "🇬🇧 English"}
TIL_NOMI = {"uz": "O‘zbekcha", "ru": "Русский", "en": "English"}

MATN = {
    "til_tanlash": {
        "uz": "🌐 Tilni tanlang\n🌐 Выберите язык\n🌐 Choose your language",
    },
    "til_ozgardi": {
        "uz": "✅ Til o‘zgartirildi: O‘zbekcha",
        "ru": "✅ Язык изменён: Русский",
        "en": "✅ Language changed: English",
    },
    "welcome": {
        "uz": (
            "<b>Assalomu alaykum!</b>\n\n"
            "Toshkent davlat iqtisodiyot universiteti talabalari va xodimlari uchun "
            "murojaatlar botiga xush kelibsiz!\n\n"
            "Ushbu bot orqali siz:\n\n"
            "• Universitetga oid savol yuborishingiz;\n"
            "• Taklif va muammolaringizni bildirishingiz;\n"
            "• Murojaatlaringiz holatini kuzatishingiz;\n"
            "• Administrator javobini olishingiz mumkin.\n\n"
            "Botdan foydalanish uchun ro‘yxatdan o‘ting.\n\n"
            "<b>Eslatma:</b> murojaatingizni yozib, tasdiqlamaguningizcha "
            "u administratorlarga yuborilmaydi."
        ),
        "ru": (
            "<b>Здравствуйте!</b>\n\n"
            "Добро пожаловать в бот обращений студентов и сотрудников Ташкентского "
            "государственного экономического университета!\n\n"
            "С помощью бота вы можете:\n\n"
            "• задать вопрос, касающийся университета;\n"
            "• сообщить о предложении или проблеме;\n"
            "• отслеживать статус своих обращений;\n"
            "• получить ответ администратора.\n\n"
            "Чтобы начать, пройдите регистрацию.\n\n"
            "<b>Примечание:</b> обращение не будет отправлено "
            "администраторам, пока вы его не подтвердите."
        ),
        "en": (
            "<b>Hello!</b>\n\n"
            "Welcome to the request bot for students and staff of Tashkent State "
            "University of Economics!\n\n"
            "With this bot you can:\n\n"
            "• ask questions about the university;\n"
            "• share suggestions and report problems;\n"
            "• track the status of your requests;\n"
            "• receive answers from the administration.\n\n"
            "Please register to get started.\n\n"
            "<b>Note:</b> your request is not sent to the administrators "
            "until you confirm it."
        ),
    },
    "tel_yuboring": {
        "uz": "Telefon raqamingizni yuboring:",
        "ru": "Отправьте свой номер телефона:",
        "en": "Please share your phone number:",
    },
    "btn_tel": {
        "uz": "📱 Telefon raqamni yuborish",
        "ru": "📱 Отправить номер телефона",
        "en": "📱 Share phone number",
    },
    "salom": {
        "uz": "Assalomu alaykum, {fish}!\n\nBotga xush kelibsiz.",
        "ru": "Здравствуйте, {fish}!\n\nДобро пожаловать.",
        "en": "Hello, {fish}!\n\nWelcome back.",
    },
    "bekor_yoq": {
        "uz": "Bekor qilinadigan faol amal yo‘q.",
        "ru": "Нет активного действия для отмены.",
        "en": "There is nothing to cancel.",
    },
    "bekor_ok": {
        "uz": "Amal bekor qilindi.",
        "ru": "Действие отменено.",
        "en": "Cancelled.",
    },
    "bekor_ok_start": {
        "uz": "Amal bekor qilindi. Qayta boshlash uchun /start yuboring.",
        "ru": "Действие отменено. Чтобы начать заново, отправьте /start.",
        "en": "Cancelled. Send /start to begin again.",
    },
    "help": {
        "uz": (
            "📌 <b>BOT BUYRUQLARI</b>\n\n"
            "/start — botni boshlash\n"
            "/help — yordam\n"
            "/menu — asosiy menyu\n"
            "/new — yangi murojaat\n"
            "/myrequests — murojaatlarim\n"
            "/profile — profilim\n"
            "/language — tilni o‘zgartirish\n"
            "/cancel — joriy amalni bekor qilish"
        ),
        "ru": (
            "📌 <b>КОМАНДЫ БОТА</b>\n\n"
            "/start — запустить бота\n"
            "/help — помощь\n"
            "/menu — главное меню\n"
            "/new — новое обращение\n"
            "/myrequests — мои обращения\n"
            "/profile — мой профиль\n"
            "/language — сменить язык\n"
            "/cancel — отменить текущее действие"
        ),
        "en": (
            "📌 <b>BOT COMMANDS</b>\n\n"
            "/start — start the bot\n"
            "/help — help\n"
            "/menu — main menu\n"
            "/new — new request\n"
            "/myrequests — my requests\n"
            "/profile — my profile\n"
            "/language — change language\n"
            "/cancel — cancel the current action"
        ),
    },
    "avval_royxat": {
        "uz": "Avval /start orqali ro‘yxatdan o‘ting.",
        "ru": "Сначала зарегистрируйтесь через /start.",
        "en": "Please register first via /start.",
    },
    "menyu": {
        "uz": "Asosiy menyu:",
        "ru": "Главное меню:",
        "en": "Main menu:",
    },
    "profil": {
        "uz": (
            "👤 <b>Sizning ma'lumotlaringiz</b>\n\n"
            "Ism-familiya: {fish}\nTelefon: {tel}\nFakultet: {fak}\n"
            "Kurs: {kurs}\nYo‘nalish: {yon}\nGuruh: {guruh}\nTil: {til}"
        ),
        "ru": (
            "👤 <b>Ваши данные</b>\n\n"
            "ФИО: {fish}\nТелефон: {tel}\nФакультет: {fak}\n"
            "Курс: {kurs}\nНаправление: {yon}\nГруппа: {guruh}\nЯзык: {til}"
        ),
        "en": (
            "👤 <b>Your details</b>\n\n"
            "Full name: {fish}\nPhone: {tel}\nFaculty: {fak}\n"
            "Course: {kurs}\nProgramme: {yon}\nGroup: {guruh}\nLanguage: {til}"
        ),
    },
    "btn_tahrir": {
        "uz": "✏️ Ma’lumotlarni tahrirlash",
        "ru": "✏️ Изменить данные",
        "en": "✏️ Edit details",
    },
    "btn_til": {
        "uz": "🌐 Til / Язык / Language",
    },
    "yangi_ism": {
        "uz": "Yangi ism va familiyangizni kiriting:\n(Bekor qilish: /cancel)",
        "ru": "Введите новые фамилию и имя:\n(Отмена: /cancel)",
        "en": "Enter your new full name:\n(Cancel: /cancel)",
    },
    "limit": {
        "uz": "Bugun {n} ta murojaat yuborib bo‘ldingiz. Iltimos, ertaga qayta urinib ko‘ring.",
        "ru": "Сегодня вы уже отправили {n} обращений. Пожалуйста, попробуйте завтра.",
        "en": "You have already sent {n} requests today. Please try again tomorrow.",
    },
    "mavzu": {
        "uz": "🏷 <b>Murojaat mavzusini tanlang:</b>\n\nBekor qilish: /cancel",
        "ru": "🏷 <b>Выберите тему обращения:</b>\n\nОтмена: /cancel",
        "en": "🏷 <b>Choose the topic of your request:</b>\n\nCancel: /cancel",
    },
    "murojaat_yoq": {
        "uz": "Sizda hali murojaatlar mavjud emas.",
        "ru": "У вас пока нет обращений.",
        "en": "You have no requests yet.",
    },
    "royxat_sarlavha": {
        "uz": "📂 <b>Oxirgi murojaatlaringiz:</b>\n",
        "ru": "📂 <b>Ваши последние обращения:</b>\n",
        "en": "📂 <b>Your recent requests:</b>\n",
    },
    "royxat_past": {
        "uz": "\nBatafsil ko‘rish uchun raqamni bosing 👇",
        "ru": "\nНажмите на номер, чтобы открыть 👇",
        "en": "\nTap a number to view details 👇",
    },
    "topilmadi": {
        "uz": "Murojaat topilmadi.",
        "ru": "Обращение не найдено.",
        "en": "Request not found.",
    },
    "notogri": {
        "uz": "Noto‘g‘ri so‘rov.",
        "ru": "Неверный запрос.",
        "en": "Invalid request.",
    },
    "notogri_tanlov": {
        "uz": "Noto‘g‘ri tanlov.",
        "ru": "Неверный выбор.",
        "en": "Invalid choice.",
    },
    "faol_emas": {
        "uz": "Bu tanlov hozir faol emas.",
        "ru": "Этот выбор сейчас неактивен.",
        "en": "This option is no longer active.",
    },
    "xatolik": {
        "uz": "Xatolik yuz berdi. Qayta urinib ko‘ring.",
        "ru": "Произошла ошибка. Попробуйте ещё раз.",
        "en": "Something went wrong. Please try again.",
    },
    "admin_yoq": {
        "uz": "Sizda administrator huquqi yo‘q.",
        "ru": "У вас нет прав администратора.",
        "en": "You do not have administrator rights.",
    },

    # --- murojaat kartasi ---
    "k_murojaat": {"uz": "Murojaat", "ru": "Обращение", "en": "Request"},
    "k_holati": {"uz": "Holati", "ru": "Статус", "en": "Status"},
    "k_ilova": {"uz": "📎 Ilova: bor", "ru": "📎 Вложение: есть", "en": "📎 Attachment: yes"},
    "k_javob": {"uz": "Javob", "ru": "Ответ", "en": "Answer"},
    "k_javob_yoq": {
        "uz": "Hozircha javob yo‘q.",
        "ru": "Ответа пока нет.",
        "en": "No answer yet.",
    },

    # --- talabaga bildirishnomalar ---
    "korilmoqda_xabar": {
        "uz": "👀 Sizning #{id} murojaatingiz ko‘rib chiqilmoqda.",
        "ru": "👀 Ваше обращение #{id} рассматривается.",
        "en": "👀 Your request #{id} is being reviewed.",
    },
    "yopildi_xabar": {
        "uz": "🔒 Sizning #{id} murojaatingiz yopildi.\nYangi savol bo‘lsa, yangi murojaat yuboring.",
        "ru": "🔒 Ваше обращение #{id} закрыто.\nЕсли у вас новый вопрос, отправьте новое обращение.",
        "en": "🔒 Your request #{id} has been closed.\nIf you have a new question, please send a new request.",
    },
    "javob_xabar": {
        "uz": "📬 <b>Sizning #{id} murojaatingizga javob:</b>\n\n{javob}",
        "ru": "📬 <b>Ответ на ваше обращение #{id}:</b>\n\n{javob}",
        "en": "📬 <b>Answer to your request #{id}:</b>\n\n{javob}",
    },
    "elon": {
        "uz": "📢 <b>E’lon</b>\n\n{matn}",
        "ru": "📢 <b>Объявление</b>\n\n{matn}",
        "en": "📢 <b>Announcement</b>\n\n{matn}",
    },

    # --- ro'yxatdan o'tish ---
    "ozingiz_tel": {
        "uz": "Iltimos, o‘zingizning telefon raqamingizni yuboring.",
        "ru": "Пожалуйста, отправьте свой собственный номер.",
        "en": "Please share your own phone number.",
    },
    "ism_kiriting": {
        "uz": "Ism va familiyangizni kiriting:",
        "ru": "Введите фамилию и имя:",
        "en": "Enter your full name:",
    },
    "tel_tugma": {
        "uz": "Telefon raqamingizni pastdagi tugma orqali yuboring.",
        "ru": "Отправьте номер с помощью кнопки ниже.",
        "en": "Please use the button below to share your phone number.",
    },
    "ism_toliq": {
        "uz": "Ism va familiyangizni to‘liq kiriting.",
        "ru": "Введите фамилию и имя полностью.",
        "en": "Please enter your full name.",
    },
    "ism_uzun": {
        "uz": "Ism-familiya juda uzun.",
        "ru": "Слишком длинное ФИО.",
        "en": "The name is too long.",
    },
    "ism_matn": {
        "uz": "Ism va familiyangizni matn ko‘rinishida yozing.",
        "ru": "Напишите фамилию и имя текстом.",
        "en": "Please type your name as text.",
    },
    "fakultet_tanlang": {
        "uz": "Fakultetingizni tanlang:",
        "ru": "Выберите факультет:",
        "en": "Choose your faculty:",
    },
    "fakultet_yozing": {
        "uz": "Fakultet nomini yozing:",
        "ru": "Напишите название факультета:",
        "en": "Type the name of your faculty:",
    },
    "fakultet_xato": {
        "uz": "Fakultet nomini to‘g‘ri kiriting.",
        "ru": "Введите корректное название факультета.",
        "en": "Please enter a valid faculty name.",
    },
    "kurs_tanlang": {
        "uz": "Kursingizni tanlang:",
        "ru": "Выберите курс:",
        "en": "Choose your year of study:",
    },
    "kurs_yozing": {
        "uz": "Kursingizni yozing:",
        "ru": "Напишите ваш курс:",
        "en": "Type your year of study:",
    },
    "kurs_xato": {
        "uz": "Kursingizni to‘g‘ri kiriting.",
        "ru": "Введите корректный курс.",
        "en": "Please enter a valid year.",
    },
    "yonalish_tanlang": {
        "uz": "Yo‘nalishingizni tanlang.\n<i>Bu guruhingiz kodining boshi: masalan MNP-81/25 → MNP</i>",
        "ru": "Выберите направление.\n<i>Это начало кода вашей группы: например, MNP-81/25 → MNP</i>",
        "en": "Choose your programme.\n<i>It is the start of your group code, e.g. MNP-81/25 → MNP</i>",
    },
    "guruh_tanlang": {
        "uz": "Guruhingizni tanlang:",
        "ru": "Выберите группу:",
        "en": "Choose your group:",
    },
    "guruh_yozing": {
        "uz": "Guruhingiz nomini yozing (masalan: MNP-81/25):",
        "ru": "Напишите название группы (например: MNP-81/25):",
        "en": "Type your group name (e.g. MNP-81/25):",
    },
    "guruh_xato": {
        "uz": "Guruh nomini to‘g‘ri kiriting.",
        "ru": "Введите корректное название группы.",
        "en": "Please enter a valid group name.",
    },
    "tugma_tanlang": {
        "uz": "Iltimos, yuqoridagi tugmalardan birini tanlang.",
        "ru": "Пожалуйста, выберите один из вариантов выше.",
        "en": "Please choose one of the buttons above.",
    },
    "matn_yozing": {
        "uz": "Iltimos, matn ko‘rinishida yozing.",
        "ru": "Пожалуйста, напишите текстом.",
        "en": "Please type it as text.",
    },
    "toliq_emas": {
        "uz": "Ma’lumotlar to‘liq emas. Qayta boshlash uchun /start yuboring.",
        "ru": "Данные неполные. Отправьте /start, чтобы начать заново.",
        "en": "Some details are missing. Send /start to start again.",
    },
    "yangilandi": {
        "uz": "✅ <b>Ma’lumotlaringiz yangilandi!</b>",
        "ru": "✅ <b>Ваши данные обновлены!</b>",
        "en": "✅ <b>Your details have been updated!</b>",
    },
    "royxatdan_otdi": {
        "uz": "✅ <b>Ro‘yxatdan muvaffaqiyatli o‘tdingiz!</b>",
        "ru": "✅ <b>Вы успешно зарегистрированы!</b>",
        "en": "✅ <b>You have registered successfully!</b>",
    },
    "reg_info": {
        "uz": "Ism-familiya: {fish}\nFakultet: {fak}\nKurs: {kurs}\nYo‘nalish: {yon}\nGuruh: {guruh}",
        "ru": "ФИО: {fish}\nФакультет: {fak}\nКурс: {kurs}\nНаправление: {yon}\nГруппа: {guruh}",
        "en": "Full name: {fish}\nFaculty: {fak}\nCourse: {kurs}\nProgramme: {yon}\nGroup: {guruh}",
    },
    "boshqa": {
        "uz": "✏️ Boshqa (qo‘lda yozish)",
        "ru": "✏️ Другое (ввести вручную)",
        "en": "✏️ Other (type manually)",
    },

    # --- murojaat yuborish ---
    "toifa_faol_emas": {
        "uz": "Bu tanlov faol emas. Yangi murojaat: /new",
        "ru": "Этот выбор неактивен. Новое обращение: /new",
        "en": "This option is no longer active. New request: /new",
    },
    "batafsil": {
        "uz": (
            "✍️ <b>Murojaatingizni batafsil yozing.</b>\n\n"
            "Murojaat kamida 10 ta belgidan iborat bo‘lishi kerak.\n"
            "Rasm yoki fayl yubormoqchi bo‘lsangiz, matnni uning izohiga "
            "yozishingiz ham mumkin.\n\n"
            "Bekor qilish uchun /cancel buyrug‘ini yuboring."
        ),
        "ru": (
            "✍️ <b>Подробно опишите ваше обращение.</b>\n\n"
            "Обращение должно содержать не менее 10 символов.\n"
            "Если хотите приложить фото или файл, текст можно написать "
            "в подписи к нему.\n\n"
            "Для отмены отправьте /cancel."
        ),
        "en": (
            "✍️ <b>Describe your request in detail.</b>\n\n"
            "The request must be at least 10 characters long.\n"
            "If you want to attach a photo or file, you can write the text "
            "in its caption.\n\n"
            "Send /cancel to cancel."
        ),
    },
    "mavzu_tugmadan": {
        "uz": "Iltimos, mavzuni yuqoridagi tugmalardan tanlang yoki /cancel yuboring.",
        "ru": "Пожалуйста, выберите тему кнопками выше или отправьте /cancel.",
        "en": "Please choose a topic using the buttons above or send /cancel.",
    },
    "qisqa": {
        "uz": "Murojaat juda qisqa. Kamida 10 ta belgi yozing.",
        "ru": "Обращение слишком короткое. Напишите не менее 10 символов.",
        "en": "The request is too short. Please write at least 10 characters.",
    },
    "uzun": {
        "uz": "Murojaat {max} belgidan oshmasligi kerak (hozir {n}).",
        "ru": "Обращение не должно превышать {max} символов (сейчас {n}).",
        "en": "The request must not exceed {max} characters (currently {n}).",
    },
    "maxsus": {
        "uz": "Matnda maxsus belgilar (&lt;, &gt;, &amp;) juda ko‘p. Iltimos, qisqaroq yozing.",
        "ru": "В тексте слишком много спецсимволов (&lt;, &gt;, &amp;). Пожалуйста, сократите его.",
        "en": "The text has too many special characters (&lt;, &gt;, &amp;). Please shorten it.",
    },
    "fayl_biriktir": {
        "uz": "📎 Kerak bo‘lsa, <b>bitta</b> rasm yoki fayl biriktiring (ixtiyoriy).\n\nYoki pastdagi tugmani bosing.",
        "ru": "📎 При необходимости прикрепите <b>одно</b> фото или файл (необязательно).\n\nИли нажмите кнопку ниже.",
        "en": "📎 You can attach <b>one</b> photo or file if needed (optional).\n\nOr tap the button below.",
    },
    "btn_fayl_otkaz": {
        "uz": "➡️ Fayl biriktirmasdan davom etish",
        "ru": "➡️ Продолжить без файла",
        "en": "➡️ Continue without a file",
    },
    "avval_matn": {
        "uz": "Avval murojaat matnini yozing, keyin fayl biriktirasiz.\nYoki matnni rasm/fayl izohiga yozib yuboring.",
        "ru": "Сначала напишите текст обращения, затем прикрепите файл.\nИли отправьте фото/файл с текстом в подписи.",
        "en": "Please write the request text first, then attach a file.\nOr send the photo/file with the text in its caption.",
    },
    "albom": {
        "uz": "ℹ️ Albomdan faqat birinchi fayl qabul qilindi.",
        "ru": "ℹ️ Из альбома принят только первый файл.",
        "en": "ℹ️ Only the first file of the album was accepted.",
    },
    "faqat_matn": {
        "uz": "Murojaatni matn ko‘rinishida yozing.",
        "ru": "Напишите обращение текстом.",
        "en": "Please write your request as text.",
    },
    "preview": {
        "uz": "📝 <b>Murojaatingiz:</b>\n\n🏷 {toifa}\n📎 {ilova}\n\n{matn}\n\nUshbu murojaatni yuborishni tasdiqlaysizmi?",
        "ru": "📝 <b>Ваше обращение:</b>\n\n🏷 {toifa}\n📎 {ilova}\n\n{matn}\n\nПодтверждаете отправку?",
        "en": "📝 <b>Your request:</b>\n\n🏷 {toifa}\n📎 {ilova}\n\n{matn}\n\nDo you want to send this request?",
    },
    "ilova_rasm": {"uz": "🖼 Rasm biriktirilgan", "ru": "🖼 Фото прикреплено", "en": "🖼 Photo attached"},
    "ilova_fayl": {"uz": "📄 Fayl biriktirilgan", "ru": "📄 Файл прикреплён", "en": "📄 File attached"},
    "ilova_yoq": {"uz": "Ilova yo‘q", "ru": "Без вложений", "en": "No attachment"},
    "izoh_uzun": {
        "uz": "ℹ️ Fayl izohi juda uzun bo‘lgani uchun murojaatga qo‘shilmadi.",
        "ru": "ℹ️ Подпись к файлу слишком длинная и не добавлена к обращению.",
        "en": "ℹ️ The file caption was too long and was not added to the request.",
    },
    "fayl_xato": {
        "uz": "Rasm yoki fayl (hujjat) yuboring, yoki «Fayl biriktirmasdan davom etish» tugmasini bosing.",
        "ru": "Отправьте фото или файл (документ) или нажмите «Продолжить без файла».",
        "en": "Send a photo or file (document), or tap “Continue without a file”.",
    },
    "btn_yubor": {"uz": "✅ Yuborish", "ru": "✅ Отправить", "en": "✅ Send"},
    "btn_tahrir_m": {"uz": "✏️ Tahrirlash", "ru": "✏️ Изменить", "en": "✏️ Edit"},
    "btn_bekor": {"uz": "❌ Bekor qilish", "ru": "❌ Отменить", "en": "❌ Cancel"},
    "tugmalardan": {
        "uz": "Yuqoridagi tugmalardan birini bosing yoki /cancel yuboring.",
        "ru": "Нажмите одну из кнопок выше или отправьте /cancel.",
        "en": "Tap one of the buttons above or send /cancel.",
    },
    "faol_m_yoq": {
        "uz": "Faol murojaat topilmadi.",
        "ru": "Активное обращение не найдено.",
        "en": "No active request found.",
    },
    "qayta_yoz": {
        "uz": "Murojaatingizni qayta yozing:",
        "ru": "Напишите обращение заново:",
        "en": "Please write your request again:",
    },
    "yakunlangan": {
        "uz": "Bu murojaat allaqachon yakunlangan.",
        "ru": "Это обращение уже завершено.",
        "en": "This request has already been completed.",
    },
    "bekor_m": {
        "uz": "Murojaat yuborish bekor qilindi.",
        "ru": "Отправка обращения отменена.",
        "en": "The request has been cancelled.",
    },
    "tasdiq_yoq": {
        "uz": "Tasdiqlash uchun murojaat topilmadi.",
        "ru": "Обращение для подтверждения не найдено.",
        "en": "No request to confirm.",
    },
    "matn_kerak": {
        "uz": "Avval murojaat matnini yozishingiz kerak.",
        "ru": "Сначала нужно написать текст обращения.",
        "en": "You need to write the request text first.",
    },
    "profil_yoq": {
        "uz": "Profil topilmadi.",
        "ru": "Профиль не найден.",
        "en": "Profile not found.",
    },
    "kunlik_tugadi": {
        "uz": "Kunlik limit ({n} ta) tugagan.",
        "ru": "Дневной лимит ({n}) исчерпан.",
        "en": "Daily limit ({n}) reached.",
    },
    "yuborildi_toast": {
        "uz": "Murojaat yuborildi.",
        "ru": "Обращение отправлено.",
        "en": "Request sent.",
    },
    "yuborildi": {
        "uz": "✅ <b>Murojaatingiz muvaffaqiyatli yuborildi!</b>\n\nMurojaat raqami: #{id}\nHolatini «{btn}» bo‘limida kuzatishingiz mumkin.",
        "ru": "✅ <b>Ваше обращение успешно отправлено!</b>\n\nНомер обращения: #{id}\nСледить за статусом можно в разделе «{btn}».",
        "en": "✅ <b>Your request has been sent!</b>\n\nRequest number: #{id}\nYou can track its status in “{btn}”.",
    },
    "tushunmadim": {
        "uz": "Tushunmadim. Menyudan foydalaning yoki /help yuboring.",
        "ru": "Не понял. Воспользуйтесь меню или отправьте /help.",
        "en": "Sorry, I didn't understand. Use the menu or send /help.",
    },
    "boshlash": {
        "uz": "Boshlash uchun /start yuboring.",
        "ru": "Чтобы начать, отправьте /start.",
        "en": "Send /start to begin.",
    },

    # --- talaba bo'lmaganlar (o'qituvchi, xodim, boshqa) ---
    "btn_xodimlar": {
        "uz": "👥 Talaba emasman (o‘qituvchi, xodim)",
        "ru": "👥 Я не студент (преподаватель, сотрудник)",
        "en": "👥 I’m not a student (teacher, staff)",
    },
    "rol_tanlang": {
        "uz": "Kim sifatida murojaat qilasiz?",
        "ru": "В качестве кого вы обращаетесь?",
        "en": "Who are you?",
    },
    "rol_oqituvchi": {"uz": "👨‍🏫 O‘qituvchi", "ru": "👨‍🏫 Преподаватель", "en": "👨‍🏫 Teacher"},
    "rol_xodim": {
        "uz": "🏢 Departament / bo‘lim xodimi",
        "ru": "🏢 Сотрудник департамента / отдела",
        "en": "🏢 Department / office staff",
    },
    "rol_boshqa": {"uz": "👤 Boshqa", "ru": "👤 Другое", "en": "👤 Other"},
    "btn_orqaga": {"uz": "⬅️ Orqaga", "ru": "⬅️ Назад", "en": "⬅️ Back"},
    "kafedra_yozing": {
        "uz": "Kafedra yoki fakultet nomini yozing:",
        "ru": "Напишите название кафедры или факультета:",
        "en": "Type the name of your department (kafedra) or faculty:",
    },
    "bolim_yozing": {
        "uz": "Departament yoki bo‘lim nomini yozing:",
        "ru": "Напишите название департамента или отдела:",
        "en": "Type the name of your department or office:",
    },
    "lavozim_yozing": {
        "uz": "Lavozimingizni yozing (masalan: katta o‘qituvchi, bosh mutaxassis):",
        "ru": "Напишите вашу должность (например: старший преподаватель, главный специалист):",
        "en": "Type your position (e.g. senior lecturer, chief specialist):",
    },
    "boshqa_kim": {
        "uz": "Kimligingizni qisqacha yozing (masalan: abituriyent, ota-ona, bitiruvchi):",
        "ru": "Кратко напишите, кто вы (например: абитуриент, родитель, выпускник):",
        "en": "Briefly say who you are (e.g. applicant, parent, graduate):",
    },
    "qisqa_matn_xato": {
        "uz": "2 tadan 100 tagacha belgi yozing.",
        "ru": "Напишите от 2 до 100 символов.",
        "en": "Please type 2 to 100 characters.",
    },
    "profil_xodim": {
        "uz": (
            "👤 <b>Sizning ma'lumotlaringiz</b>\n\n"
            "Ism-familiya: {fish}\nTelefon: {tel}\nKim: {rol}\n"
            "Bo‘lim / kafedra: {bolim}\nLavozim: {lav}\nTil: {til}"
        ),
        "ru": (
            "👤 <b>Ваши данные</b>\n\n"
            "ФИО: {fish}\nТелефон: {tel}\nКто: {rol}\n"
            "Отдел / кафедра: {bolim}\nДолжность: {lav}\nЯзык: {til}"
        ),
        "en": (
            "👤 <b>Your details</b>\n\n"
            "Full name: {fish}\nPhone: {tel}\nRole: {rol}\n"
            "Department / office: {bolim}\nPosition: {lav}\nLanguage: {til}"
        ),
    },
    "reg_info_xodim": {
        "uz": "Ism-familiya: {fish}\nKim: {rol}\nBo‘lim / kafedra: {bolim}\nLavozim: {lav}",
        "ru": "ФИО: {fish}\nКто: {rol}\nОтдел / кафедра: {bolim}\nДолжность: {lav}",
        "en": "Full name: {fish}\nRole: {rol}\nDepartment / office: {bolim}\nPosition: {lav}",
    },

    # --- asosiy menyu tugmalari ---
    "btn_yangi": {"uz": "✉️ Murojaat yuborish", "ru": "✉️ Отправить обращение", "en": "✉️ Send a request"},
    "btn_meninglar": {"uz": "📂 Mening murojaatlarim", "ru": "📂 Мои обращения", "en": "📂 My requests"},
    "btn_profil": {"uz": "👤 Mening ma'lumotlarim", "ru": "👤 Мои данные", "en": "👤 My details"},
    "btn_yordam": {"uz": "ℹ️ Yordam", "ru": "ℹ️ Помощь", "en": "ℹ️ Help"},
}


def t(til, kalit, /, **kw):
    """Tarjima. Topilmasa o'zbekchasi olinadi."""
    variantlar = MATN[kalit]
    matn = variantlar.get(til) or variantlar["uz"]
    return matn.format(**kw) if kw else matn


def hamma_tarjimalar(kalit):
    """Reply-tugmalarni har qanday tilda tanish uchun."""
    return set(MATN[kalit].values())


def tugma_normal(matn):
    """Tugma matnini solishtirish uchun soddalashtiradi: tutuq belgisining
    har xil turlari (' ‘ ’ ʼ ʻ `) bir xil qilinadi, emoji va ortiqcha
    belgilar olib tashlanadi, katta-kichik harf farqi yo'qoladi.
    Shunda eski klaviaturadagi yoki qo'lda yozilgan tugma matni ham ishlaydi."""
    s = (matn or "").lower()
    for belgi in "‘’ʼʻ`´":
        s = s.replace(belgi, "'")
    s = re.sub(r"[^\w']+", " ", s)
    return " ".join(s.split())


def tugma(kalit):
    """Menyu tugmasi filtri (barcha tillarda, tutuq belgisiga sezgir emas)."""
    variantlar = {tugma_normal(v) for v in MATN[kalit].values()}
    return F.text.func(lambda x: tugma_normal(x) in variantlar)


# Foydalanuvchi turi: talaba / oqituvchi / xodim / boshqa
TALABA = "talaba"
ROLLAR = ["oqituvchi", "xodim", "boshqa"]
ROL_NOMI = {
    TALABA: "Talaba",
    "oqituvchi": "O‘qituvchi",
    "xodim": "Departament / bo‘lim xodimi",
    "boshqa": "Boshqa",
}


def rol_nomi(rol, til):
    if rol in ROLLAR:
        return t(til, "rol_" + rol)
    return ROL_NOMI.get(rol or TALABA, rol)


HOLAT_TARJIMA = {
    "ru": {YANGI: "Новое", KORILMOQDA: "На рассмотрении",
           JAVOB_BERILDI: "Получен ответ", YOPILGAN: "Закрыто"},
    "en": {YANGI: "New", KORILMOQDA: "In review",
           JAVOB_BERILDI: "Answered", YOPILGAN: "Closed"},
}

TOIFA_TARJIMA = {
    "ru": [
        "📚 Учёба и расписание", "💳 Контракт / оплата", "🏠 Общежитие",
        "💰 Стипендия", "📄 Документы и справки", "💡 Предложение", "❓ Другое",
    ],
    "en": [
        "📚 Studies and timetable", "💳 Contract / payment", "🏠 Dormitory",
        "💰 Scholarship", "📄 Documents and certificates", "💡 Suggestion",
        "❓ Other",
    ],
}

FAKULTET_TARJIMA = {
    "Menejment": ("Менеджмент", "Management"),
    "Iqtisodiyot": ("Экономика", "Economics"),
    "Raqamli iqtisodiyot": ("Цифровая экономика", "Digital Economy"),
    "Turizm": ("Туризм", "Tourism"),
    "Moliya": ("Финансы", "Finance"),
    "Buxgalteriya hisobi": ("Бухгалтерский учёт", "Accounting"),
    "Soliq": ("Налоги", "Taxation"),
    "Bank ishi": ("Банковское дело", "Banking"),
    "Marketing": ("Маркетинг", "Marketing"),
    "Polotsk (qo‘shma ta’lim)": ("Полоцк (совместное обучение)", "Polotsk (joint programme)"),
    "Magistratura (masofaviy)": ("Магистратура (дистанционная)", "Master's (distance)"),
    "Davlat auditi (3+1)": ("Государственный аудит (3+1)", "State Audit (3+1)"),
    "Davlat auditi": ("Государственный аудит", "State Audit"),
    "Magistratura": ("Магистратура", "Master's"),
    "Kechki ta’lim": ("Вечернее обучение", "Evening study"),
    "Masofaviy ta’lim": ("Дистанционное обучение", "Distance learning"),
    "Ikkinchi oliy ta’lim": ("Второе высшее образование", "Second degree"),
    "Ural (qo‘shma) bakalavr": ("Урал (совместное) бакалавриат", "Ural (joint) Bachelor's"),
    "Ural (qo‘shma) magistratura": ("Урал (совместное) магистратура", "Ural (joint) Master's"),
    "Ural (qo‘shma) masofaviy": ("Урал (совместное) дистанционное", "Ural (joint) distance"),
    "Ural (qo‘shma) sirtqi": ("Урал (совместное) заочное", "Ural (joint) part-time"),
    "Indoneziya (qo‘shma ta’lim)": ("Индонезия (совместное обучение)", "Indonesia (joint programme)"),
    "Sirtqi ta’lim": ("Заочное обучение", "Part-time study"),
}

KURS_TARJIMA = {
    "Magistratura": ("Магистратура", "Master's"),
    "Masofaviy": ("Дистанционное", "Distance"),
    "Bakalavr": ("Бакалавриат", "Bachelor's"),
}


def holat_nomi(holat, til):
    return HOLAT_TARJIMA.get(til, {}).get(holat, holat)


def toifa_nomi(toifa, til):
    if til in TOIFA_TARJIMA and toifa in TOIFALAR:
        return TOIFA_TARJIMA[til][TOIFALAR.index(toifa)]
    return toifa


def toifalar(til):
    return TOIFA_TARJIMA.get(til, TOIFALAR)


def fakultet_nomi(fak, til):
    if til in ("ru", "en") and fak in FAKULTET_TARJIMA:
        return FAKULTET_TARJIMA[fak][0 if til == "ru" else 1]
    return fak


def kurs_nomi(kurs, til):
    if til not in ("ru", "en") or not kurs:
        return kurs
    m = re.match(r"^(\d)-kurs$", kurs)
    if m:
        return f"{m.group(1)} курс" if til == "ru" else f"Year {m.group(1)}"
    if kurs in KURS_TARJIMA:
        return KURS_TARJIMA[kurs][0 if til == "ru" else 1]
    return kurs


def til_taxmini(language_code):
    """Telegram ilovasi tilidan taxmin (til hali tanlanmagan bo'lsa)."""
    kod = (language_code or "").lower()
    if kod.startswith("ru"):
        return "ru"
    if kod.startswith("en"):
        return "en"
    return "uz"



# =========================================================
# HOLATLAR (FSM)
# =========================================================

class TalabaHolati(StatesGroup):
    telefon = State()
    fish = State()
    fakultet = State()
    fakultet_qolda = State()
    kurs = State()
    kurs_qolda = State()
    yonalish = State()
    guruh = State()
    guruh_qolda = State()
    rol = State()               # talaba emaslar: kim ekanini tanlash
    xodim_bolim = State()       # bo'lim / kafedra nomi
    xodim_lavozim = State()     # lavozim (yoki "boshqa" uchun kimligi)
    murojaat_toifa = State()
    murojaat = State()
    murojaat_fayl = State()
    murojaat_tasdiqlash = State()


class AdminHolati(StatesGroup):
    javob_kutilmoqda = State()
    elon_kutilmoqda = State()
    elon_tasdiq = State()


ADMIN_HOLATLARI = {
    AdminHolati.javob_kutilmoqda.state,
    AdminHolati.elon_kutilmoqda.state,
    AdminHolati.elon_tasdiq.state,
}

MUROJAAT_HOLATLARI = {
    TalabaHolati.murojaat_toifa.state,
    TalabaHolati.murojaat.state,
    TalabaHolati.murojaat_fayl.state,
    TalabaHolati.murojaat_tasdiqlash.state,
}


# =========================================================
# KLAVIATURALAR
# =========================================================

def telefon_klaviaturasi(til="uz"):
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=t(til, "btn_tel"),
                    request_contact=True
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def talaba_menu(til="uz"):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(til, "btn_yangi"))],
            [
                KeyboardButton(text=t(til, "btn_meninglar")),
                KeyboardButton(text=t(til, "btn_profil"))
            ],
            [KeyboardButton(text=t(til, "btn_yordam"))]
        ],
        resize_keyboard=True
    )


def til_klaviaturasi():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=nom, callback_data=f"til:{kod}")]
            for kod, nom in TILLAR.items()
        ]
    )


def tanlash_klaviaturasi(prefix, royxat, ustun=1, boshqa=False, til="uz"):
    """Tugmalar indeks bilan: callback_data = 'prefix:3'. boshqa=True bo'lsa
    oxirida alohida qatorda 'Boshqa' tugmasi (indeks = len(royxat))."""
    buttons = []
    qator = []

    for i, nom in enumerate(royxat):
        qator.append(
            InlineKeyboardButton(text=nom, callback_data=f"{prefix}:{i}")
        )
        if len(qator) == ustun:
            buttons.append(qator)
            qator = []

    if qator:
        buttons.append(qator)

    if boshqa:
        buttons.append([
            InlineKeyboardButton(
                text=t(til, "boshqa"), callback_data=f"{prefix}:{len(royxat)}"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def fakultet_klaviaturasi(til="uz"):
    """Fakultetlar + 'Boshqa (qo'lda)' + oxirida talaba bo'lmaganlar uchun tugma."""
    klav = tanlash_klaviaturasi(
        "fakultet",
        [fakultet_nomi(f, til) for f in FAKULTETLAR],
        boshqa=True, til=til
    )
    klav.inline_keyboard.append([
        InlineKeyboardButton(text=t(til, "btn_xodimlar"), callback_data="rol:menu")
    ])
    return klav


def rol_klaviaturasi(til="uz"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(til, "rol_" + r), callback_data=f"rol:{r}")]
            for r in ROLLAR
        ] + [[
            InlineKeyboardButton(text=t(til, "btn_orqaga"), callback_data="rol:orqaga")
        ]]
    )


def fayl_klaviaturasi(til="uz"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(til, "btn_fayl_otkaz"),
                    callback_data="fayl:otkazish"
                )
            ]
        ]
    )


def murojaat_tasdiqlash(til="uz"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(til, "btn_yubor"),
                    callback_data="murojaat:yuborish"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(til, "btn_tahrir_m"),
                    callback_data="murojaat:tahrirlash"
                ),
                InlineKeyboardButton(
                    text=t(til, "btn_bekor"),
                    callback_data="murojaat:bekor"
                )
            ]
        ]
    )


def profil_klaviaturasi(til="uz"):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(til, "btn_tahrir"),
                    callback_data="profil:tahrirlash"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(til, "btn_til"),
                    callback_data="til:menu"
                )
            ]
        ]
    )


def admin_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📊 Statistika",
                    callback_data="admin:statistika"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆕 Yangi",
                    callback_data="admin:royxat:yangi"
                ),
                InlineKeyboardButton(
                    text="👀 Ko‘rilmoqda",
                    callback_data="admin:royxat:korilmoqda"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Oxirgi murojaatlar",
                    callback_data="admin:royxat:hammasi"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📤 CSV eksport",
                    callback_data="admin:eksport"
                ),
                InlineKeyboardButton(
                    text="📢 E’lon",
                    callback_data="admin:elon"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💾 Zaxira nusxa",
                    callback_data="admin:backup"
                )
            ]
        ]
    )


def javob_klaviaturasi(murojaat_id, holat=None):
    if holat == YOPILGAN:
        return None

    qator1 = []

    if holat in (None, YANGI):
        qator1.append(
            InlineKeyboardButton(
                text="👀 Ko‘rilmoqda",
                callback_data=f"holat:{murojaat_id}:k"
            )
        )

    qator1.append(
        InlineKeyboardButton(
            text="✍️ Javob berish",
            callback_data=f"javob:{murojaat_id}"
        )
    )

    return InlineKeyboardMarkup(
        inline_keyboard=[
            qator1,
            [
                InlineKeyboardButton(
                    text="🔒 Yopish",
                    callback_data=f"holat:{murojaat_id}:y"
                )
            ]
        ]
    )


def elon_tasdiq_klaviaturasi():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Hammaga yuborish",
                    callback_data="elon:yuborish"
                ),
                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data="elon:bekor"
                )
            ]
        ]
    )


def royxat_klaviaturasi(murojaatlar):
    """Talabaning murojaatlari ro'yxati: har biri uchun tugma."""
    qatorlar = []
    qator = []

    for m in murojaatlar:
        qator.append(
            InlineKeyboardButton(
                text=f"#{m['id']}",
                callback_data=f"korish:{m['id']}"
            )
        )
        if len(qator) == 5:
            qatorlar.append(qator)
            qator = []

    if qator:
        qatorlar.append(qator)

    return InlineKeyboardMarkup(inline_keyboard=qatorlar)


# =========================================================
# MA'LUMOTLAR BAZASI (bitta doimiy ulanish, WAL rejimi)
# =========================================================

db = None


def hozir():
    return datetime.now(TZ)


def hozir_str():
    return hozir().strftime("%Y-%m-%d %H:%M:%S")


async def _yoz(sql, params=()):
    cursor = await db.execute(sql, params)
    await db.commit()
    lastrowid = cursor.lastrowid
    await cursor.close()
    return lastrowid


async def _ozgartir(sql, params=()):
    """UPDATE/DELETE: nechta qator o'zgarganini qaytaradi."""
    cursor = await db.execute(sql, params)
    await db.commit()
    soni = cursor.rowcount
    await cursor.close()
    return soni


async def _bitta(sql, params=()):
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    await cursor.close()
    return dict(row) if row else None


async def _hammasi(sql, params=()):
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    await cursor.close()
    return [dict(r) for r in rows]


async def _qiymat(sql, params=()):
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    await cursor.close()
    return row[0] if row else 0


async def _ustun_qoshish(jadval, ustun, tur):
    cursor = await db.execute(f"PRAGMA table_info({jadval})")
    ustunlar = [row[1] for row in await cursor.fetchall()]
    await cursor.close()

    if ustun not in ustunlar:
        await db.execute(f"ALTER TABLE {jadval} ADD COLUMN {ustun} {tur}")


async def init_db():
    global db

    papka = os.path.dirname(DB_PATH)
    if papka:
        os.makedirs(papka, exist_ok=True)

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row

    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA cache_size=-16000")   # ~16 MB kesh

    await db.execute("""
        CREATE TABLE IF NOT EXISTS talabalar (
            user_id INTEGER PRIMARY KEY,
            telefon TEXT,
            fish TEXT,
            fakultet TEXT,
            guruh TEXT
        )
    """)

    await db.execute("""
        CREATE TABLE IF NOT EXISTS murojaatlar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            matn TEXT,
            holat TEXT DEFAULT 'Yangi',
            javob TEXT,
            sana TEXT
        )
    """)

    # Adminlarga yuborilgan kartalar (holat o'zgarsa, hammasini yangilash uchun)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS admin_kartalar (
            murojaat_id INTEGER,
            chat_id INTEGER,
            message_id INTEGER,
            PRIMARY KEY (murojaat_id, chat_id)
        )
    """)

    # Foydalanuvchi tanlagan til (ro'yxatdan o'tishdan oldin ham kerak)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS tillar (
            user_id INTEGER PRIMARY KEY,
            til TEXT
        )
    """)

    # FSM holatlari (bot qayta ishga tushsa ham yo'qolmaydi)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS fsm (
            kalit TEXT PRIMARY KEY,
            holat TEXT,
            data TEXT
        )
    """)

    # Eski bazalar uchun yangi ustunlar
    await _ustun_qoshish("talabalar", "kurs", "TEXT DEFAULT ''")
    await _ustun_qoshish("talabalar", "yonalish", "TEXT DEFAULT ''")
    # talaba / oqituvchi / xodim / boshqa. Eski yozuvlar — talaba.
    await _ustun_qoshish("talabalar", "turi", "TEXT DEFAULT 'talaba'")
    await _ustun_qoshish("murojaatlar", "toifa", "TEXT DEFAULT ''")
    await _ustun_qoshish("murojaatlar", "fayl_id", "TEXT")
    await _ustun_qoshish("murojaatlar", "fayl_turi", "TEXT")

    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_murojaat_user "
        "ON murojaatlar(user_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_murojaat_user_sana "
        "ON murojaatlar(user_id, sana)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_murojaat_holat "
        "ON murojaatlar(holat)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_murojaat_sana "
        "ON murojaatlar(sana)"
    )

    await db.commit()
    logging.info("Baza tayyor: %s", DB_PATH)


async def close_db():
    global db
    if db is not None:
        await db.close()
        db = None


SELECT_M = """
    SELECT m.id, m.user_id, m.matn, m.holat, m.javob, m.sana,
           m.toifa, m.fayl_id, m.fayl_turi,
           t.fish, t.telefon, t.fakultet, t.kurs, t.yonalish, t.guruh,
           t.turi, l.til
    FROM murojaatlar m
    LEFT JOIN talabalar t ON t.user_id = m.user_id
    LEFT JOIN tillar l ON l.user_id = m.user_id
"""


async def talaba_saqlash(user_id, telefon, fish, fakultet, kurs, yonalish, guruh,
                         turi=TALABA):
    """Talaba uchun: fakultet/kurs/yo'nalish/guruh.
    O'qituvchi/xodim uchun: fakultet = bo'lim yoki kafedra, kurs = lavozim."""
    await _yoz("""
        INSERT INTO talabalar
        (user_id, telefon, fish, fakultet, kurs, yonalish, guruh, turi)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            telefon=excluded.telefon,
            fish=excluded.fish,
            fakultet=excluded.fakultet,
            kurs=excluded.kurs,
            yonalish=excluded.yonalish,
            guruh=excluded.guruh,
            turi=excluded.turi
    """, (user_id, telefon, fish, fakultet, kurs, yonalish, guruh, turi))


async def talaba_olish(user_id):
    return await _bitta("""
        SELECT user_id, telefon, fish, fakultet, kurs, yonalish, guruh, turi
        FROM talabalar
        WHERE user_id=?
    """, (user_id,))


_til_kesh = {}


async def til_olish(user_id):
    """Saqlangan til yoki None (hali tanlanmagan)."""
    if user_id in _til_kesh:
        return _til_kesh[user_id]

    til = await _qiymat("SELECT til FROM tillar WHERE user_id=?", (user_id,))
    til = til if til in TILLAR else None

    if len(_til_kesh) > 100000:
        _til_kesh.clear()
    _til_kesh[user_id] = til
    return til


async def tl(user_id):
    """Har doim biror til qaytaradi (tanlanmagan bo'lsa o'zbekcha)."""
    return await til_olish(user_id) or "uz"


async def til_saqlash(user_id, til):
    _til_kesh[user_id] = til
    await _yoz("""
        INSERT INTO tillar (user_id, til) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET til=excluded.til
    """, (user_id, til))


async def barcha_tillar():
    rows = await _hammasi("SELECT user_id, til FROM tillar")
    return {r["user_id"]: r["til"] for r in rows}


async def barcha_talaba_idlari():
    rows = await _hammasi("SELECT user_id FROM talabalar")
    return [r["user_id"] for r in rows]


async def murojaat_qoshish(user_id, matn, toifa, fayl_id, fayl_turi):
    return await _yoz("""
        INSERT INTO murojaatlar
        (user_id, matn, holat, sana, toifa, fayl_id, fayl_turi)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, matn, YANGI, hozir_str(), toifa, fayl_id, fayl_turi))


async def murojaat_olish(murojaat_id):
    return await _bitta(
        SELECT_M + " WHERE m.id=?",
        (murojaat_id,)
    )


async def korilmoqda_belgilash(murojaat_id):
    """Faqat 'Yangi' bo'lsa o'zgartiradi. Ikki admin bir vaqtda bossa ham
    talabaga bitta xabar boradi."""
    return await _ozgartir(
        "UPDATE murojaatlar SET holat=? WHERE id=? AND holat=?",
        (KORILMOQDA, murojaat_id, YANGI)
    ) > 0


async def yopish(murojaat_id):
    return await _ozgartir(
        "UPDATE murojaatlar SET holat=? WHERE id=? AND holat!=?",
        (YOPILGAN, murojaat_id, YOPILGAN)
    ) > 0


async def murojaat_javoblash(murojaat_id, javob):
    """Javobni bitta SQL so'rovda qo'shadi (ikki admin bir vaqtda javob
    yozsa ham biri ikkinchisini o'chirib yubormaydi). Yopilgan murojaatga
    javob yozilmaydi."""
    yangi = f"[{hozir_str()}]\n{javob}"

    return await _ozgartir("""
        UPDATE murojaatlar
        SET javob = CASE
                WHEN javob IS NULL OR javob = '' THEN ?
                ELSE javob || ?
            END,
            holat = ?
        WHERE id = ? AND holat != ?
    """, (yangi, "\n\n" + yangi, JAVOB_BERILDI, murojaat_id, YOPILGAN)) > 0


async def talaba_murojaatlari(user_id, limit=10):
    return await _hammasi(
        SELECT_M + " WHERE m.user_id=? ORDER BY m.id DESC LIMIT ?",
        (user_id, limit)
    )


async def murojaatlar_royxati(holat=None, limit=10):
    if holat:
        return await _hammasi(
            SELECT_M + " WHERE m.holat=? ORDER BY m.id DESC LIMIT ?",
            (holat, limit)
        )

    return await _hammasi(
        SELECT_M + " ORDER BY m.id DESC LIMIT ?",
        (limit,)
    )


async def bugungi_soni(user_id):
    bugun = hozir().strftime("%Y-%m-%d") + " 00:00:00"

    return await _qiymat(
        "SELECT COUNT(*) FROM murojaatlar WHERE user_id=? AND sana>=?",
        (user_id, bugun)
    )


async def statistika():
    bugun = hozir().strftime("%Y-%m-%d") + " 00:00:00"

    return {
        "talabalar": await _qiymat("SELECT COUNT(*) FROM talabalar"),
        "turlar": await _hammasi(
            "SELECT COALESCE(NULLIF(turi, ''), 'talaba') AS turi, COUNT(*) AS soni "
            "FROM talabalar GROUP BY 1 ORDER BY soni DESC"
        ),
        "jami": await _qiymat("SELECT COUNT(*) FROM murojaatlar"),
        "bugun": await _qiymat(
            "SELECT COUNT(*) FROM murojaatlar WHERE sana>=?",
            (bugun,)
        ),
        "holatlar": await _hammasi(
            "SELECT holat, COUNT(*) AS soni FROM murojaatlar "
            "GROUP BY holat"
        ),
        "toifalar": await _hammasi(
            "SELECT toifa, COUNT(*) AS soni FROM murojaatlar "
            "GROUP BY toifa ORDER BY soni DESC"
        ),
    }


async def eksport_qatorlari():
    return await _hammasi(SELECT_M + " ORDER BY m.id ASC")


async def admin_karta_saqlash(murojaat_id, chat_id, message_id):
    await _yoz("""
        INSERT INTO admin_kartalar (murojaat_id, chat_id, message_id)
        VALUES (?, ?, ?)
        ON CONFLICT(murojaat_id, chat_id) DO UPDATE SET
            message_id=excluded.message_id
    """, (murojaat_id, chat_id, message_id))


async def admin_kartalari(murojaat_id):
    return await _hammasi(
        "SELECT chat_id, message_id FROM admin_kartalar WHERE murojaat_id=?",
        (murojaat_id,)
    )


async def bildirilmagan_murojaatlar(admin_id):
    """Bot o'chib turganda adminga yetib bormay qolgan yangi murojaatlar."""
    chegara = (hozir() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")

    rows = await _hammasi("""
        SELECT m.id FROM murojaatlar m
        WHERE m.holat = ? AND m.sana >= ?
          AND NOT EXISTS (
              SELECT 1 FROM admin_kartalar k
              WHERE k.murojaat_id = m.id AND k.chat_id = ?
          )
        ORDER BY m.id ASC
        LIMIT 300
    """, (YANGI, chegara, admin_id))

    return [r["id"] for r in rows]


# =========================================================
# FSM SAQLOVCHI (SQLite) — bot qayta ishga tushsa ham
# talabaning yarim qolgan ro'yxatdan o'tishi / murojaati saqlanadi.
# O'qish xotiradagi keshdan (tez), yozish bazaga.
# =========================================================

class SQLiteStorage(BaseStorage):

    def __init__(self):
        self._kesh = {}

    @staticmethod
    def _kalit(key: StorageKey):
        return ":".join(str(x) for x in (
            key.bot_id,
            key.chat_id,
            key.user_id,
            key.thread_id or "",
            key.business_connection_id or "",
            key.destiny,
        ))

    async def _yuklash(self, kalit):
        if kalit in self._kesh:
            return self._kesh[kalit]

        row = await _bitta("SELECT holat, data FROM fsm WHERE kalit=?", (kalit,))

        if row:
            try:
                data = json.loads(row["data"]) if row["data"] else {}
            except (ValueError, TypeError):
                data = {}
            qiymat = [row["holat"], data]
        else:
            qiymat = [None, {}]

        if len(self._kesh) > 50000:
            self._kesh.clear()

        self._kesh[kalit] = qiymat
        return qiymat

    async def _saqlash(self, kalit, holat, data):
        self._kesh[kalit] = [holat, data]

        if holat is None and not data:
            await _yoz("DELETE FROM fsm WHERE kalit=?", (kalit,))
            return

        await _yoz("""
            INSERT INTO fsm (kalit, holat, data) VALUES (?, ?, ?)
            ON CONFLICT(kalit) DO UPDATE SET
                holat=excluded.holat, data=excluded.data
        """, (kalit, holat, json.dumps(data, ensure_ascii=False)))

    async def set_state(self, key: StorageKey, state=None):
        holat = state.state if isinstance(state, State) else state
        kalit = self._kalit(key)
        _, data = await self._yuklash(kalit)
        await self._saqlash(kalit, holat, data)

    async def get_state(self, key: StorageKey):
        holat, _ = await self._yuklash(self._kalit(key))
        return holat

    async def set_data(self, key: StorageKey, data):
        kalit = self._kalit(key)
        holat, _ = await self._yuklash(kalit)
        await self._saqlash(kalit, holat, dict(data))

    async def get_data(self, key: StorageKey):
        _, data = await self._yuklash(self._kalit(key))
        return dict(data)

    async def close(self):
        pass


# =========================================================
# YORDAMCHI FUNKSIYALAR
# =========================================================

def admin_ekanmi(user_id):
    return user_id in ADMIN_IDS


def toza_matn(matn):
    """Bir qatorli maydonlar uchun (ism, fakultet, guruh)."""
    return " ".join((matn or "").split()).strip()


def toza_kop_qator(matn):
    """Murojaat va javob uchun: qator tashlashlar saqlanadi,
    ortiqcha probellar va 2 tadan ortiq bo'sh qatorlar olib tashlanadi."""
    natija = []
    bosh = 0

    for qator in (matn or "").strip().splitlines():
        qator = " ".join(qator.split())

        if not qator:
            bosh += 1
            if bosh > 1:
                continue
        else:
            bosh = 0

        natija.append(qator)

    return "\n".join(natija).strip()


def esc(value):
    """Foydalanuvchi matnini HTML uchun xavfsiz qiladi."""
    return escape(str(value if value is not None else ""))


def kesish(matn, n, oxiri=False):
    matn = matn or ""

    if len(matn) <= n:
        return matn

    if oxiri:
        return "…" + matn[-n:]

    return matn[:n] + "…"


def indeks_olish(data, royxat_uzunligi):
    """callback_data dan indeksni xavfsiz oladi. Xato bo'lsa None."""
    try:
        index = int(data.split(":")[1])
    except (ValueError, IndexError):
        return None

    if index < 0 or index >= royxat_uzunligi:
        return None

    return index


def murojaat_matni_xatosi(matn, til="uz"):
    """Murojaat matnini tekshiradi. Xato bo'lsa xabar matni, aks holda None."""
    if len(matn) < 10:
        return t(til, "qisqa")

    if len(matn) > MAX_MATN:
        return t(til, "uzun", max=MAX_MATN, n=len(matn))

    if len(esc(matn)) > 3300:
        return t(til, "maxsus")

    return None


def talaba_karta(m, n=None, til="uz"):
    belgi = HOLAT_BELGI.get(m["holat"], "📄")
    ilova = "\n" + t(til, "k_ilova") if m["fayl_id"] else ""
    javob = m["javob"] or t(til, "k_javob_yoq")

    matn_n = min(1000, n) if n else 1000
    javob_n = min(1200, n) if n else 1200

    return (
        f"📄 <b>{t(til, 'k_murojaat')} #{m['id']}</b>\n"
        f"🏷 {esc(toifa_nomi(m['toifa'], til) or '—')}\n"
        f"{belgi} {t(til, 'k_holati')}: {esc(holat_nomi(m['holat'], til))}\n"
        f"🕒 {esc(m['sana'])}{ilova}\n\n"
        f"<b>{t(til, 'k_murojaat')}:</b>\n{esc(kesish(m['matn'], matn_n))}\n\n"
        f"<b>{t(til, 'k_javob')}:</b>\n{esc(kesish(javob, javob_n, oxiri=True))}"
    )


def sigadigan_karta(karta_funksiyasi, m):
    """Telegram 4096 belgi chegarasidan oshmaydigan karta matnini qaytaradi.
    (<, >, & belgilari ko'p bo'lsa HTML uzayib ketadi.)"""
    for n in (None, 600, 250, 80):
        matn = karta_funksiyasi(m, n)
        if len(matn) <= 4000:
            return matn

    return karta_funksiyasi(m, 30)


def kim_qatori(m):
    """Admin kartasidagi '🎓 fakultet, kurs, guruh' qatori.
    O'qituvchi/xodim uchun: '🏢 Xodim · bo'lim · lavozim'."""
    turi = m.get("turi") or TALABA

    if turi == TALABA:
        return (
            f"🎓 {esc(m['fakultet'])}, {esc(m['kurs'])}"
            f"{', ' + esc(m['yonalish']) if m['yonalish'] else ''}, "
            f"<b>{esc(m['guruh'])}</b>"
        )

    belgi = {"oqituvchi": "👨‍🏫", "xodim": "🏢"}.get(turi, "👤")
    qismlar = [f"<b>{esc(ROL_NOMI.get(turi, turi))}</b>"]
    for qiymat in (m["fakultet"], m["kurs"]):
        if qiymat:
            qismlar.append(esc(qiymat))
    return f"{belgi} " + " · ".join(qismlar)


def admin_karta(m, n=None):
    belgi = HOLAT_BELGI.get(m["holat"], "📄")
    ilova = "📎 Ilova: bor\n" if m["fayl_id"] else ""

    murojaat_matni = kesish(m["matn"], n) if n else m["matn"]
    javob_n = min(1000, n) if n else 1000

    matn = (
        f"📩 <b>Murojaat #{m['id']}</b>  {belgi} {esc(m['holat'])}\n"
        f"🏷 {esc(m['toifa'] or '—')}\n"
        f"🕒 {esc(m['sana'])}\n\n"
        f"👤 <a href=\"tg://user?id={int(m['user_id'])}\">{esc(m['fish'])}</a>\n"
        f"📞 {esc(m['telefon'])}\n"
        f"{kim_qatori(m)}\n"
        f"🆔 <code>{int(m['user_id'])}</code>"
        f"{'  🌐 ' + TIL_NOMI[m['til']] if m.get('til') in TIL_NOMI else ''}\n"
        f"{ilova}\n"
        f"<b>Murojaat:</b>\n{esc(murojaat_matni)}"
    )

    if m["javob"]:
        matn += (
            "\n\n<b>Javob:</b>\n"
            f"{esc(kesish(m['javob'], javob_n, oxiri=True))}"
        )

    return matn


# =========================================================
# TELEGRAM CHEKLOVLARI BILAN ISHLASH
# =========================================================

class TezlikCheklovchi:
    """Botning barcha chiquvchi xabarlarini soniyasiga N tadan oshirmaydi.
    Talabalarga javoblar (ustuvor) ommaviy xabarlardan OLDIN o'tadi."""

    def __init__(self, soniyasiga):
        self.oraliq = 1.0 / soniyasiga
        self.keyingi = 0.0
        self.qulf = asyncio.Lock()
        self.ustuvor_kutmoqda = 0

    async def kut(self, ustuvor=True):
        if ustuvor:
            self.ustuvor_kutmoqda += 1
        else:
            # Talabalar javob kutayotgan bo'lsa, ommaviy xabar navbat beradi
            while self.ustuvor_kutmoqda > 0:
                await asyncio.sleep(0.05)

        try:
            async with self.qulf:
                now = time.monotonic()

                if self.keyingi > now:
                    await asyncio.sleep(self.keyingi - now)
                    now = self.keyingi

                self.keyingi = now + self.oraliq
        finally:
            if ustuvor:
                self.ustuvor_kutmoqda -= 1

    def toxtat(self, soniya):
        """Telegram 'kuting' desa — hamma yuborishni shu muddatga suramiz."""
        self.keyingi = max(self.keyingi, time.monotonic() + soniya)


tezlik = TezlikCheklovchi(UMUMIY_TEZLIK)

# Fon vazifalari (e'lon, admin kartalari) shu belgini qo'yadi
ommaviy_rejim = contextvars.ContextVar("ommaviy_rejim", default=False)

XABAR_METODLARI = (
    tg.SendMessage, tg.SendPhoto, tg.SendDocument, tg.SendVideo,
    tg.CopyMessage, tg.ForwardMessage, tg.EditMessageText,
)


class QaytaUrinishMiddleware(BaseRequestMiddleware):
    """1) Har bir xabar umumiy tezlik navbatidan o'tadi.
    2) Telegram 'juda ko'p so'rov, X soniya kuting' (429) desa yoki tarmoq
    uzilsa — kutib, qayta yuboradi. BARCHA so'rovlarga ta'sir qiladi."""

    def __init__(self, urinishlar=5):
        self.urinishlar = urinishlar

    async def __call__(self, make_request, bot, method):
        xabarmi = isinstance(method, XABAR_METODLARI)

        for urinish in range(self.urinishlar):
            if xabarmi:
                await tezlik.kut(ustuvor=not ommaviy_rejim.get())

            try:
                return await make_request(bot, method)

            except TelegramRetryAfter as e:
                if urinish == self.urinishlar - 1 or e.retry_after > MAX_KUTISH:
                    raise
                logging.warning(
                    "Flood limit: %s soniya kutilmoqda (%s)",
                    e.retry_after, type(method).__name__
                )
                tezlik.toxtat(e.retry_after)
                await asyncio.sleep(e.retry_after + random.uniform(0.1, 1.0))

            except TelegramNetworkError:
                if urinish == self.urinishlar - 1:
                    raise
                await asyncio.sleep(1 + urinish)


async def xavfsiz_yuborish(chat_id, text, **kwargs):
    """Xabar yuboradi. Yuborilgan Message ni, bo'lmasa None qaytaradi.
    Bloklangan / topilmagan chatlarda botni to'xtatmaydi."""
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        return None
    except TelegramBadRequest as e:
        logging.warning("Xabar yuborilmadi (chat %s): %s", chat_id, e)
        return None
    except Exception:
        logging.exception("Xabar yuborishda xato (chat %s)", chat_id)
        return None


async def fayl_yuborish(chat_id, m):
    if not m["fayl_id"]:
        return

    caption = f"📎 #{m['id']} murojaat ilovasi"

    try:
        if m["fayl_turi"] == "photo":
            await bot.send_photo(chat_id, m["fayl_id"], caption=caption)
        else:
            await bot.send_document(chat_id, m["fayl_id"], caption=caption)
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logging.warning("Fayl yuborilmadi (chat %s): %s", chat_id, e)
    except Exception:
        logging.exception("Faylni yuborishda xato (chat %s)", chat_id)


async def admin_karta_yuborish(chat_id, m):
    xabar = await xavfsiz_yuborish(
        chat_id,
        sigadigan_karta(admin_karta, m),
        reply_markup=javob_klaviaturasi(m["id"], m["holat"])
    )

    if xabar:
        await admin_karta_saqlash(m["id"], chat_id, xabar.message_id)

    await fayl_yuborish(chat_id, m)
    return xabar


async def karta_tahrirlash(chat_id, message_id, m):
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=sigadigan_karta(admin_karta, m),
            reply_markup=javob_klaviaturasi(m["id"], m["holat"])
        )
    except TelegramBadRequest as e:
        # "message is not modified" yoki o'chirilgan xabar — muhim emas
        if "not modified" not in str(e):
            logging.info("Karta yangilanmadi (%s): %s", chat_id, e)
    except Exception:
        logging.exception("Karta tahrirlashda xato")


async def kartalarni_yangilash(murojaat_id, istisno=None):
    """Barcha adminlardagi kartani yangi holat bilan yangilaydi."""
    m = await murojaat_olish(murojaat_id)
    if not m:
        return

    ommaviy_rejim.set(True)

    for k in await admin_kartalari(murojaat_id):
        if not k["message_id"]:
            continue
        if istisno and (k["chat_id"], k["message_id"]) == istisno:
            continue
        await karta_tahrirlash(k["chat_id"], k["message_id"], m)


# Fon vazifalari o'chib ketmasligi uchun havolasi saqlanadi
_fon_vazifalar = set()


def fonda(coro):
    vazifa = asyncio.create_task(coro)
    _fon_vazifalar.add(vazifa)
    vazifa.add_done_callback(_fon_vazifalar.discard)
    return vazifa


# Bir vaqtda ikki marta "Yuborish" bosilishidan himoya
_yuborilmoqda = set()


# ---------- Adminlarga xabar navbati ----------
# Talaba "Yuborish"ni bosganda javob darhol qaytadi, adminlarga karta
# esa fonda, navbat bilan yuboriladi. 500 ta murojaat bir vaqtda kelsa ham
# talabalar kutib qolmaydi.

_admin_navbat = {}
_admin_ishchilar = []


TOPLAM_CHEGARASI = 3     # navbatda shundan ko'p bo'lsa, bitta umumiy ro'yxat


async def toplam_yuborish(admin_id, idlar):
    """Ko'p murojaat birdaniga kelsa — har biri uchun alohida karta emas,
    bitta xabarda ro'yxat (50 tadan). Admin "🆕 Yangi" yoki /find bilan ochadi."""
    for i in range(0, len(idlar), 50):
        qism = idlar[i:i + 50]
        belgilar = ",".join("?" * len(qism))
        rows = await _hammasi(
            SELECT_M + f" WHERE m.id IN ({belgilar}) ORDER BY m.id", tuple(qism)
        )

        qatorlar = [f"🆕 <b>{len(rows)} ta yangi murojaat:</b>\n"]
        for m in rows:
            qatorlar.append(
                f"<b>#{m['id']}</b> · {esc(kesish(m['fish'] or '—', 30))} · "
                f"{esc(m['toifa'] or '—')}"
            )
        qatorlar.append("\nOchish: /find RAQAM yoki «🆕 Yangi» tugmasi (/admin)")

        xabar = await xavfsiz_yuborish(
            admin_id,
            "\n".join(qatorlar),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🆕 Yangi murojaatlar",
                    callback_data="admin:royxat:yangi"
                )
            ]])
        )

        if xabar:
            # message_id=0: "ro'yxatda bildirilgan", karta sifatida tahrirlanmaydi
            for m in rows:
                await _yoz("""
                    INSERT OR IGNORE INTO admin_kartalar
                    (murojaat_id, chat_id, message_id) VALUES (?, ?, 0)
                """, (m["id"], admin_id))

        await asyncio.sleep(ADMIN_ORALIQ)


async def admin_ishchi(admin_id, navbat):
    ommaviy_rejim.set(True)

    while True:
        idlar = [await navbat.get()]
        while not navbat.empty():
            idlar.append(navbat.get_nowait())

        try:
            if len(idlar) > TOPLAM_CHEGARASI:
                await toplam_yuborish(admin_id, idlar)
            else:
                for murojaat_id in idlar:
                    m = await murojaat_olish(murojaat_id)
                    if m and m["holat"] != YOPILGAN:
                        await admin_karta_yuborish(admin_id, m)
                        # Telegram bitta chatga ~1 xabar/soniya ruxsat beradi
                        await asyncio.sleep(
                            ADMIN_ORALIQ * (2 if m["fayl_id"] else 1)
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Adminga (%s) xabar yuborishda xato", admin_id)
        finally:
            for _ in idlar:
                navbat.task_done()


def adminlarga_xabar(murojaat_id):
    for navbat in _admin_navbat.values():
        navbat.put_nowait(murojaat_id)


async def admin_ishchilarni_boshlash():
    for admin_id in ADMIN_IDS:
        navbat = asyncio.Queue()
        _admin_navbat[admin_id] = navbat
        _admin_ishchilar.append(
            asyncio.create_task(admin_ishchi(admin_id, navbat))
        )

        # Bot o'chib turganda yetkazilmay qolganlarni qayta navbatga qo'yamiz
        for murojaat_id in await bildirilmagan_murojaatlar(admin_id):
            navbat.put_nowait(murojaat_id)


async def admin_ishchilarni_toxtatish():
    for vazifa in _admin_ishchilar:
        vazifa.cancel()
    await asyncio.gather(*_admin_ishchilar, return_exceptions=True)
    _admin_ishchilar.clear()


# =========================================================
# MIDDLEWARE: SPAMDAN HIMOYA
# =========================================================

class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, interval=0.5):
        self.interval = interval
        self.last = {}
        self.albomlar = {}

    def _tozalash(self, now):
        if len(self.last) > 10000:
            chegara = now - 60
            self.last = {k: v for k, v in self.last.items() if v > chegara}

        if len(self.albomlar) > 1000:
            chegara = now - 60
            self.albomlar = {
                k: v for k, v in self.albomlar.items() if v > chegara
            }

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")

        if user is None:
            return await handler(event, data)

        now = time.monotonic()

        # Albom (bir nechta rasm birga): faqat birinchisi o'tadi,
        # qolganlari indamay tashlanadi.
        albom = getattr(event, "media_group_id", None)
        if albom:
            if albom in self.albomlar:
                return
            self.albomlar[albom] = now
            self.last[user.id] = now
            self._tozalash(now)
            return await handler(event, data)

        if now - self.last.get(user.id, 0) < self.interval:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer()
                except Exception:
                    pass
            return

        self.last[user.id] = now
        self._tozalash(now)

        return await handler(event, data)


class TilMiddleware(BaseMiddleware):
    """Har bir handlerga `til` (uz/ru/en) ni beradi."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None:
            data["til"] = (
                await til_olish(user.id) or til_taxmini(user.language_code)
            )
        else:
            data["til"] = "uz"
        return await handler(event, data)


def menyu_tugmalari():
    natija = set()
    for kalit in ("btn_yangi", "btn_meninglar", "btn_profil", "btn_yordam"):
        natija |= {tugma_normal(v) for v in hamma_tarjimalar(kalit)}
    return natija


MENYU_TUGMALARI = menyu_tugmalari()


class AdminHolatNazorati(BaseMiddleware):
    """XATO TUZATILDI: admin «Javob berish» (yoki E'lon) ni bosib, keyin
    menyu tugmasini yoki boshqa buyruqni bossa, bot javob rejimida qolib
    ketardi va keyingi tasodifiy matn talabaga JAVOB bo'lib ketardi.
    Endi menyu tugmasi yoki buyruq (/cancel dan tashqari) bosilsa, admin
    rejimi avtomatik bekor qilinadi va bu haqda xabar beriladi."""

    async def __call__(self, handler, event, data):
        state = data.get("state")
        matn = getattr(event, "text", None) or ""

        menyu_yoki_buyruq = tugma_normal(matn) in MENYU_TUGMALARI or (
            matn.startswith("/") and not matn.startswith("/cancel")
        )

        if state is not None and menyu_yoki_buyruq:
            holat = await state.get_state()

            if holat in ADMIN_HOLATLARI:
                eski = await state.get_data()
                await state.clear()

                if holat == AdminHolati.javob_kutilmoqda.state:
                    xabar = (
                        f"ℹ️ #{eski.get('murojaat_id')} murojaatiga javob "
                        "yozish bekor qilindi (hech narsa yuborilmadi)."
                    )
                else:
                    xabar = "ℹ️ E’lon yuborish bekor qilindi."

                await xavfsiz_yuborish(event.chat.id, xabar)

        return await handler(event, data)


# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
bot.session.middleware(QaytaUrinishMiddleware())

dp = Dispatcher(storage=SQLiteStorage())

# Bot faqat shaxsiy chatda ishlaydi (guruhlarda javob bermaydi)
dp.message.filter(F.chat.type == "private")

dp.message.outer_middleware(ThrottleMiddleware(0.5))
dp.callback_query.outer_middleware(ThrottleMiddleware(0.4))
dp.message.outer_middleware(TilMiddleware())
dp.callback_query.outer_middleware(TilMiddleware())
dp.message.outer_middleware(AdminHolatNazorati())


@dp.errors()
async def xatolik_ushlash(event: ErrorEvent):
    # Bitta xato butun botni to'xtatib qo'ymasligi uchun
    logging.error("Handlerda xato: %s", event.exception, exc_info=event.exception)

    # Tugma bosilgan bo'lsa, "soat" belgisi osilib qolmasin
    call = event.update.callback_query
    if call is not None:
        try:
            await call.answer(t(await tl(call.from_user.id), "xatolik"))
        except Exception:
            pass

    # Oddiy xabarda xato bo'lsa ham foydalanuvchi javobsiz qolmasin
    message = event.update.message
    if message is not None and message.from_user is not None:
        try:
            await xavfsiz_yuborish(
                message.chat.id, t(await tl(message.from_user.id), "xatolik")
            )
        except Exception:
            pass

    return True


async def yoz(user_id, text, **kwargs):
    """Callback'lar uchun: call.message eski bo'lsa ham ishlaydi
    (shaxsiy chatda chat_id == user_id)."""
    return await xavfsiz_yuborish(user_id, text, **kwargs)


# =========================================================
# BUYRUQLAR VA MENYU
# (holatlardan OLDIN turadi, shuning uchun har doim ishlaydi)
# =========================================================

async def royxatni_boshlash(chat_id, state, til):
    await state.clear()
    await state.set_state(TalabaHolati.telefon)

    await xavfsiz_yuborish(
        chat_id,
        t(til, "welcome") + "\n\n" + t(til, "tel_yuboring"),
        reply_markup=telefon_klaviaturasi(til)
    )


@dp.message(Command("start"))
async def start(message: Message, state: FSMContext, til: str):

    await state.clear()

    # Til hali tanlanmagan bo'lsa — avval tilni so'raymiz
    if await til_olish(message.from_user.id) is None:
        await message.answer(t(til, "til_tanlash"), reply_markup=til_klaviaturasi())
        return

    talaba = await talaba_olish(message.from_user.id)

    if talaba:
        await message.answer(
            t(til, "salom", fish=esc(talaba["fish"])),
            reply_markup=talaba_menu(til)
        )
        return

    await royxatni_boshlash(message.chat.id, state, til)


@dp.message(Command("language"))
async def til_buyruq(message: Message):
    await message.answer(t("uz", "til_tanlash"), reply_markup=til_klaviaturasi())


@dp.callback_query(F.data.startswith("til:"))
async def til_tanlash(call: CallbackQuery, state: FSMContext):

    kod = call.data.split(":")[1]

    if kod == "menu":
        await call.answer()
        await yoz(call.from_user.id, t("uz", "til_tanlash"), reply_markup=til_klaviaturasi())
        return

    if kod not in TILLAR:
        await call.answer()
        return

    await til_saqlash(call.from_user.id, kod)
    await call.answer(TIL_NOMI[kod])

    talaba = await talaba_olish(call.from_user.id)
    holat = await state.get_state()

    if talaba:
        await yoz(call.from_user.id, t(kod, "til_ozgardi"), reply_markup=talaba_menu(kod))
    elif holat in (None, TalabaHolati.telefon.state):
        await royxatni_boshlash(call.from_user.id, state, kod)
    else:
        # Ro'yxatdan o'tish yarmida til almashtirildi — jarayon davom etadi
        await yoz(call.from_user.id, t(kod, "til_ozgardi"))


@dp.message(Command("cancel"))
async def bekor_qilish(message: Message, state: FSMContext, til: str):

    if await state.get_state() is None:
        await message.answer(t(til, "bekor_yoq"))
        return

    await state.clear()

    talaba = await talaba_olish(message.from_user.id)

    if talaba:
        await message.answer(t(til, "bekor_ok"), reply_markup=talaba_menu(til))
    else:
        await message.answer(t(til, "bekor_ok_start"), reply_markup=ReplyKeyboardRemove())


@dp.message(Command("help"))
@dp.message(tugma("btn_yordam"))
async def yordam(message: Message, til: str):

    matn = t(til, "help")

    if admin_ekanmi(message.from_user.id):
        matn += (
            "\n\n👨‍💻 <b>Administrator:</b>\n"
            "/admin — admin paneli\n"
            "/find 12 — murojaatni raqam bo‘yicha topish\n"
            "/export — barcha murojaatlar (CSV)\n"
            "/broadcast — barcha talabalarga e’lon\n"
            "/backup — bazaning zaxira nusxasi"
        )

    await message.answer(matn)


@dp.message(Command("menu"))
async def menyu(message: Message, til: str):

    talaba = await talaba_olish(message.from_user.id)

    if not talaba:
        await message.answer(t(til, "avval_royxat"))
        return

    await message.answer(t(til, "menyu"), reply_markup=talaba_menu(til))


@dp.message(Command("profile"))
@dp.message(tugma("btn_profil"))
async def profil(message: Message, til: str):

    talaba = await talaba_olish(message.from_user.id)

    if not talaba:
        await message.answer(t(til, "avval_royxat"))
        return

    if (talaba.get("turi") or TALABA) != TALABA:
        await message.answer(
            t(
                til, "profil_xodim",
                fish=esc(talaba["fish"]),
                tel=esc(talaba["telefon"]),
                rol=esc(rol_nomi(talaba["turi"], til)),
                bolim=esc(talaba["fakultet"] or "—"),
                lav=esc(talaba["kurs"] or "—"),
                til=TIL_NOMI.get(til, til),
            ),
            reply_markup=profil_klaviaturasi(til)
        )
        return

    await message.answer(
        t(
            til, "profil",
            fish=esc(talaba["fish"]),
            tel=esc(talaba["telefon"]),
            fak=esc(fakultet_nomi(talaba["fakultet"], til)),
            kurs=esc(kurs_nomi(talaba["kurs"], til)),
            yon=esc(yonalish_nomi(talaba["yonalish"]) if talaba["yonalish"] else "—"),
            guruh=esc(talaba["guruh"]),
            til=TIL_NOMI.get(til, til),
        ),
        reply_markup=profil_klaviaturasi(til)
    )


@dp.callback_query(F.data == "profil:tahrirlash")
async def profil_tahrirlash(call: CallbackQuery, state: FSMContext, til: str):

    talaba = await talaba_olish(call.from_user.id)

    if not talaba:
        await call.answer(t(til, "avval_royxat"), show_alert=True)
        return

    await state.clear()
    await state.update_data(telefon=talaba["telefon"])
    await state.set_state(TalabaHolati.fish)

    await call.answer()

    await yoz(
        call.from_user.id,
        t(til, "yangi_ism"),
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(Command("new"))
@dp.message(tugma("btn_yangi"))
async def murojaat_boshlash(message: Message, state: FSMContext, til: str):

    talaba = await talaba_olish(message.from_user.id)

    if not talaba:
        await message.answer(t(til, "avval_royxat"))
        return

    if await bugungi_soni(message.from_user.id) >= KUNLIK_LIMIT:
        await message.answer(t(til, "limit", n=KUNLIK_LIMIT))
        return

    await state.clear()
    await state.set_state(TalabaHolati.murojaat_toifa)

    await message.answer(
        t(til, "mavzu"),
        reply_markup=tanlash_klaviaturasi("toifa", toifalar(til))
    )


@dp.message(Command("myrequests"))
@dp.message(tugma("btn_meninglar"))
async def mening_murojaatlarim(message: Message, til: str):
    # 10 ta alohida xabar o'rniga BITTA xabar: ro'yxat + tugmalar.

    murojaatlar = await talaba_murojaatlari(message.from_user.id, limit=10)

    if not murojaatlar:
        await message.answer(t(til, "murojaat_yoq"))
        return

    qatorlar = [t(til, "royxat_sarlavha")]

    for m in murojaatlar:
        belgi = HOLAT_BELGI.get(m["holat"], "📄")
        qatorlar.append(
            f"{belgi} <b>#{m['id']}</b> · {esc(toifa_nomi(m['toifa'], til) or '—')}\n"
            f"     {esc(holat_nomi(m['holat'], til))} · {esc((m['sana'] or '')[:16])}"
        )

    qatorlar.append(t(til, "royxat_past"))

    await message.answer(
        "\n".join(qatorlar),
        reply_markup=royxat_klaviaturasi(murojaatlar)
    )


@dp.callback_query(F.data.startswith("korish:"))
async def murojaat_korish(call: CallbackQuery, til: str):

    try:
        murojaat_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer(t(til, "notogri"))
        return

    m = await murojaat_olish(murojaat_id)

    # Talaba faqat o'z murojaatini ko'ra oladi
    if not m or m["user_id"] != call.from_user.id:
        await call.answer(t(til, "topilmadi"), show_alert=True)
        return

    await call.answer()
    await yoz(
        call.from_user.id,
        sigadigan_karta(lambda x, n: talaba_karta(x, n, til), m)
    )


# =========================================================
# ADMIN PANEL
# =========================================================

@dp.message(Command("admin"))
async def admin_panel(message: Message):

    if not admin_ekanmi(message.from_user.id):
        await message.answer(t(await tl(message.from_user.id), "admin_yoq"))
        return

    await message.answer(
        "👨‍💻 <b>ADMINISTRATOR PANELI</b>\n\n"
        "Raqam bo‘yicha qidirish: /find 12\n\n"
        f"<i>Bot versiyasi: {VERSIYA}</i>",
        reply_markup=admin_menu()
    )


@dp.message(Command("find"))
async def murojaat_topish(message: Message, command: CommandObject):

    if not admin_ekanmi(message.from_user.id):
        await message.answer(t(await tl(message.from_user.id), "admin_yoq"))
        return

    arg = (command.args or "").strip().lstrip("#")

    if not arg.isdigit():
        await message.answer("Foydalanish: /find 12")
        return

    m = await murojaat_olish(int(arg))

    if not m:
        await message.answer("Bunday murojaat topilmadi.")
        return

    await admin_karta_yuborish(message.chat.id, m)


@dp.callback_query(F.data == "admin:statistika")
async def admin_statistika(call: CallbackQuery):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    await call.answer()

    s = await statistika()

    holat_soni = {r["holat"]: r["soni"] for r in s["holatlar"]}

    holatlar_matni = "\n".join(
        f"{HOLAT_BELGI[h]} {h}: {holat_soni.get(h, 0)}"
        for h in (YANGI, KORILMOQDA, JAVOB_BERILDI, YOPILGAN)
    )

    toifalar_matni = "\n".join(
        f"• {esc(r['toifa'] or '—')}: {r['soni']}"
        for r in s["toifalar"][:10]
    ) or "—"

    navbatda = sum(n.qsize() for n in _admin_navbat.values())

    await yoz(
        call.from_user.id,
        "📊 <b>STATISTIKA</b>\n\n"
        f"👥 Foydalanuvchilar: {s['talabalar']}\n"
        + "".join(
            f"   • {ROL_NOMI.get(r['turi'], r['turi'])}: {r['soni']}\n"
            for r in s["turlar"]
        ) +
        f"📨 Jami murojaatlar: {s['jami']}\n"
        f"📅 Bugun: {s['bugun']}\n"
        f"⏳ Adminlarga yuborish navbatida: {navbatda}\n\n"
        f"<b>Holatlar:</b>\n{holatlar_matni}\n\n"
        f"<b>Mavzular:</b>\n{toifalar_matni}"
    )


HOLAT_FILTR = {
    "yangi": YANGI,
    "korilmoqda": KORILMOQDA,
    "hammasi": None,
}


@dp.callback_query(F.data.startswith("admin:royxat:"))
async def admin_royxat(call: CallbackQuery):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    kod = call.data.split(":")[2]

    if kod not in HOLAT_FILTR:
        await call.answer("Noto‘g‘ri tanlov.")
        return

    murojaatlar = await murojaatlar_royxati(HOLAT_FILTR[kod], limit=10)

    await call.answer()

    if not murojaatlar:
        await yoz(call.from_user.id, "Murojaatlar yo‘q.")
        return

    for m in murojaatlar:
        await admin_karta_yuborish(call.from_user.id, m)
        await asyncio.sleep(0.1)


def csv_xavfsiz(qiymat):
    """Excelda '=' yoki '+' bilan boshlangan matn formula bo'lib ishlab
    ketmasligi uchun (CSV injection himoyasi)."""
    s = "" if qiymat is None else str(qiymat)

    if s and s[0] in "=+-@\t\r":
        return "'" + s

    return s


async def eksport_yuborish(chat_id):
    try:
        qatorlar = await eksport_qatorlari()

        buf = io.StringIO()
        yozuvchi = csv.writer(buf)

        yozuvchi.writerow([
            "ID", "Sana", "Mavzu", "Holat", "Kim", "Ism-familiya", "Telefon",
            "Fakultet / bo‘lim", "Kurs / lavozim", "Yo‘nalish", "Guruh",
            "Telegram ID", "Murojaat", "Javob"
        ])

        for m in qatorlar:
            yozuvchi.writerow([
                m["id"], m["sana"], csv_xavfsiz(m["toifa"]), m["holat"],
                ROL_NOMI.get(m.get("turi") or TALABA, m.get("turi")),
                csv_xavfsiz(m["fish"]), csv_xavfsiz(m["telefon"]),
                csv_xavfsiz(m["fakultet"]), csv_xavfsiz(m["kurs"]), csv_xavfsiz(m["yonalish"]),
                csv_xavfsiz(m["guruh"]), m["user_id"],
                csv_xavfsiz(m["matn"]), csv_xavfsiz(m["javob"])
            ])

        # utf-8-sig: Excel o'zbekcha harflarni to'g'ri ochishi uchun
        malumot = buf.getvalue().encode("utf-8-sig")

        if len(malumot) > ZAXIRA_MAX_HAJM:
            await xavfsiz_yuborish(
                chat_id,
                "❌ CSV fayl 50 MB dan katta, Telegram orqali yuborib bo‘lmaydi."
            )
            return

        await bot.send_document(
            chat_id,
            BufferedInputFile(
                malumot,
                filename=f"murojaatlar_{hozir().strftime('%Y%m%d_%H%M')}.csv"
            ),
            caption=f"📤 Jami: {len(qatorlar)} ta murojaat"
        )

    except Exception as e:
        logging.exception("Eksportda xato")
        await xavfsiz_yuborish(chat_id, f"❌ Eksportda xato: {esc(e)}")


@dp.message(Command("export"))
async def eksport_buyruq(message: Message):

    if not admin_ekanmi(message.from_user.id):
        await message.answer(t(await tl(message.from_user.id), "admin_yoq"))
        return

    await message.answer("⏳ Tayyorlanmoqda...")
    await eksport_yuborish(message.chat.id)


@dp.callback_query(F.data == "admin:eksport")
async def eksport_tugma(call: CallbackQuery):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    await call.answer("Tayyorlanmoqda...")
    await eksport_yuborish(call.from_user.id)


async def zaxira_yuborish(chat_id):
    # SQLite backup API: yozuvlar davom etayotgan bo'lsa ham
    # to'liq va buzilmagan nusxa olinadi.
    vaqtinchalik = f"{DB_PATH}.zaxira_{int(time.time())}"

    try:
        async with aiosqlite.connect(vaqtinchalik) as nusxa:
            await db.backup(nusxa)

        hajm = os.path.getsize(vaqtinchalik)

        if hajm > ZAXIRA_MAX_HAJM:
            await xavfsiz_yuborish(
                chat_id,
                f"❌ Baza hajmi {hajm // (1024 * 1024)} MB. Telegram 50 MB dan "
                "katta faylni yubormaydi. Railway Volume'dan to‘g‘ridan-to‘g‘ri oling."
            )
            return

        await bot.send_document(
            chat_id,
            FSInputFile(
                vaqtinchalik,
                filename=f"zaxira_{hozir().strftime('%Y%m%d_%H%M')}.db"
            ),
            caption=f"💾 Baza zaxira nusxasi ({hajm // 1024} KB)"
        )

    except Exception as e:
        logging.exception("Zaxira nusxada xato")
        await xavfsiz_yuborish(chat_id, f"❌ Zaxira nusxada xato: {esc(e)}")

    finally:
        try:
            os.remove(vaqtinchalik)
        except OSError:
            pass


@dp.message(Command("backup"))
async def zaxira_buyruq(message: Message):

    if not admin_ekanmi(message.from_user.id):
        await message.answer(t(await tl(message.from_user.id), "admin_yoq"))
        return

    await message.answer("⏳ Tayyorlanmoqda...")
    await zaxira_yuborish(message.chat.id)


@dp.callback_query(F.data == "admin:backup")
async def zaxira_tugma(call: CallbackQuery):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    await call.answer("Tayyorlanmoqda...")
    await zaxira_yuborish(call.from_user.id)


@dp.callback_query(F.data.startswith("holat:"))
async def holat_ozgartirish(call: CallbackQuery):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    try:
        _, id_matn, kod = call.data.split(":")
        murojaat_id = int(id_matn)
    except ValueError:
        await call.answer("Noto‘g‘ri so‘rov.")
        return

    m = await murojaat_olish(murojaat_id)

    if not m:
        await call.answer("Murojaat topilmadi.", show_alert=True)
        return

    if kod == "k":
        # Shartli UPDATE: ikki admin bir vaqtda bossa ham faqat bittasi o'tadi
        if not await korilmoqda_belgilash(murojaat_id):
            await call.answer(f"Holati: {m['holat']}", show_alert=True)
            return

        await call.answer("Ko‘rilmoqda deb belgilandi.")

        await xavfsiz_yuborish(
            m["user_id"],
            t(await tl(m["user_id"]), "korilmoqda_xabar", id=murojaat_id)
        )

    elif kod == "y":
        if not await yopish(murojaat_id):
            await call.answer("Murojaat allaqachon yopilgan.", show_alert=True)
            return

        await call.answer("Murojaat yopildi.")

        await xavfsiz_yuborish(
            m["user_id"],
            t(await tl(m["user_id"]), "yopildi_xabar", id=murojaat_id)
        )

    else:
        await call.answer("Noto‘g‘ri so‘rov.")
        return

    # Bosilgan kartani darhol, boshqa adminlardagilarni fonda yangilaymiz
    yangilangan = await murojaat_olish(murojaat_id)

    istisno = None
    if yangilangan and call.message is not None:
        istisno = (call.message.chat.id, call.message.message_id)
        await karta_tahrirlash(istisno[0], istisno[1], yangilangan)

    fonda(kartalarni_yangilash(murojaat_id, istisno=istisno))


@dp.callback_query(F.data.startswith("javob:"))
async def javob_boshlash(call: CallbackQuery, state: FSMContext):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    try:
        murojaat_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Murojaat raqami noto‘g‘ri.")
        return

    murojaat = await murojaat_olish(murojaat_id)

    if not murojaat:
        await call.answer("Murojaat topilmadi.", show_alert=True)
        return

    if murojaat["holat"] == YOPILGAN:
        await call.answer(
            "Bu murojaat yopilgan. Unga javob yozib bo‘lmaydi.",
            show_alert=True
        )
        return

    await state.clear()
    await state.update_data(murojaat_id=murojaat_id)
    await state.set_state(AdminHolati.javob_kutilmoqda)

    await call.answer()

    await yoz(
        call.from_user.id,
        f"✍️ <b>#{murojaat_id} murojaatiga javob yozing.</b>\n\n"
        f"<i>{esc(kesish(murojaat['matn'], 200))}</i>\n\n"
        "Keyingi yuborgan matningiz talabaga javob bo‘lib ketadi.\n"
        "Bekor qilish: pastdagi tugma, /cancel yoki istalgan menyu tugmasi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Javobni bekor qilish",
                callback_data=f"jbekor:{murojaat_id}"
            )
        ]])
    )


@dp.callback_query(F.data.startswith("jbekor:"))
async def javob_bekor(call: CallbackQuery, state: FSMContext):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    if await state.get_state() == AdminHolati.javob_kutilmoqda.state:
        await state.clear()
        await call.answer("Javob bekor qilindi.")
        await yoz(call.from_user.id, "ℹ️ Javob yozish bekor qilindi.")
    else:
        await call.answer("Faol javob yo‘q.")


# ---------- E'lon (hammaga xabar) ----------

async def elon_boshlash_umumiy(chat_id, state):
    await state.clear()
    await state.set_state(AdminHolati.elon_kutilmoqda)

    await xavfsiz_yuborish(
        chat_id,
        "📢 Barcha talabalarga yuboriladigan e’lon matnini yozing.\n"
        "Bekor qilish: /cancel"
    )


@dp.message(Command("broadcast"))
async def elon_buyruq(message: Message, state: FSMContext):

    if not admin_ekanmi(message.from_user.id):
        await message.answer(t(await tl(message.from_user.id), "admin_yoq"))
        return

    await elon_boshlash_umumiy(message.chat.id, state)


@dp.callback_query(F.data == "admin:elon")
async def elon_tugma(call: CallbackQuery, state: FSMContext):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    await call.answer()
    await elon_boshlash_umumiy(call.from_user.id, state)


@dp.message(AdminHolati.elon_kutilmoqda, F.text)
async def elon_matn_qabul(message: Message, state: FSMContext):

    if not admin_ekanmi(message.from_user.id):
        await state.clear()
        return

    matn = toza_kop_qator(message.text)

    if len(matn) < 3:
        await message.answer("E’lon matni juda qisqa.")
        return

    if len(esc(matn)) > 3500:
        await message.answer("E’lon 3500 belgidan oshmasligi kerak.")
        return

    await state.update_data(elon_matni=matn)
    await state.set_state(AdminHolati.elon_tasdiq)

    await message.answer(
        "📢 <b>E’lon ko‘rinishi:</b>\n\n"
        f"{esc(matn)}\n\n"
        "Barcha talabalarga yuboraymi?",
        reply_markup=elon_tasdiq_klaviaturasi()
    )


@dp.message(AdminHolati.elon_kutilmoqda)
async def elon_faqat_matn(message: Message):
    await message.answer("E’lonni matn ko‘rinishida yuboring.")


@dp.message(AdminHolati.elon_tasdiq)
async def elon_tugma_kut(message: Message):
    await message.answer("Tugmalardan birini bosing yoki /cancel yuboring.")


@dp.callback_query(F.data == "elon:bekor")
async def elon_bekor(call: CallbackQuery, state: FSMContext):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    if await state.get_state() in (
        AdminHolati.elon_kutilmoqda.state,
        AdminHolati.elon_tasdiq.state,
    ):
        await state.clear()

    await call.answer()
    await yoz(call.from_user.id, "E’lon bekor qilindi.")


async def elon_tarqatish(admin_chat_id, matn):
    ommaviy_rejim.set(True)

    idlar = await barcha_talaba_idlari()
    tillar = await barcha_tillar()

    yuborildi = 0
    yetmadi = 0
    boshlandi = time.monotonic()

    for user_id in idlar:
        # Tezlik middleware'da cheklanadi; talabalarga javoblar ustuvor
        ok = await xavfsiz_yuborish(
            user_id,
            t(tillar.get(user_id, "uz"), "elon", matn=esc(matn))
        )

        if ok:
            yuborildi += 1
        else:
            yetmadi += 1

    davomiylik = int(time.monotonic() - boshlandi)

    await xavfsiz_yuborish(
        admin_chat_id,
        "📢 E’lon yuborildi.\n\n"
        f"✅ Yetkazildi: {yuborildi}\n"
        f"❌ Yetkazilmadi: {yetmadi}\n"
        f"⏱ {davomiylik} soniya"
    )


@dp.callback_query(F.data == "elon:yuborish")
async def elon_yuborish(call: CallbackQuery, state: FSMContext):

    if not admin_ekanmi(call.from_user.id):
        await call.answer("Ruxsat yo‘q.", show_alert=True)
        return

    if await state.get_state() != AdminHolati.elon_tasdiq.state:
        await call.answer("Faol e’lon topilmadi.", show_alert=True)
        return

    data = await state.get_data()
    matn = data.get("elon_matni")

    await state.clear()

    if not matn:
        await call.answer("E’lon matni topilmadi.", show_alert=True)
        return

    await call.answer()

    soni = len(await barcha_talaba_idlari())
    taxmin = max(1, int(soni / UMUMIY_TEZLIK))

    await yoz(
        call.from_user.id,
        f"📢 Yuborish boshlandi ({soni} ta talaba, taxminan {taxmin} soniya).\n"
        "Tugagach xabar beraman."
    )

    fonda(elon_tarqatish(call.from_user.id, matn))


# =========================================================
# RO'YXATDAN O'TISH
# =========================================================

@dp.message(TalabaHolati.telefon, F.contact)
async def telefon_qabul(message: Message, state: FSMContext, til: str):

    if message.contact.user_id != message.from_user.id:
        await message.answer(t(til, "ozingiz_tel"))
        return

    telefon = message.contact.phone_number or ""
    if telefon and not telefon.startswith("+"):
        telefon = "+" + telefon

    await state.update_data(telefon=telefon)
    await state.set_state(TalabaHolati.fish)

    await message.answer(t(til, "ism_kiriting"), reply_markup=ReplyKeyboardRemove())


@dp.message(TalabaHolati.telefon)
async def telefon_xato(message: Message, til: str):
    await message.answer(t(til, "tel_tugma"), reply_markup=telefon_klaviaturasi(til))


@dp.message(TalabaHolati.fish, F.text)
async def fish_qabul(message: Message, state: FSMContext, til: str):

    fish = toza_matn(message.text)

    if len(fish) < 5:
        await message.answer(t(til, "ism_toliq"))
        return

    if len(fish) > 100:
        await message.answer(t(til, "ism_uzun"))
        return

    await state.update_data(fish=fish)
    await state.set_state(TalabaHolati.fakultet)

    await message.answer(
        t(til, "fakultet_tanlang"),
        reply_markup=fakultet_klaviaturasi(til)
    )


@dp.message(TalabaHolati.fish)
async def fish_faqat_matn(message: Message, til: str):
    await message.answer(t(til, "ism_matn"))


async def kurs_sorash(chat_id, state):
    til = await tl(chat_id)
    data = await state.get_data()
    fakultet = data.get("fakultet")
    royxat = kurslar(fakultet)

    # Bo'limda bitta kurs bo'lsa (masalan magistratura masofaviy) — so'ramaymiz
    if fakultet in BAZA and len(royxat) == 1:
        await state.update_data(kurs=royxat[0])
        await yonalish_sorash(chat_id, state)
        return

    await state.set_state(TalabaHolati.kurs)
    await xavfsiz_yuborish(
        chat_id,
        t(til, "kurs_tanlang"),
        reply_markup=tanlash_klaviaturasi(
            "kurs", [kurs_nomi(k, til) for k in royxat],
            ustun=2, boshqa=True, til=til
        )
    )


async def yonalish_sorash(chat_id, state):
    til = await tl(chat_id)
    data = await state.get_data()
    fakultet, kurs = data.get("fakultet"), data.get("kurs")

    if not guruh_variantlari(fakultet, kurs):
        # Bu fakultet/kurs uchun ro'yxat yo'q — guruh qo'lda yoziladi
        await state.update_data(yonalish="")
        await state.set_state(TalabaHolati.guruh_qolda)
        await xavfsiz_yuborish(chat_id, t(til, "guruh_yozing"))
        return

    if not yonalish_bosqichi_kerakmi(fakultet, kurs):
        await state.update_data(yonalish=None)
        await guruhlarni_korsatish(chat_id, state)
        return

    y = yonalishlar(fakultet, kurs)
    nomlar = [f"{yonalish_nomi(kod)} ({len(g)})" for kod, g in y.items()]

    await state.set_state(TalabaHolati.yonalish)
    await xavfsiz_yuborish(
        chat_id,
        t(til, "yonalish_tanlang"),
        reply_markup=tanlash_klaviaturasi(
            "yon", nomlar, ustun=1 if YONALISH_NOMLARI else 3,
            boshqa=True, til=til
        )
    )


async def guruhlarni_korsatish(chat_id, state):
    til = await tl(chat_id)
    data = await state.get_data()
    guruhlar = guruh_variantlari(
        data.get("fakultet"), data.get("kurs"), data.get("yonalish")
    )

    if not guruhlar:
        await state.set_state(TalabaHolati.guruh_qolda)
        await xavfsiz_yuborish(chat_id, t(til, "guruh_yozing"))
        return

    await state.set_state(TalabaHolati.guruh)
    await xavfsiz_yuborish(
        chat_id,
        t(til, "guruh_tanlang"),
        reply_markup=tanlash_klaviaturasi(
            "guruh", guruhlar, ustun=3, boshqa=True, til=til
        )
    )


@dp.callback_query(F.data.startswith("fakultet:"))
async def fakultet_tanlash(call: CallbackQuery, state: FSMContext, til: str):

    if await state.get_state() != TalabaHolati.fakultet.state:
        await call.answer(t(til, "faol_emas"))
        return

    index = indeks_olish(call.data, len(FAKULTETLAR) + 1)

    if index is None:
        await call.answer(t(til, "notogri_tanlov"))
        return

    await call.answer()

    if index == len(FAKULTETLAR):
        await state.set_state(TalabaHolati.fakultet_qolda)
        await yoz(call.from_user.id, t(til, "fakultet_yozing"))
        return

    await state.update_data(fakultet=FAKULTETLAR[index], kurs=None, yonalish=None)
    await kurs_sorash(call.from_user.id, state)


@dp.message(TalabaHolati.fakultet_qolda, F.text)
async def fakultet_qolda(message: Message, state: FSMContext, til: str):

    fakultet = toza_matn(message.text)

    if len(fakultet) < 2 or len(fakultet) > 100:
        await message.answer(t(til, "fakultet_xato"))
        return

    await state.update_data(fakultet=fakultet, kurs=None, yonalish="")
    await kurs_sorash(message.chat.id, state)


@dp.callback_query(F.data.startswith("kurs:"))
async def kurs_tanlash(call: CallbackQuery, state: FSMContext, til: str):

    if await state.get_state() != TalabaHolati.kurs.state:
        await call.answer(t(til, "faol_emas"))
        return

    data = await state.get_data()
    royxat = kurslar(data.get("fakultet"))
    index = indeks_olish(call.data, len(royxat) + 1)

    if index is None:
        await call.answer(t(til, "notogri_tanlov"))
        return

    await call.answer()

    if index == len(royxat):
        await state.set_state(TalabaHolati.kurs_qolda)
        await yoz(call.from_user.id, t(til, "kurs_yozing"))
        return

    await state.update_data(kurs=royxat[index], yonalish=None)
    await yonalish_sorash(call.from_user.id, state)


@dp.message(TalabaHolati.kurs_qolda, F.text)
async def kurs_qolda(message: Message, state: FSMContext, til: str):

    kurs = toza_matn(message.text)

    if len(kurs) < 1 or len(kurs) > 50:
        await message.answer(t(til, "kurs_xato"))
        return

    # Kurs qo'lda kiritilsa, guruh ham qo'lda olinadi.
    await state.update_data(kurs=kurs, yonalish="")
    await state.set_state(TalabaHolati.guruh_qolda)
    await message.answer(t(til, "guruh_yozing"))


@dp.callback_query(F.data.startswith("yon:"))
async def yonalish_tanlash(call: CallbackQuery, state: FSMContext, til: str):

    if await state.get_state() != TalabaHolati.yonalish.state:
        await call.answer(t(til, "faol_emas"))
        return

    data = await state.get_data()
    kodlar = list(yonalishlar(data.get("fakultet"), data.get("kurs")))
    index = indeks_olish(call.data, len(kodlar) + 1)

    if index is None:
        await call.answer(t(til, "notogri_tanlov"))
        return

    await call.answer()

    if index == len(kodlar):
        await state.update_data(yonalish="")
        await state.set_state(TalabaHolati.guruh_qolda)
        await yoz(call.from_user.id, t(til, "guruh_yozing"))
        return

    await state.update_data(yonalish=kodlar[index])
    await guruhlarni_korsatish(call.from_user.id, state)


@dp.callback_query(F.data.startswith("guruh:"))
async def guruh_tanlash(call: CallbackQuery, state: FSMContext, til: str):

    if await state.get_state() != TalabaHolati.guruh.state:
        await call.answer(t(til, "faol_emas"))
        return

    data = await state.get_data()
    guruhlar = guruh_variantlari(
        data.get("fakultet"), data.get("kurs"), data.get("yonalish")
    )
    index = indeks_olish(call.data, len(guruhlar) + 1)

    if index is None:
        await call.answer(t(til, "notogri_tanlov"))
        return

    await call.answer()

    if index == len(guruhlar):
        await state.set_state(TalabaHolati.guruh_qolda)
        await yoz(call.from_user.id, t(til, "guruh_yozing"))
        return

    await royxatdan_otkazish(call.from_user.id, state, guruhlar[index])


@dp.message(TalabaHolati.guruh_qolda, F.text)
async def guruh_qolda(message: Message, state: FSMContext, til: str):

    guruh = toza_matn(message.text)

    if len(guruh) < 2 or len(guruh) > 50:
        await message.answer(t(til, "guruh_xato"))
        return

    await royxatdan_otkazish(message.from_user.id, state, guruh.upper())


# ---------- Talaba bo'lmaganlar: o'qituvchi, xodim, boshqa ----------

@dp.callback_query(F.data.startswith("rol:"))
async def rol_tanlash(call: CallbackQuery, state: FSMContext, til: str):

    holat = await state.get_state()
    if holat not in (TalabaHolati.fakultet.state, TalabaHolati.rol.state):
        await call.answer(t(til, "faol_emas"))
        return

    kod = call.data.split(":", 1)[1]
    await call.answer()

    if kod == "menu":
        await state.set_state(TalabaHolati.rol)
        await yoz(call.from_user.id, t(til, "rol_tanlang"),
                  reply_markup=rol_klaviaturasi(til))
        return

    if kod == "orqaga":
        await state.set_state(TalabaHolati.fakultet)
        await yoz(call.from_user.id, t(til, "fakultet_tanlang"),
                  reply_markup=fakultet_klaviaturasi(til))
        return

    if kod not in ROLLAR:
        return

    await state.update_data(turi=kod, bolim="", lavozim="")

    if kod == "boshqa":
        # Abituriyent, ota-ona va h.k.: faqat kimligi so'raladi
        await state.set_state(TalabaHolati.xodim_lavozim)
        await yoz(call.from_user.id, t(til, "boshqa_kim"))
        return

    await state.set_state(TalabaHolati.xodim_bolim)
    await yoz(
        call.from_user.id,
        t(til, "kafedra_yozing" if kod == "oqituvchi" else "bolim_yozing")
    )


def qisqa_matn_togrimi(matn):
    return 2 <= len(matn) <= 100


@dp.message(TalabaHolati.xodim_bolim, F.text)
async def xodim_bolim_qabul(message: Message, state: FSMContext, til: str):

    bolim = toza_matn(message.text)
    if not qisqa_matn_togrimi(bolim):
        await message.answer(t(til, "qisqa_matn_xato"))
        return

    await state.update_data(bolim=bolim)
    await state.set_state(TalabaHolati.xodim_lavozim)
    await message.answer(t(til, "lavozim_yozing"))


@dp.message(TalabaHolati.xodim_lavozim, F.text)
async def xodim_lavozim_qabul(message: Message, state: FSMContext, til: str):

    lavozim = toza_matn(message.text)
    if not qisqa_matn_togrimi(lavozim):
        await message.answer(t(til, "qisqa_matn_xato"))
        return

    await state.update_data(lavozim=lavozim)
    await xodimni_royxatdan_otkazish(message.from_user.id, state)


async def xodimni_royxatdan_otkazish(user_id, state):

    til = await tl(user_id)
    data = await state.get_data()
    turi = data.get("turi")

    if turi not in ROLLAR or not all(data.get(k) for k in ("telefon", "fish")):
        await state.clear()
        await xavfsiz_yuborish(user_id, t(til, "toliq_emas"))
        return

    avval_bor = await talaba_olish(user_id) is not None

    await talaba_saqlash(
        user_id=user_id,
        telefon=data["telefon"],
        fish=data["fish"],
        fakultet=data.get("bolim") or "",
        kurs=data.get("lavozim") or "",
        yonalish="",
        guruh="",
        turi=turi,
    )

    await state.clear()

    sarlavha = t(til, "yangilandi" if avval_bor else "royxatdan_otdi")

    await xavfsiz_yuborish(
        user_id,
        f"{sarlavha}\n\n" + t(
            til, "reg_info_xodim",
            fish=esc(data["fish"]),
            rol=esc(rol_nomi(turi, til)),
            bolim=esc(data.get("bolim") or "—"),
            lav=esc(data.get("lavozim") or "—"),
        ),
        reply_markup=talaba_menu(til)
    )


@dp.message(TalabaHolati.rol)
@dp.message(TalabaHolati.fakultet)
@dp.message(TalabaHolati.kurs)
@dp.message(TalabaHolati.yonalish)
@dp.message(TalabaHolati.guruh)
async def tugma_kutilmoqda(message: Message, til: str):
    await message.answer(t(til, "tugma_tanlang"))


@dp.message(TalabaHolati.xodim_bolim)
@dp.message(TalabaHolati.xodim_lavozim)
@dp.message(TalabaHolati.fakultet_qolda)
@dp.message(TalabaHolati.kurs_qolda)
@dp.message(TalabaHolati.guruh_qolda)
async def qolda_faqat_matn(message: Message, til: str):
    await message.answer(t(til, "matn_yozing"))


async def royxatdan_otkazish(user_id, state, guruh):

    til = await tl(user_id)
    data = await state.get_data()

    if not all(data.get(k) for k in ("telefon", "fish", "fakultet", "kurs")):
        await state.clear()
        await xavfsiz_yuborish(user_id, t(til, "toliq_emas"))
        return

    avval_bor = await talaba_olish(user_id) is not None

    yon = data.get("yonalish") or (
        yonalish_kodi(guruh) if data["fakultet"] in BAZA else ""
    )

    await talaba_saqlash(
        user_id=user_id,
        telefon=data["telefon"],
        fish=data["fish"],
        fakultet=data["fakultet"],
        kurs=data["kurs"],
        yonalish=yon,
        guruh=guruh
    )

    await state.clear()

    sarlavha = t(til, "yangilandi" if avval_bor else "royxatdan_otdi")

    await xavfsiz_yuborish(
        user_id,
        f"{sarlavha}\n\n" + t(
            til, "reg_info",
            fish=esc(data["fish"]),
            fak=esc(fakultet_nomi(data["fakultet"], til)),
            kurs=esc(kurs_nomi(data["kurs"], til)),
            yon=esc(yonalish_nomi(yon) if yon else "—"),
            guruh=esc(guruh),
        ),
        reply_markup=talaba_menu(til)
    )


# =========================================================
# MUROJAAT YUBORISH
# =========================================================

@dp.callback_query(F.data.startswith("toifa:"))
async def toifa_tanlash(call: CallbackQuery, state: FSMContext, til: str):

    if await state.get_state() != TalabaHolati.murojaat_toifa.state:
        await call.answer(t(til, "toifa_faol_emas"), show_alert=True)
        return

    index = indeks_olish(call.data, len(TOIFALAR))

    if index is None:
        await call.answer(t(til, "notogri_tanlov"))
        return

    # Bazaga o'zbekcha nom yoziladi (admin uchun), talabaga o'z tilida ko'rsatiladi
    await state.update_data(
        toifa=TOIFALAR[index],
        murojaat_matni=None,
        fayl_id=None,
        fayl_turi=None
    )
    await state.set_state(TalabaHolati.murojaat)

    await call.answer()
    await yoz(call.from_user.id, t(til, "batafsil"))


@dp.message(TalabaHolati.murojaat_toifa)
async def toifa_kutilmoqda(message: Message, til: str):
    await message.answer(t(til, "mavzu_tugmadan"))


@dp.message(TalabaHolati.murojaat, F.text)
async def murojaat_matn_qabul(message: Message, state: FSMContext, til: str):

    matn = toza_kop_qator(message.text)

    xato = murojaat_matni_xatosi(matn, til)
    if xato:
        await message.answer(xato)
        return

    await state.update_data(
        murojaat_matni=matn,
        fayl_id=None,
        fayl_turi=None
    )
    await state.set_state(TalabaHolati.murojaat_fayl)

    await message.answer(t(til, "fayl_biriktir"), reply_markup=fayl_klaviaturasi(til))


def fayl_malumoti(message: Message):
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.document:
        return message.document.file_id, "document"
    return None, None


@dp.message(TalabaHolati.murojaat, F.photo | F.document)
async def murojaat_fayl_bilan(message: Message, state: FSMContext, til: str):
    """Matn rasm/fayl izohida yozilgan bo'lsa — bir yo'la qabul qilamiz."""

    matn = toza_kop_qator(message.caption)

    if not matn:
        await message.answer(t(til, "avval_matn"))
        return

    xato = murojaat_matni_xatosi(matn, til)
    if xato:
        await message.answer(xato)
        return

    fayl_id, fayl_turi = fayl_malumoti(message)

    await state.update_data(
        murojaat_matni=matn,
        fayl_id=fayl_id,
        fayl_turi=fayl_turi
    )

    if message.media_group_id:
        await message.answer(t(til, "albom"))

    await tasdiqlashni_korsatish(message.chat.id, state)


@dp.message(TalabaHolati.murojaat)
async def murojaat_faqat_matn(message: Message, til: str):
    await message.answer(t(til, "faqat_matn"))


async def tasdiqlashni_korsatish(chat_id, state):

    til = await tl(chat_id)
    data = await state.get_data()

    await state.set_state(TalabaHolati.murojaat_tasdiqlash)

    ilova = {
        "photo": t(til, "ilova_rasm"),
        "document": t(til, "ilova_fayl"),
    }.get(data.get("fayl_turi"), t(til, "ilova_yoq"))

    await xavfsiz_yuborish(
        chat_id,
        t(
            til, "preview",
            toifa=esc(toifa_nomi(data.get("toifa"), til)),
            ilova=ilova,
            matn=esc(data.get("murojaat_matni")),
        ),
        reply_markup=murojaat_tasdiqlash(til)
    )


@dp.message(TalabaHolati.murojaat_fayl, F.photo | F.document)
async def fayl_qabul(message: Message, state: FSMContext, til: str):

    fayl_id, fayl_turi = fayl_malumoti(message)
    yangilash = {"fayl_id": fayl_id, "fayl_turi": fayl_turi}

    # Fayl izohida qo'shimcha matn bo'lsa, murojaatga qo'shamiz
    izoh = toza_kop_qator(message.caption)
    if izoh:
        data = await state.get_data()
        birga = toza_kop_qator(f"{data.get('murojaat_matni') or ''}\n\n{izoh}")

        if murojaat_matni_xatosi(birga, til):
            await message.answer(t(til, "izoh_uzun"))
        else:
            yangilash["murojaat_matni"] = birga

    await state.update_data(**yangilash)

    if message.media_group_id:
        await message.answer(t(til, "albom"))

    await tasdiqlashni_korsatish(message.chat.id, state)


@dp.callback_query(F.data == "fayl:otkazish")
async def fayl_otkazish(call: CallbackQuery, state: FSMContext, til: str):

    if await state.get_state() != TalabaHolati.murojaat_fayl.state:
        await call.answer(t(til, "faol_emas"), show_alert=True)
        return

    await call.answer()
    await tasdiqlashni_korsatish(call.from_user.id, state)


@dp.message(TalabaHolati.murojaat_fayl)
async def fayl_xato(message: Message, til: str):
    await message.answer(t(til, "fayl_xato"))


@dp.message(TalabaHolati.murojaat_tasdiqlash)
async def tasdiqlash_kutilmoqda(message: Message, til: str):
    await message.answer(t(til, "tugmalardan"))


@dp.callback_query(F.data == "murojaat:tahrirlash")
async def murojaat_tahrirlash(call: CallbackQuery, state: FSMContext, til: str):

    if await state.get_state() != TalabaHolati.murojaat_tasdiqlash.state:
        await call.answer(t(til, "faol_m_yoq"), show_alert=True)
        return

    await state.set_state(TalabaHolati.murojaat)

    await call.answer()
    await yoz(call.from_user.id, t(til, "qayta_yoz"))


@dp.callback_query(F.data == "murojaat:bekor")
async def murojaat_bekor(call: CallbackQuery, state: FSMContext, til: str):

    # Faqat murojaat yozilayotgan bo'lsa bekor qilamiz. Eski xabardagi
    # tugma bosilsa, masalan profil tahririni buzib yubormaydi.
    if await state.get_state() not in MUROJAAT_HOLATLARI:
        await call.answer(t(til, "yakunlangan"))
        return

    await state.clear()

    await call.answer()
    await yoz(call.from_user.id, t(til, "bekor_m"), reply_markup=talaba_menu(til))


@dp.callback_query(F.data == "murojaat:yuborish")
async def murojaat_yuborish(call: CallbackQuery, state: FSMContext, til: str):

    user_id = call.from_user.id

    # Ikki marta bosilishidan himoya
    if user_id in _yuborilmoqda:
        await call.answer()
        return

    _yuborilmoqda.add(user_id)

    try:
        if await state.get_state() != TalabaHolati.murojaat_tasdiqlash.state:
            await call.answer(t(til, "tasdiq_yoq"), show_alert=True)
            return

        data = await state.get_data()

        matn = toza_kop_qator(data.get("murojaat_matni"))

        # Himoya: matn bo'lmasa, yuborilmaydi.
        if len(matn) < 10:
            await state.set_state(TalabaHolati.murojaat)
            await call.answer(t(til, "matn_kerak"), show_alert=True)
            return

        talaba = await talaba_olish(user_id)

        if not talaba:
            await state.clear()
            await call.answer(t(til, "profil_yoq"), show_alert=True)
            return

        if await bugungi_soni(user_id) >= KUNLIK_LIMIT:
            await state.clear()
            await call.answer(t(til, "kunlik_tugadi", n=KUNLIK_LIMIT), show_alert=True)
            return

        # Faqat tasdiqlangandan keyin bazaga yoziladi.
        murojaat_id = await murojaat_qoshish(
            user_id,
            matn,
            data.get("toifa") or "",
            data.get("fayl_id"),
            data.get("fayl_turi")
        )

        await state.clear()

    finally:
        _yuborilmoqda.discard(user_id)

    await call.answer(t(til, "yuborildi_toast"))

    # Adminlarga fonda, navbat bilan yuboriladi — talaba kutib qolmaydi
    adminlarga_xabar(murojaat_id)

    await yoz(
        user_id,
        t(til, "yuborildi", id=murojaat_id, btn=t(til, "btn_meninglar")),
        reply_markup=talaba_menu(til)
    )


# =========================================================
# ADMIN JAVOBI
# =========================================================

@dp.message(AdminHolati.javob_kutilmoqda, F.text)
async def admin_javob(message: Message, state: FSMContext):

    if not admin_ekanmi(message.from_user.id):
        await state.clear()
        return

    javob = toza_kop_qator(message.text)

    if len(javob) < 2:
        await message.answer("Javob matnini kiriting.")
        return

    if len(javob) > MAX_MATN:
        await message.answer(f"Javob {MAX_MATN} belgidan oshmasligi kerak.")
        return

    if len(esc(javob)) > 3800:
        await message.answer("Javobda maxsus belgilar juda ko‘p. Qisqartiring.")
        return

    data = await state.get_data()
    murojaat_id = data.get("murojaat_id")

    murojaat = await murojaat_olish(murojaat_id)

    if not murojaat:
        await message.answer("Murojaat topilmadi.")
        await state.clear()
        return

    # Avval bazaga saqlaymiz — talaba botni bloklagan bo'lsa ham javob yo'qolmaydi
    if not await murojaat_javoblash(murojaat_id, javob):
        await message.answer(
            "Bu murojaat yopilgan, javob saqlanmadi."
        )
        await state.clear()
        return

    await state.clear()

    ok = await xavfsiz_yuborish(
        murojaat["user_id"],
        t(await tl(murojaat["user_id"]), "javob_xabar",
          id=murojaat_id, javob=esc(javob))
    )

    if ok:
        await message.answer(f"✅ #{murojaat_id} murojaatiga javob yuborildi.")
    else:
        await message.answer(
            f"⚠️ #{murojaat_id} murojaatiga javob saqlandi, lekin talabaga "
            "yetkazilmadi (u botni bloklagan bo‘lishi mumkin). Talaba botga "
            "qaytsa, «Mening murojaatlarim» bo‘limida ko‘radi."
        )

    fonda(kartalarni_yangilash(murojaat_id))


@dp.message(AdminHolati.javob_kutilmoqda)
async def admin_javob_faqat_matn(message: Message):
    await message.answer("Javobni matn ko‘rinishida yuboring.")


# =========================================================
# NOMA'LUM XABARLAR (eng oxirida turishi shart)
# =========================================================

@dp.message()
async def noma_lum(message: Message, til: str):

    talaba = await talaba_olish(message.from_user.id)

    if talaba:
        await message.answer(t(til, "tushunmadim"), reply_markup=talaba_menu(til))
    else:
        await message.answer(t(til, "boshlash"))


# =========================================================
# ISHGA TUSHIRISH
# =========================================================

BUYRUQ_MATNLARI = {
    "uz": ["Botni boshlash", "Yordam", "Asosiy menyu", "Yangi murojaat",
           "Mening murojaatlarim", "Profilim", "Tilni o‘zgartirish",
           "Joriy amalni bekor qilish"],
    "ru": ["Запустить бота", "Помощь", "Главное меню", "Новое обращение",
           "Мои обращения", "Мой профиль", "Сменить язык",
           "Отменить текущее действие"],
    "en": ["Start the bot", "Help", "Main menu", "New request",
           "My requests", "My profile", "Change language",
           "Cancel the current action"],
}
BUYRUQLAR = ["start", "help", "menu", "new", "myrequests", "profile",
             "language", "cancel"]


def talaba_buyruqlari(til):
    return [
        BotCommand(command=c, description=d)
        for c, d in zip(BUYRUQLAR, BUYRUQ_MATNLARI[til])
    ]


ADMIN_BUYRUQLARI = talaba_buyruqlari("uz") + [
    BotCommand(command="admin", description="Administrator paneli"),
    BotCommand(command="find", description="Murojaatni raqam bo‘yicha topish"),
    BotCommand(command="export", description="CSV eksport"),
    BotCommand(command="broadcast", description="Hammaga e’lon"),
    BotCommand(command="backup", description="Zaxira nusxa"),
]


async def buyruqlarni_ornatish():
    # Telegram menyusi foydalanuvchi ilovasining tiliga qarab ko'rsatiladi
    await bot.set_my_commands(talaba_buyruqlari("uz"))
    await bot.set_my_commands(talaba_buyruqlari("ru"), language_code="ru")
    await bot.set_my_commands(talaba_buyruqlari("en"), language_code="en")

    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(
                ADMIN_BUYRUQLARI,
                scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            # Admin hali botga /start bosmagan bo'lishi mumkin
            logging.warning("Admin %s uchun buyruqlar o‘rnatilmadi", admin_id)


async def main():

    await init_db()

    try:
        # Webhook bo'lsa o'chiramiz. Bot o'chib turganda kelgan xabarlar
        # SAQLANADI va qayta ishga tushganda javob beriladi.
        await bot.delete_webhook(drop_pending_updates=False)

        await buyruqlarni_ornatish()
        await admin_ishchilarni_boshlash()

        logging.info("TDIU murojaatlar boti ishga tushdi. Versiya: %s", VERSIYA)

        polling_sozlama = {}
        if "tasks_concurrency_limit" in inspect.signature(dp.start_polling).parameters:
            # Bir vaqtda juda ko'p update kelsa, xotira to'lib ketmasligi uchun
            polling_sozlama["tasks_concurrency_limit"] = MAX_PARALLEL

        # Tarmoq uzilishlarida aiogram o'zi qayta ulanadi.
        # SIGTERM (Railway qayta deploy) kelganda toza to'xtaydi.
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            **polling_sozlama
        )

    finally:
        await admin_ishchilarni_toxtatish()

        for vazifa in list(_fon_vazifalar):
            vazifa.cancel()
        await asyncio.gather(*_fon_vazifalar, return_exceptions=True)

        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
