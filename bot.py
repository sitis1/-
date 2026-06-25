import os
import random
import logging
import httpx
import urllib.parse
import psycopg2
import psycopg2.extras
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (Application, CommandHandler, MessageHandler,
                           CallbackQueryHandler, filters, ContextTypes,
                           ConversationHandler, JobQueue)
from groq import Groq

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GROQ_API_KEY   = os.environ["GROQ_API_KEY"]
ADMIN_ID       = int(os.environ.get("ADMIN_ID", "0"))
DATABASE_URL   = os.environ["DATABASE_URL"]
HF_TOKEN       = os.environ.get("HF_TOKEN", "")

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)

MENU, CHAT, PROPOSAL, BIO, PRICE, IMAGE = range(6)
BASE_PRICE_RUB = 200
BASE_PRICE_USDT = 2.2
TRIAL_DAYS = 1


# ─── БД ──────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE subscriptions
                    ADD COLUMN IF NOT EXISTS next_remind_at TIMESTAMP,
                    ADD COLUMN IF NOT EXISTS free_weekly_limit INT DEFAULT 2
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_uses (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    used_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    referrer_id BIGINT NOT NULL,
                    referred_id BIGINT PRIMARY KEY,
                    bonus_given BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    code VARCHAR(50) PRIMARY KEY,
                    discount INT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS promo_uses (
                    user_id BIGINT NOT NULL,
                    code VARCHAR(50) NOT NULL,
                    used_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (user_id, code)
                )
            """)
        conn.commit()


def get_promo_discount(code: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT discount FROM promo_codes WHERE code = %s", (code.strip().upper(),))
            row = cur.fetchone()
            return row[0] if row else None


def add_promo_code(code: str, discount: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO promo_codes (code, discount, created_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (code) DO UPDATE SET discount = EXCLUDED.discount
            """, (code.strip().upper(), discount))
        conn.commit()


def delete_promo_code(code: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM promo_codes WHERE code = %s", (code.strip().upper(),))
        conn.commit()


def list_promo_codes():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code, discount FROM promo_codes ORDER BY created_at DESC")
            return cur.fetchall()


def has_used_promo(user_id: int, code: str) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM promo_uses WHERE user_id = %s AND code = %s",
                (user_id, code.strip().upper())
            )
            return cur.fetchone() is not None


def mark_promo_used(user_id: int, code: str):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO promo_uses (user_id, code) VALUES (%s, %s)
                ON CONFLICT (user_id, code) DO NOTHING
            """, (user_id, code.strip().upper()))
        conn.commit()


def calc_price_with_promo(code: str = None):
    if code:
        discount = get_promo_discount(code)
        if discount:
            price_rub = round(BASE_PRICE_RUB * (1 - discount / 100))
            price_usdt = round(BASE_PRICE_USDT * (1 - discount / 100), 2)
            return price_rub, price_usdt, discount
    return BASE_PRICE_RUB, BASE_PRICE_USDT, 0


def get_subscription_end(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT expires_at FROM subscriptions WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None


def is_subscribed(user_id: int) -> bool:
    end = get_subscription_end(user_id)
    return end is not None and datetime.now() < end


def has_ever_started(user_id: int) -> bool:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM subscriptions WHERE user_id = %s", (user_id,))
            return cur.fetchone() is not None


def activate_trial(user_id: int):
    expires = datetime.now() + timedelta(days=TRIAL_DAYS)
    limit = random.randint(1, 3)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscriptions (user_id, expires_at, free_weekly_limit, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET expires_at = EXCLUDED.expires_at,
                        free_weekly_limit = EXCLUDED.free_weekly_limit,
                        updated_at = NOW()
            """, (user_id, expires, limit))
        conn.commit()


def activate_paid(user_id: int, days: int = 30):
    current = get_subscription_end(user_id)
    if current and current > datetime.now():
        expires = current + timedelta(days=days)
    else:
        expires = datetime.now() + timedelta(days=days)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscriptions (user_id, expires_at, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE
                    SET expires_at = EXCLUDED.expires_at,
                        next_remind_at = NULL,
                        updated_at = NOW()
            """, (user_id, expires))
        conn.commit()


def days_left(user_id: int) -> int:
    end = get_subscription_end(user_id)
    if not end:
        return 0
    return max(0, (end - datetime.now()).days)


# ─── Лимит бесплатных вопросов ───────────────────────────────

def get_free_chat_uses_this_week(user_id: int) -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM chat_uses
                WHERE user_id = %s AND used_at > NOW() - INTERVAL '7 days'
            """, (user_id,))
            return cur.fetchone()[0]


def get_free_weekly_limit(user_id: int) -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT free_weekly_limit FROM subscriptions WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return row[0] if row and row[0] else 2


def record_chat_use(user_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO chat_uses (user_id) VALUES (%s)", (user_id,))
        conn.commit()


def can_use_free_chat(user_id: int) -> bool:
    used = get_free_chat_uses_this_week(user_id)
    limit = get_free_weekly_limit(user_id)
    return used < limit


# ─── Реферальная программа ───────────────────────────────────

def save_referral(referrer_id: int, referred_id: int):
    if referrer_id == referred_id:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO referrals (referrer_id, referred_id)
                VALUES (%s, %s)
                ON CONFLICT (referred_id) DO NOTHING
            """, (referrer_id, referred_id))
        conn.commit()


def get_referrer(referred_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT referrer_id FROM referrals
                WHERE referred_id = %s AND bonus_given = FALSE
            """, (referred_id,))
            row = cur.fetchone()
            return row[0] if row else None


def mark_referral_bonus_given(referred_id: int):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE referrals SET bonus_given = TRUE WHERE referred_id = %s
            """, (referred_id,))
        conn.commit()


def get_referral_count(referrer_id: int) -> int:
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM referrals WHERE referrer_id = %s AND bonus_given = TRUE
            """, (referrer_id,))
            return cur.fetchone()[0]


# ─── Клавиатуры ──────────────────────────────────────────────

MAIN_KEYBOARD = [
    ["📨 Написать предложение клиенту", "👤 Составить биографию"],
    ["💰 Обосновать цену",              "🔔 Follow-up клиенту"],
    ["💬 Задать вопрос",                "📚 Советы новичку"],
    ["🎨 Генерация картинок",           "💳 Моя подписка"],
    ["🔗 Пригласить друга"],
]

FREE_KEYBOARD = [
    ["💬 Задать вопрос",    "📚 Советы новичку"],
    ["💳 Моя подписка",    "🔗 Пригласить друга"],
]


def main_menu_keyboard():
    return ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)


def free_menu_keyboard():
    return ReplyKeyboardMarkup(FREE_KEYBOARD, resize_keyboard=True)


def get_keyboard(user_id: int):
    return main_menu_keyboard() if is_subscribed(user_id) else free_menu_keyboard()


# ─── Проверка подписки ───────────────────────────────────────

async def check_sub(update: Update) -> bool:
    uid = update.effective_user.id
    if is_subscribed(uid):
        d = days_left(uid)
        if d <= 1:
            await update.message.reply_text(
                "⚠️ Твоя подписка заканчивается сегодня!\n"
                "Напиши /pay чтобы продлить и не потерять доступ."
            )
        return True
    await update.message.reply_text(
        "🔒 <b>Эта функция доступна только по подписке.</b>\n\n"
        "Стоимость: <b>200 руб/месяц</b>\n"
        "Напиши /pay для оплаты\n\n"
        "Или пригласи друга — получишь <b>7 дней бесплатно</b>! /ref",
        parse_mode="HTML"
    )
    return False


# ─── /start ──────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name
    username = update.effective_user.username

    # Обработка реферальной ссылки
    if ctx.args and ctx.args[0].startswith("ref_"):
        try:
            referrer_id = int(ctx.args[0][4:])
            if not has_ever_started(uid):
                save_referral(referrer_id, uid)
        except ValueError:
            pass

    if not has_ever_started(uid):
        activate_trial(uid)
        await update.message.reply_text(
            f"Привет, {name}! 👋\n\n"
            "🎁 Тебе активирован <b>бесплатный пробный период на 1 день!</b>\n\n"
            "Я — AI-помощник для фрилансеров. Помогу писать предложения клиентам, "
            "составлять профиль, обосновывать цену и отвечать на вопросы по фрилансу.\n\n"
            "📢 Подписывайся на наш канал: @freelanceburmalda\n\n"
            "После пробного периода подписка стоит <b>200 руб/месяц</b>.\n\n"
            "Выбери что тебе нужно 👇",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )
        uname_str = f"@{username}" if username else "без username"
        await notify_admin(ctx,
            f"🆕 <b>Новый пользователь!</b>\n"
            f"👤 {name} ({uname_str})\n"
            f"🆔 <code>{uid}</code>\n"
            f"🎁 Активирован пробный период на 1 день\n\n"
            f"Для активации платной подписки:\n<code>/activate {uid} 30</code>"
        )
    elif is_subscribed(uid):
        await update.message.reply_text(
            f"С возвращением, {name}! 👋\n\nВыбери что тебе нужно 👇",
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            f"С возвращением, {name}! 👋\n\n"
            "⚠️ Твой пробный период закончился.\n\n"
            "В <b>бесплатной версии</b> доступно:\n"
            "• 💬 Вопросы по фрилансу (лимит в неделю)\n"
            "• 📚 Советы\n\n"
            "Оформи подписку за <b>200 руб/мес</b> — получишь полный доступ.\n"
            "Напиши /pay или пригласи друга /ref 🎁",
            parse_mode="HTML",
            reply_markup=free_menu_keyboard()
        )
    return MENU


# ─── Напоминания (JobQueue) ───────────────────────────────────

REMINDER_TEXTS = [
    "👋 Привет! Напоминаю — твой пробный период закончился.\n\n"
    "Оформи подписку за <b>200 руб/мес</b> и получи полный доступ к AI-помощнику.\n"
    "👉 /pay\n\nИли пригласи друга и получи <b>7 дней бесплатно</b>: /ref",

    "💡 Знаешь ли ты, что подписчики зарабатывают на фрилансе на 40% больше, "
    "потому что умеют правильно подавать себя?\n\n"
    "Попробуй полный доступ за <b>200 руб/мес</b> 👉 /pay",

    "🔓 Разблокируй полный функционал:\n"
    "📨 Предложения клиентам\n"
    "👤 Создание биографии\n"
    "💰 Обоснование цены\n"
    "🔔 Follow-up письма\n\n"
    "Всего <b>200 руб/мес</b> 👉 /pay\n\nИли позови друга и получи неделю бесплатно: /ref",
]


async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT user_id FROM subscriptions
                    WHERE expires_at < NOW()
                      AND (next_remind_at IS NULL OR next_remind_at <= NOW())
                """)
                users = [row[0] for row in cur.fetchall()]

        for uid in users:
            if uid == ADMIN_ID:
                continue
            try:
                text = random.choice(REMINDER_TEXTS)
                await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
                next_remind = datetime.now() + timedelta(hours=random.randint(5, 12))
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE subscriptions SET next_remind_at = %s WHERE user_id = %s",
                            (next_remind, uid)
                        )
                    conn.commit()
            except Exception as e:
                logger.warning(f"Не удалось отправить напоминание {uid}: {e}")
    except Exception as e:
        logger.error(f"Ошибка в send_reminders: {e}")


# ─── Уведомление админа ──────────────────────────────────────

async def notify_admin(ctx: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await ctx.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Admin notify error: {e}")


# ─── Подписка ────────────────────────────────────────────────

async def subscription_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    end = get_subscription_end(uid)
    if end and datetime.now() < end:
        d = days_left(uid)
        await update.message.reply_text(
            f"💳 <b>Твоя подписка</b>\n\n"
            f"✅ Активна — осталось <b>{d} дн.</b>\n"
            f"До: {end.strftime('%d.%m.%Y %H:%M')}\n\n"
            f"Для продления напиши /pay",
            parse_mode="HTML"
        )
    else:
        used = get_free_chat_uses_this_week(uid)
        limit = get_free_weekly_limit(uid)
        await update.message.reply_text(
            "💳 <b>Подписка</b>\n\n"
            "❌ Нет активной подписки\n\n"
            f"💬 Бесплатных вопросов на этой неделе: <b>{used}/{limit}</b>\n\n"
            "Стоимость полного доступа: <b>200 руб/месяц</b>\n"
            "Напиши /pay для оплаты\n\n"
            "Или пригласи друга и получи <b>7 дней бесплатно</b>! /ref",
            parse_mode="HTML"
        )
    return MENU


async def pay_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    code = ctx.args[0] if ctx.args else None

    if code:
        discount = get_promo_discount(code)
        if not discount:
            await update.message.reply_text(
                "❌ Промокод не найден или недействителен.\n\nПопробуй другой или напиши /pay без кода."
            )
            return MENU
        if has_used_promo(uid, code):
            await update.message.reply_text(
                "⛔ Этот промокод уже был использован в этом чате.\n\n"
                "Каждый промокод одноразовый. Напиши /pay без кода для оплаты по полной цене."
            )
            return MENU
        mark_promo_used(uid, code)
        price_rub, price_usdt, discount = calc_price_with_promo(code)
    else:
        price_rub, price_usdt, discount = BASE_PRICE_RUB, BASE_PRICE_USDT, 0

    discount_line = f"🎁 Промокод применён: скидка <b>{discount}%</b>\n\n" if discount else ""

    await update.message.reply_text(
        f"💳 <b>Оплата подписки</b>\n\n"
        f"{discount_line}"
        f"Стоимость: <b>{price_rub} руб/месяц</b> (≈ {price_usdt} USDT)\n\n"
        "Оплата через <b>@CryptoBot</b> в Telegram:\n\n"
        "1️⃣ Открой @CryptoBot\n"
        "2️⃣ Нажми «Перевести» (Send)\n"
        "3️⃣ Введи ID получателя:\n"
        "<code>7394479104</code>\n"
        f"4️⃣ Укажи сумму: <b>{price_usdt} USDT</b>\n"
        "5️⃣ Подтверди перевод\n\n"
        "После оплаты отправь сюда скриншот чека"
        f"{' и укажи промокод ' + code.upper() if code else ''} — активирую подписку в течение 15 минут.\n\n"
        "По вопросам: @sitis_1",
        parse_mode="HTML"
    )
    return MENU


# ─── Админ: управление промокодами ───────────────────────────

async def admin_addpromo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    try:
        code = ctx.args[0]
        discount = int(ctx.args[1])
        if not (1 <= discount <= 99):
            await update.message.reply_text("Скидка должна быть от 1 до 99%")
            return
        add_promo_code(code, discount)
        await update.message.reply_text(
            f"✅ Промокод <b>{code.upper()}</b> создан со скидкой <b>{discount}%</b>\n\n"
            f"Пользователь активирует его командой:\n<code>/pay {code.upper()}</code>",
            parse_mode="HTML"
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Формат: /addpromo КОД скидка\n\nПример: /addpromo FRIEND10 10"
        )


async def admin_delpromo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    try:
        code = ctx.args[0]
        delete_promo_code(code)
        await update.message.reply_text(f"🗑 Промокод {code.upper()} удалён")
    except IndexError:
        await update.message.reply_text("Формат: /delpromo КОД")


async def admin_listpromo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    codes = list_promo_codes()
    if not codes:
        await update.message.reply_text("Промокодов пока нет.\n\nСоздать: /addpromo КОД скидка")
        return
    text = "🎁 <b>Активные промокоды:</b>\n\n"
    for code, discount in codes:
        text += f"<code>{code}</code> — {discount}%\n"
    await update.message.reply_text(text, parse_mode="HTML")


# ─── Реферальная программа ───────────────────────────────────

async def referral_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    bot_username = (await ctx.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
    count = get_referral_count(uid)
    await update.message.reply_text(
        "🔗 <b>Реферальная программа</b>\n\n"
        "Пригласи друга — когда он оплатит подписку, ты получишь <b>7 дней полного доступа</b> бесплатно!\n\n"
        f"Твоя ссылка:\n<code>{ref_link}</code>\n\n"
        f"👥 Друзей, которые оплатили: <b>{count}</b>\n\n"
        "Просто скопируй ссылку и отправь другу 👆",
        parse_mode="HTML"
    )
    return MENU


# ─── Админ команды ───────────────────────────────────────────

async def admin_activate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    try:
        target_id = int(ctx.args[0])
        days = int(ctx.args[1]) if len(ctx.args) > 1 else 30
        activate_paid(target_id, days)
        end = get_subscription_end(target_id)
        await update.message.reply_text(
            f"✅ Активировано для {target_id} на {days} дней\nДо: {end.strftime('%d.%m.%Y')}"
        )
        await ctx.bot.send_message(
            chat_id=target_id,
            text=f"✅ Твоя подписка активирована на {days} дней!\nПриятного использования 🎉",
            reply_markup=main_menu_keyboard()
        )
        # Проверка реферала
        referrer_id = get_referrer(target_id)
        if referrer_id:
            activate_paid(referrer_id, 7)
            mark_referral_bonus_given(target_id)
            await ctx.bot.send_message(
                chat_id=referrer_id,
                text="🎁 <b>Бонус!</b> Твой друг оплатил подписку — тебе начислено <b>7 дней полного доступа</b>! 🎉",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
            await update.message.reply_text(f"🔗 Реферер {referrer_id} получил +7 дней бонуса!")
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}\nИспользование: /activate user_id дней")


async def admin_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, expires_at FROM subscriptions ORDER BY expires_at DESC")
            rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("Нет пользователей")
        return
    text = "👥 <b>Пользователи:</b>\n\n"
    for uid, end in rows:
        status = "✅" if datetime.now() < end else "❌"
        text += f"{status} <code>{uid}</code> — до {end.strftime('%d.%m.%Y')}\n"
    await update.message.reply_text(text, parse_mode="HTML")


async def admin_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа")
        return
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM subscriptions")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM subscriptions WHERE expires_at > NOW()")
            active = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM subscriptions WHERE expires_at <= NOW()")
            expired = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM referrals WHERE bonus_given = TRUE")
            ref_paid = cur.fetchone()[0]
    await update.message.reply_text(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"✅ Активных подписок: <b>{active}</b>\n"
        f"❌ Истёкших: <b>{expired}</b>\n"
        f"🔗 Реферальных оплат: <b>{ref_paid}</b>",
        parse_mode="HTML"
    )


# ─── AI ──────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "Ты — дружелюбный AI-помощник для начинающих фрилансеров. "
    "Отвечаешь кратко, по делу, на русском языке. "
    "Помогаешь с поиском клиентов, написанием предложений, ценообразованием, "
    "работой на биржах (Upwork, Fiverr, Kwork). "
    "Помни контекст разговора и ссылайся на предыдущие ответы если это уместно."
)


async def ask_ai(prompt: str, history: list[dict] | None = None) -> str:
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=700,
            temperature=0.7
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return f"Ошибка AI: {e}"


# ─── Советы ──────────────────────────────────────────────────

# ─── История чата (в памяти, сессионная) ─────────────────────
# Формат: {user_id: [{"role": "user"/"assistant", "content": "..."}]}
chat_history: dict[int, list[dict]] = {}
MAX_HISTORY = 10  # максимум сообщений в истории (5 диалогов)


def get_history(user_id: int) -> list[dict]:
    return chat_history.get(user_id, [])


def add_to_history(user_id: int, role: str, content: str):
    if user_id not in chat_history:
        chat_history[user_id] = []
    chat_history[user_id].append({"role": role, "content": content})
    # Обрезаем до MAX_HISTORY сообщений
    if len(chat_history[user_id]) > MAX_HISTORY:
        chat_history[user_id] = chat_history[user_id][-MAX_HISTORY:]


def clear_history(user_id: int):
    chat_history.pop(user_id, None)


TIPS = [
    "🎯 <b>Выбери нишу</b>\nСпециализация = выше ставка и меньше конкуренция.",
    "📁 <b>Портфолио важнее всего</b>\nСделай 3–5 учебных проектов прежде чем брать первый заказ.",
    "⭐ <b>Первые заказы — за репутацию</b>\nБерись дёшево, но делай идеально.",
    "💬 <b>Отвечай быстро</b>\nКлиенты выбирают того, кто ответил первым.",
    "📈 <b>Поднимай цену регулярно</b>\nКаждые 5–10 заказов повышай ставку на 20–30%.",
    "🤝 <b>Ищи постоянных клиентов</b>\nОдин клиент на $500/мес лучше 10 клиентов по $50.",
    "📝 <b>Всегда договор или ТЗ</b>\nДаже в мессенджере фиксируй задачу и оплату текстом.",
    "🚫 <b>Не бойся отказывать</b>\nПлохой клиент забирает время у хорошего.",
]
tip_index = {}


async def show_tip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    i = tip_index.get(uid, 0)
    tip_index[uid] = (i + 1) % len(TIPS)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Следующий совет ➡️", callback_data="next_tip")]])
    await update.message.reply_text(TIPS[i], parse_mode="HTML", reply_markup=kb)
    return MENU


async def next_tip_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    i = tip_index.get(uid, 0)
    tip_index[uid] = (i + 1) % len(TIPS)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Следующий совет ➡️", callback_data="next_tip")]])
    await query.edit_message_text(TIPS[i], parse_mode="HTML", reply_markup=kb)


# ─── Хэндлеры (только для подписчиков) ──────────────────────

async def proposal_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_sub(update): return MENU
    await update.message.reply_text(
        "📨 Опиши в одном сообщении:\n• Твоя специализация\n• Что нужно клиенту\n• Твой опыт\n\n"
        "<i>Пример: Я веб-разработчик. Клиент ищет лендинг для кофейни. У меня 10 подобных проектов.</i>",
        parse_mode="HTML"
    )
    return PROPOSAL


async def proposal_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Генерирую предложение...")
    result = await ask_ai(
        f"Напиши профессиональное предложение клиенту для фрилансера. "
        f"Информация: {update.message.text}. "
        f"150-200 слов, убедительно, без воды, с конкретными выгодами для клиента. Только на русском языке."
    )
    await update.message.reply_text(f"📨 <b>Готово!</b>\n\n{result}", parse_mode="HTML",
                                    reply_markup=main_menu_keyboard())
    return MENU


async def bio_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_sub(update): return MENU
    await update.message.reply_text(
        "👤 Напиши в одном сообщении:\n• Твои навыки\n• Лет опыта\n• Главное достижение\n\n"
        "<i>Пример: Python, автоматизация, 2 года, автоматизировал отчёты для 15 компаний.</i>",
        parse_mode="HTML"
    )
    return BIO


async def bio_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Составляю биографию...")
    result = await ask_ai(
        f"Напиши профессиональную биографию для профиля фрилансера на Upwork/Fiverr. "
        f"Информация: {update.message.text}. "
        f"100-150 слов, уверенно, без пустых слов. Начни с сильного утверждения, не с 'Я'. Только на русском языке."
    )
    await update.message.reply_text(f"👤 <b>Готово!</b>\n\n{result}", parse_mode="HTML",
                                    reply_markup=main_menu_keyboard())
    return MENU


async def price_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_sub(update): return MENU
    await update.message.reply_text(
        "💰 Напиши в одном сообщении:\n• Услуга\n• Твоя цена\n• Что входит в работу\n\n"
        "<i>Пример: Создание лендинга, $250, включает дизайн + вёрстка + 2 правки, срок 5 дней.</i>",
        parse_mode="HTML"
    )
    return PRICE


async def price_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Составляю обоснование...")
    result = await ask_ai(
        f"Напиши убедительное обоснование цены для фрилансера. "
        f"Информация: {update.message.text}. "
        f"100-120 слов, покажи ценность, без извинений за цену. Только на русском языке."
    )
    await update.message.reply_text(f"💰 <b>Готово!</b>\n\n{result}", parse_mode="HTML",
                                    reply_markup=main_menu_keyboard())
    return MENU


async def followup_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_sub(update): return MENU
    await update.message.reply_text("⏳ Пишу follow-up...")
    result = await ask_ai(
        "Напиши вежливый follow-up фрилансера клиенту который не ответил 3 дня. "
        "50-70 слов, дружелюбно, с призывом к действию. Только на русском языке."
    )
    await update.message.reply_text(f"🔔 <b>Готово!</b>\n\n{result}", parse_mode="HTML",
                                    reply_markup=main_menu_keyboard())
    return MENU


# ─── Чат (с лимитом для бесплатных) ─────────────────────────

async def chat_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_subscribed(uid):
        history_len = len(get_history(uid))
        history_note = f" (в памяти {history_len // 2} сообщ.)" if history_len else ""
        await update.message.reply_text(
            f"💬 Задай любой вопрос по фрилансу{history_note}!\n"
            "AI помнит наш разговор в рамках сессии.\n\n"
            "Для возврата напиши /menu  |  /clearchat — очистить историю",
        )
        return CHAT
    # Бесплатный режим
    if can_use_free_chat(uid):
        used = get_free_chat_uses_this_week(uid)
        limit = get_free_weekly_limit(uid)
        await update.message.reply_text(
            f"💬 Задай вопрос по фрилансу!\n"
            f"<i>Осталось бесплатных вопросов на этой неделе: {limit - used}</i>\n\n"
            "Для возврата напиши /menu",
            parse_mode="HTML"
        )
        return CHAT
    else:
        limit = get_free_weekly_limit(uid)
        await update.message.reply_text(
            f"⛔ Ты использовал все {limit} бесплатных вопроса на этой неделе.\n\n"
            "Лимит сбросится через 7 дней.\n\n"
            "🔓 Оформи подписку за <b>200 руб/мес</b> — задавай вопросы без ограничений!\n"
            "👉 /pay\n\nИли пригласи друга и получи <b>7 дней бесплатно</b>: /ref",
            parse_mode="HTML"
        )
        return MENU


async def clear_chat_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_history(uid)
    await update.message.reply_text("🗑 История чата очищена. Начинаем заново!")
    return CHAT


async def chat_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_text = update.message.text

    if not is_subscribed(uid):
        if not can_use_free_chat(uid):
            limit = get_free_weekly_limit(uid)
            await update.message.reply_text(
                f"⛔ Бесплатный лимит исчерпан ({limit} вопроса в неделю).\n\n"
                "Оформи подписку: /pay\nПригласи друга: /ref",
                parse_mode="HTML",
                reply_markup=free_menu_keyboard()
            )
            return MENU
        record_chat_use(uid)
        used = get_free_chat_uses_this_week(uid)
        limit = get_free_weekly_limit(uid)
        # Бесплатным без истории
        await update.message.chat.send_action("typing")
        result = await ask_ai(user_text)
        remaining = limit - used
        if remaining > 0:
            extra = f"\n\n<i>Осталось бесплатных вопросов: {remaining}</i>"
        else:
            extra = "\n\n<i>Это был последний бесплатный вопрос на этой неделе. Оформи подписку: /pay</i>"
        await update.message.reply_text(result + extra, parse_mode="HTML")
        return CHAT

    # Платный режим — с историей
    history = get_history(uid)
    await update.message.chat.send_action("typing")
    result = await ask_ai(user_text, history=history)
    add_to_history(uid, "user", user_text)
    add_to_history(uid, "assistant", result)
    history_len = len(get_history(uid)) // 2
    footer = f"\n\n<i>💬 {history_len} сообщ. в памяти  |  /clearchat — очистить</i>"
    await update.message.reply_text(result + footer, parse_mode="HTML")
    return CHAT


# ─── Генерация картинок ──────────────────────────────────────

async def image_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_sub(update): return MENU
    await update.message.reply_text(
        "🎨 <b>Генерация картинок</b>\n\n"
        "Опиши что хочешь увидеть — чем подробнее, тем лучше!\n\n"
        "<i>Примеры:\n"
        "• Фрилансер за ноутбуком в уютном кафе, мягкий свет\n"
        "• Логотип для IT-компании в синих тонах, минимализм\n"
        "• Рабочее место дизайнера с двумя мониторами</i>\n\n"
        "Для отмены напиши /menu",
        parse_mode="HTML"
    )
    return IMAGE


async def image_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text.strip()
    msg = await update.message.reply_text("🎨 Генерирую картинку, подожди 20–40 секунд...")
    try:
        hf_url = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
        headers = {"Authorization": f"Bearer {HF_TOKEN}"}
        payload = {"inputs": prompt}

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(hf_url, json=payload, headers=headers)

        if resp.status_code == 200:
            image_bytes = resp.content
            await msg.delete()
            await update.message.reply_photo(
                photo=image_bytes,
                caption=f"🎨 <b>Готово!</b>\n\n<i>{prompt[:200]}</i>",
                parse_mode="HTML",
                reply_markup=main_menu_keyboard()
            )
        else:
            raise ValueError(f"HF API error {resp.status_code}: {resp.text[:200]}")

    except Exception as e:
        logger.error(f"Image gen error: {e}")
        await msg.edit_text(
            "❌ Не удалось сгенерировать картинку. Попробуй ещё раз или измени описание.\n\n"
            "/menu — вернуться в меню"
        )
    return MENU


# ─── Скриншот оплаты ─────────────────────────────────────────

async def payment_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = update.effective_user.first_name
    username = update.effective_user.username
    uname_str = f"@{username}" if username else "без username"
    await update.message.reply_text(
        "✅ Скриншот получен! Активирую подписку в течение 15 минут.\n"
        "Если вопросы — @sitis_1"
    )
    try:
        await ctx.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"💳 <b>Новая оплата!</b>\n"
                f"👤 {name} ({uname_str})\n"
                f"🆔 <code>{uid}</code>\n\n"
                f"Для активации:\n<code>/activate {uid} 30</code>"
            ),
            parse_mode="HTML"
        )
        await ctx.bot.forward_message(
            chat_id=ADMIN_ID,
            from_chat_id=uid,
            message_id=update.message.message_id
        )
    except Exception as e:
        logger.error(f"Payment notify error: {e}")
    return MENU


# ─── Прочие хэндлеры ─────────────────────────────────────────

async def back_to_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    clear_history(uid)
    await update.message.reply_text("Главное меню 👇", reply_markup=get_keyboard(uid))
    return MENU


async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    uid = update.effective_user.id
    subscribed = is_subscribed(uid)

    if "предложение" in text.lower():
        return await proposal_start(update, ctx)
    if "биографию" in text.lower():
        return await bio_start(update, ctx)
    if "цену" in text.lower():
        return await price_start(update, ctx)
    if "follow" in text.lower():
        return await followup_generate(update, ctx)
    if "вопрос" in text.lower():
        return await chat_start(update, ctx)
    if "совет" in text.lower():
        return await show_tip(update, ctx)
    if "подписка" in text.lower():
        return await subscription_info(update, ctx)
    if "пригласить" in text.lower() or "реферал" in text.lower():
        return await referral_cmd(update, ctx)
    if "картинк" in text.lower() or "генерац" in text.lower():
        return await image_start(update, ctx)

    await update.message.reply_text("Выбери действие из меню 👇", reply_markup=get_keyboard(uid))
    return MENU


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "🤖 <b>FreelanceAI Bot</b>\n\n"
        "/start — главное меню\n"
        "/menu — вернуться в меню\n"
        "/pay — оплатить подписку\n"
        "/ref — реферальная ссылка\n"
        "/help — справка\n\n"
        "📢 Наш канал: @freelanceburmalda",
        parse_mode="HTML",
        reply_markup=get_keyboard(uid)
    )
    return MENU


# ─── main ─────────────────────────────────────────────────────

def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, start),
        ],
        states={
            MENU:     [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
            CHAT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, chat_message),
                       CommandHandler("menu", back_to_menu),
                       CommandHandler("clearchat", clear_chat_cmd)],
            PROPOSAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, proposal_generate)],
            BIO:      [MessageHandler(filters.TEXT & ~filters.COMMAND, bio_generate)],
            PRICE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, price_generate)],
            IMAGE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, image_generate)],
        },
        fallbacks=[CommandHandler("start", start), CommandHandler("menu", back_to_menu)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pay", pay_info))
    app.add_handler(CommandHandler("addpromo", admin_addpromo))
    app.add_handler(CommandHandler("delpromo", admin_delpromo))
    app.add_handler(CommandHandler("listpromo", admin_listpromo))
    app.add_handler(CommandHandler("ref", referral_cmd))
    app.add_handler(CommandHandler("activate", admin_activate))
    app.add_handler(CommandHandler("users", admin_users))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CallbackQueryHandler(next_tip_callback, pattern="^next_tip$"))
    app.add_handler(MessageHandler(filters.PHOTO, payment_screenshot))

    # Напоминания каждые 30 минут (реальная отправка через 5-12 ч по логике)
    app.job_queue.run_repeating(send_reminders, interval=1800, first=60)

    logger.info("Бот запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
