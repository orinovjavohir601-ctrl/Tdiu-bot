import asyncio
import contextvars
import csv
import inspect
import io
import json
import logging
import os
import random
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

FACULTIES = [
    "Raqamli iqtisodiyot",
    "Bank ishi",
    "Moliya",
    "Buxgalteriya hisobi",
    "Iqtisodiyot",
    "Menejment",
    "Marketing",
    "Boshqa"
]

COURSES = [
    "1-kurs",
    "2-kurs",
    "3-kurs",
    "4-kurs",
    "Boshqa"
]

# (fakultet, kurs) -> guruhlar ro'yxati. Bo'sh bo'lsa, guruh qo'lda yoziladi.
# Misol:
# GROUPS = {
#     ("Moliya", "1-kurs"): ["M-101", "M-102"],
# }
GROUPS = {}

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

BTN_YANGI = "✉️ Murojaat yuborish"
BTN_MENINGLAR = "📂 Mening murojaatlarim"
BTN_PROFIL = "👤 Mening ma'lumotlarim"
BTN_YORDAM = "ℹ️ Yordam"


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
    guruh = State()
    guruh_qolda = State()
    murojaat_toifa = State()
    murojaat = State()
    murojaat_fayl = State()
    murojaat_tasdiqlash = State()


class AdminHolati(StatesGroup):
    javob_kutilmoqda = State()
    elon_kutilmoqda = State()
    elon_tasdiq = State()


MUROJAAT_HOLATLARI = {
    TalabaHolati.murojaat_toifa.state,
    TalabaHolati.murojaat.state,
    TalabaHolati.murojaat_fayl.state,
    TalabaHolati.murojaat_tasdiqlash.state,
}


# =========================================================
# KLAVIATURALAR
# =========================================================

def telefon_klaviaturasi():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text="📱 Telefon raqamni yuborish",
                    request_contact=True
                )
            ]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def talaba_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_YANGI)],
            [
                KeyboardButton(text=BTN_MENINGLAR),
                KeyboardButton(text=BTN_PROFIL)
            ],
            [KeyboardButton(text=BTN_YORDAM)]
        ],
        resize_keyboard=True
    )


def tanlash_klaviaturasi(prefix, royxat):
    buttons = []

    for i, nom in enumerate(royxat):
        buttons.append([
            InlineKeyboardButton(
                text=nom,
                callback_data=f"{prefix}:{i}"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def fayl_klaviaturasi():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➡️ Fayl biriktirmasdan davom etish",
                    callback_data="fayl:otkazish"
                )
            ]
        ]
    )


def murojaat_tasdiqlash():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Yuborish",
                    callback_data="murojaat:yuborish"
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Tahrirlash",
                    callback_data="murojaat:tahrirlash"
                ),
                InlineKeyboardButton(
                    text="❌ Bekor qilish",
                    callback_data="murojaat:bekor"
                )
            ]
        ]
    )


def profil_klaviaturasi():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✏️ Ma’lumotlarni tahrirlash",
                    callback_data="profil:tahrirlash"
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
           t.fish, t.telefon, t.fakultet, t.kurs, t.guruh
    FROM murojaatlar m
    LEFT JOIN talabalar t ON t.user_id = m.user_id
"""


async def talaba_saqlash(user_id, telefon, fish, fakultet, kurs, guruh):
    await _yoz("""
        INSERT INTO talabalar
        (user_id, telefon, fish, fakultet, kurs, guruh)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            telefon=excluded.telefon,
            fish=excluded.fish,
            fakultet=excluded.fakultet,
            kurs=excluded.kurs,
            guruh=excluded.guruh
    """, (user_id, telefon, fish, fakultet, kurs, guruh))


async def talaba_olish(user_id):
    return await _bitta("""
        SELECT user_id, telefon, fish, fakultet, kurs, guruh
        FROM talabalar
        WHERE user_id=?
    """, (user_id,))


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

WELCOME = """
<b>Assalomu alaykum!</b>

Toshkent davlat iqtisodiyot universiteti talabalari uchun
murojaatlar botiga xush kelibsiz!

Ushbu bot orqali siz:

• Universitetga oid savol yuborishingiz;
• Taklif va muammolaringizni bildirishingiz;
• Murojaatlaringiz holatini kuzatishingiz;
• Administrator javobini olishingiz mumkin.

Botdan foydalanish uchun ro‘yxatdan o‘ting.

<b>Eslatma:</b> murojaatingizni yozib, tasdiqlamaguningizcha
u administratorlarga yuborilmaydi.
"""


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


def murojaat_matni_xatosi(matn):
    """Murojaat matnini tekshiradi. Xato bo'lsa xabar matni, aks holda None."""
    if len(matn) < 10:
        return "Murojaat juda qisqa. Kamida 10 ta belgi yozing."

    if len(matn) > MAX_MATN:
        return f"Murojaat {MAX_MATN} belgidan oshmasligi kerak (hozir {len(matn)})."

    if len(esc(matn)) > 3300:
        return (
            "Matnda maxsus belgilar (&lt;, &gt;, &amp;) juda ko‘p. "
            "Iltimos, qisqaroq yozing."
        )

    return None


def talaba_karta(m, n=None):
    belgi = HOLAT_BELGI.get(m["holat"], "📄")
    ilova = "\n📎 Ilova: bor" if m["fayl_id"] else ""
    javob = m["javob"] or "Hozircha javob yo‘q."

    matn_n = min(1000, n) if n else 1000
    javob_n = min(1200, n) if n else 1200

    return (
        f"📄 <b>Murojaat #{m['id']}</b>\n"
        f"🏷 {esc(m['toifa'] or '—')}\n"
        f"{belgi} Holati: {esc(m['holat'])}\n"
        f"🕒 {esc(m['sana'])}{ilova}\n\n"
        f"<b>Murojaat:</b>\n{esc(kesish(m['matn'], matn_n))}\n\n"
        f"<b>Javob:</b>\n{esc(kesish(javob, javob_n, oxiri=True))}"
    )


def sigadigan_karta(karta_funksiyasi, m):
    """Telegram 4096 belgi chegarasidan oshmaydigan karta matnini qaytaradi.
    (<, >, & belgilari ko'p bo'lsa HTML uzayib ketadi.)"""
    for n in (None, 600, 250, 80):
        matn = karta_funksiyasi(m, n)
        if len(matn) <= 4000:
            return matn

    return karta_funksiyasi(m, 30)


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
        f"🎓 {esc(m['fakultet'])}, {esc(m['kurs'])}, {esc(m['guruh'])}\n"
        f"🆔 <code>{int(m['user_id'])}</code>\n"
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


@dp.errors()
async def xatolik_ushlash(event: ErrorEvent):
    # Bitta xato butun botni to'xtatib qo'ymasligi uchun
    logging.error("Handlerda xato: %s", event.exception, exc_info=event.exception)

    # Tugma bosilgan bo'lsa, "soat" belgisi osilib qolmasin
    call = event.update.callback_query
    if call is not None:
        try:
            await call.answer("Xatolik yuz berdi. Qayta urinib ko‘ring.")
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

@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):

    await state.clear()

    talaba = await talaba_olish(message.from_user.id)

    if talaba:
        await message.answer(
            f"Assalomu alaykum, {esc(talaba['fish'])}!\n\n"
            "Botga xush kelibsiz.",
            reply_markup=talaba_menu()
        )
        return

    await state.set_state(TalabaHolati.telefon)

    await message.answer(
        WELCOME + "\n\nTelefon raqamingizni yuboring:",
        reply_markup=telefon_klaviaturasi()
    )


@dp.message(Command("cancel"))
async def bekor_qilish(message: Message, state: FSMContext):

    if await state.get_state() is None:
        await message.answer("Bekor qilinadigan faol amal yo‘q.")
        return

    await state.clear()

    talaba = await talaba_olish(message.from_user.id)

    if talaba:
        await message.answer(
            "Amal bekor qilindi.",
            reply_markup=talaba_menu()
        )
    else:
        await message.answer(
            "Amal bekor qilindi. Qayta boshlash uchun /start yuboring.",
            reply_markup=ReplyKeyboardRemove()
        )


@dp.message(Command("help"))
@dp.message(F.text == BTN_YORDAM)
async def yordam(message: Message):

    matn = (
        "📌 <b>BOT BUYRUQLARI</b>\n\n"
        "/start — botni boshlash\n"
        "/help — yordam\n"
        "/menu — asosiy menyu\n"
        "/new — yangi murojaat\n"
        "/myrequests — murojaatlarim\n"
        "/profile — profilim\n"
        "/cancel — joriy amalni bekor qilish"
    )

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
async def menyu(message: Message):

    talaba = await talaba_olish(message.from_user.id)

    if not talaba:
        await message.answer("Avval /start orqali ro‘yxatdan o‘ting.")
        return

    await message.answer("Asosiy menyu:", reply_markup=talaba_menu())


@dp.message(Command("profile"))
@dp.message(F.text == BTN_PROFIL)
async def profil(message: Message):

    talaba = await talaba_olish(message.from_user.id)

    if not talaba:
        await message.answer("Avval /start orqali ro‘yxatdan o‘ting.")
        return

    await message.answer(
        "👤 <b>Sizning ma'lumotlaringiz</b>\n\n"
        f"Ism-familiya: {esc(talaba['fish'])}\n"
        f"Telefon: {esc(talaba['telefon'])}\n"
        f"Fakultet: {esc(talaba['fakultet'])}\n"
        f"Kurs: {esc(talaba['kurs'])}\n"
        f"Guruh: {esc(talaba['guruh'])}",
        reply_markup=profil_klaviaturasi()
    )


@dp.callback_query(F.data == "profil:tahrirlash")
async def profil_tahrirlash(call: CallbackQuery, state: FSMContext):

    talaba = await talaba_olish(call.from_user.id)

    if not talaba:
        await call.answer("Avval /start orqali ro‘yxatdan o‘ting.", show_alert=True)
        return

    await state.clear()
    await state.update_data(telefon=talaba["telefon"])
    await state.set_state(TalabaHolati.fish)

    await call.answer()

    await yoz(
        call.from_user.id,
        "Yangi ism va familiyangizni kiriting:\n"
        "(Bekor qilish: /cancel)",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(Command("new"))
@dp.message(F.text == BTN_YANGI)
async def murojaat_boshlash(message: Message, state: FSMContext):

    talaba = await talaba_olish(message.from_user.id)

    if not talaba:
        await message.answer("Avval ro‘yxatdan o‘ting: /start")
        return

    if await bugungi_soni(message.from_user.id) >= KUNLIK_LIMIT:
        await message.answer(
            f"Bugun {KUNLIK_LIMIT} ta murojaat yuborib bo‘ldingiz. "
            "Iltimos, ertaga qayta urinib ko‘ring."
        )
        return

    await state.clear()
    await state.set_state(TalabaHolati.murojaat_toifa)

    await message.answer(
        "🏷 <b>Murojaat mavzusini tanlang:</b>\n\n"
        "Bekor qilish: /cancel",
        reply_markup=tanlash_klaviaturasi("toifa", TOIFALAR)
    )


@dp.message(Command("myrequests"))
@dp.message(F.text == BTN_MENINGLAR)
async def mening_murojaatlarim(message: Message):
    # 10 ta alohida xabar o'rniga BITTA xabar: ro'yxat + tugmalar.
    # Ko'p talaba bir vaqtda bossa ham Telegram chegarasiga urilmaydi.

    murojaatlar = await talaba_murojaatlari(message.from_user.id, limit=10)

    if not murojaatlar:
        await message.answer("Sizda hali murojaatlar mavjud emas.")
        return

    qatorlar = ["📂 <b>Oxirgi murojaatlaringiz:</b>\n"]

    for m in murojaatlar:
        belgi = HOLAT_BELGI.get(m["holat"], "📄")
        qatorlar.append(
            f"{belgi} <b>#{m['id']}</b> · {esc(m['toifa'] or '—')}\n"
            f"     {esc(m['holat'])} · {esc((m['sana'] or '')[:16])}"
        )

    qatorlar.append("\nBatafsil ko‘rish uchun raqamni bosing 👇")

    await message.answer(
        "\n".join(qatorlar),
        reply_markup=royxat_klaviaturasi(murojaatlar)
    )


@dp.callback_query(F.data.startswith("korish:"))
async def murojaat_korish(call: CallbackQuery):

    try:
        murojaat_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer("Noto‘g‘ri so‘rov.")
        return

    m = await murojaat_olish(murojaat_id)

    # Talaba faqat o'z murojaatini ko'ra oladi
    if not m or m["user_id"] != call.from_user.id:
        await call.answer("Murojaat topilmadi.", show_alert=True)
        return

    await call.answer()
    await yoz(call.from_user.id, sigadigan_karta(talaba_karta, m))


# =========================================================
# ADMIN PANEL
# =========================================================

@dp.message(Command("admin"))
async def admin_panel(message: Message):

    if not admin_ekanmi(message.from_user.id):
        await message.answer("Sizda administrator huquqi yo‘q.")
        return

    await message.answer(
        "👨‍💻 <b>ADMINISTRATOR PANELI</b>\n\n"
        "Raqam bo‘yicha qidirish: /find 12",
        reply_markup=admin_menu()
    )


@dp.message(Command("find"))
async def murojaat_topish(message: Message, command: CommandObject):

    if not admin_ekanmi(message.from_user.id):
        await message.answer("Sizda administrator huquqi yo‘q.")
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
        f"👥 Talabalar: {s['talabalar']}\n"
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
            "ID", "Sana", "Mavzu", "Holat", "Ism-familiya", "Telefon",
            "Fakultet", "Kurs", "Guruh", "Telegram ID", "Murojaat", "Javob"
        ])

        for m in qatorlar:
            yozuvchi.writerow([
                m["id"], m["sana"], csv_xavfsiz(m["toifa"]), m["holat"],
                csv_xavfsiz(m["fish"]), csv_xavfsiz(m["telefon"]),
                csv_xavfsiz(m["fakultet"]), csv_xavfsiz(m["kurs"]),
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
        await message.answer("Sizda administrator huquqi yo‘q.")
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
        await message.answer("Sizda administrator huquqi yo‘q.")
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
            f"👀 Sizning #{murojaat_id} murojaatingiz ko‘rib chiqilmoqda."
        )

    elif kod == "y":
        if not await yopish(murojaat_id):
            await call.answer("Murojaat allaqachon yopilgan.", show_alert=True)
            return

        await call.answer("Murojaat yopildi.")

        await xavfsiz_yuborish(
            m["user_id"],
            f"🔒 Sizning #{murojaat_id} murojaatingiz yopildi.\n"
            "Yangi savol bo‘lsa, yangi murojaat yuboring."
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
        f"#{murojaat_id} murojaatiga javob matnini yuboring.\n"
        "Bekor qilish: /cancel"
    )


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
        await message.answer("Sizda administrator huquqi yo‘q.")
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

    yuborildi = 0
    yetmadi = 0
    boshlandi = time.monotonic()

    for user_id in idlar:
        # Tezlik middleware'da cheklanadi; talabalarga javoblar ustuvor
        ok = await xavfsiz_yuborish(
            user_id,
            f"📢 <b>E’lon</b>\n\n{esc(matn)}"
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
async def telefon_qabul(message: Message, state: FSMContext):

    if message.contact.user_id != message.from_user.id:
        await message.answer(
            "Iltimos, o‘zingizning telefon raqamingizni yuboring."
        )
        return

    telefon = message.contact.phone_number or ""
    if telefon and not telefon.startswith("+"):
        telefon = "+" + telefon

    await state.update_data(telefon=telefon)
    await state.set_state(TalabaHolati.fish)

    await message.answer(
        "Ism va familiyangizni kiriting:",
        reply_markup=ReplyKeyboardRemove()
    )


@dp.message(TalabaHolati.telefon)
async def telefon_xato(message: Message):
    await message.answer(
        "Telefon raqamingizni pastdagi tugma orqali yuboring.",
        reply_markup=telefon_klaviaturasi()
    )


@dp.message(TalabaHolati.fish, F.text)
async def fish_qabul(message: Message, state: FSMContext):

    fish = toza_matn(message.text)

    if len(fish) < 5:
        await message.answer("Ism va familiyangizni to‘liq kiriting.")
        return

    if len(fish) > 100:
        await message.answer("Ism-familiya juda uzun.")
        return

    await state.update_data(fish=fish)
    await state.set_state(TalabaHolati.fakultet)

    await message.answer(
        "Fakultetingizni tanlang:",
        reply_markup=tanlash_klaviaturasi("fakultet", FACULTIES)
    )


@dp.message(TalabaHolati.fish)
async def fish_faqat_matn(message: Message):
    await message.answer("Ism va familiyangizni matn ko‘rinishida yozing.")


@dp.callback_query(F.data.startswith("fakultet:"))
async def fakultet_tanlash(call: CallbackQuery, state: FSMContext):

    if await state.get_state() != TalabaHolati.fakultet.state:
        await call.answer("Bu tanlov hozir faol emas.")
        return

    index = indeks_olish(call.data, len(FACULTIES))

    if index is None:
        await call.answer("Noto‘g‘ri tanlov.")
        return

    await call.answer()

    fakultet = FACULTIES[index]

    if fakultet == "Boshqa":
        await state.set_state(TalabaHolati.fakultet_qolda)
        await yoz(call.from_user.id, "Fakultet nomini yozing:")
    else:
        await state.update_data(fakultet=fakultet)
        await state.set_state(TalabaHolati.kurs)

        await yoz(
            call.from_user.id,
            "Kursingizni tanlang:",
            reply_markup=tanlash_klaviaturasi("kurs", COURSES)
        )


@dp.message(TalabaHolati.fakultet_qolda, F.text)
async def fakultet_qolda(message: Message, state: FSMContext):

    fakultet = toza_matn(message.text)

    if len(fakultet) < 2 or len(fakultet) > 100:
        await message.answer("Fakultet nomini to‘g‘ri kiriting.")
        return

    await state.update_data(fakultet=fakultet)
    await state.set_state(TalabaHolati.kurs)

    await message.answer(
        "Kursingizni tanlang:",
        reply_markup=tanlash_klaviaturasi("kurs", COURSES)
    )


@dp.callback_query(F.data.startswith("kurs:"))
async def kurs_tanlash(call: CallbackQuery, state: FSMContext):

    if await state.get_state() != TalabaHolati.kurs.state:
        await call.answer("Bu tanlov hozir faol emas.")
        return

    index = indeks_olish(call.data, len(COURSES))

    if index is None:
        await call.answer("Noto‘g‘ri tanlov.")
        return

    await call.answer()

    kurs = COURSES[index]

    if kurs == "Boshqa":
        await state.set_state(TalabaHolati.kurs_qolda)
        await yoz(call.from_user.id, "Kursingizni yozing:")
    else:
        await state.update_data(kurs=kurs)
        await guruhlarni_korsatish(call.from_user.id, state)


@dp.message(TalabaHolati.kurs_qolda, F.text)
async def kurs_qolda(message: Message, state: FSMContext):

    kurs = toza_matn(message.text)

    if len(kurs) < 2 or len(kurs) > 50:
        await message.answer("Kursingizni to‘g‘ri kiriting.")
        return

    await state.update_data(kurs=kurs)

    # Kurs qo'lda kiritilsa, guruh ham qo'lda olinadi.
    await state.set_state(TalabaHolati.guruh_qolda)

    await message.answer("Guruhingiz nomini yozing:")


async def guruhlarni_korsatish(chat_id, state):

    data = await state.get_data()

    guruhlar = GROUPS.get((data.get("fakultet"), data.get("kurs")), [])

    if not guruhlar:
        await state.set_state(TalabaHolati.guruh_qolda)
        await xavfsiz_yuborish(
            chat_id,
            "Guruhlar ro‘yxati hali kiritilmagan.\n"
            "Guruhingiz nomini yozing:"
        )
        return

    guruhlar = list(guruhlar) + ["Boshqa"]

    await state.set_state(TalabaHolati.guruh)

    await xavfsiz_yuborish(
        chat_id,
        "Guruhingizni tanlang:",
        reply_markup=tanlash_klaviaturasi("guruh", guruhlar)
    )


@dp.callback_query(F.data.startswith("guruh:"))
async def guruh_tanlash(call: CallbackQuery, state: FSMContext):

    if await state.get_state() != TalabaHolati.guruh.state:
        await call.answer("Bu tanlov hozir faol emas.")
        return

    data = await state.get_data()

    guruhlar = list(
        GROUPS.get((data.get("fakultet"), data.get("kurs")), [])
    ) + ["Boshqa"]

    index = indeks_olish(call.data, len(guruhlar))

    if index is None:
        await call.answer("Noto‘g‘ri tanlov.")
        return

    await call.answer()

    guruh = guruhlar[index]

    if guruh == "Boshqa":
        await state.set_state(TalabaHolati.guruh_qolda)
        await yoz(call.from_user.id, "Guruhingizni yozing:")
    else:
        await royxatdan_otkazish(call.from_user.id, state, guruh)


@dp.message(TalabaHolati.guruh_qolda, F.text)
async def guruh_qolda(message: Message, state: FSMContext):

    guruh = toza_matn(message.text)

    if len(guruh) < 2 or len(guruh) > 50:
        await message.answer("Guruh nomini to‘g‘ri kiriting.")
        return

    await royxatdan_otkazish(message.from_user.id, state, guruh)


@dp.message(TalabaHolati.fakultet)
@dp.message(TalabaHolati.kurs)
@dp.message(TalabaHolati.guruh)
async def tugma_kutilmoqda(message: Message):
    await message.answer("Iltimos, yuqoridagi tugmalardan birini tanlang.")


@dp.message(TalabaHolati.fakultet_qolda)
@dp.message(TalabaHolati.kurs_qolda)
@dp.message(TalabaHolati.guruh_qolda)
async def qolda_faqat_matn(message: Message):
    await message.answer("Iltimos, matn ko‘rinishida yozing.")


async def royxatdan_otkazish(user_id, state, guruh):

    data = await state.get_data()

    if not all(data.get(k) for k in ("telefon", "fish", "fakultet", "kurs")):
        await state.clear()
        await xavfsiz_yuborish(
            user_id,
            "Ma’lumotlar to‘liq emas. Qayta boshlash uchun /start yuboring."
        )
        return

    avval_bor = await talaba_olish(user_id) is not None

    await talaba_saqlash(
        user_id=user_id,
        telefon=data["telefon"],
        fish=data["fish"],
        fakultet=data["fakultet"],
        kurs=data["kurs"],
        guruh=guruh
    )

    await state.clear()

    sarlavha = (
        "✅ <b>Ma’lumotlaringiz yangilandi!</b>"
        if avval_bor
        else "✅ <b>Ro‘yxatdan muvaffaqiyatli o‘tdingiz!</b>"
    )

    await xavfsiz_yuborish(
        user_id,
        f"{sarlavha}\n\n"
        f"Ism-familiya: {esc(data['fish'])}\n"
        f"Fakultet: {esc(data['fakultet'])}\n"
        f"Kurs: {esc(data['kurs'])}\n"
        f"Guruh: {esc(guruh)}",
        reply_markup=talaba_menu()
    )


# =========================================================
# MUROJAAT YUBORISH
# =========================================================

@dp.callback_query(F.data.startswith("toifa:"))
async def toifa_tanlash(call: CallbackQuery, state: FSMContext):

    if await state.get_state() != TalabaHolati.murojaat_toifa.state:
        await call.answer(
            "Bu tanlov faol emas. Yangi murojaat: /new",
            show_alert=True
        )
        return

    index = indeks_olish(call.data, len(TOIFALAR))

    if index is None:
        await call.answer("Noto‘g‘ri tanlov.")
        return

    await state.update_data(
        toifa=TOIFALAR[index],
        murojaat_matni=None,
        fayl_id=None,
        fayl_turi=None
    )
    await state.set_state(TalabaHolati.murojaat)

    await call.answer()

    await yoz(
        call.from_user.id,
        "✍️ <b>Murojaatingizni batafsil yozing.</b>\n\n"
        "Murojaat kamida 10 ta belgidan iborat bo‘lishi kerak.\n"
        "Rasm yoki fayl yubormoqchi bo‘lsangiz, matnni uning izohiga "
        "yozishingiz ham mumkin.\n\n"
        "Bekor qilish uchun /cancel buyrug‘ini yuboring."
    )


@dp.message(TalabaHolati.murojaat_toifa)
async def toifa_kutilmoqda(message: Message):
    await message.answer(
        "Iltimos, mavzuni yuqoridagi tugmalardan tanlang "
        "yoki /cancel yuboring."
    )


@dp.message(TalabaHolati.murojaat, F.text)
async def murojaat_matn_qabul(message: Message, state: FSMContext):

    matn = toza_kop_qator(message.text)

    xato = murojaat_matni_xatosi(matn)
    if xato:
        await message.answer(xato)
        return

    await state.update_data(
        murojaat_matni=matn,
        fayl_id=None,
        fayl_turi=None
    )
    await state.set_state(TalabaHolati.murojaat_fayl)

    await message.answer(
        "📎 Kerak bo‘lsa, <b>bitta</b> rasm yoki fayl biriktiring (ixtiyoriy).\n\n"
        "Yoki pastdagi tugmani bosing.",
        reply_markup=fayl_klaviaturasi()
    )


def fayl_malumoti(message: Message):
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.document:
        return message.document.file_id, "document"
    return None, None


@dp.message(TalabaHolati.murojaat, F.photo | F.document)
async def murojaat_fayl_bilan(message: Message, state: FSMContext):
    """Matn rasm/fayl izohida yozilgan bo'lsa — bir yo'la qabul qilamiz."""

    matn = toza_kop_qator(message.caption)

    if not matn:
        await message.answer(
            "Avval murojaat matnini yozing, keyin fayl biriktirasiz.\n"
            "Yoki matnni rasm/fayl izohiga yozib yuboring."
        )
        return

    xato = murojaat_matni_xatosi(matn)
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
        await message.answer("ℹ️ Albomdan faqat birinchi fayl qabul qilindi.")

    await tasdiqlashni_korsatish(message.chat.id, state)


@dp.message(TalabaHolati.murojaat)
async def murojaat_faqat_matn(message: Message):
    await message.answer("Murojaatni matn ko‘rinishida yozing.")


async def tasdiqlashni_korsatish(chat_id, state):

    data = await state.get_data()

    await state.set_state(TalabaHolati.murojaat_tasdiqlash)

    ilova = {
        "photo": "🖼 Rasm biriktirilgan",
        "document": "📄 Fayl biriktirilgan",
    }.get(data.get("fayl_turi"), "Ilova yo‘q")

    await xavfsiz_yuborish(
        chat_id,
        "📝 <b>Murojaatingiz:</b>\n\n"
        f"🏷 {esc(data.get('toifa'))}\n"
        f"📎 {ilova}\n\n"
        f"{esc(data.get('murojaat_matni'))}\n\n"
        "Ushbu murojaatni yuborishni tasdiqlaysizmi?",
        reply_markup=murojaat_tasdiqlash()
    )


@dp.message(TalabaHolati.murojaat_fayl, F.photo | F.document)
async def fayl_qabul(message: Message, state: FSMContext):

    fayl_id, fayl_turi = fayl_malumoti(message)
    yangilash = {"fayl_id": fayl_id, "fayl_turi": fayl_turi}

    # Fayl izohida qo'shimcha matn bo'lsa, murojaatga qo'shamiz
    izoh = toza_kop_qator(message.caption)
    if izoh:
        data = await state.get_data()
        birga = toza_kop_qator(f"{data.get('murojaat_matni') or ''}\n\n{izoh}")

        if murojaat_matni_xatosi(birga):
            await message.answer(
                "ℹ️ Fayl izohi juda uzun bo‘lgani uchun murojaatga qo‘shilmadi."
            )
        else:
            yangilash["murojaat_matni"] = birga

    await state.update_data(**yangilash)

    if message.media_group_id:
        await message.answer("ℹ️ Albomdan faqat birinchi fayl qabul qilindi.")

    await tasdiqlashni_korsatish(message.chat.id, state)


@dp.callback_query(F.data == "fayl:otkazish")
async def fayl_otkazish(call: CallbackQuery, state: FSMContext):

    if await state.get_state() != TalabaHolati.murojaat_fayl.state:
        await call.answer("Bu tugma hozir faol emas.", show_alert=True)
        return

    await call.answer()
    await tasdiqlashni_korsatish(call.from_user.id, state)


@dp.message(TalabaHolati.murojaat_fayl)
async def fayl_xato(message: Message):
    await message.answer(
        "Rasm yoki fayl (hujjat) yuboring, yoki "
        "«Fayl biriktirmasdan davom etish» tugmasini bosing."
    )


@dp.message(TalabaHolati.murojaat_tasdiqlash)
async def tasdiqlash_kutilmoqda(message: Message):
    await message.answer(
        "Yuqoridagi tugmalardan birini bosing yoki /cancel yuboring."
    )


@dp.callback_query(F.data == "murojaat:tahrirlash")
async def murojaat_tahrirlash(call: CallbackQuery, state: FSMContext):

    if await state.get_state() != TalabaHolati.murojaat_tasdiqlash.state:
        await call.answer("Faol murojaat topilmadi.", show_alert=True)
        return

    await state.set_state(TalabaHolati.murojaat)

    await call.answer()
    await yoz(call.from_user.id, "Murojaatingizni qayta yozing:")


@dp.callback_query(F.data == "murojaat:bekor")
async def murojaat_bekor(call: CallbackQuery, state: FSMContext):

    # Faqat murojaat yozilayotgan bo'lsa bekor qilamiz. Eski xabardagi
    # tugma bosilsa, masalan profil tahririni buzib yubormaydi.
    if await state.get_state() not in MUROJAAT_HOLATLARI:
        await call.answer("Bu murojaat allaqachon yakunlangan.")
        return

    await state.clear()

    await call.answer()
    await yoz(
        call.from_user.id,
        "Murojaat yuborish bekor qilindi.",
        reply_markup=talaba_menu()
    )


@dp.callback_query(F.data == "murojaat:yuborish")
async def murojaat_yuborish(call: CallbackQuery, state: FSMContext):

    user_id = call.from_user.id

    # Ikki marta bosilishidan himoya
    if user_id in _yuborilmoqda:
        await call.answer()
        return

    _yuborilmoqda.add(user_id)

    try:
        if await state.get_state() != TalabaHolati.murojaat_tasdiqlash.state:
            await call.answer(
                "Tasdiqlash uchun murojaat topilmadi.",
                show_alert=True
            )
            return

        data = await state.get_data()

        matn = toza_kop_qator(data.get("murojaat_matni"))

        # Himoya: matn bo'lmasa, yuborilmaydi.
        if len(matn) < 10:
            await state.set_state(TalabaHolati.murojaat)
            await call.answer(
                "Avval murojaat matnini yozishingiz kerak.",
                show_alert=True
            )
            return

        talaba = await talaba_olish(user_id)

        if not talaba:
            await state.clear()
            await call.answer("Talaba profili topilmadi.", show_alert=True)
            return

        if await bugungi_soni(user_id) >= KUNLIK_LIMIT:
            await state.clear()
            await call.answer(
                f"Kunlik limit ({KUNLIK_LIMIT} ta) tugagan.",
                show_alert=True
            )
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

    await call.answer("Murojaat yuborildi.")

    # Adminlarga fonda, navbat bilan yuboriladi — talaba kutib qolmaydi
    adminlarga_xabar(murojaat_id)

    await yoz(
        user_id,
        "✅ <b>Murojaatingiz muvaffaqiyatli yuborildi!</b>\n\n"
        f"Murojaat raqami: #{murojaat_id}\n"
        "Holatini «📂 Mening murojaatlarim» bo‘limida kuzatishingiz mumkin.",
        reply_markup=talaba_menu()
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
        f"📬 <b>Sizning #{murojaat_id} murojaatingizga javob:</b>\n\n"
        f"{esc(javob)}"
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
async def noma_lum(message: Message):

    talaba = await talaba_olish(message.from_user.id)

    if talaba:
        await message.answer(
            "Tushunmadim. Menyudan foydalaning yoki /help yuboring.",
            reply_markup=talaba_menu()
        )
    else:
        await message.answer("Boshlash uchun /start yuboring.")


# =========================================================
# ISHGA TUSHIRISH
# =========================================================

TALABA_BUYRUQLARI = [
    BotCommand(command="start", description="Botni boshlash"),
    BotCommand(command="help", description="Yordam"),
    BotCommand(command="menu", description="Asosiy menyu"),
    BotCommand(command="new", description="Yangi murojaat"),
    BotCommand(command="myrequests", description="Mening murojaatlarim"),
    BotCommand(command="profile", description="Profilim"),
    BotCommand(command="cancel", description="Joriy amalni bekor qilish"),
]

ADMIN_BUYRUQLARI = TALABA_BUYRUQLARI + [
    BotCommand(command="admin", description="Administrator paneli"),
    BotCommand(command="find", description="Murojaatni raqam bo‘yicha topish"),
    BotCommand(command="export", description="CSV eksport"),
    BotCommand(command="broadcast", description="Hammaga e’lon"),
    BotCommand(command="backup", description="Zaxira nusxa"),
]


async def buyruqlarni_ornatish():
    await bot.set_my_commands(TALABA_BUYRUQLARI)

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

        logging.info("TDIU murojaatlar boti ishga tushdi.")

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
