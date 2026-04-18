import asyncio
import logging
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, Request
import uvicorn
from openai import AsyncOpenAI
import httpx
import aiohttp
import random

load_dotenv()

# ====================== CONFIG ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAPA_SECRET = os.getenv("CHAPA_SECRET_KEY")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY")
DB_URL = os.getenv("DB_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", 0))
PORT = int(os.getenv("PORT", 10000))

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
chapa_client = Chapa(CHAPA_SECRET)
openai_client = AsyncOpenAI(api_key=OPENAI_KEY)

pool = None
api_cache = {}
last_post_date = None
user_rate_limit = {}      # Anti-spam
user_gpt_usage = {}

# ====================== DATABASE ======================
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=10)
    
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                plan TEXT DEFAULT 'free',
                expiry TIMESTAMP,
                balance NUMERIC(12,2) DEFAULT 0.0,
                referred_by BIGINT,
                referral_count INT DEFAULT 0,
                joined TIMESTAMP DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS payments (
                tx_ref TEXT PRIMARY KEY,
                user_id BIGINT,
                plan TEXT,
                amount NUMERIC(10,2),
                payment_method TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
    logging.info("✅ Database initialized")

# ====================== HELPERS ======================
async def is_vip(user_id: int) -> bool:
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT plan, expiry FROM users WHERE user_id = $1", user_id)
    if not user or user['plan'] == 'free':
        return False
    return user['expiry'] is not None and user['expiry'] > datetime.now()

async def add_user(user_id: int, username: str = None, first_name: str = None, referred_by: int = None):
    async with pool.acquire() as conn:
        # Insert or update user
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name, referred_by)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id) DO UPDATE 
            SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
        """, user_id, username, first_name, referred_by)

        # Auto referral reward
        if referred_by and referred_by != user_id:
            await conn.execute("""
                UPDATE users 
                SET referral_count = referral_count + 1,
                    balance = balance + 50.00
                WHERE user_id = $1
            """, referred_by)
            await bot.send_message(referred_by, "🎉 You earned 50 ETB for a new referral!")

# Rate limit (anti-spam)
def can_use_command(user_id: int, command: str, cooldown: int = 30) -> bool:
    key = f"{user_id}:{command}"
    now = datetime.now().timestamp()
    last = user_rate_limit.get(key, 0)
    if now - last < cooldown:
        return False
    user_rate_limit[key] = now
    return True

# Improved API-Football with better caching
async def api_football(endpoint: str, params: dict = None):
    key = f"{endpoint}-{str(params)}"
    if key in api_cache:
        data, ts = api_cache[key]
        if (datetime.now() - ts).seconds < 90:   # 90 seconds cache
            return data

    async with aiohttp.ClientSession() as session:
        headers = {'x-apisports-key': API_FOOTBALL_KEY}
        async with session.get(f"https://v3.football.api-sports.io{endpoint}", 
                               headers=headers, params=params or {}) as resp:
            if resp.status == 200:
                data = (await resp.json()).get('response', [])
                api_cache[key] = (data, datetime.now())
                return data
    return []

def smart_predict_match(home: str, away: str):
    # Statistical + slight ML feel (Poisson + team bias)
    base_home = 1.7 if any(x in home for x in ["Man City", "Real", "Bayern", "Liverpool"]) else 1.3
    lambda_home = random.uniform(base_home, 2.5)
    lambda_away = random.uniform(0.8, 2.0)
    
    hg = int(random.gauss(lambda_home, 0.7))
    ag = int(random.gauss(lambda_away, 0.7))
    hg, ag = max(0, hg), max(0, ag)
    
    if hg > ag + 1: winner = f"🏠 {home} wins convincingly"
    elif ag > hg + 1: winner = f"🏆 {away} wins convincingly"
    elif hg > ag: winner = f"🏠 {home} wins"
    elif ag > hg: winner = f"🏆 {away} wins"
    else: winner = "🤝 Draw"
    
    return {"score": f"{hg}-{ag}", "winner": winner, "confidence": random.randint(62, 89)}

# ====================== BACKGROUND TASKS ======================
async def channel_auto_poster():
    global last_post_date
    while True:
        try:
            today = datetime.now().date()
            if last_post_date == today:
                await asyncio.sleep(3600)
                continue

            if CHANNEL_ID:
                data = await api_football("/fixtures", {"date": today.strftime("%Y-%m-%d"), "league": "39"})
                if data:
                    match = random.choice(data[:5])
                    h = match['teams']['home']['name']
                    a = match['teams']['away']['name']
                    pred = smart_predict_match(h, a)
                    text = f"""🚨 <b>Ethio Football AI Daily Drop</b>

{h} vs {a}
🔮 Prediction: {pred['score']} • {pred['winner']}
Confidence: {pred['confidence']}% 

Join the bot for more → @{ (await bot.get_me()).username }"""
                    await bot.send_message(CHANNEL_ID, text, disable_web_page_preview=True)
                    last_post_date = today
                    logging.info("✅ Daily channel post sent")
        except Exception as e:
            logging.error(f"Channel poster error: {e}")
        await asyncio.sleep(3600)

# ====================== FASTAPI ======================
app = FastAPI(title="Ethio Football AI Bot")

@app.get("/")
async def root():
    return {"status": "✅ Football AI Bot is LIVE on Render Web Service!"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update = types.Update.model_validate(await request.json())
        await dp.feed_update(bot, update)
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return {"status": "error"}

@app.post("/chapa-webhook")
async def chapa_webhook(request: Request):
    try:
        data = await request.json()
        tx_ref = data.get("tx_ref")
        status = data.get("status")
        if status == "success" and tx_ref:
            async with pool.acquire() as conn:
                payment = await conn.fetchrow("SELECT * FROM payments WHERE tx_ref = $1 AND status = 'pending'", tx_ref)
                if payment:
                    await conn.execute("UPDATE payments SET status = 'success' WHERE tx_ref = $1", tx_ref)
                    expiry = datetime.now() + timedelta(days=30)
                    await conn.execute("UPDATE users SET plan=$1, expiry=$2 WHERE user_id=$3", 
                                     payment['plan'], expiry, payment['user_id'])
                    await bot.send_message(payment['user_id'], "🎉 <b>Payment Successful!</b>\nVIP activated!")
        return {"status": "success"}
    except Exception as e:
        logging.error(e)
        return {"status": "error"}

# ====================== BOT HANDLERS ======================
@dp.message(Command("start"))
async def start(message: types.Message):
    user = message.from_user
    referred_by = None
    if len(message.text.split()) > 1:
        try:
            referred_by = int(message.text.split()[1])
        except:
            pass
    await add_user(user.id, user.username, user.first_name, referred_by)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 Main Menu", callback_data="main_menu")]])
    await message.answer("⚽ <b>Welcome to Ethio Football AI</b>\nSmart Predictions • Live • VIP Tips", reply_markup=kb)

@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Live Matches", callback_data="live")],
        [InlineKeyboardButton(text="🔮 Predictions", callback_data="predictions")],
        [InlineKeyboardButton(text="🤖 GPT Analyst", callback_data="gpt")],
        [InlineKeyboardButton(text="🔥 Daily Best Bet", callback_data="daily_bet")],
        [InlineKeyboardButton(text="💎 VIP Plans", callback_data="vip")],
        [InlineKeyboardButton(text="👥 Referral", callback_data="referral")]
    ])
    await callback.message.edit_text("⚽ <b>Main Menu</b>", reply_markup=kb)

# ====================== VIP PLANS + PAYMENT ======================

@dp.callback_query(F.data == "vip")
async def vip_plans(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Monthly VIP - 299 ETB", callback_data="vip_monthly")],
        [InlineKeyboardButton(text="🔥 Quarterly VIP - 699 ETB", callback_data="vip_quarterly")],
        [InlineKeyboardButton(text="← Back", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "💎 <b>VIP Plans</b>\n\n"
        "• Unlimited predictions\n"
        "• GPT Analyst\n"
        "• Live alerts\n"
        "• No ads", 
        reply_markup=kb
    )


@dp.callback_query(F.data.startswith("vip_"))
async def handle_vip_payment(callback: types.CallbackQuery):
    # Determine plan and amount
    if "monthly" in callback.data:
        plan = "monthly"
        amount = 299
    else:
        plan = "quarterly"
        amount = 699

    tx_ref = f"tx_{callback.from_user.id}_{int(datetime.now().timestamp())}"

    # Save payment record to database
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments (tx_ref, user_id, plan, amount, payment_method) "
            "VALUES ($1, $2, $3, $4, 'chapa')",
            tx_ref, callback.from_user.id, plan, amount
        )

    # === Create Chapa Payment Link ===
    try:
        # Make sure you have these variables available (email is required by Chapa)
        user_email = "user@example.com"   # ← Change this! Better to get from user data

        res = await create_payment(
            email=user_email,
            amount=amount,
            tx_ref=tx_ref
        )

        checkout_url = res["data"]["checkout_url"]   # This is the payment link

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Pay Now", url=checkout_url)]
        ])

        await callback.message.edit_text(
            f"💰 Pay <b>{amount} ETB</b> for {plan.upper()} VIP",
            reply_markup=kb
        )

    except Exception as e:
        await callback.message.edit_text(
            "❌ Failed to create payment link. Please try again later."
        )
        print(f"Payment creation error: {e}")


@dp.callback_query(F.data == "pay_telebirr")
async def telebirr_payment(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📱 <b>Telebirr Manual Payment</b>\n\n"
        "Send exactly <b>299 ETB</b> (Monthly) or <b>699 ETB</b> (Quarterly) to:\n"
        "<code>+251 9XX XXX XXX</code>\n\n"
        "After payment, send the screenshot + your Telegram ID to admin.\n"
        "Approval usually within 10-30 minutes.",
        parse_mode="HTML"
    )

# Referral + Leaderboard
@dp.callback_query(F.data == "referral")
async def referral(callback: types.CallbackQuery):
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={callback.from_user.id}"
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT referral_count, balance FROM users WHERE user_id = $1", callback.from_user.id)
        count = user['referral_count'] if user else 0
        balance = user['balance'] if user else 0
    
    text = f"""👥 <b>Referral Program</b>

Your Link: <code>{ref_link}</code>

Referrals: {count}
Balance: {balance} ETB

💰 Every referral = 50 ETB credit
Top 10 referrers get free VIP monthly!"""
    await callback.message.edit_text(text)

# Live Matches
@dp.callback_query(F.data == "live")
async def live_matches(callback: types.CallbackQuery):
    data = await api_football("/fixtures", {"live": "all"})
    if not data:
        return await callback.answer("No live matches right now", show_alert=True)
    text = "🔴 <b>Live Matches</b>\n\n"
    for m in data[:10]:
        h = m['teams']['home']['name']
        a = m['teams']['away']['name']
        score = f"{m['goals']['home'] or 0}-{m['goals']['away'] or 0}"
        text += f"{h} {score} {a}\n"
    await callback.message.edit_text(text)

# Daily Best Bet
@dp.callback_query(F.data == "daily_bet")
async def daily_bet_handler(callback: types.CallbackQuery):
    if not await is_vip(callback.from_user.id):
        return await callback.answer("💎 VIP only!", show_alert=True)
    data = await api_football("/fixtures", {"date": datetime.now().strftime("%Y-%m-%d")})
    if not data:
        return await callback.message.edit_text("No matches today")
    match = random.choice(data[:8])
    h = match['teams']['home']['name']
    a = match['teams']['away']['name']
    pred = smart_predict_match(h, a)
    await callback.message.edit_text(f"🔥 <b>Daily Best Bet</b>\n\n{h} vs {a}\n{pred['score']} • {pred['winner']}\nConfidence: {pred['confidence']}%")

# GPT Analyst (with rate limit)
@dp.callback_query(F.data == "gpt")
async def gpt_start(callback: types.CallbackQuery):
    if not await is_vip(callback.from_user.id):
        return await callback.answer("💎 Pro+ VIP required", show_alert=True)
    await callback.message.edit_text("🤖 Send any match for AI analysis (e.g. Arsenal vs Chelsea)")

@dp.message()
async def gpt_handler(message: types.Message):
    if not await is_vip(message.from_user.id):
        return
    if not can_use_command(message.from_user.id, "gpt", 30):
        return await message.answer("⏳ Wait 30 seconds before next GPT request")
    
    prompt = message.text.strip()
    try:
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": f"You are a top football analyst. Analyze: {prompt}"}],
            max_tokens=600
        )
        await message.answer(f"🤖 <b>GPT Analyst</b>\n\n{resp.choices[0].message.content}")
    except:
        await message.answer("AI busy, try again later.")

# Admin panel
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠️ Admin Panel ready\nUse /users or /approve_telebirr")

# ====================== STARTUP ======================
async def on_startup():
    await init_db()
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"
    await bot.set_webhook(webhook_url)
    logging.info(f"🚀 Webhook set: {webhook_url}")
    
    # Background tasks
    asyncio.create_task(channel_auto_poster())
    logging.info("✅ All background tasks started (auto poster + more)")

# ====================== RUN ======================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(on_startup())
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, log_level="info")
