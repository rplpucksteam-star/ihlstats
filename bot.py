import asyncio
import html
import io  # ДОБАВЛЕНО ДЛЯ КАРТИНОК
import logging
import os
import re
from typing import List, Tuple, Optional, Dict, Any

import asyncpg
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont  # ДОБАВЛЕНО

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
    BufferedInputFile,  # ДОБАВЛЕНО ДЛЯ ОТПРАВКИ ФОТО
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
# Путь к шрифту с кириллицей
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
        # ИЗМЕНИТЕ РАЗМЕР ШРИФТА (число 36) НА НУЖНЫЙ, ЕСЛИ ТЕКСТ ВЫЛАЗИТ ЗА РАМКИ
        font = ImageFont.truetype(FONT_PATH, 32)
    except IOError:
        logger.warning(f"Шрифт {FONT_PATH} не найден, используется стандартный!")
        font = ImageFont.load_default()

    # ИЗМЕНЕНИЕ: теперь берём только первых 9 игроков (было [:10])
    for idx, player in enumerate(players[:9]):
        if idx >= len(ROW_COORDS): break

        x, y = ROW_COORDS[idx]
        
        medal = ["🥇", "🥈", "🥉"][idx] if idx < 3 else f"{idx+1}."
        
        # Форматируем название команды (она уже приходит в SQL запросе как team_name)
        team_name = player["team_name"] if player["team_name"] else "Без команды"
        
        # Формируем текст в зависимости от типа статистики
        if stat_key == "goalkeepers":
            # Ник Номер | Команда | %ОБ | a/b
            save_pct = player["save_pct"]
            saves = player["saves"]
            shots = player["shots_against"]
            text = f"{medal} {player['nickname']} #{player['number']} | {team_name} | {save_pct}% ОБ | {saves}/{shots}"
        else:
            # Ник Номер | Команда | Количество данных
            value = player[stat_key]
            text = f"{medal} {player['nickname']} #{player['number']} | {team_name} | {value}"

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


# ---------- Запросы к БД ----------
async def create_team(name: str, emoji_char: Optional[str], custom_emoji_id: Optional[str]):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            INSERT INTO teams (name, emoji_char, custom_emoji_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (name) DO UPDATE
                SET emoji_char = EXCLUDED.emoji_char,
                    custom_emoji_id = EXCLUDED.custom_emoji_id
            RETURNING *;
            """,
            name,
            emoji_char,
            custom_emoji_id,
        )


async def delete_team(team_id: int) -> None:
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM teams WHERE id = $1;", team_id)


async def get_teams():
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM teams ORDER BY name;")


async def get_team(team_id: int):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM teams WHERE id = $1;", team_id)


async def upsert_roster_player(nickname: str, number: int, team_id: int):
    """Привязывает игрока к команде, создаёт при отсутствии."""
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM players WHERE LOWER(nickname) = LOWER($1) AND number = $2;",
            nickname,
            number,
        )
        if existing:
            await conn.execute(
                "UPDATE players SET team_id = $1 WHERE id = $2;", team_id, existing["id"]
            )
            return existing["id"], False

        row = await conn.fetchrow(
            "INSERT INTO players (nickname, number, team_id) VALUES ($1, $2, $3) RETURNING id;",
            nickname,
            number,
            team_id,
        )
        return row["id"], True


async def clear_team_roster_except(team_id: int, keep_player_ids: List[int]) -> None:
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        if keep_player_ids:
            await conn.execute(
                "UPDATE players SET team_id = NULL WHERE team_id = $1 AND NOT (id = ANY($2::int[]));",
                team_id,
                keep_player_ids,
            )
        else:
            await conn.execute("UPDATE players SET team_id = NULL WHERE team_id = $1;", team_id)


async def get_team_roster(team_id: int):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT *, (goals + assists) AS points
            FROM players
            WHERE team_id = $1
            ORDER BY is_goalkeeper ASC, points DESC, goals DESC, nickname ASC;
            """,
            team_id,
        )


async def search_players(nickname_query: str):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT p.*, t.name AS team_name, t.emoji_char, t.custom_emoji_id,
                   (p.goals + p.assists) AS points
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            WHERE LOWER(p.nickname) LIKE LOWER($1)
            ORDER BY p.nickname
            LIMIT 20;
            """,
            f"%{nickname_query}%",
        )


async def get_player_full(player_id: int):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT p.*, t.name AS team_name, t.emoji_char, t.custom_emoji_id,
                   (p.goals + p.assists) AS points
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            WHERE p.id = $1;
            """,
            player_id,
        )


async def get_or_create_player(nickname: str, number: int):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM players WHERE LOWER(nickname) = LOWER($1) AND number = $2;",
            nickname,
            number,
        )
        if row:
            return row, False
        row = await conn.fetchrow(
            "INSERT INTO players (nickname, number) VALUES ($1, $2) RETURNING *;",
            nickname,
            number,
        )
        return row, True


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


async def apply_goalkeeper_stats(nickname: str, number: int, saves: int, shots: int, sign: str):
    row, created = await get_or_create_player(nickname, number)
    mult = 1 if sign == "+" else -1
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE players
            SET saves = GREATEST(saves + $1, 0),
                shots_against = GREATEST(shots_against + $2, 0),
                matches_played = GREATEST(matches_played + $3, 0),
                is_goalkeeper = TRUE
            WHERE id = $4;
            """,
            mult * saves,
            mult * shots,
            mult * 1,
            row["id"],
        )
    return row["id"], created


async def top_scorers(limit: int = 10):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT p.*, t.name AS team_name, t.emoji_char, t.custom_emoji_id, (p.goals + p.assists) AS points
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            WHERE p.is_goalkeeper = FALSE
            ORDER BY points DESC, p.goals DESC, p.nickname ASC
            LIMIT $1;
            """,
            limit,
        )


async def top_snipers(limit: int = 10):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT p.*, t.name AS team_name, t.emoji_char, t.custom_emoji_id, (p.goals + p.assists) AS points
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            WHERE p.is_goalkeeper = FALSE
            ORDER BY p.goals DESC, p.assists DESC, p.nickname ASC
            LIMIT $1;
            """,
            limit,
        )


async def top_assistants(limit: int = 10):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT p.*, t.name AS team_name, t.emoji_char, t.custom_emoji_id, (p.goals + p.assists) AS points
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            WHERE p.is_goalkeeper = FALSE
            ORDER BY p.assists DESC, p.goals DESC, p.nickname ASC
            LIMIT $1;
            """,
            limit,
        )


async def top_goalkeepers(limit: int = 10):
    if pool is None:
        raise RuntimeError("Пул не инициализирован")
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT p.*, t.name AS team_name, t.emoji_char, t.custom_emoji_id,
                   CASE WHEN p.shots_against > 0 THEN ROUND((p.saves::numeric / p.shots_against::numeric) * 100, 1) ELSE 0 END AS save_pct
            FROM players p
            LEFT JOIN teams t ON p.team_id = t.id
            WHERE p.is_goalkeeper = TRUE AND p.matches_played > 0
            ORDER BY save_pct DESC, p.saves DESC, p.matches_played DESC, p.nickname ASC
            LIMIT $1;
            """,
            limit,
        )


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


def format_top_list(stat_key: str, players) -> str:
    title = STAT_TITLES[stat_key]
    label = STAT_LABELS[stat_key]
    if not players:
        return f"<b>{title}</b>\n\n📊 Пока нет данных для отображения."

    lines = [f"<b>{title}</b>\n"]
    for idx, p in enumerate(players, start=1):
        marker = MEDALS.get(idx, f"<b>{idx}.</b>")
        team_str = _team_label_html(p)
        value = p[stat_key]
        nick = html.escape(p['nickname'])
        lines.append(
            f"{marker} <b>{nick} #{p['number']}</b> — {team_str}\n"
            f"     ▸ <b>{value}</b> {label} (👕 Матчей: {p['matches_played']})"
        )
    return "\n".join(lines)


def format_top_goalkeepers(players) -> str:
    title = "🥅 Топ-10 вратарей (% отраженных бросков)"
    if not players:
        return f"<b>{title}</b>\n\n📊 Пока нет данных для отображения."

    lines = [f"<b>{title}</b>\n"]
    for idx, p in enumerate(players, start=1):
        marker = MEDALS.get(idx, f"<b>{idx}.</b>")
        team_str = _team_label_html(p)
        save_pct = p["save_pct"]
        nick = html.escape(p['nickname'])
        lines.append(
            f"{marker} <b>{nick} #{p['number']}</b> — {team_str}\n"
            f"     ▸ <b>{save_pct}% ОБ</b> ({p['saves']}/{p['shots_against']} бросков, 👕 Матчей: {p['matches_played']})"
        )
    return "\n".join(lines)


async def get_main_menu_text() -> str:
    """Формирует текст главного меню с лидерами лиги."""
    best_gk = await top_goalkeepers(1)
    best_sn = await top_snipers(1)
    best_sc = await top_scorers(1)
    best_as = await top_assistants(1)

    def _format_leader(lst, val_key=None, suffix=""):
        if not lst:
            return "Пока нет данных"
        p = lst[0]
        nick = html.escape(p['nickname'])
        team_str = _team_label_html(p)
        if val_key:
            val = p[val_key]
            return f"<b>{nick} #{p['number']}</b> — {team_str} (<b>{val}{suffix}</b>)"
        return f"<b>{nick} #{p['number']}</b> — {team_str}"

    gk_str = _format_leader(best_gk, "save_pct", "% ОБ")
    sn_str = _format_leader(best_sn, "goals", " гол.")
    sc_str = _format_leader(best_sc, "points", " очк.")
    as_str = _format_leader(best_as, "assists", " пас.")

    return (
        "🏒 <b>Добро пожаловать в бота Innovative Hockey League!</b>\n\n"
        f"🥅 <b>Самый Лучший Вратарь Лиги:</b> {gk_str}\n"
        f"🎯 <b>Самый Лучший Снайпер Лиги:</b> {sn_str}\n"
        f"🏆 <b>Самый Лучший Бомбардир Лиги:</b> {sc_str}\n"
        f"🅰️ <b>Самый Лучший Ассистент Лиги:</b> {as_str}\n\n"
        "Выберите интересующий раздел из меню ниже 👇"
    )


# ---------- Клавиатуры ----------
BTN_MAIN_MENU = "🏠 Главное меню"


def persistent_reply_kb() -> ReplyKeyboardMarkup:
    """Нижняя постоянная клавиатура."""
    keyboard = [
        [KeyboardButton(text=BTN_MAIN_MENU)]
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def main_menu_inline_kb() -> InlineKeyboardMarkup:
    """Встроенные кнопки главного меню под сообщением."""
    rows = [
        [InlineKeyboardButton(text="🏆 Топ-10 бомбардиров", callback_data="user:top:scorers")],
        [
            InlineKeyboardButton(text="🎯 Топ-10 снайперов", callback_data="user:top:snipers"),
            InlineKeyboardButton(text="🅰️ Топ-10 ассистентов", callback_data="user:top:assists")
        ],
        [InlineKeyboardButton(text="🥅 Топ-10 вратарей", callback_data="user:top:goalkeepers")],
        [
            InlineKeyboardButton(text="👥 Состав команды", callback_data="user:teams"),
            InlineKeyboardButton(text="🔍 Найти игрока", callback_data="user:search")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_main_kb() -> InlineKeyboardMarkup:
    """Кнопка возврата в главное меню."""
    rows = [
        [InlineKeyboardButton(text="⬅️ Вернуться в главное меню", callback_data="user:main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teams_inline_kb(teams) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{t['emoji_char'] or '🏒'} {t['name']}",
            callback_data=f"team:{t['id']}"
        )]
        for t in teams
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Вернуться в главное меню", callback_data="user:main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def team_roster_back_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⬅️ Назад к выбору команд", callback_data="user:teams")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="user:main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def players_choice_kb(players) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"👤 {p['nickname']} #{p['number']}", callback_data=f"player:{p['id']}")]
        for p in players
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Вернуться в главное меню", callback_data="user:main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_main_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🏒 Управление командами", callback_data="adm:teams")],
        [InlineKeyboardButton(text="📋 Обновить состав команды", callback_data="adm:roster")],
        [InlineKeyboardButton(text="➕ Внести очки / статистику", callback_data="adm:points")],
        [InlineKeyboardButton(text="🚪 Выйти из админки", callback_data="adm:exit")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_teams_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Создать команду", callback_data="adm:team:create")],
        [InlineKeyboardButton(text="🗑 Удалить команду", callback_data="adm:team:delete")],
        [InlineKeyboardButton(text="📃 Список команд", callback_data="adm:team:list")],
        [InlineKeyboardButton(text="⬅️ Назад в меню админки", callback_data="adm:back")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_cancel_kb(back_to: str = "adm:back") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="⬅️ Назад / Отмена", callback_data=back_to)]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def teams_list_kb(teams, callback_prefix: str, back_to: str = "adm:teams") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{t['emoji_char'] or '🏒'} {t['name']}", callback_data=f"{callback_prefix}:{t['id']}")]
        for t in teams
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_to)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


# --- ОБНОВЛЕННЫЕ ОБРАБОТЧИКИ ТОПОВ (ОТПРАВКА ФОТО) ---

@user_router.callback_query(F.data == "user:top:scorers")
async def show_top_scorers(callback: CallbackQuery):
    await callback.answer()
    players = await top_scorers(10)
    if not players:
        await callback.message.edit_text("📊 Пока нет данных.", reply_markup=back_to_main_kb())
        return
    
    # Генерируем картинку (теперь только 9 игроков)
    img_buffer = create_top_image("points", players)
    
    # Отправляем фото
    await callback.message.answer_photo(
        photo=BufferedInputFile(img_buffer.getvalue(), filename="top_scorers.png"),
        caption=format_top_list("points", players),
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

    img_buffer = create_top_image("goalkeepers", players)

    await callback.message.answer_photo(
        photo=BufferedInputFile(img_buffer.getvalue(), filename="top_goalkeepers.png"),
        caption=format_top_goalkeepers(players),
        reply_markup=back_to_main_kb()
    )


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


@user_router.callback_query(F.data.startswith("team:"))
async def show_team_roster(callback: CallbackQuery):
    team_id = int(callback.data.split(":")[1])
    team = await get_team(team_id)
    if team is None:
        await callback.answer("⚠️ Команда не найдена", show_alert=True)
        return
    await callback.answer()
    roster = await get_team_roster(team_id)

    m = Msg()
    m.add_custom_emoji(team["emoji_char"], team["custom_emoji_id"])
    m.add_bold(f" {team['name']}\n")
    m.add_text(f"👥 Игроков в составе: {len(roster)}\n\n")

    if not roster:
        m.add_text("📋 Состав пока пуст.")
    else:
        for p in roster:
            if p["is_goalkeeper"]:
                pct = round(p["saves"] / p["shots_against"] * 100, 1) if p["shots_against"] else 0.0
                m.add_bold(f"🥅 {p['nickname']} #{p['number']}\n")
                m.add_text(
                    f"    👕 Матчей: {p['matches_played']} · 🛡 Отражено: {p['saves']}/{p['shots_against']} ({pct}%)\n\n"
                )
            else:
                m.add_bold(f"⛸ {p['nickname']} #{p['number']}\n")
                m.add_text(
                    f"    👕 Матчей: {p['matches_played']} · 🎯 Г: {p['goals']} · 🅰️ П: {p['assists']} · 🏆 О: {p['points']}\n\n"
                )

    await send_msg(callback.message, m, reply_markup=team_roster_back_kb())


@user_router.callback_query(F.data == "user:search")
async def find_player_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(UserSearchStates.waiting_nickname)
    await callback.message.edit_text(
        "🔍 <b>Введите ник игрока (без номера):</b>",
        reply_markup=back_to_main_kb()
    )


async def _send_player_card(target: Message, p) -> None:
    m = Msg()
    icon = "🥅" if p["is_goalkeeper"] else "⛸"
    m.add_text(f"{icon} ")
    m.add_bold(f"{p['nickname']} #{p['number']}\n")

    if p["team_name"]:
        m.add_custom_emoji(p["emoji_char"], p["custom_emoji_id"])
        m.add_text(f" {p['team_name']}\n\n")
    else:
        m.add_text(f"{NO_TEAM_LABEL}\n\n")

    m.add_text(f"👕 Матчей сыграно: {p['matches_played']}\n")
    if p["is_goalkeeper"]:
        pct = round(p["saves"] / p["shots_against"] * 100, 1) if p["shots_against"] else 0.0
        m.add_text(f"🛡 Отражено бросков: {p['saves']}/{p['shots_against']} ({pct}%)\n")
    else:
        m.add_text(f"🎯 Голы: {p['goals']}\n🅰️ Передачи: {p['assists']}\n🏆 Очки: {p['points']}\n")

    await send_msg(target, m, reply_markup=back_to_main_kb())


@user_router.message(UserSearchStates.waiting_nickname)
async def find_player_process(message: Message, state: FSMContext):
    await state.clear()
    query = (message.text or "").strip()
    if not query:
        await message.answer(
            "⚠️ Пустой запрос. Попробуйте ещё раз через главное меню.",
            reply_markup=back_to_main_kb()
        )
        return

    players = await search_players(query)
    if not players:
        await message.answer(
            "😕 Игрок не найден. Проверьте ник и попробуйте снова.",
            reply_markup=back_to_main_kb()
        )
        return
    if len(players) == 1:
        await _send_player_card(message, players[0])
        return

    await message.answer(
        f"🔍 Найдено несколько игроков ({len(players)}). Выберите нужного:",
        reply_markup=players_choice_kb(players),
    )


@user_router.callback_query(F.data.startswith("player:"))
async def show_player_by_callback(callback: CallbackQuery):
    player_id = int(callback.data.split(":")[1])
    player = await get_player_full(player_id)
    if player is None:
        await callback.answer("⚠️ Игрок не найден", show_alert=True)
        return
    await callback.answer()
    await _send_player_card(callback.message, player)


# ---------- Админские хендлеры ----------
admin_router = Router()
ADMIN_TITLE = "🛠 <b>Админ-панель Innovative Hockey League</b>\n\nВыберите нужный раздел:"


@admin_router.message(Command("adminka"))
async def cmd_adminka(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔️ У вас нет доступа к админ-панели.")
        return
    await state.clear()
    await message.answer(ADMIN_TITLE, reply_markup=admin_main_kb())


@admin_router.callback_query(F.data == "adm:exit")
async def adm_exit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    await callback.answer("🚪 Вы вышли из админки")
    await state.clear()
    text = await get_main_menu_text()
    await callback.message.edit_text(text, reply_markup=main_menu_inline_kb())


@admin_router.callback_query(F.data == "adm:back")
async def adm_back(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await callback.message.edit_text(ADMIN_TITLE, reply_markup=admin_main_kb())


# --- Команды ---
@admin_router.callback_query(F.data == "adm:teams")
async def adm_teams_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    await callback.message.edit_text("🏒 <b>Управление командами:</b>\n\nВыберите нужный пункт:", reply_markup=admin_teams_kb())


@admin_router.callback_query(F.data == "adm:team:create")
async def adm_team_create_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminTeamStates.creating_name)
    await callback.message.edit_text(
        "✏️ <b>Отправьте название команды.</b>\n\n"
        "💎 <i>Подсказка:</i> Если хотите прикрепить премиум-эмодзи как логотип — вставьте его прямо в текст названия (в любом месте), бот сам его распознает и сохранит.\n\n"
        "Например: <code>&lt;эмодзи&gt; ФК Атлант</code>",
        reply_markup=admin_cancel_kb("adm:teams")
    )


@admin_router.message(AdminTeamStates.creating_name)
async def adm_team_create_finish(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    name, fallback, custom_emoji_id = parse_team_name_message(message)
    if not name:
        await message.answer(
            "⚠️ Название не может быть пустым. Попробуйте снова.",
            reply_markup=admin_main_kb()
        )
        return
    team = await create_team(name, fallback, custom_emoji_id)
    logo_note = " (с логотипом ✨)" if custom_emoji_id else ""
    esc_name = html.escape(team['name'])
    await message.answer(
        f"✅ <b>Команда «{esc_name}»{logo_note} успешно создана!</b>\n\n"
        f"🛠 <b>Админ-панель:</b>",
        reply_markup=admin_main_kb()
    )


@admin_router.callback_query(F.data == "adm:team:delete")
async def adm_team_delete_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    teams = await get_teams()
    if not teams:
        await callback.answer("⚠️ Нет команд для удаления", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "🗑 <b>Выберите команду для удаления:</b>",
        reply_markup=teams_list_kb(teams, "adm:team:del", back_to="adm:teams")
    )


@admin_router.callback_query(F.data.startswith("adm:team:del:"))
async def adm_team_delete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    team_id = int(callback.data.split(":")[-1])
    team = await get_team(team_id)
    if team is None:
        await callback.answer("⚠️ Команда уже удалена", show_alert=True)
        return
    await callback.answer()
    await delete_team(team_id)
    esc_name = html.escape(team['name'])
    await callback.message.edit_text(
        f"🗑 <b>Команда «{esc_name}» удалена.</b>\n"
        f"ℹ️ Её игроки остались в базе, но теперь без привязки к команде.\n\n"
        f"🛠 <b>Админ-панель:</b>",
        reply_markup=admin_main_kb(),
    )


@admin_router.callback_query(F.data == "adm:team:list")
async def adm_team_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    await callback.answer()
    teams = await get_teams()
    if not teams:
        text = "📊 <b>Команд пока нет.</b>"
    else:
        text = "📃 <b>Список всех команд:</b>\n\n" + "\n".join(
            f"• {t['emoji_char'] or '🏒'} {html.escape(t['name'])}" for t in teams
        )
    await callback.message.edit_text(text, reply_markup=admin_teams_kb())


# --- Обновление состава ---
@admin_router.callback_query(F.data == "adm:roster")
async def adm_roster_menu(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    await state.clear()
    teams = await get_teams()
    if not teams:
        await callback.answer("⚠️ Сначала создайте хотя бы одну команду", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "📋 <b>Выберите команду, состав которой нужно обновить:</b>",
        reply_markup=teams_list_kb(teams, "adm:roster:team", back_to="adm:back"),
    )


@admin_router.callback_query(F.data.startswith("adm:roster:team:"))
async def adm_roster_team_selected(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    team_id = int(callback.data.split(":")[-1])
    team = await get_team(team_id)
    if team is None:
        await callback.answer("⚠️ Команда не найдена", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminRosterStates.waiting_list)
    await state.update_data(team_id=team_id)
    example = "miulio #9\nfrong #21\npetrov #64"
    esc_name = html.escape(team['name'])
    await callback.message.edit_text(
        f"📋 Команда: <b>{esc_name}</b>\n\n"
        f"✏️ Отправьте список игроков, каждый на новой строке, в формате:\n<code>{example}</code>\n\n"
        "⚠️ <i>Этот список <b>полностью заменит</b> текущий состав команды.</i>",
        reply_markup=admin_cancel_kb("adm:roster")
    )


@admin_router.message(AdminRosterStates.waiting_list)
async def adm_roster_list_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    team_id = data.get("team_id")
    await state.clear()

    if team_id is None:
        await message.answer("⚠️ Ошибка контекста, начните заново.", reply_markup=admin_main_kb())
        return

    entries, errors = parse_roster_lines(message.text or "")
    if not entries:
        await message.answer(
            "⚠️ Не удалось разобрать ни одной строки. Проверьте формат "
            "(ник #номер, каждый игрок на новой строке) и попробуйте снова.",
            reply_markup=admin_main_kb()
        )
        return

    keep_ids: List[int] = []
    created, updated = 0, 0
    for nickname, number in entries:
        player_id, was_created = await upsert_roster_player(nickname, number, team_id)
        keep_ids.append(player_id)
        if was_created:
            created += 1
        else:
            updated += 1

    await clear_team_roster_except(team_id, keep_ids)
    team = await get_team(team_id)
    esc_name = html.escape(team['name'])

    summary = (
        f"✅ <b>Состав команды «{esc_name}» успешно обновлён!</b>\n\n"
        f"🆕 Новых игроков: {created}\n"
        f"🔄 Привязано существующих: {updated}"
    )
    if errors:
        bad = "\n".join(f"• строка {i}: {html.escape(line)}" for i, line in errors)
        summary += f"\n\n⚠️ <b>Не распознаны строки:</b>\n{bad}"

    summary += "\n\n🛠 <b>Админ-панель:</b>"
    await message.answer(summary, reply_markup=admin_main_kb())


# --- Добавление очков ---
@admin_router.callback_query(F.data == "adm:points")
async def adm_points_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    await callback.answer()
    await state.set_state(AdminPointsStates.waiting_list)
    example = "miulio #9 2 8 +\ndube #42 15/18 + gk\nsigma #1 4 5 +"
    await callback.message.edit_text(
        "➕ <b>Отправьте статистику матча</b> — каждая строка отдельным игроком.\n\n"
        "⛸ <b>Полевые игроки:</b>\n<code>ник #номер голы передачи +/-</code>\n\n"
        "🥅 <b>Вратари:</b>\n<code>ник #номер отражено/всего +/- gk</code>\n\n"
        f"📌 <b>Пример:</b>\n<code>{example}</code>\n\n"
        "🔹 «+» — добавить статистику и засчитать матч, «−» — вычесть.\n"
        "🔹 Если игрока нет в базе — он будет создан автоматически.",
        reply_markup=admin_cancel_kb("adm:back")
    )


@admin_router.message(AdminPointsStates.waiting_list)
async def adm_points_process(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()

    results, errors = parse_points_lines(message.text or "")
    if not results:
        await message.answer(
            "⚠️ Не удалось разобрать ни одной строки. Проверьте формат и попробуйте снова.",
            reply_markup=admin_main_kb()
        )
        return

    report_lines = []
    for r in results:
        nick = html.escape(r['nickname'])
        if r["type"] == "gk":
            _, created = await apply_goalkeeper_stats(
                r["nickname"], r["number"], r["saves"], r["shots"], r["sign"]
            )
            tag = "🆕" if created else "✏️"
            report_lines.append(
                f"{tag} 🥅 <b>{nick} #{r['number']}</b>: {r['sign']} {r['saves']}/{r['shots']}"
            )
        else:
            _, created = await apply_skater_stats(
                r["nickname"], r["number"], r["goals"], r["assists"], r["sign"]
            )
            tag = "🆕" if created else "✏️"
            report_lines.append(
                f"{tag} ⛸ <b>{nick} #{r['number']}</b>: {r['sign']} Г:{r['goals']} П:{r['assists']}"
            )

    summary = "✅ <b>Статистика успешно обновлена!</b>\n\n" + "\n".join(report_lines)
    if errors:
        bad = "\n".join(f"• строка {i}: {html.escape(line)}" for i, line in errors)
        summary += f"\n\n⚠️ <b>Не распознаны строки:</b>\n{bad}"

    summary += "\n\n🛠 <b>Админ-панель:</b>"
    await message.answer(summary, reply_markup=admin_main_kb())


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
