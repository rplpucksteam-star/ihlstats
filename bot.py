import asyncio
import html
import io
import logging
import os
import re
from typing import List, Tuple, Optional, Dict, Any

import asyncpg
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    MessageEntity,
    ReplyKeyboardMarkup,
    BufferedInputFile,
)

# ---------- Настройка логирования ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# ---------- Конфигурация ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = {
    int(x) for x in _admin_ids_raw.replace(" ", "").split(",") if x.strip().isdigit()
}

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не задан. Добавьте переменную окружения BOT_TOKEN "
        "(получить токен можно у @BotFather)."
    )

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL не задан. Добавьте PostgreSQL плагин на Railway и укажите его "
        "connection string в переменной окружения DATABASE_URL."
    )

if not ADMIN_IDS:
    logger.warning(
        "Переменная ADMIN_IDS пуста — команда /adminka недоступна никому."
    )

# ---------- НАСТРОЙКИ ДЛЯ ГЕНЕРАЦИИ КАРТИНОК ----------
# Пути к вашим шаблонам (лежат в папке с ботом)
TOP_IMAGES = {
    "points": "top_scorers.png",        # Бомбардиры
    "goals": "top_snipers.png",         # Снайперы
    "assists": "top_assistants.png",    # Ассистенты
    "goalkeepers": "top_goalkeepers.png", # Вратари
}
# Путь к шрифту с кириллицей (скачайте файл .ttf и положите рядом)
FONT_PATH = "DejaVuSans-Bold.ttf" 
# Координаты (X, Y) центров 10 строк. ОБЯЗАТЕЛЬНО ЗАМЕРЬТЕ В ФОТОШОПЕ!
ROW_COORDS = [
    (540, 200), (540, 280), (540, 360), (540, 440), (540, 520),
    (540, 600), (540, 680), (540, 760), (540, 840), (540, 920)
]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ---------- Функция создания картинки ----------
def create_top_image(stat_key: str, players: list) -> io.BytesIO:
    image_path = TOP_IMAGES.get(stat_key, TOP_IMAGES["goals"])
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype(FONT_PATH, 36)
    except IOError:
        logger.warning(f"Шрифт {FONT_PATH} не найден, используется стандартный!")
        font = ImageFont.load_default()

    for idx, player in enumerate(players[:10]):
        if idx >= len(ROW_COORDS): 
            break

        x, y = ROW_COORDS[idx]
        
        # Формируем текст. Если хотите убрать эмодзи-медали, удалите эту часть.
        medal = ["🥇", "🥈", "🥉"][idx] if idx < 3 else f"{idx+1}."
        text = f"{medal} {player['nickname']} #{player['number']} | {player[stat_key]}"

        # anchor="mm" центрирует текст строго по координатам (x, y)
        draw.text((x, y), text, font=font, fill="#1a4e5a", anchor="mm")

    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    return img_bytes

# ---------- База данных ----------
pool: Optional[asyncpg.Pool] = None

def _normalize_dsn(dsn: str) -> str:
    """Приводит postgres:// к postgresql:// для совместимости с asyncpg."""
    if dsn.startswith("postgres://"):
        return "postgresql://" + dsn[len("postgres://"):]
    return dsn

async def init_pool() -> None:
    global pool
    try:
        pool = await asyncpg.create_pool(
            dsn=_normalize_dsn(DATABASE_URL),
            min_size=1,
            max_size=5,
            timeout=10.0
        )
        await create_tables()
        logger.info("Пул соединений с PostgreSQL инициализирован.")
    except Exception as e:
        logger.critical("Не удалось подключиться к БД: %s", e)
        raise

async def close_pool() -> None:
    if pool is not None:
        await pool.close()
        logger.info("Пул соединений закрыт.")

async def create_tables() -> None:
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                emoji_char TEXT,
                custom_emoji_id TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT now()
            );
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                id SERIAL PRIMARY KEY,
                nickname TEXT NOT NULL,
                number INTEGER NOT NULL,
                team_id INTEGER REFERENCES teams(id) ON DELETE SET NULL,
                goals INTEGER NOT NULL DEFAULT 0,
                assists INTEGER NOT NULL DEFAULT 0,
                matches_played INTEGER NOT NULL DEFAULT 0,
                saves INTEGER NOT NULL DEFAULT 0,
                shots_against INTEGER NOT NULL DEFAULT 0,
                is_goalkeeper BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT now(),
                UNIQUE (nickname, number)
            );
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_players_nickname ON players (LOWER(nickname));"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_players_team ON players (team_id);"
        )
        logger.info("Таблицы созданы (или уже существуют).")

# ... (Все остальные функции и запросы к БД остаются без изменений) ...

async def apply_skater_stats(nickname: str, number: int, goals: int, assists: int, sign: str):
    row, created = await get_or_create_player(nickname, number)
    mult = 1 if sign == "+" else -1
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE players
            SET goals = GREATEST(goals + $1, 0),
                assists = GREATEST(assists + $2, 0),
                matches_played = GREATEST(matches_played + $3, 0)
            WHERE id = $4;
            """,
            mult * goals,
            mult * assists,
            mult * 1,
            row["id"],
        )
    return row["id"], created

# ... (Остальные функции top_scorers, top_snipers и т.д. остаются без изменений) ...

# ---------- FSM состояния ----------
class AdminTeamStates(StatesGroup):
    creating_name = State()

class AdminRosterStates(StatesGroup):
    waiting_list = State()

class AdminPointsStates(StatesGroup):
    waiting_list = State()

class UserSearchStates(StatesGroup):
    waiting_nickname = State()

# ---------- Парсинг текстовых списков ----------
ROSTER_LINE_RE = re.compile(r"^\s*(\S+)\s*#\s*(\d+)\s*$")
SKATER_LINE_RE = re.compile(
    r"^\s*(\S+)\s*#\s*(\d+)\s+(\d+)\s+(\d+)\s+([+\-])\s*$"
)
GOALKEEPER_LINE_RE = re.compile(
    r"^\s*(\S+)\s*#\s*(\d+)\s+(\d+)\s*/\s*(\d+)\s+([+\-])\s*gk\s*$",
    re.IGNORECASE,
)

def parse_roster_lines(text: str):
    entries = []
    errors = []
    for i, raw in enumerate(text.strip().splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        m = ROSTER_LINE_RE.match(line)
        if not m:
            errors.append((i, line))
            continue
        nickname, number = m.group(1), int(m.group(2))
        entries.append((nickname, number))
    return entries, errors

def parse_points_lines(text: str):
    results = []
    errors = []
    for i, raw in enumerate(text.strip().splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        m_gk = GOALKEEPER_LINE_RE.match(line)
        if m_gk:
            nickname, number, saves, shots, sign = m_gk.groups()
            results.append(
                {
                    "type": "gk",
                    "nickname": nickname,
                    "number": int(number),
                    "saves": int(saves),
                    "shots": int(shots),
                    "sign": sign,
                    "raw": line,
                }
            )
            continue

        m_sk = SKATER_LINE_RE.match(line)
        if m_sk:
            nickname, number, goals, assists, sign = m_sk.groups()
            results.append(
                {
                    "type": "skater",
                    "nickname": nickname,
                    "number": int(number),
                    "goals": int(goals),
                    "assists": int(assists),
                    "sign": sign,
                    "raw": line,
                }
            )
            continue

        errors.append((i, line))

    return results, errors

# ---------- Форматирование сообщений ----------
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
STAT_TITLES = {
    "points": "🏆 Топ-10 бомбардиров (голы + передачи)",
    "goals": "🎯 Топ-10 снайперов (голы)",
    "assists": "🅰️ Топ-10 ассистентов (передачи)",
}
STAT_LABELS = {
    "points": "очков",
    "goals": "голов",
    "assists": "передач",
}
NO_TEAM_LABEL = "❌ Без команды"

def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2

def extract_utf16_substring(text: str, offset: int, length: int) -> str:
    u16 = text.encode("utf-16-le")
    piece = u16[offset * 2: (offset + length) * 2]
    return piece.decode("utf-16-le")

def _strip_utf16_range(text: str, offset: int, length: int) -> str:
    u16 = text.encode("utf-16-le")
    new = u16[:offset * 2] + u16[(offset + length) * 2:]
    return new.decode("utf-16-le")

class Msg:
    """Построитель сообщений с ручными сущностями (bold / custom_emoji)."""

    def __init__(self):
        self.text = ""
        self.entities: List[MessageEntity] = []

    def add_text(self, s: str) -> "Msg":
        self.text += s
        return self

    def add_bold(self, s: str) -> "Msg":
        offset = _utf16_len(self.text)
        self.text += s
        length = _utf16_len(s)
        if length:
            self.entities.append(MessageEntity(type="bold", offset=offset, length=length))
        return self

    def add_custom_emoji(self, fallback: Optional[str], custom_emoji_id: Optional[str]) -> "Msg":
        fallback = fallback or "🏒"
        offset = _utf16_len(self.text)
        self.text += fallback
        length = _utf16_len(fallback)
        if custom_emoji_id and length:
            self.entities.append(
                MessageEntity(
                    type="custom_emoji",
                    offset=offset,
                    length=length,
                    custom_emoji_id=custom_emoji_id,
                )
            )
        return self

    def build(self):
        return self.text, (self.entities or None)

async def send_msg(target: Message, m: Msg, reply_markup=None) -> None:
    text, entities = m.build()
    kwargs = {"reply_markup": reply_markup} if reply_markup is not None else {}
    await target.answer(text, entities=entities, parse_mode=None, **kwargs)

def parse_team_name_message(message: Message):
    text = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    for e in entities:
        if e.type == "custom_emoji":
            fallback = extract_utf16_substring(text, e.offset, e.length)
            clean = _strip_utf16_range(text, e.offset, e.length).strip()
            return clean, fallback, e.custom_emoji_id
    return text.strip(), None, None

def _team_label_html(row) -> str:
    if not row["team_name"]:
        return NO_TEAM_LABEL
    t_name = html.escape(row["team_name"])
    if row["custom_emoji_id"]:
        fallback = row["emoji_char"] or "🏒"
        return f'<tg-emoji emoji-id="{row["custom_emoji_id"]}">{fallback}</tg-emoji> {t_name}'
    elif row["emoji_char"]:
        return f'{row["emoji_char"]} {t_name}'
    return f'🏒 {t_name}'

# ... (Остальные функции форматирования остаются без изменений) ...

# ---------- Пользовательские хендлеры ----------
user_router = Router()

@user_router.message(CommandStart())
@user_router.message(F.text == BTN_MAIN_MENU)
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = await get_main_menu_text()
    await message.answer(
        text,
        reply_markup=persistent_reply_kb()
    )
    # Сообщение с кнопками под ним
    await message.answer(
        "📍 <b>Главное меню:</b>",
        reply_markup=main_menu_inline_kb()
    )

@user_router.callback_query(F.data == "user:main_menu")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    text = await get_main_menu_text()
    await callback.message.edit_text(
        text,
        reply_markup=main_menu_inline_kb()
    )

# --- ИЗМЕНЁННЫЕ ОБРАБОТЧИКИ ТОПОВ (ОТПРАВКА ФОТО) ---

@user_router.callback_query(F.data == "user:top:scorers")
async def show_top_scorers(callback: CallbackQuery):
    await callback.answer()
    players = await top_scorers(10)
    if not players:
        await callback.message.edit_text("📊 Пока нет данных.", reply_markup=back_to_main_kb())
        return
    
    # Генерируем картинку
    img_buffer = create_top_image("points", players)
    
    # Отправляем фото
    await callback.message.answer_photo(
        photo=BufferedInputFile(img_buffer.getvalue(), filename="top_scorers.png"),
        caption=format_top_list("points", players), # Подпись с текстовым дублем
        reply_markup=back_to_main_kb()
    )


@user_router.callback_query(F.data == "user:top:snipers")
async def show_top_snipers(callback: CallbackQuery):
    await callback.answer()
    players = await top_snipers(10)
    if not players:
        await callback.message.edit_text("📊 Пока нет данных.", reply_markup=back_to_main_kb())
        return

    img_buffer = create_top_image("goals", players)

    await callback.message.answer_photo(
        photo=BufferedInputFile(img_buffer.getvalue(), filename="top_snipers.png"),
        caption=format_top_list("goals", players),
        reply_markup=back_to_main_kb()
    )


@user_router.callback_query(F.data == "user:top:assists")
async def show_top_assistants(callback: CallbackQuery):
    await callback.answer()
    players = await top_assistants(10)
    if not players:
        await callback.message.edit_text("📊 Пока нет данных.", reply_markup=back_to_main_kb())
        return

    img_buffer = create_top_image("assists", players)

    await callback.message.answer_photo(
        photo=BufferedInputFile(img_buffer.getvalue(), filename="top_assistants.png"),
        caption=format_top_list("assists", players),
        reply_markup=back_to_main_kb()
    )


@user_router.callback_query(F.data == "user:top:goalkeepers")
async def show_top_goalkeepers(callback: CallbackQuery):
    await callback.answer()
    players = await top_goalkeepers(10)
    if not players:
        await callback.message.edit_text("📊 Пока нет данных.", reply_markup=back_to_main_kb())
        return

    # Для вратарей статистика в строке будет "saves"
    img_buffer = create_top_image("goalkeepers", players)

    await callback.message.answer_photo(
        photo=BufferedInputFile(img_buffer.getvalue(), filename="top_goalkeepers.png"),
        caption=format_top_goalkeepers(players), # Используем отдельную функцию для вратарей
        reply_markup=back_to_main_kb()
    )

# ... (Остальная часть кода с командами, поиском и админкой остается без изменений) ...

@user_router.callback_query(F.data == "user:teams")
async def show_team_roster_menu(callback: CallbackQuery):
    await callback.answer()
    teams = await get_teams()
    if not teams:
        await callback.message.edit_text(
            "📊 <b>Команды пока не добавлены.</b> Загляните позже 🙂",
            reply_markup=back_to_main_kb()
        )
        return
    await callback.message.edit_text("👥 <b>Выберите команду:</b>", reply_markup=teams_inline_kb(teams))

# ... (Остальной код без изменений)

# ---------- Точка входа ----------
async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрируем роутеры (админский – первым)
    dp.include_router(admin_router)
    dp.include_router(user_router)

    # Инициализируем БД
    await init_pool()

    try:
        await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Бот запущен, начинаю polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.critical("Критическая ошибка во время работы: %s", e)
        raise
    finally:
        await close_pool()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
