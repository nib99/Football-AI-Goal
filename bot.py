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
openai_client = AsyncOpenAI(api_key=OPENAI_KEY)

pool = None
api_cache = {}
last_post_date = None
user_rate_limit = {}
user_gpt_usage = {}

# ====================== CHAPA CLIENT ======================
class Chapa:
    def __init__(self, secret_key):
        self.secret_key = secret_key
        self.base_url = "https://api.chapa.co/v1"

    async def initialize(self, **kwargs):
        headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/json"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/transaction/initialize", json=kwargs, headers=headers) as resp:
                return await resp.json()

chapa_client = Chapa(CHAPA_SECRET)

# ====================== DATABASE ======================
async def init_db():
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=10)
    
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (...);  -- your table code here
            CREATE TABLE IF NOT EXISTS payments (...); -- your table code here
        """)
    logging.info("✅ Database initialized")

# ====================== HELPERS ======================
async def create_payment(email: str, amount: float, tx_ref: str):
    """Create Chapa payment link"""
    return await chapa_client.initialize(
        amount=amount,
        currency="ETB",
        tx_ref=tx_ref,
        title="Football AI VIP",
        description="VIP Subscription",
        callback_url=f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/chapa-webhook",
        return_url="https://t.me/yourbot",   # Change to your bot username
        email=email
    )

# ... (keep your other helper functions: is_vip, add_user, can_use_command, api_football, smart_predict_match)

# ====================== FASTAPI ======================
app = FastAPI(title="Ethio Football AI Bot")

@app.get("/")
async def root():
    return {"status": "✅ Football AI Bot is LIVE!"}

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
                payment = await conn.fetchrow(
                    "SELECT * FROM payments WHERE tx_ref = $1 AND status = 'pending'", tx_ref
                )
                if payment:
                    await conn.execute("UPDATE payments SET status = 'success' WHERE tx_ref = $1", tx_ref)
                    expiry = datetime.now() + timedelta(days=30 if payment['plan'] == 'monthly' else 90)
                    await conn.execute(
                        "UPDATE users SET plan=$1, expiry=$2 WHERE user_id=$3",
                        payment['plan'], expiry, payment['user_id']
                    )
                    await bot.send_message(payment['user_id'], "🎉 <b>Payment Successful!</b>\n\nYour VIP has been activated!")
        return {"status": "success"}
    except Exception as e:
        logging.error(f"Chapa webhook error: {e}")
        return {"status": "error"}

# ====================== BOT HANDLERS ======================
# (Keep all your handlers: start, main_menu, vip_plans, handle_vip_payment, etc.)

# Just fix this part:
@dp.callback_query(F.data.startswith("vip_"))
async def handle_vip_payment(callback: types.CallbackQuery):
    if "monthly" in callback.data:
        plan = "monthly"
        amount = 299
    else:
        plan = "quarterly"
        amount = 699

    tx_ref = f"tx_{callback.from_user.id}_{int(datetime.now().timestamp())}"

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments (tx_ref, user_id, plan, amount, payment_method) "
            "VALUES ($1, $2, $3, $4, 'chapa')",
            tx_ref, callback.from_user.id, plan, amount
        )

    try:
        # Better to collect email from user or database
        user_email = f"user_{callback.from_user.id}@example.com"

        res = await create_payment(email=user_email, amount=amount, tx_ref=tx_ref)
        
        if res.get("status") == "success":
            checkout_url = res["data"]["checkout_url"]
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Pay Now", url=checkout_url)]
            ])
            await callback.message.edit_text(
                f"💰 Pay <b>{amount} ETB</b> for {plan.upper()} VIP Plan", 
                reply_markup=kb
            )
        else:
            await callback.message.edit_text("❌ Failed to generate payment link.")
    except Exception as e:
        logging.error(e)
        await callback.message.edit_text("❌ Payment service error. Try again later.")

# ====================== STARTUP ======================
@app.on_event("startup")
async def on_startup():
    await init_db()
    webhook_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook"
    await bot.set_webhook(webhook_url, drop_pending_updates=True)
    logging.info(f"🚀 Webhook set to: {webhook_url}")
    
    asyncio.create_task(channel_auto_poster())

# ====================== RUN ======================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run("bot:app", host="0.0.0.0", port=PORT, log_level="info")
