import asyncio
import logging
import os
import sqlite3
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Message, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import aiosqlite

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("8657731994:AAFgwuJbbd2fqvtXUqapczb9Y1I1ajW-FDM")
if not BOT_TOKEN:
    raise ValueError("❌ Не найден BOT_TOKEN в переменных окружения")

ADMIN_IDS_STR = os.getenv("5706071030", "")
ADMIN_IDS = [int(id_str.strip()) for id_str in ADMIN_IDS_STR.split(",") if id_str.strip()]

if not ADMIN_IDS:
    print("⚠️ ВНИМАНИЕ: ADMIN_IDS не указаны.")

# Используем только SQLite для простоты
USE_POSTGRES = False
DB_PATH = "hockey_cards.db"
BACKUP_DIR = "/tmp/backups" if os.getenv("RENDER") else "backups"

# Создаем папку для бекапов
Path(BACKUP_DIR).mkdir(exist_ok=True)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- ИНИЦИАЛИЗАЦИЯ ---
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# КД на получение карточки - 4 часа
CARD_COOLDOWN_HOURS = 4

# --- СОСТОЯНИЯ ДЛЯ FSM ---
class CardCreation(StatesGroup):
    waiting_for_name = State()
    waiting_for_photo = State()
    waiting_for_rarity = State()
    waiting_for_team = State()

class PromoCreation(StatesGroup):
    waiting_for_code = State()
    waiting_for_card_selection = State()
    waiting_for_amount = State()
    waiting_for_uses = State()

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица карточек
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                photo_id TEXT NOT NULL,
                rarity TEXT NOT NULL,
                team TEXT NOT NULL
            )
        """)
        
        # Таблица пользователей и их карточек
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                card_id INTEGER NOT NULL,
                obtained_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (card_id) REFERENCES cards (id) ON DELETE CASCADE
            )
        """)
        
        # Таблица промокодов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promocodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                bonus_card_id INTEGER,
                bonus_cards_amount INTEGER DEFAULT 1,
                max_uses INTEGER DEFAULT 1,
                uses INTEGER DEFAULT 0,
                created_by INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)
        
        # Таблица использования промокодов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                promo_id INTEGER NOT NULL,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, promo_id)
            )
        """)
        
        await db.commit()
    
    logger.info("✅ База данных инициализирована")

# --- ФУНКЦИИ ДЛЯ БЕКАПОВ ---
async def create_backup() -> Optional[str]:
    """Создает резервную копию БД и возвращает путь к файлу."""
    if not os.path.exists(DB_PATH):
        return None
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = Path(BACKUP_DIR) / f"backup_{timestamp}.db"
    
    shutil.copy2(DB_PATH, backup_path)
    logger.info(f"💾 Создан бекап: {backup_path}")
    return str(backup_path)

async def send_backup_to_admins():
    """Отправляет бекап всем админам."""
    backup_path = await create_backup()
    if not backup_path:
        return
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_document(
                admin_id,
                FSInputFile(backup_path),
                caption=f"📅 Автоматический бекап от {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить бекап админу {admin_id}: {e}")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# --- КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "🏒 Добро пожаловать в коллекционирование хоккейных карточек МХЛ!\n\n"
        "📋 Мои команды:\n"
        "/cards — посмотреть свои карточки\n"
        "/get_card — получить новую карточку (раз в 4 часа)\n"
        "/promo — активировать промокод\n\n"
        f"⏱ КД на получение: {CARD_COOLDOWN_HOURS} часа"
    )

@dp.message(Command("get_card"))
async def cmd_get_card(message: Message):
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверка времени последнего получения
        cursor = await db.execute(
            "SELECT obtained_at FROM user_cards WHERE user_id = ? ORDER BY obtained_at DESC LIMIT 1",
            (user_id,)
        )
        last_card = await cursor.fetchone()
        
        if last_card:
            last_time = datetime.fromisoformat(last_card[0].replace('Z', '+00:00'))
            time_diff = datetime.now(last_time.tzinfo) - last_time
            # Конвертируем в часы
            hours_passed = time_diff.total_seconds() / 3600
            
            if hours_passed < CARD_COOLDOWN_HOURS:
                wait_hours = CARD_COOLDOWN_HOURS - hours_passed
                hours = int(wait_hours)
                minutes = int((wait_hours - hours) * 60)
                await message.answer(f"⏳ Следующую карточку можно получить через {hours} ч. {minutes} мин.")
                return
        
        # Выдача случайной карточки
        cursor = await db.execute("SELECT id, name, photo_id, rarity, team FROM cards ORDER BY RANDOM() LIMIT 1")
        card = await cursor.fetchone()
        
        if not card:
            await message.answer("😕 В базе пока нет карточек. Зайдите позже.")
            return
        
        card_id, name, photo_id, rarity, team = card
        
        # Добавляем карточку пользователю
        await db.execute(
            "INSERT INTO user_cards (user_id, card_id) VALUES (?, ?)",
            (user_id, card_id)
        )
        await db.commit()
        
        # Отправляем карточку
        await message.answer_photo(
            photo_id,
            caption=f"🎴 <b>Вы получили новую карточку!</b>\n\n"
                    f"<b>{name}</b>\n"
                    f"Редкость: {rarity}\n"
                    f"Команда: {team}",
            parse_mode="HTML"
        )

@dp.message(Command("cards"))
async def cmd_my_cards(message: Message):
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT c.id, c.name, c.rarity, c.team, COUNT(uc.id) as count
            FROM user_cards uc
            JOIN cards c ON uc.card_id = c.id
            WHERE uc.user_id = ?
            GROUP BY c.id
            ORDER BY count DESC
        """, (user_id,))
        cards = await cursor.fetchall()
        
        if not cards:
            await message.answer("😴 У вас пока нет карточек. Получите первую: /get_card")
            return
        
        text = "🎴 <b>Ваша коллекция:</b>\n\n"
        for i, (_, name, rarity, team, count) in enumerate(cards, 1):
            text += f"{i}. {name} ({rarity}, {team}) — {count} шт.\n"
        
        await message.answer(text, parse_mode="HTML")

@dp.message(Command("promo"))
async def cmd_promo_input(message: Message):
    await message.answer("🔑 Введите промокод:")

@dp.message(F.text)
async def handle_promo_input(message: Message):
    if message.text.startswith('/'):
        return
    
    code = message.text.strip().upper()
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем промокод
        cursor = await db.execute(
            "SELECT id, bonus_card_id, bonus_cards_amount, max_uses, uses, is_active FROM promocodes WHERE code = ?",
            (code,)
        )
        promo = await cursor.fetchone()
        
        if not promo:
            await message.answer("❌ Промокод не найден")
            return
        
        promo_id, card_id, amount, max_uses, uses, is_active = promo
        
        if not is_active:
            await message.answer("❌ Промокод не активен")
            return
        
        if uses >= max_uses:
            await message.answer("❌ Достигнут лимит использования промокода")
            return
        
        # Проверяем, использовал ли уже этот промокод пользователь
        cursor = await db.execute(
            "SELECT id FROM promo_uses WHERE user_id = ? AND promo_id = ?",
            (user_id, promo_id)
        )
        if await cursor.fetchone():
            await message.answer("❌ Вы уже использовали этот промокод")
            return
        
        # Выдаем карточки
        for _ in range(amount):
            await db.execute(
                "INSERT INTO user_cards (user_id, card_id) VALUES (?, ?)",
                (user_id, card_id)
            )
        
        # Записываем использование
        await db.execute(
            "INSERT INTO promo_uses (user_id, promo_id) VALUES (?, ?)",
            (user_id, promo_id)
        )
        await db.execute(
            "UPDATE promocodes SET uses = uses + 1 WHERE id = ?",
            (promo_id,)
        )
        await db.commit()
        
        # Получаем название карточки
        cursor = await db.execute("SELECT name FROM cards WHERE id = ?", (card_id,))
        card = await cursor.fetchone()
        card_name = card[0] if card else "Неизвестная карточка"
        
        await message.answer(f"✅ Промокод активирован! Вы получили {amount} карточек: {card_name}")

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ У вас нет доступа к админ-панели.")
        return
    
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Создать карточку", callback_data="admin:create_card")
    keyboard.button(text="🎫 Управление промокодами", callback_data="admin:promo_menu")
    keyboard.button(text="💾 Создать бекап", callback_data="admin:create_backup")
    keyboard.button(text="📊 Статистика", callback_data="admin:stats")
    keyboard.adjust(2, 1, 1)
    
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=keyboard.as_markup(), parse_mode="HTML")

@dp.callback_query(F.data == "admin:create_backup")
async def admin_create_backup(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    
    await callback.answer("Создаю бекап...")
    backup_path = await create_backup()
    
    if backup_path:
        await bot.send_document(
            callback.from_user.id,
            FSInputFile(backup_path),
            caption=f"📦 Ручной бекап от {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        await callback.message.answer("✅ Бекап создан и отправлен в ЛС")
    else:
        await callback.message.answer("❌ Не удалось создать бекап")

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    
    await callback.answer()
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM cards")
        total_cards = (await cursor.fetchone())[0]
        
        cursor = await db.execute("SELECT COUNT(DISTINCT user_id) FROM user_cards")
        total_users = (await cursor.fetchone())[0]
        
        cursor = await db.execute("SELECT COUNT(*) FROM user_cards")
        total_collected = (await cursor.fetchone())[0]
        
        cursor = await db.execute("SELECT COUNT(*) FROM promocodes")
        total_promos = (await cursor.fetchone())[0]
    
    text = (f"📊 <b>Статистика бота:</b>\n"
            f"👥 Пользователей: {total_users}\n"
            f"🎴 Всего карточек в игре: {total_cards}\n"
            f"🃏 Собрано карточек: {total_collected}\n"
            f"🎫 Промокодов: {total_promos}")
    
    await callback.message.answer(text, parse_mode="HTML")

# --- СОЗДАНИЕ КАРТОЧКИ ---
@dp.callback_query(F.data == "admin:create_card")
async def create_card_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    
    await callback.answer()
    await callback.message.answer("Введите имя игрока:")
    await state.set_state(CardCreation.waiting_for_name)

@dp.message(CardCreation.waiting_for_name)
async def card_name_entered(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Отправьте фото карточки:")
    await state.set_state(CardCreation.waiting_for_photo)

@dp.message(CardCreation.waiting_for_photo, F.photo)
async def card_photo_entered(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    
    keyboard = InlineKeyboardBuilder()
    for rarity in ["Обычная", "Редкая", "Эпическая", "Легендарная"]:
        keyboard.button(text=rarity, callback_data=f"rarity:{rarity}")
    keyboard.adjust(2)
    
    await message.answer("Выберите редкость:", reply_markup=keyboard.as_markup())
    await state.set_state(CardCreation.waiting_for_rarity)

@dp.callback_query(CardCreation.waiting_for_rarity, F.data.startswith("rarity:"))
async def card_rarity_chosen(callback: CallbackQuery, state: FSMContext):
    rarity = callback.data.split(":")[1]
    await state.update_data(rarity=rarity)
    await callback.answer()
    await callback.message.edit_text(f"Редкость: {rarity}\nВведите название команды:")
    await state.set_state(CardCreation.waiting_for_team)

@dp.message(CardCreation.waiting_for_team)
async def card_team_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO cards (name, photo_id, rarity, team) VALUES (?, ?, ?, ?)",
            (data['name'], data['photo_id'], data['rarity'], message.text)
        )
        await db.commit()
    
    await message.answer(f"✅ Карточка '{data['name']}' успешно добавлена!")
    await state.clear()

# --- УПРАВЛЕНИЕ ПРОМОКОДАМИ ---
@dp.callback_query(F.data == "admin:promo_menu")
async def promo_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    
    await callback.answer()
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Создать промокод", callback_data="promo:create")
    keyboard.button(text="📋 Список промокодов", callback_data="promo:list")
    keyboard.button(text="◀️ Назад", callback_data="admin:back")
    keyboard.adjust(1)
    
    await callback.message.edit_text("🎫 Управление промокодами", reply_markup=keyboard.as_markup())

@dp.callback_query(F.data == "promo:create")
async def promo_create_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    
    await callback.answer()
    await callback.message.answer("Введите код промокода (например: START2024):")
    await state.set_state(PromoCreation.waiting_for_code)

@dp.message(PromoCreation.waiting_for_code)
async def promo_code_entered(message: Message, state: FSMContext):
    code = message.text.upper()
    await state.update_data(code=code)
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, name FROM cards ORDER BY id")
        cards = await cursor.fetchall()
        
        if not cards:
            await message.answer("В базе нет карточек. Сначала создайте карточку.")
            await state.clear()
            return
        
        text = "Выберите ID карточки для бонуса:\n\n"
        for card_id, name in cards:
            text += f"{card_id}. {name}\n"
        
        await state.update_data(cards_dict={str(card[0]): card[1] for card in cards})
    
    await message.answer(text)
    await state.set_state(PromoCreation.waiting_for_card_selection)

@dp.message(PromoCreation.waiting_for_card_selection)
async def promo_card_chosen(message: Message, state: FSMContext):
    try:
        card_id = int(message.text)
    except ValueError:
        await message.answer("Пожалуйста, введите число (ID карточки)")
        return
    
    data = await state.get_data()
    if str(card_id) not in data.get('cards_dict', {}):
        await message.answer("Неверный ID карточки. Попробуйте снова:")
        return
    
    await state.update_data(card_id=card_id)
    await message.answer("Введите количество карточек, которые получит пользователь:")
    await state.set_state(PromoCreation.waiting_for_amount)

@dp.message(PromoCreation.waiting_for_amount)
async def promo_amount_entered(message: Message, state: FSMContext):
    try:
        amount = int(message.text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите положительное число")
        return
    
    await state.update_data(amount=amount)
    await message.answer("Введите максимальное количество использований промокода:")
    await state.set_state(PromoCreation.waiting_for_uses)

@dp.message(PromoCreation.waiting_for_uses)
async def promo_uses_entered(message: Message, state: FSMContext):
    try:
        max_uses = int(message.text)
        if max_uses <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Пожалуйста, введите положительное число")
        return
    
    data = await state.get_data()
    
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO promocodes (code, bonus_card_id, bonus_cards_amount, max_uses, created_by) VALUES (?, ?, ?, ?, ?)",
                (data['code'], data['card_id'], data['amount'], max_uses, message.from_user.id)
            )
            await db.commit()
            await message.answer(f"✅ Промокод {data['code']} успешно создан!")
        except sqlite3.IntegrityError:
            await message.answer("❌ Промокод с таким названием уже существует")
    
    await state.clear()

@dp.callback_query(F.data == "promo:list")
async def promo_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    
    await callback.answer()
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT p.code, c.name, p.uses, p.max_uses, p.is_active
            FROM promocodes p 
            LEFT JOIN cards c ON p.bonus_card_id = c.id 
            ORDER BY p.created_at DESC 
            LIMIT 10
        """)
        promos = await cursor.fetchall()
        
        if not promos:
            await callback.message.answer("Нет созданных промокодов")
            return
        
        text = "🎫 <b>Последние промокоды:</b>\n\n"
        for code, card_name, uses, max_uses, is_active in promos:
            status = "✅" if is_active else "❌"
            text += (f"{status} <code>{code}</code>\n"
                    f"Карточка: {card_name}\n"
                    f"Использовано: {uses}/{max_uses}\n\n")
    
    await callback.message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "admin:back")
async def back_to_admin(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    
    await callback.answer()
    keyboard = InlineKeyboardBuilder()
    keyboard.button(text="➕ Создать карточку", callback_data="admin:create_card")
    keyboard.button(text="🎫 Управление промокодами", callback_data="admin:promo_menu")
    keyboard.button(text="💾 Создать бекап", callback_data="admin:create_backup")
    keyboard.button(text="📊 Статистика", callback_data="admin:stats")
    keyboard.adjust(2, 1, 1)
    
    await callback.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=keyboard.as_markup(), parse_mode="HTML")

# --- АВТОМАТИЧЕСКИЙ БЕКАП ---
async def scheduled_backup():
    logger.info("Выполнение автоматического бекапа по расписанию...")
    await send_backup_to_admins()

# --- ЗАПУСК ---
async def main():
    await init_db()
    
    # Настройка планировщика для ежедневного бекапа в 3:00
    scheduler.add_job(scheduled_backup, CronTrigger(hour=3, minute=0))
    scheduler.start()
    
    logger.info("🤖 Бот запущен!")
    
    # Удаляем вебхук на всякий случай
    await bot.delete_webhook(drop_pending_updates=True)
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
