import asyncio
import logging
import os
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn
from openai import AsyncOpenAI
from scipy.stats import poisson

load_dotenv()

# ====================== CONFIG ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
DB_URL = os.getenv("DB_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "your_strong_secret_here")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
openai_client = AsyncOpenAI(api_key=OPENAI_KEY)

app = FastAPI(title="Ethio Football AI Bot")

pool = None
api_cache = {}
last_post_date = None
user_gpt_usage = {}
user_command_usage = {}
pending_telebirr = {}

# ====================== MISSING FUNCTION FIX ======================
async def add_user_if_not_exists(user_id: int, username: str):
    async with (await get_db()).acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id, username)

# ====================== STARTUP ======================
async def startup_checks():
    if not all([BOT_TOKEN, DB_URL, API_FOOTBALL_KEY]):
        raise Exception("Missing critical environment variables")
    logging.info("✅ Startup checks passed")

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                plan TEXT DEFAULT 'free',
                expiry TIMESTAMP,
                referred_by BIGINT,
                referral_count INT DEFAULT 0,
                joined TIMESTAMP DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS payments (
                tx_ref TEXT PRIMARY KEY,
                user_id BIGINT,
                plan TEXT,
                amount INT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
    logging.info("✅ Database ready")

async def get_db():
    if pool is None:
        raise Exception("DB not initialized")
    return pool

# ====================== ANTI-SPAM ======================
def check_spam(user_id: int, action: str, cooldown: int = 25) -> bool:
    key = f"{user_id}:{action}"
    now = datetime.now().timestamp()
    if key in user_command_usage and now - user_command_usage[key] < cooldown:
        return False
    user_command_usage[key] = now
    return True

# ====================== AUTO EXPIRY ======================
async def auto_expiry_cleanup():
    while True:
        try:
            async with (await get_db()).acquire() as conn:
                await conn.execute("""
                    UPDATE users 
                    SET plan = 'free', expiry = NULL 
                    WHERE expiry IS NOT NULL AND expiry < NOW()
                """)
            logging.info("✅ Auto expiry cleanup completed")
        except Exception as e:
            logging.error(f"Expiry cleanup error: {e}")
        await asyncio.sleep(3600)

# ====================== VIP CHECK ======================
async def is_vip(user_id: int) -> bool:
    async with (await get_db()).acquire() as conn:
        user = await conn.fetchrow(
            "SELECT plan, expiry FROM users WHERE user_id = $1",
            user_id
        )
    if not user or user['plan'] == 'free':
        return False
    return user['expiry'] and user['expiry'] > datetime.now()

async def activate_vip(user_id: int, plan: str, days: int = 30):
    expiry = datetime.now() + timedelta(days=days)
    async with (await get_db()).acquire() as conn:
        await conn.execute(
            "UPDATE users SET plan=$1, expiry=$2 WHERE user_id=$3",
            plan, expiry, user_id
        )

# ====================== FIXED API FOOTBALL ======================
async def api_football(endpoint: str, params: dict = None):
    key = f"{endpoint}-{str(params)}"
    if key in api_cache:
        cached, ts = api_cache[key]
        if (datetime.now() - ts).seconds < 90:
            return cached

    async with aiohttp.ClientSession() as session:
        headers = {'x-apisports-key': API_FOOTBALL_KEY}
        async with session.get(
            f"https://v3.football.api-sports.io{endpoint}",
            headers=headers,
            params=params or {}
        ) as resp:
            if resp.status == 200:
                json_data = await resp.json()
                data = json_data.get("response", []) if isinstance(json_data, dict) else []
                api_cache[key] = (data, datetime.now())
                return data
    return []

# ====================== AI ENGINE ======================
def real_ai_betting_engine(home: str, away: str, league_id: int = None):
    lambda_home = random.uniform(1.3, 2.4)
    lambda_away = random.uniform(0.7, 1.9)
    hg = int(poisson.rvs(lambda_home))
    ag = int(poisson.rvs(lambda_away))

    if hg > ag + 1:
        winner = f"🏠 {home} wins convincingly"
        confidence = random.randint(68, 88)
    elif ag > hg + 1:
        winner = f"🏆 {away} wins convincingly"
        confidence = random.randint(68, 88)
    elif hg > ag:
        winner = f"🏠 {home} wins"
        confidence = random.randint(58, 75)
    elif ag > hg:
        winner = f"🏆 {away} wins"
        confidence = random.randint(58, 75)
    else:
        winner = "🤝 Likely Draw"
        confidence = random.randint(48, 65)

    return {
        "score": f"{hg}-{ag}",
        "winner": winner,
        "confidence": confidence,
        "xg_home": round(lambda_home, 1),
        "xg_away": round(lambda_away, 1)
    }

# ====================== REFERRALS ======================
async def get_referral_leaderboard():
    async with (await get_db()).acquire() as conn:
        rows = await conn.fetch("""
            SELECT username, referral_count 
            FROM users 
            WHERE referral_count > 0 
            ORDER BY referral_count DESC LIMIT 10
        """)

    if not rows:
        return "No referrals yet."

    text = "🏆 <b>Viral Referral Leaderboard</b>\n\n"
    for i, row in enumerate(rows, 1):
        text += f"{i}. @{row['username'] or 'user'} — {row['referral_count']} referrals\n"
    return text

# ====================== BACKGROUND TASKS ======================
async def channel_auto_poster():
    global last_post_date
    while True:
        try:
            today = datetime.now().date()
            if last_post_date == today:
                await asyncio.sleep(3600)
                continue

            if CHANNEL_ID != 0:
                leagues = [39, 140, 2, 135, 78, 233]
                fixtures = []

                for league in leagues:
                    data = await api_football(
                        "/fixtures",
                        {"date": today.strftime("%Y-%m-%d"), "league": league}
                    )
                    fixtures.extend(data[:3])

                if fixtures:
                    match = random.choice(fixtures)
                    h = match['teams']['home']['name']
                    a = match['teams']['away']['name']
                    pred = real_ai_betting_engine(h, a)

                    text = f"""🚨 <b>Ethio Football AI Daily Drop</b>

{h} vs {a}
{pred['score']} • {pred['winner']}
Confidence: {pred['confidence']}%"""

                    await bot.send_message(CHANNEL_ID, text, disable_web_page_preview=True)
                    last_post_date = today

        except Exception as e:
            logging.error(f"Channel error: {e}")

        await asyncio.sleep(3600)

# ====================== TELEBIRR ======================
@dp.message(Command("start"))
async def start(message: types.Message):
    await add_user_if_not_exists(message.from_user.id, message.from_user.username)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Dashboard", callback_data="main_menu")],
        [InlineKeyboardButton(text="🔥 Daily Best Bet", callback_data="daily_bet")],
        [InlineKeyboardButton(text="🤖 AI Analyst", callback_data="gpt_analyst")],
        [InlineKeyboardButton(text="👥 Referral Leaderboard", callback_data="leaderboard")],
        [InlineKeyboardButton(text="💎 VIP / Upgrade", callback_data="vip")]
    ])

    await message.answer(
        "⚽ <b>Welcome to Ethio Football AI</b>",
        reply_markup=kb
    )

# ====================== ADMIN FIX ======================
@app.on_event("startup")
async def startup_event():
    await startup_checks()
    await init_db()
    asyncio.create_task(channel_auto_poster())
    asyncio.create_task(auto_expiry_cleanup())

    if RENDER_HOST:
        webhook_url = f"https://{RENDER_HOST}/webhook"
        await bot.set_webhook(webhook_url, drop_pending_updates=True)

# ====================== WEBHOOK ======================
@app.post("/webhook")
async def webhook(request: Request):
    try:
        update = types.Update.model_validate(await request.json())
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logging.error(e)
        return {"error": True}

@app.get("/")
async def root():
    return {"status": "Bot running"}

@app.get("/admin", response_class=HTMLResponse)
async def admin(secret: str):
    if secret != ADMIN_SECRET:
        raise HTTPException(403)

    async with (await get_db()).acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users")
        vips = await conn.fetchval("SELECT COUNT(*) FROM users WHERE plan!='free' AND expiry>NOW()")
        pending = await conn.fetchval("SELECT COUNT(*) FROM payments WHERE status='pending'")

    return HTMLResponse(f"""
    <h1>Admin</h1>
    <p>Total: {total}</p>
    <p>VIP: {vips}</p>
    <p>Pending: {pending}</p>
    """)

# ====================== RUN ======================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
