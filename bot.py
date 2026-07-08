 # ─── COMPLETE BOT.PY ──────────────────────────────────────────────────────────
# Copy this entire file and deploy on Railway.
# Replace "yourupi@bank" with your UPI ID (backup if QR image missing).
# Place your QR image as "upi_qr.png" in the same folder.

import asyncio
import os
import time
import json
import random
import logging
import traceback
import re
import glob
import signal
import sys
import math
import hashlib
import unicodedata
import platform
import datetime
from typing import Dict, Set, Optional
from io import BytesIO
from collections import Counter
import requests
import qrcode
from gtts import gTTS
import yt_dlp
from telethon import TelegramClient, events, functions, types
from telethon.errors import FloodWaitError, RPCError, SessionPasswordNeededError, MessageNotModifiedError, UnauthorizedError
from telethon.sessions import StringSession
from cryptography.fernet import Fernet
import asyncpg

# ─── CONFIGURATION ───
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MY_OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "8909378644").split(",") if x.strip()}

# ─── QR IMAGE PATH (Static UPI QR) ───
QR_IMAGE_PATH =  "upi_qr.jpg"

if os.path.exists(QR_IMAGE_PATH):
    # ✅ Agar file mil gayi toh user ko STATIC IMAGE dikhegi
    await event.edit(..., file=QR_IMAGE_PATH)
else:
    # ❌ Agar file nahi mili toh bot dynamically QR code generate karega
    upi_link = f"upi://pay?pa={UPI_ID}&pn=YourBotName&am=45&cu=INR"
    qr = qrcode.make(upi_link)
    
# ─── CHANNEL VERIFICATION ───
REQUIRED_CHANNELS = [
    {"id": -1003896742623, "invite": "https://t.me/+slCWwd6XmSc5OTU9", "name": "Channel 1"},
    {"id": -1003971062167, "invite": "https://t.me/botscripts18", "name": "Channel 2"},
    {"id": -1004452969098, "invite": "https://t.me/userbotsupport_ZA", "name": "Channel 3"},
]

# ─── BROADCAST USERS STORAGE ───
USERS_FILE = "broadcast_users.json"

def load_users():
    try:
        with open(USERS_FILE, "r") as f:
            return set(json.load(f))
    except:
        return set()

def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(list(users), f)

broadcast_users = load_users()

# ─── DATABASE & ENCRYPTION ───
db_pool = None
cipher = None

async def init_db():
    global db_pool
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise Exception("DATABASE_URL not set")
    db_pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                user_id BIGINT PRIMARY KEY,
                session_encrypted TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                key_name TEXT PRIMARY KEY,
                key_value TEXT NOT NULL
            )
        """)
        # ─── PREMIUM TABLES ───
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS premium_users (
                user_id BIGINT PRIMARY KEY,
                expiry_date TIMESTAMP NOT NULL,
                gifted_by BIGINT,
                premium_active BOOLEAN DEFAULT TRUE,
                blocked_commands TEXT[] DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ─── Add columns if they don't exist (for existing DB)
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                               WHERE table_name='premium_users' AND column_name='premium_active') THEN
                    ALTER TABLE premium_users ADD COLUMN premium_active BOOLEAN DEFAULT TRUE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                               WHERE table_name='premium_users' AND column_name='blocked_commands') THEN
                    ALTER TABLE premium_users ADD COLUMN blocked_commands TEXT[] DEFAULT '{}';
                END IF;
            END $$;
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS used_utrs (
                utr TEXT PRIMARY KEY,
                user_id BIGINT,
                used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_payments (
                user_id BIGINT,
                utr TEXT,
                amount INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, utr)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_approvals (
                user_id BIGINT PRIMARY KEY,
                screenshot_msg_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ─── WALLET TABLE ───
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id BIGINT PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

async def get_encryption_key():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT key_value FROM app_config WHERE key_name = 'encryption_key'")
        if row:
            return row['key_value']
        else:
            new_key = Fernet.generate_key().decode()
            await conn.execute(
                "INSERT INTO app_config (key_name, key_value) VALUES ($1, $2)",
                "encryption_key", new_key
            )
            return new_key

async def init_cipher():
    global cipher
    key = await get_encryption_key()
    cipher = Fernet(key.encode())

def encrypt_session(sess: str) -> str:
    if cipher is None:
        raise RuntimeError("Cipher not initialized")
    return cipher.encrypt(sess.encode()).decode()

def decrypt_session(encrypted: str) -> str:
    if cipher is None:
        raise RuntimeError("Cipher not initialized")
    return cipher.decrypt(encrypted.encode()).decode()

async def save_session(user_id: int, session_str: str):
    encrypted = encrypt_session(session_str)
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_sessions (user_id, session_encrypted) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET session_encrypted = $2, updated_at = CURRENT_TIMESTAMP",
            user_id, encrypted
        )

async def load_sessions() -> dict:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, session_encrypted FROM user_sessions")
    sessions = {}
    for row in rows:
        try:
            sess = decrypt_session(row['session_encrypted'])
            sessions[row['user_id']] = sess
        except Exception:
            await delete_session(row['user_id'])
            continue
    return sessions

async def delete_session(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM user_sessions WHERE user_id = $1", user_id)

# ─── PREMIUM & WALLET FUNCTIONS ───
premium_pool = None

async def get_balance(user_id: int) -> int:
    async with premium_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM user_wallets WHERE user_id = $1", user_id)
        if row:
            return row['balance']
        else:
            await conn.execute("INSERT INTO user_wallets (user_id, balance) VALUES ($1, 0)", user_id)
            return 0

async def add_balance(user_id: int, amount: int):
    async with premium_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO user_wallets (user_id, balance) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET balance = user_wallets.balance + $2, updated_at = CURRENT_TIMESTAMP",
            user_id, amount
        )

async def deduct_balance(user_id: int, amount: int) -> bool:
    async with premium_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM user_wallets WHERE user_id = $1", user_id)
        if not row or row['balance'] < amount:
            return False
        await conn.execute(
            "UPDATE user_wallets SET balance = balance - $1, updated_at = CURRENT_TIMESTAMP WHERE user_id = $2",
            amount, user_id
        )
        return True

async def is_user_premium(user_id: int) -> bool:
    if premium_pool is None:
        return False
    async with premium_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expiry_date, premium_active FROM premium_users WHERE user_id = $1 AND expiry_date > NOW()",
            user_id
        )
        if not row:
            return False
        return row['premium_active'] is True

async def add_premium(user_id: int, days: int = 30, gifted_by: int = None):
    async with premium_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO premium_users (user_id, expiry_date, gifted_by, premium_active, blocked_commands) "
            "VALUES ($1, NOW() + INTERVAL '$2 days', $3, TRUE, '{}') "
            "ON CONFLICT (user_id) DO UPDATE SET expiry_date = NOW() + INTERVAL '$2 days', gifted_by = $3, premium_active = TRUE",
            user_id, days, gifted_by
        )

async def toggle_premium(user_id: int) -> bool:
    async with premium_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT premium_active FROM premium_users WHERE user_id = $1", user_id)
        if not row:
            return False
        new_state = not row['premium_active']
        await conn.execute(
            "UPDATE premium_users SET premium_active = $1 WHERE user_id = $2",
            new_state, user_id
        )
        return new_state

async def get_blocked_commands(user_id: int) -> list:
    async with premium_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT blocked_commands FROM premium_users WHERE user_id = $1", user_id)
        if row and row['blocked_commands']:
            return row['blocked_commands']
        return []

async def add_blocked_command(user_id: int, cmd: str):
    async with premium_pool.acquire() as conn:
        await conn.execute(
            "UPDATE premium_users SET blocked_commands = array_append(blocked_commands, $1) WHERE user_id = $2",
            cmd.lower(), user_id
        )

async def remove_blocked_command(user_id: int, cmd: str):
    async with premium_pool.acquire() as conn:
        await conn.execute(
            "UPDATE premium_users SET blocked_commands = array_remove(blocked_commands, $1) WHERE user_id = $2",
            cmd.lower(), user_id
        )

async def is_utr_used(utr: str) -> bool:
    async with premium_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM used_utrs WHERE utr = $1", utr)
        return row is not None

async def mark_utr_used(utr: str, user_id: int):
    async with premium_pool.acquire() as conn:
        await conn.execute("INSERT INTO used_utrs (utr, user_id) VALUES ($1, $2)", utr, user_id)

async def set_pending_approval(user_id: int, msg_id: int):
    async with premium_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO pending_approvals (user_id, screenshot_msg_id, status) VALUES ($1, $2, 'pending') "
            "ON CONFLICT (user_id) DO UPDATE SET screenshot_msg_id = $2, status = 'pending', created_at = CURRENT_TIMESTAMP",
            user_id, msg_id
        )

async def clear_pending_approval(user_id: int):
    async with premium_pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_approvals WHERE user_id = $1", user_id)

async def get_pending_user(user_id: int):
    async with premium_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM pending_approvals WHERE user_id = $1 AND status = 'pending'", user_id)

# ─── MAIN BOT ───
main_bot = TelegramClient("main_bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)
user_states = {}

active_userbots = {}
user_sessions = {}

print("🚀 Main Bot started with Admin Logger Engine...")

async def is_user_in_channel(user_id, channel_data):
    try:
        channel = await main_bot.get_entity(channel_data["id"])
        await main_bot.get_permissions(channel, user_id)
        return True
    except Exception:
        return False

def get_join_buttons():
    buttons = []
    for idx, ch in enumerate(REQUIRED_CHANNELS, 1):
        buttons.append([types.KeyboardButtonUrl(text=f"🔗 Join {ch['name']}", url=ch["invite"])])
    buttons.append([types.KeyboardButtonCallback(text="✅ I have joined all", data=b"verify_channels")])
    return buttons

async def shutdown_handler(sig, frame):
    print("🛑 Shutting down gracefully...")
    for uid in broadcast_users:
        try:
            await main_bot.send_message(uid, "⚠️ **Bot is going offline for maintenance/restart.**\nWe'll be back soon!")
            await asyncio.sleep(0.5)
        except:
            pass
    for uid, client in active_userbots.items():
        try:
            await client.disconnect()
        except:
            pass
    await main_bot.disconnect()
    sys.exit(0)

signal.signal(signal.SIGTERM, lambda s, f: asyncio.create_task(shutdown_handler(s, f)))
signal.signal(signal.SIGINT, lambda s, f: asyncio.create_task(shutdown_handler(s, f)))

async def safe_reply(event, text, buttons=None, **kwargs):
    try:
        return await event.reply(text, buttons=buttons, **kwargs)
    except FloodWaitError as e:
        wait = e.seconds + 1
        print(f"⏳ Main bot flood wait: {wait}s")
        await asyncio.sleep(wait)
        return await event.reply(text, buttons=buttons, **kwargs)
    except Exception:
        return None

async def safe_respond(event, text, **kwargs):
    try:
        return await event.respond(text, **kwargs)
    except FloodWaitError as e:
        wait = e.seconds + 1
        print(f"⏳ Main bot flood wait: {wait}s")
        await asyncio.sleep(wait)
        return await event.respond(text, **kwargs)
    except Exception:
        return None

async def safe_edit(event, text, buttons=None, **kwargs):
    try:
        return await event.edit(text, buttons=buttons, **kwargs)
    except FloodWaitError as e:
        wait = e.seconds + 1
        print(f"⏳ Main bot flood wait: {wait}s")
        await asyncio.sleep(wait)
        return await event.edit(text, buttons=buttons, **kwargs)
    except MessageNotModifiedError:
        pass
    except Exception:
        return None

async def safe_send_main(chat, text, **kwargs):
    try:
        return await main_bot.send_message(chat, text, **kwargs)
    except FloodWaitError as e:
        wait = e.seconds + 1
        print(f"⏳ Main bot flood wait: {wait}s")
        await asyncio.sleep(wait)
        return await main_bot.send_message(chat, text, **kwargs)
    except Exception:
        return None

# ─── MAIN BOT HANDLERS ───

# UPDATED /start with inline buttons
@main_bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    user_id = event.sender_id
    broadcast_users.add(user_id)
    save_users(broadcast_users)
    print(f"✅ User {user_id} added to broadcast list via /start")

    buttons = [
        [types.KeyboardButtonCallback("💳 Buy Premium", b"buy_premium")],
        [types.KeyboardButtonCallback("💰 Balance", b"check_balance")],
        [types.KeyboardButtonCallback("📤 Deposit", b"deposit")]
    ]

    await safe_reply(
        event,
        "╔═══════════════════════════════════════════╗\n"
        "║  ✦ 👑 ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️ 𝐀𝐔𝐓𝐎-𝐃𝐄𝐏𝐋𝐎𝐘 👑 ✦  ║\n"
        "╚═══════════════════════════════════════════╝\n\n"
        "Welcome to the **Ultimate Userbot Manager**.\n"
        "• To start your personal userbot, type `/login`\n"
        "• To stop it, use `/logout`\n"
        "• Click the buttons below to manage your premium.\n\n"
        "Enjoy the premium experience! 🚀",
        buttons=buttons
    )

# /login handler (keep original)
@main_bot.on(events.NewMessage(pattern="/login"))
async def login_handler(event):
    user_id = event.sender_id
    chat_id = event.chat_id

    not_joined = []
    for ch in REQUIRED_CHANNELS:
        if not await is_user_in_channel(user_id, ch):
            not_joined.append(ch)

    if not_joined:
        msg = "❌ **You must join all the following channels first:**\n\n"
        for ch in not_joined:
            msg += f"• {ch['name']} ({ch['invite']})\n"
        msg += "\nAfter joining, click the **'✅ I have joined all'** button below."
        buttons = get_join_buttons()
        await safe_reply(event, msg, buttons=buttons)
        return

    user_states[chat_id] = {"step": "NUMBER"}
    await safe_reply(
        event,
        "📱 **Step 1:** Please send your Telegram phone number **with country code**.\n"
        "Example: `+919876543210`"
    )

# Callback handler for /start buttons + channel verification
@main_bot.on(events.CallbackQuery)
async def callback_handler(event):
    data = event.data
    user_id = event.sender_id

    # Channel verification
    if data == b"verify_channels":
        chat_id = event.chat_id
        not_joined = []
        for ch in REQUIRED_CHANNELS:
            if not await is_user_in_channel(user_id, ch):
                not_joined.append(ch)

        if not_joined:
            msg = "❌ **You still haven't joined:**\n"
            for ch in not_joined:
                msg += f"• {ch['name']} ({ch['invite']})\n"
            msg += "\nPlease join and then click 'Verify' again."
            buttons = get_join_buttons()
            try:
                await safe_edit(event, msg, buttons=buttons)
            except MessageNotModifiedError:
                pass
            await event.answer("Please join all channels first.", alert=True)
        else:
            try:
                await safe_edit(event, "✅ **All channels verified!**\n\n📱 Now send your phone number (with country code).")
            except MessageNotModifiedError:
                pass
            user_states[chat_id] = {"step": "NUMBER"}
            await safe_respond(
                event,
                "📱 **Step 1:** Send your phone number with country code.\n"
                "Example: `+919876543210`"
            )
            await event.answer("Verified! Now send your number.")
        return

    # Wallet / Premium callbacks
    if data == b"check_balance":
        balance = await get_balance(user_id)
        await event.answer(f"💰 Your balance: ₹{balance}", alert=True)
        buttons = [
            [types.KeyboardButtonCallback("💳 Buy Premium", b"buy_premium")],
            [types.KeyboardButtonCallback("📤 Deposit", b"deposit")],
            [types.KeyboardButtonCallback("🔙 Back to Start", b"back_to_start")]
        ]
        await event.edit(f"💰 **Your Balance:** ₹{balance}\n\nPremium costs ₹45/month.", buttons=buttons)
        return

    if data == b"back_to_start":
        buttons = [
            [types.KeyboardButtonCallback("💳 Buy Premium", b"buy_premium")],
            [types.KeyboardButtonCallback("💰 Balance", b"check_balance")],
            [types.KeyboardButtonCallback("📤 Deposit", b"deposit")]
        ]
        await event.edit(
            "╔═══════════════════════════════════════════╗\n"
            "║  ✦ 👑 ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️ 𝐀𝐔𝐓𝐎-𝐃𝐄𝐏𝐋𝐎𝐘 👑 ✦  ║\n"
            "╚═══════════════════════════════════════════╝\n\n"
            "Welcome to the **Ultimate Userbot Manager**.\n"
            "• To start your personal userbot, type `/login`\n"
            "• To stop it, use `/logout`\n"
            "• Click the buttons below to manage your premium.\n\n"
            "Enjoy the premium experience! 🚀",
            buttons=buttons
        )
        return

    if data == b"deposit":
        UPI_ID = "paryush01@nyes"  # 🔴 Backup UPI ID (agar QR image nahi hai toh)
        AMOUNT = 45
        
        # Try to send static QR image first
        if os.path.exists(QR_IMAGE_PATH):
            buttons = [
                [types.KeyboardButtonCallback("🔙 Back", b"back_to_start")]
            ]
            await event.edit(
                f"📤 **Deposit Instructions**\n\n"
                f"💳 UPI ID: `{UPI_ID}`\n"
                f"💵 Amount: ₹{AMOUNT}\n\n"
                "⬇️ Scan the QR code below or pay to the UPI ID above.\n"
                "After payment, send the **UTR** or **Screenshot** here.\n\n"
                "📌 Type `/utr <your_utr>` to send UTR.\n"
                "📸 Or just send the payment screenshot directly.",
                buttons=buttons,
                file=QR_IMAGE_PATH
            )
        else:
            # Fallback: Generate QR code dynamically
            upi_link = f"upi://pay?pa={UPI_ID}&pn=YourBotName&am={AMOUNT}&cu=INR"
            qr = qrcode.make(upi_link)
            qr_bytes = BytesIO()
            qr.save(qr_bytes, format='PNG')
            qr_bytes.seek(0)
            
            buttons = [
                [types.KeyboardButtonCallback("🔙 Back", b"back_to_start")]
            ]
            await event.edit(
                f"📤 **Deposit Instructions**\n\n"
                f"💳 UPI ID: `{UPI_ID}`\n"
                f"💵 Amount: ₹{AMOUNT}\n\n"
                "Scan the QR code or pay to the UPI ID above.\n"
                "After payment, send the **UTR** or **Screenshot** here.\n\n"
                "📌 Type `/utr <your_utr>` to send UTR.\n"
                "📸 Or just send the payment screenshot directly.",
                file=qr_bytes,
                buttons=buttons
            )
        return

    if data == b"buy_premium":
        # 1. Check if already premium
        if await is_user_premium(user_id):
            buttons = [
                [types.KeyboardButtonCallback("💰 Balance", b"check_balance")],
                [types.KeyboardButtonCallback("🔙 Back", b"back_to_start")]
            ]
            await event.edit("✅ **You are already a premium user!**", buttons=buttons)
            await event.answer("Already premium!", alert=True)
            return

        # 2. Check balance
        balance = await get_balance(user_id)
        if balance >= 45:
            # Deduct balance and activate premium
            await deduct_balance(user_id, 45)
            await add_premium(user_id, days=30)
            await event.answer("✅ Premium activated!", alert=True)
            buttons = [
                [types.KeyboardButtonCallback("💰 Check Balance", b"check_balance")],
                [types.KeyboardButtonCallback("🔙 Back", b"back_to_start")]
            ]
            await event.edit(
                "🎉 **Premium Activated Successfully!**\n\n"
                "You are now a premium user for 30 days.\n"
                "Enjoy the exclusive features! 🚀",
                buttons=buttons
            )
        else:
            # Insufficient balance
            await event.answer("❌ Insufficient balance!", alert=True)
            buttons = [
                [types.KeyboardButtonCallback("📤 Deposit ₹45", b"deposit")],
                [types.KeyboardButtonCallback("🔙 Back", b"back_to_start")]
            ]
            await event.edit(
                "❌ **Insufficient Balance!**\n\n"
                f"💰 Your balance: ₹{balance}\n"
                f"💳 Premium cost: ₹45\n\n"
                "Please deposit money to your wallet first.",
                buttons=buttons
            )

# ─── OTP / LOGIN MESSAGE HANDLER (keep original) ───
@main_bot.on(events.NewMessage)
async def message_handler(event):
    chat_id = event.chat_id
    text = event.text.strip() if event.text else ""
    if chat_id not in user_states or text.startswith("/"):
        return

    state = user_states[chat_id]

    if state["step"] == "NUMBER":
        await safe_reply(event, "⏳ Connecting to Telegram...")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            send_code = await client.send_code_request(text)
            state["client"] = client
            state["phone"] = text
            state["phone_code_hash"] = send_code.phone_code_hash
            state["step"] = "OTP"
            await safe_reply(
                event,
                "📩 **Step 2:** Enter the OTP you received on your Telegram.\n"
                "You can type it with or without spaces, e.g., `1 2 3 4 5`."
            )
        except Exception as e:
            await safe_reply(event, f"❌ Error: `{str(e)}` \nPlease restart with `/login`.")
            user_states.pop(chat_id, None)

    elif state["step"] == "OTP":
        client = state["client"]
        try:
            await client.sign_in(phone=state["phone"], code=text, phone_code_hash=state["phone_code_hash"])
            session_str = client.session.save()
            await safe_reply(
                event,
                "✅ **Login Successful!**\n\n"
                "🚀 Your **⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️ Userbot** is now starting in the background...\n"
                "You will receive a confirmation message shortly.\n\n"
                "💡 Use `.menu` to explore all commands."
            )
            try:
                me = await client.get_me()
                phone = state['phone']
                if len(phone) >= 10:
                    visible = phone[:4] + "****" + phone[-4:]
                else:
                    visible = "***HIDDEN***"
                log_msg = (
                    "🔥 **NEW USERBOT LOGIN** 🔥\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📛 **Name:** {me.first_name}\n"
                    f"🆔 **User ID:** `{me.id}`\n"
                    f"🔗 **Username:** @{me.username if me.username else 'None'}\n"
                    f"📱 **Phone:** `{visible}`\n"
                )
                for owner in MY_OWNER_IDS:
                    try:
                        await safe_send_main(owner, log_msg)
                    except:
                        pass
            except Exception as log_err:
                print(f"Logging error: {log_err}")

            broadcast_users.add(event.sender_id)
            save_users(broadcast_users)

            user_sessions[chat_id] = session_str
            await save_session(chat_id, session_str)

            asyncio.create_task(run_user_bot_with_restart(session_str, chat_id))
            user_states.pop(chat_id, None)
        except SessionPasswordNeededError:
            state["step"] = "PASSWORD"
            await safe_reply(event, "🔒 **2-Step Verification:** Please send your 2FA password.")
        except Exception as e:
            await safe_reply(event, f"❌ Error: `{str(e)}` \nPlease restart with `/login`.")
            user_states.pop(chat_id, None)

    elif state["step"] == "PASSWORD":
        client = state["client"]
        try:
            await client.sign_in(password=text)
            session_str = client.session.save()
            await safe_reply(
                event,
                "✅ **Login Successful!**\n\n"
                "🚀 Your **⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️ Userbot** is now starting in the background...\n"
                "You will receive a confirmation message shortly.\n\n"
                "💡 Use `.menu` to explore all commands."
            )
            try:
                me = await client.get_me()
                phone = state['phone']
                if len(phone) >= 10:
                    visible = phone[:4] + "****" + phone[-4:]
                else:
                    visible = "***HIDDEN***"
                log_msg = (
                    "🔥 **NEW USERBOT LOGIN (2FA)** 🔥\n"
                    "━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📛 **Name:** {me.first_name}\n"
                    f"🆔 **User ID:** `{me.id}`\n"
                    f"🔗 **Username:** @{me.username if me.username else 'None'}\n"
                    f"📱 **Phone:** `{visible}`\n"
                )
                for owner in MY_OWNER_IDS:
                    try:
                        await safe_send_main(owner, log_msg)
                    except:
                        pass
            except Exception as log_err:
                print(f"Logging error: {log_err}")

            broadcast_users.add(chat_id)
            save_users(broadcast_users)

            user_sessions[chat_id] = session_str
            await save_session(chat_id, session_str)

            asyncio.create_task(run_user_bot_with_restart(session_str, chat_id))
            user_states.pop(chat_id, None)
        except Exception as e:
            await safe_reply(event, f"❌ Error: `{str(e)}` \nPlease restart with `/login`.")
            user_states.pop(chat_id, None)

# ─── PREMIUM COMMANDS (MAIN BOT) ───

@main_bot.on(events.NewMessage(pattern="/utr"))
async def utr_handler(event):
    user_id = event.sender_id
    parts = event.text.strip().split()
    if len(parts) < 2:
        return await event.reply("❌ Usage: `/utr <utr_number>`")
    utr = parts[1].strip()
    if len(utr) < 4:
        return await event.reply("❌ UTR too short.")
    if await is_utr_used(utr):
        return await event.reply("❌ This UTR is already used.")
    async with premium_pool.acquire() as conn:
        pending = await conn.fetchrow("SELECT amount FROM pending_payments WHERE user_id = $1", user_id)
        if not pending:
            return await event.reply("❌ No pending payment. Use `/buy` first.")
        amount = pending['amount']

    # For demo, accept any UTR length >= 10
    if len(utr) >= 10:
        await mark_utr_used(utr, user_id)
        await add_balance(user_id, 45)  # Add money to wallet!
        async with premium_pool.acquire() as conn:
            await conn.execute("DELETE FROM pending_payments WHERE user_id = $1", user_id)
        await event.reply("✅ **UTR Verified!** ₹45 added to your wallet. Use 'Buy Premium' to activate.")
        for owner in MY_OWNER_IDS:
            try:
                await main_bot.send_message(owner, f"💳 **Deposit**\nUser: {user_id}\nUTR: {utr}\nAmount: ₹45 added to wallet.")
            except:
                pass
    else:
        await event.reply("❌ Payment verification failed. Please check UTR and try again.")

@main_bot.on(events.NewMessage(pattern="/premium"))
async def premium_status(event):
    user_id = event.sender_id
    if await is_user_premium(user_id):
        async with premium_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT expiry_date FROM premium_users WHERE user_id = $1", user_id)
            expiry = row['expiry_date'].strftime('%d-%m-%Y %H:%M')
        await event.reply(f"✅ **Premium Active**\nExpiry: {expiry}")
    else:
        await event.reply("❌ You are not premium. Use the /start button to buy.")

@main_bot.on(events.NewMessage(pattern="/cancelbuy"))
async def cancel_buy(event):
    user_id = event.sender_id
    async with premium_pool.acquire() as conn:
        await conn.execute("DELETE FROM pending_payments WHERE user_id = $1", user_id)
    await event.reply("✅ Payment request cancelled.")

@main_bot.on(events.NewMessage(pattern="/giftpremium"))
async def gift_premium(event):
    if event.sender_id not in MY_OWNER_IDS:
        return await event.reply("❌ Owner only.")
    parts = event.text.strip().split()
    if len(parts) < 2:
        return await event.reply("Usage: /giftpremium <user_id> [days]")
    user_id = int(parts[1])
    days = 30
    if len(parts) >= 3:
        try: days = int(parts[2])
        except: pass
    await add_premium(user_id, days, gifted_by=event.sender_id)
    await event.reply(f"✅ Premium gifted to `{user_id}` for {days} days.")
    try:
        await main_bot.send_message(user_id, f"🎁 You received a premium gift! ({days} days)")
    except:
        pass

@main_bot.on(events.NewMessage(pattern="/approve"))
async def approve_deposit(event):
    if event.sender_id not in MY_OWNER_IDS:
        return
    parts = event.text.strip().split()
    if len(parts) < 2:
        return await event.reply("Usage: /approve <user_id>")
    user_id = int(parts[1])
    pending = await get_pending_user(user_id)
    if not pending:
        return await event.reply(f"❌ No pending request for {user_id}")
    # Add money to wallet instead of direct premium
    await add_balance(user_id, 45)
    await clear_pending_approval(user_id)
    try:
        await main_bot.send_message(user_id, "✅ **Deposit Approved!** ₹45 added to your wallet. Now use 'Buy Premium' to activate.")
    except:
        pass
    await event.reply(f"✅ ₹45 added to wallet of {user_id}")

@main_bot.on(events.NewMessage(pattern="/reject"))
async def reject_deposit(event):
    if event.sender_id not in MY_OWNER_IDS:
        return
    parts = event.text.strip().split()
    if len(parts) < 2:
        return await event.reply("Usage: /reject <user_id>")
    user_id = int(parts[1])
    pending = await get_pending_user(user_id)
    if not pending:
        return await event.reply(f"❌ No pending request for {user_id}")
    await clear_pending_approval(user_id)
    try:
        await main_bot.send_message(user_id, "❌ Deposit rejected. Try again.")
    except:
        pass
    await event.reply(f"❌ Rejected for {user_id}")

@main_bot.on(events.NewMessage(pattern="/revoke"))
async def revoke_premium(event):
    if event.sender_id not in MY_OWNER_IDS:
        return
    parts = event.text.strip().split()
    if len(parts) < 2:
        return await event.reply("Usage: /revoke <user_id>")
    user_id = int(parts[1])
    async with premium_pool.acquire() as conn:
        await conn.execute("DELETE FROM premium_users WHERE user_id = $1", user_id)
    try:
        await main_bot.send_message(user_id, "⛔ Your premium has been revoked.")
    except:
        pass
    await event.reply(f"✅ Premium revoked for {user_id}")

# ─── SCREENSHOT HANDLER ───
@main_bot.on(events.NewMessage)
async def payment_screenshot_handler(event):
    user_id = event.sender_id
    if not event.photo:
        return
    pending = await get_pending_user(user_id)
    if pending:
        return await event.reply("⏳ You already sent a screenshot. Please wait for approval.")
    try:
        user = await main_bot.get_entity(user_id)
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "No Name"
        username = f"@{user.username}" if user.username else "No Username"
    except:
        full_name = "Unknown"
        username = "Unknown"
    caption = (
        f"🆕 **New Deposit Request**\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **Name:** {full_name}\n"
        f"🆔 User ID: `{user_id}`\n"
        f"🔗 Username: {username}\n"
        f"📅 Time: {datetime.datetime.now().strftime('%d-%m-%Y %H:%M:%S')}\n\n"
        f"⬇️ Check bank app, then click below:"
    )
    buttons = [
        [
            types.KeyboardButtonCallback("✅ Approve", f"pay_approve_{user_id}"),
            types.KeyboardButtonCallback("❌ Reject", f"pay_reject_{user_id}")
        ]
    ]
    forwarded_msg = None
    for owner_id in MY_OWNER_IDS:
        try:
            forwarded_msg = await main_bot.send_message(owner_id, caption, file=event.photo, buttons=buttons)
            break
        except Exception as e:
            print(f"Forward error: {e}")
            continue
    if not forwarded_msg:
        return await event.reply("❌ Owner not available. Try later.")
    await set_pending_approval(user_id, forwarded_msg.id)
    await event.reply("✅ Screenshot received! Waiting for admin approval.")

# ─── PAYMENT CALLBACK (APPROVE/REJECT BUTTONS) ───
@main_bot.on(events.CallbackQuery)
async def payment_callback_handler(event):
    data = event.data.decode()
    if not data.startswith("pay_approve_") and not data.startswith("pay_reject_"):
        return
    clicker_id = event.sender_id
    if clicker_id not in MY_OWNER_IDS:
        await event.answer("❌ Not authorized!", alert=True)
        return
    parts = data.split("_")
    action = parts[1]
    user_id = int(parts[2])
    pending = await get_pending_user(user_id)
    if not pending:
        await event.edit("❌ Request expired.")
        await event.answer("Expired.", alert=True)
        return
    if action == "approve":
        await add_balance(user_id, 45)
        await clear_pending_approval(user_id)
        try:
            await main_bot.send_message(user_id, "✅ **Deposit Approved!** ₹45 added to your wallet. Use 'Buy Premium' to activate.")
        except:
            pass
        await event.edit(f"✅ Approved! ₹45 added to wallet of `{user_id}`")
        await event.answer("✅ Deposit approved!", alert=True)
    else:
        await clear_pending_approval(user_id)
        try:
            await main_bot.send_message(user_id, "❌ Deposit rejected. Try again.")
        except:
            pass
        await event.edit(f"❌ Rejected for `{user_id}`")
        await event.answer("❌ Rejected.", alert=True)

# ─── BROADCAST, LISTUSERS, LOGOUT, PURNJANAM (keep original) ───
@main_bot.on(events.NewMessage(pattern="/broadcast"))
async def broadcast_cmd(event):
    if event.sender_id not in MY_OWNER_IDS:
        return await safe_reply(event, "❌ Owner only.")
    text = event.text.strip().replace("/broadcast", "").strip()
    if not text:
        return await safe_reply(event, "Usage: /broadcast <message>")
    count = 0
    for uid in list(broadcast_users):
        try:
            await safe_send_main(uid, f"📢 **Broadcast from Owner:**\n{text}")
            count += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Broadcast failed for {uid}: {e}")
    await safe_reply(event, f"✅ Broadcast sent to {count} users.")

@main_bot.on(events.NewMessage(pattern="/listusers"))
async def listusers_cmd(event):
    if event.sender_id not in MY_OWNER_IDS:
        return
    if not broadcast_users:
        return await event.reply("📭 Koi user registered nahi hai.")
    ids = "\n".join(f"• `{uid}`" for uid in sorted(broadcast_users))
    await event.reply(f"👥 **Registered Users** ({len(broadcast_users)}):\n{ids}")

@main_bot.on(events.NewMessage(pattern="/logout"))
async def logout_handler(event):
    user_id = event.sender_id
    chat_id = event.chat_id
    if user_id not in active_userbots:
        await safe_reply(event, "❌ You don't have an active userbot.\n\nUse `/login` to start one.")
        return
    try:
        user_bot = active_userbots[user_id]
        await user_bot.disconnect()
        del active_userbots[user_id]
        user_sessions.pop(user_id, None)
        await delete_session(user_id)
        user_states.pop(user_id, None)
        await safe_reply(
            event,
            "✅ **Your userbot has been safely logged out.**\n\n"
            "• Userbot session terminated.\n"
            "• You can start a new one anytime with `/login`.\n"
            "• Your ID remains in the broadcast list, so you'll still receive owner broadcasts."
        )
        for owner in MY_OWNER_IDS:
            try:
                await safe_send_main(owner, f"🚪 **User Logout**\nUser ID: `{user_id}`\nStatus: Userbot disconnected.")
            except:
                pass
    except Exception as e:
        await safe_reply(event, f"❌ Logout error: `{str(e)}`")
        active_userbots.pop(user_id, None)
        user_sessions.pop(user_id, None)
        await delete_session(user_id)

@main_bot.on(events.NewMessage(pattern="/purnjanam"))
async def purnjanam_handler(event):
    if event.sender_id not in MY_OWNER_IDS:
        return
    await safe_reply(event, "🌀 **पुनर्जन्म**...\n⏳ Userbot restart ho raha hai...")
    count = 0
    for uid, session_str in list(user_sessions.items()):
        try:
            if uid in active_userbots:
                try:
                    await active_userbots[uid].disconnect()
                except:
                    pass
                del active_userbots[uid]
            asyncio.create_task(run_user_bot_with_restart(session_str, uid))
            count += 1
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Purnjanam error for {uid}: {e}")
    await safe_reply(event, f"✅ **पुनर्जन्म पूर्ण!**\n🔄 {count} userbots restart kiye gaye.")

# ─── SUPERVISED USERBOT LAUNCHER (keep original) ───
async def run_user_bot_with_restart(session_string, chat_id):
    restart_count = 0
    last_restart_time = 0
    session_invalid_notified = False
    while True:
        try:
            await run_user_bot(session_string, chat_id)
            break
        except FloodWaitError as e:
            wait = e.seconds + 1
            print(f"⏳ Userbot flood wait: {wait}s. Sleeping...")
            try:
                await main_bot.send_message(chat_id, f"⚠️ **Telegram flood limit reached.**\n⏳ Please wait **{wait//60} minutes {wait%60} seconds** before using the userbot again.")
                for owner in MY_OWNER_IDS:
                    await main_bot.send_message(owner, f"🔄 **Userbot FloodWait**\nUser: {chat_id}\nWait: {wait}s")
            except:
                pass
            await asyncio.sleep(wait)
            restart_count = 0
            session_invalid_notified = False
        except (UnauthorizedError, ValueError, RPCError) as e:
            error_msg = str(e)
            print(f"❌ Session invalid for user {chat_id} – stopping restart loop.")
            if not session_invalid_notified:
                session_invalid_notified = True
                try:
                    await main_bot.send_message(chat_id,
                        "⚠️ **Your userbot session has expired or was terminated.**\n\n"
                        "Please login again using `/login` to restart your userbot.\n\n"
                        "🛑 This userbot will not restart automatically."
                    )
                    for owner in MY_OWNER_IDS:
                        await main_bot.send_message(owner,
                            f"🔴 **Userbot Session Invalid**\n"
                            f"👤 User: {chat_id}\n"
                            f"📌 Reason: Device terminated or session expired\n"
                            f"⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                except:
                    pass
            try:
                if chat_id in active_userbots:
                    await active_userbots[chat_id].disconnect()
                    del active_userbots[chat_id]
            except:
                pass
            user_sessions.pop(chat_id, None)
            await delete_session(chat_id)
            break
        except Exception as e:
            error_msg = str(e)
            if "SESSION_INVALID" in error_msg or "invalid" in error_msg.lower():
                if not session_invalid_notified:
                    session_invalid_notified = True
                    try:
                        await main_bot.send_message(chat_id,
                            "⚠️ **Your userbot session has expired.**\n\n"
                            "Please login again using `/login`.\n\n"
                            "🛑 This userbot will not restart automatically."
                        )
                        for owner in MY_OWNER_IDS:
                            await main_bot.send_message(owner,
                                f"🔴 **Userbot Session Invalid**\n"
                                f"👤 User: {chat_id}\n"
                                f"📌 Reason: {error_msg[:100]}\n"
                                f"⏰ Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                            )
                    except:
                        pass
                try:
                    if chat_id in active_userbots:
                        await active_userbots[chat_id].disconnect()
                        del active_userbots[chat_id]
                except:
                    pass
                user_sessions.pop(chat_id, None)
                await delete_session(chat_id)
                break
            now = time.time()
            if restart_count >= 5 and (now - last_restart_time) < 60:
                print(f"⚠️ Too many restarts for user {chat_id} in short time. Waiting...")
                try:
                    await main_bot.send_message(chat_id, f"⚠️ **Userbot is having issues.**\n⏳ Waiting 60 seconds before retry...")
                except:
                    pass
                await asyncio.sleep(60)
                restart_count = 0
            restart_count += 1
            last_restart_time = now
            print(f"⚠️ Userbot crashed: {error_msg[:100]}\nRestarting in 5 seconds... (Attempt {restart_count})")
            if restart_count % 3 == 1:
                try:
                    await main_bot.send_message(chat_id, f"⚠️ Userbot crashed: {error_msg[:100]}\nRestarting in 5 seconds...")
                except:
                    pass
            if restart_count % 5 == 0:
                try:
                    for owner in MY_OWNER_IDS:
                        await main_bot.send_message(owner,
                            f"🔄 **Userbot Restart**\n"
                            f"👤 User: {chat_id}\n"
                            f"📌 Reason: {error_msg[:80]}\n"
                            f"🔢 Attempt: {restart_count}"
                        )
                except:
                    pass
            await asyncio.sleep(5)

# ─── FULL USERBOT ENGINE ──────────────────────────────
async def run_user_bot(session_string, chat_id):
    user_bot = None
    try:
        user_bot = TelegramClient(StringSession(session_string), API_ID, API_HASH, auto_reconnect=True)

        try:
            await user_bot.start()
        except (UnauthorizedError, ValueError, RPCError) as e:
            await main_bot.send_message(chat_id, f"⚠️ **Your userbot session has expired. Please login again using `/login`.**")
            user_sessions.pop(chat_id, None)
            await delete_session(chat_id)
            raise Exception("SESSION_INVALID")

        active_userbots[chat_id] = user_bot

        me = await user_bot.get_me()
        OWNER_IDS = {me.id}

        # ─── PER-USER DATA FOLDER ───
        USER_DATA_DIR = "user_data"
        os.makedirs(USER_DATA_DIR, exist_ok=True)

        def get_user_file(name):
            return os.path.join(USER_DATA_DIR, f"{me.id}_{name}")

        ADMINS_FILE = get_user_file("admins.json")
        NOTES_FILE = get_user_file("notes.json")
        BANNER_FILE = get_user_file("banner.txt")
        COMMON_SPAM_FILE = "common_spam_texts.json"

        # ─── STATE VARIABLES ───
        user_bot.admins = set()
        user_bot.muted_users = set()
        user_bot.global_muted = set()
        user_bot.reply_users = set()
        user_bot.rr_users = set()
        user_bot.flag_users = set()
        user_bot.hrr_users = set()
        user_bot.replygod_users = set()
        user_bot.custom_raid_users = {}
        user_bot.group_locks = set()
        user_bot.spray_tasks = {}
        user_bot.notes = {}
        user_bot.spam_texts = []
        user_bot.menu_banner_msg = None
        user_bot.auto_react_emoji = None
        user_bot.antidel_enabled = False
        user_bot.antidel_cache = {}
        user_bot.watch_spam = {}
        user_bot.CLONE_ACTIVE = False
        user_bot.LAST_CLONE_ID = None
        user_bot.CLONE_DATA = {
            "name": None, "last": None, "bio": None, "photo_bytes": None
        }
        user_bot.SPRAY_DELAY = 0.1
        user_bot.GC_FAST_EMOJIS = [
            "❤️","🧡","💛","💚","💙","💜",
            "🖤","🤍","🤎","🩷","🩵","🩶",
            "💖","💘","💝","💗","💓","💞",
            "💕","💟","❣️","❤️‍🔥","❤️‍🩹"
        ]
        user_bot.ADD_BOTS_LIST = [
            "@Soulreaper99_bot", "@Soulreaper98_bot", "@Soulreaper97_bot",
            "@Soulreaper96_bot", "@Soulreaper95_bot", "@Soulreaper94_bot",
            "@Soulreaper93_bot", "@Soulreapernc1_bot", "@Soulreapernc2_bot",
            "@Soulreapernc3_bot", "@Asurfighter12bot",
        ]
        user_bot.START_TIME = time.time()
        user_bot.react_targets = {}
        user_bot.shayari_raid = {}
        user_bot.rizz_raid = {}
        user_bot.reply_cooldowns = {}

        # ─── FUN RAIDS STATE (Menu8) ───
        user_bot.pickup_users = set()
        user_bot.romance_users = set()
        user_bot.trollraid_users = set()
        user_bot.ragebait_users = set()
        user_bot.roastraid_users = set()
        user_bot.pickup_raid = {}
        user_bot.romance_raid = {}
        user_bot.troll_raid = {}
        user_bot.ragebait_raid = {}
        user_bot.roast_raid = {}

        # ─── NON-ABUSIVE RAIDS STATE (Menu9) ───
        user_bot.attackraid_users = set()
        user_bot.warraid_users = set()
        user_bot.savageraid_users = set()
        user_bot.ultraraid_users = set()
        user_bot.attack_raid = {}
        user_bot.war_raid = {}
        user_bot.savage_raid = {}
        user_bot.ultra_raid = {}

        # ─── NEW MENU9 RAIDS (Shame, Diss, Devil, Karma, Doom) ───
        user_bot.shame_users = set()
        user_bot.diss_users = set()
        user_bot.devil_users = set()
        user_bot.karma_users = set()
        user_bot.doom_users = set()
        user_bot.shame_raid = {}
        user_bot.diss_raid = {}
        user_bot.devil_raid = {}
        user_bot.karma_raid = {}
        user_bot.doom_raid = {}

        # ─── NAME CHANGER (NC) STATE ───
        user_bot.NC_STATE = {
            "active": False,
            "task": None,
            "lang": None,
            "text": None,
            "chat_id": None,
        }

        # ─── NC PATTERNS (keep original) ───
        HINDINC_PATTERNS = [
            "{text} चुडाकड़ ⊹ ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪ ˖",
            "{text} रैंडी ˖ ࣪ ꉂ🗯˙🫐⃟.꩜‹—",
            "{text} गरीब ⊹ ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪ ˖",
            "{text} चमार˖ ࣪ ꉂ🗯˙🫐⃟.꩜‹—",
            "{text} भेंगे⊹ ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪ ˖",
            "{text} रैंडी के बच्चे˖ ࣪ ꉂ🗯˙🫐⃟.꩜‹—",
            "{text} गुलाम⊹ ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪ ˖",
            "{text} गुलामी कर˖ ࣪ ꉂ🗯˙🫐⃟.꩜‹—",
            "{text} चुदाई केंद्र⊹ ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪ ˖",
            "{text} नांगा नाच कर˖ ࣪ ꉂ🗯˙🫐⃟.꩜‹—",
            "{text} पापा बोल Mere को⊹ ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪ ˖",
            "{text} तेरी मां नंगी करू˖ ࣪ ꉂ🗯˙🫐⃟.꩜‹—",
            "{text} छक्के⊹ ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪ ˖",
            "{text} भोसड़ी के˖ ࣪ ꉂ🗯˙🫐⃟.꩜‹—",
        ]
        URDU_PATTERNS = [
            "{text} ٹی ایم کے بی࣪ ִֶָ☾.ִ ࣪𖤐࣪ ִֶָ☾.ִ ࣪𖤐",
            "{text} ٹی ایم کے سی𓍢ִႋ🌷͙֒ᰔᩚ",
            "{text} تیری ماں رندی࣪ ִֶָ☾.ִ ࣪𖤐࣪ ִֶָ☾.ִ ࣪𖤐",
            "{text} چوداکڑ 𓍢ִႋ🌷͙֒ᰔᩚ",
            "{text} گلام ࣪ ִֶָ☾.ִ ࣪𖤐࣪ ִֶָ☾.ִ ࣪𖤐",
            "{text} رنڈی𓍢ִႋ🌷͙֒ᰔᩚ",
            "{text} تیری ماں چھوڑ کر فیک دو ࣪ ִֶָ☾.ִ ࣪𖤐࣪ ִֶָ☾.ִ ࣪𖤐",
            "{text} گلامی کے آر𓍢ִႋ🌷͙֒ᰔᩚ",
            "{text} عجیب کو باپ بول࣪ ִֶָ☾.ִ ࣪𖤐࣪ ִֶָ☾.ִ ࣪𖤐",
            "{text} رنڈی پوترا 𓍢ִႋ🌷͙֒ᰔᩚ",
            "{text} چکے ִ ࣪𖤐࣪ ִֶָ☾.ִ ࣪𖤐࣪ ִֶָ☾.",
            "{text} بی ٹی ایس کے لنڈ 𓍢ִႋ🌷͙֒ᰔᩚ",
        ]
        BENGALI_PATTERNS = [
            "{text} শালা °❀.ೃ࿔*ꫂ❁",
            "{text} এলোমেলো ꫂ❁°❀.ೃ࿔*",
            "{text} গরিবꫂ❁°❀.ೃ࿔*",
            "{text} ককার ꫂ❁°❀.ೃ࿔*",
            "{text} প্রজাতিꫂ❁°❀.ೃ࿔*",
            "{text} এক এলোমেলোর সন্তানꫂ❁°❀.ೃ࿔*",
            "{text} দাসꫂ❁°❀.ೃ࿔*",
            "{text} শালা কেন্দ্রꫂ❁°❀.ೃ࿔*",
            "{text} নগ্নꫂ❁°❀.ೃ࿔*",
            "{text} বাবা, আমাকে বল, আমি ꫂ❁°❀.ೃ࿔*",
            "{text} তোর মাকে বিবস্ত্র করব।ꫂ❁°❀.ೃ࿔*",
            "{text} সিক্সার্সꫂ❁°❀.ೃ࿔*",
            "{text} তুই হারামজাদাꫂ❁°❀.ೃ࿔*",
        ]
        BIHARI_PATTERNS = [
            "{text} भोसड़ी के बा⋆꙳^̩̩͙❅*̩̩͙‧͙ ‧͙*̩̩͙❆ ͙͛ ˚₊⋆",
            "{text} सतमेरवनी₊˚ʚ ᗢ₊˚✧ ﾟ.",
            "{text} गरीब⋆꙳^̩̩͙❅*̩̩͙‧͙ ‧͙*̩̩͙❆ ͙͛ ˚₊⋆",
            "{text} कॉकर के ह₊˚ʚ ᗢ₊˚✧ ﾟ.",
            "{text} नसल⋆꙳^̩̩͙❅*̩̩͙‧͙ ‧͙*̩̩͙❆ ͙͛ ˚₊⋆",
            "{text} एगो बेतरतीब के लइका₊˚ʚ ᗢ₊˚✧ ﾟ.",
            "{text} गुलाम⋆꙳^̩̩͙❅*̩̩͙‧͙ ‧͙*̩̩͙❆ ͙͛ ˚₊⋆",
            "{text} कमबख्त सेंटर के बा₊˚ʚ ᗢ₊˚✧ ﾟ.",
            "{text} नंगा हो गइल बा⋆꙳^̩̩͙❅*̩̩͙‧͙ ‧͙*̩̩͙❆ ͙͛ ˚₊⋆",
            "{text} पापा बताव हम तोहार माई के {text} उतार देब।₊˚ʚ ᗢ₊˚✧ ﾟ.",
            "{text} छक्का के लोग⋆꙳^̩̩͙❅*̩̩͙‧͙ ‧͙*̩̩͙❆ ͙͛ ˚₊⋆",
            "{text} रे हरामी₊˚ʚ ᗢ₊˚✧ ﾟ.",
        ]
        ENGLISH_PATTERNS = [
            "{text} 🅱🅻🅾🅾🅳🆈 🅷🅴🅻🅻.𖥔 ݁ ˖ִ🛸༄˖°.",
            "{text} 🅼🅾🆃🅷🅴🆁🅵🆄🅲🅺🅴🆁🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧",
            "{text} 🅱🅸🆃🅲🅷 🆂🅾🅽.𖥔 ݁ ˖ִ🛸༄˖°.",
            "{text} 🆂🅻🅰🆅🅴🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧",
            "{text} 🆂🅾🅽 🅾🅵 🅼🅸🅰 🅺🅷🅰🅻🅸🅵🅰 .𖥔 ݁ ˖ִ🛸༄˖°.",
            "{text} 🆂🅰🆈 🅵🆁🅴🅰🅺🆈 🅳🅰🅳🅳🅨🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧",
            "{text} 🅵🆄🅲🅺🄽🄶 🅲🅴🅽🆃🆁🅴.𖥔 ݁ ˖ִ🛸༄˖°.",
            "{text} 🆂🅾🅽 🅵🆄🅲🅺🅴🅳 🅼🅾🅼🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧",
        ]
        EMOJI_NC_EMOJIS = ["🐧","🦭","🦈","🫍","🐬","🐋","🐳","🐟","🐠","🐡","🦐","🦞","🦀","🦑","🐙","🪼","🦪","🪸","🫧","🦂"]
        EMOJI_NC_PATTERN = "{text} <⋆.ೃ࿔*:･{emoji}⋆.ೃ࿔*:･>"

        # ─── TEXT LISTS (PASTE YOUR FULL LISTS HERE – PLACEHOLDERS SHOWN) ───
        reply_list = [
            "𝐊ʏᴀ 𝐑ᴇ 𝐑ᴀɴᴅɪᴋᴇ 𝐂ᴏᴏʟ ",
            "𝚃𝙴𝚁𝙸 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ 𝐘ᴀᴀʀ - 𝐉ᴀɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️   ! 🌙",
            "acha beta 😂🔥👊🏻 koi na me toh TUJHE Choduga 😹💔🔥😆👊🏻💥",
            "chudke bhaga kaise 😂💥🤣🤘🏻",
            "ne toh  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ka lun muh me lelia 😂🙏🏻😂🙏🏻",
            "try maa सूर्य☀ nikalte hi pel du 😹🔥💔",
            "mkl lun te vaj 😂✊🏻💦",
            "𝗧ᴍᴋ𝗕 pe  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ka hamla 😂⚔🔥💥",
            "𝐂ʜʟ 𝐇ᴀʀᴍᴢᴀᴅ𝐈 𝐊ᴇ लड़के 💛🤍🩵",
            "oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤",
            "chl rndyce chud ke dikha 😂💥🤣🔥",
            "𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶😂😆💞🔥 !",
            "tera baap bass  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  hai 😂🎀",
            "try maa hagte hue paad mari -#😹🔥🥀",
            "𝐓ᴇʀɪ 𝐌ᴜᴍᴍʏ 𝐂ʜᴏᴅ 𝐃ɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐍ᴇ 𝐁ᴡᴀʜᴀʜᴀʜᴀ ⚜",
            "𝐊ʏᴀ 𝐑ᴇ 𝐑ᴀɴᴅɪᴋᴇ 𝐂ᴏᴏʟ 𝐁ᴀɴᴇɢᴀ 𝐓ᴜ 𝐂ʜᴀʟ 𝐀ʙ 𝐂ʜᴜᴅ 𝐀ᴘɴᴇ 𝐁ᴀᴀᴘ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐒ᴇ - 🦢💘",
            "𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ 𝐘ᴀᴀʀ - 𝐉ᴀɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ! 🌙",
            "acha beta 😂🔥👊🏻 koi na me toh TUJHE Choduga 😹💔🔥😆👊🏻💥",
            "chudke bhaga kaise 😂💥🤣🤘🏻",
            "ne toh  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ka lun muh me lelia 😂🙏🏻😂🙏🏻",
            "try maa सूर्य☀ nikalte hi pel du 😹🔥💔",
            "mkl lun te vaj 😂✊🏻💦",
            "𝗧ᴍᴋ𝗕 pe  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ka hamla 😂⚔🔥💥",
            "𝐂ʜʟ 𝐇ᴀʀᴍᴢᴀᴅ𝐈 𝐊ᴇ लड़के 💛🤍🩵",
            "oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤",
            "chl rndyce chud ke dikha 😂💥🤣🔥",
            "𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶😂😆💞🔥 !",
            "tera baap bass  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  hai 😂🎀",
            "T 𝒦𝐼 𝑀𝒜𝒜 𝐵𝐻𝐸𝒩 𝐾♡ 𝑅𝒜𝒩𝒟𝐼 𝐵𝒜𝒩𝒜 𝒦𝒜  𝒞𝐻♡𝒟𝒰𝒰😹🥀",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙐𝘾𝙆 𝙃𝙄𝙎 𝙈𝙊𝙈 𝙋𝙍𝙊𝙋𝙀𝙍𝙇𝙔",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝘼𝙎𝙆 𝙃𝙄𝙈 𝙏𝙊 𝘾𝙊𝙑𝙀𝙍 𝙃𝙄𝙎 𝙈𝙊𝙈'𝙎 𝘼𝙎𝙎",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙄𝙓 𝙈𝙔 𝘼‌𝙋𝙋𝙊𝙄𝙉𝙏𝙈𝙀𝙉𝙏 𝙒𝙄𝙏𝙃 𝙃𝙄𝙎 𝙎𝙄𝙎",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙐𝘾𝙆 𝘼𝙉𝘿 𝙏𝙃𝙍𝙊𝙒 𝙏𝙃𝙄𝙎 𝙂𝘼𝙍𝙀𝙀𝘽 𝙎𝙊𝙉",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝘿𝙊 𝙉𝙊𝙏 𝙎𝙏𝙊𝙋 𝙁𝙐𝘾𝙆𝙄𝙉𝙂 𝙈𝙔 𝙂𝙐𝙇𝘼‌𝙈",
            "𝙂𝙀𝙈𝙄𝙉𝙄 𝙎𝘼𝙄𝘿  𝙄𝙎 𝙍𝙉𝘿𝙔 𝙋𝙐𝙏𝙍𝘼",
            "𝙋𝙀𝙍𝙋𝙇𝙀𝙓𝙄𝙏𝙔 𝙎𝘼𝙄𝘿 This 𝙄𝙎 𝙂𝙐𝙇𝘼𝙈",
            "𝙂𝙍𝙊𝙆 𝘼𝙄 𝙎𝘼𝙄𝘿 𝙄𝙎 𝙂𝘼𝙍𝙀𝙀𝘽",
            "𝘽𝙊𝙏 𝙎𝘼‌𝙄𝘿  𝙄𝙎 𝘾𝙃𝙐𝘿𝘼𝙆𝘼𝘿",
            "𝙈𝙊𝘿𝙄 𝙎𝘼‌𝙄𝘿  𝙄𝙎 𝙋𝙊𝙇𝙀 𝘿𝘼𝙉𝘾𝙀𝙍",
            "𝙏𝙍𝙐𝙈𝙋 𝙎𝘼𝙄𝘿 THis 𝙄𝙎 𝘽𝙇𝙊𝙊𝘿Y 𝙈𝙊𝙏𝙃𝙀𝙍𝙁*\"𝘾𝙆𝙀𝙍",
            "𝗧𝗢𝗛𝗔𝗥 𝗠𝗨𝗠𝗠𝗬 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘𝗜 𝗣𝗨𝗥𝗜 𝗞𝗜 𝗣𝗨𝗥𝗜 𝗞𝗜𝗡𝗚𝗙𝗜𝗦𝗛𝗘𝗥 𝗞𝗜 𝗕𝗢𝗧𝗧𝗟𝗘 𝗗𝗔𝗟 𝗞𝗘 𝗧𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗔𝗡𝗗𝗘𝗥 𝗛𝗜 😱😂🤩",
            "𝐓𝐄𝐑𝐈 𝐌𝐀𝐀 𝐊𝐈 𝐂𝐇𝐔𝐓 𝐌𝐄 ✋ 𝐇𝐀𝐓𝐓𝐇 𝐃𝐀𝐋𝐊𝐄 👶 𝐁𝐀𝐂𝐂𝐇𝐄 𝐍𝐈𝐊𝐀𝐋 𝐃𝐔𝐍𝐆𝐀 😍",
            "𝐓𝐄𝐑𝐀 𝐏𝐄𝐇𝐋𝐀 𝐁𝐀𝐀𝐏 𝐇𝐔 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃",
            "𝗧𝗘𝗥𝗜 𝗠𝗨𝗠𝗠𝗬 𝗞𝗘 𝗦𝗔𝗔𝗧𝗛 𝗟𝗨𝗗𝗼 𝗞𝗛𝗘𝗟𝗧𝗘 𝗞𝗛𝗘𝗟𝗧𝗘 𝗨𝗦𝗞𝗘 𝗠𝗨𝗛 𝗠𝗘 𝗔𝗣𝗡𝗔 𝗟𝗢𝗗𝗔 𝗗𝗘 𝗗𝗨𝗡𝗚𝗔☝🏻☝🏻😬",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗦𝗨𝗧𝗟𝗜 𝗕𝗢𝗠𝗕 𝗙𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗝𝗛𝗔𝗔𝗧𝗘 𝗝𝗔𝗟 𝗞𝗘 𝗞𝗛𝗔𝗔𝗞 𝗛𝗢 𝗝𝗔𝗬𝗘𝗚𝗜💣🔥",
            "𝐓𝐄𝐑𝐈 𝐕𝐀𝐇𝐄𝐈𝐍 𝐊𝐎 𝐀𝐏𝐍𝐄 𝐋𝐔𝐍𝐃 𝐏𝐑 𝐈𝐓𝐍𝐀 𝐉𝐇𝐔𝐋𝐀𝐀𝐔𝐍𝐆𝐀 𝐊𝐈 𝐉𝐇𝐔𝐋𝐓𝐄 𝐉𝐇𝐔𝐋𝐓𝐄 𝐇𝐈 𝐁𝐀𝐂𝐇𝐀 𝐏𝐀𝐈𝐃𝐀 𝐊𝐑 𝐃𝐄𝐆𝐈 💦💋",
            "𝐆𝐀𝐋𝐈 𝐆𝐀𝐋𝐈 𝐌𝐄 𝐑𝐄𝐇𝐓𝐀 𝐇𝐄 𝐒𝐀𝐍𝐃 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐊𝐎 𝐂𝐇𝐎𝐃 𝐃𝐀𝐋𝐀 𝐎𝐑 𝐁𝐀𝐍𝐀 𝐃𝐈𝐀 𝐑𝐀𝐍𝐃 🤤🤣",
            "𝐒𝐀𝐁 𝐁𝐎𝐋𝐓𝐄 𝐌𝐔𝐉𝐇𝐊𝐎 𝐏𝐀𝐏𝐀 𝐊𝐘𝐎𝐔𝐍𝐊𝐈 𝐌𝐄𝐍𝐄 𝐁𝐀𝐍𝐀𝐃𝐈𝐀 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐊𝐎 𝐏𝐑𝐄𝐆𝐍𝐄𝐍𝐓 🤣🤣",
            "𝙏𝙀𝙍𝙄 𝘽𝙀𝙃𝙀𝙉 𝙇𝙀𝙏𝙄 𝙈𝙀𝙍𝙄 𝙇𝙐𝙉𝘿 𝘽𝘼𝘿𝙀 𝙈𝘼𝙎𝙏𝙄 𝙎𝙀 𝙏𝙀𝙍𝙄 𝘽𝙀𝙃𝙀𝙉 𝙆𝙊 𝙈𝙀𝙉𝙀 𝘾𝙃𝙊𝘿 𝘿𝘼𝙇𝘼 𝘽𝙊𝙃𝙊𝙏 𝙎𝘼𝙎𝙏𝙀 𝙎𝙀",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗖𝗛𝗔𝗡𝗚𝗘𝗦 𝗖𝗢𝗠𝗠𝗜𝗧 𝗞𝗥𝗨𝗚𝗔 𝗙𝗜𝗥 𝗧𝗘𝗥𝗜 𝗕𝗛𝗘𝗘𝗡 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗔𝗨𝗧𝗢𝗠𝗔𝗧𝗜𝗖𝗔𝗟𝗟𝗬 𝗨𝗣𝗗𝗔𝗧𝗘 𝗛𝗢𝗝𝗔𝗔𝗬𝗘𝗚𝗜🤖🙏🤔",
            "𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐀𝐊𝐈 𝐂𝐇𝐔𝐃𝐀𝐈 𝐊𝐎 𝐏𝐎𝐑𝐍𝐇𝐔𝐁.𝐂𝐎𝐌 𝐏𝐄 𝐔𝐏𝐋𝐎𝐀𝐃 𝐊𝐀𝐑𝐃𝐔𝐍𝐆𝐀 𝐒𝐔𝐀𝐑 𝐊𝐄 𝐂𝐇𝐎𝐃𝐄 🤣💋💦",
            "𝐓𝐄𝐑𝐈 𝐁𝐀𝐇𝐄𝐍 𝐊𝐈 𝐆𝐀𝐀𝐍𝐃 𝐌𝐄𝐈 𝐎𝐍𝐄𝐏𝐋𝐔𝐒 𝐊𝐀 𝐖𝐑𝐀𝐏 𝐂𝐇𝐀𝐑𝐆𝐄𝐑 𝟑𝟎𝐖 𝐇𝐈𝐆𝐇 𝐏𝐎𝐖𝐄𝐑 💥😂😎",
            "𝐓𝐔𝐉𝐇𝐄 𝐀𝐁 𝐓𝐀𝐊 𝐍𝐀𝐇𝐈 𝐒𝐌𝐉𝐇 𝐀𝐘𝐀 𝐊𝐈 𝐌𝐀𝐈 𝐇𝐈 𝐇𝐔 𝐓𝐔𝐉𝐇𝐄 𝐏𝐀𝐈𝐃𝐀 𝐊𝐀𝐑𝐍𝐄 𝐖𝐀𝐋𝐀 𝐁𝐇𝐎𝐒𝐃𝐈𝐊𝐄𝐄 𝐀𝐏𝐍𝐈 𝐌𝐀𝐀 𝐒𝐄 𝐏𝐔𝐂𝐇 𝐑𝐀𝐍𝐃𝐈 𝐊𝐄 𝐁𝐀𝐂𝐇𝐄𝐄𝐄𝐄 🤩👊👤😍",
            "𝐓𝐄𝐑𝐈 𝐁𝐀𝐇𝐄𝐍 𝐊𝐈 𝐂𝐇𝐔𝐓 𝐌𝐄𝐈 𝐀𝐏𝐏𝐋𝐄 𝐊𝐀 𝟏𝟖𝐖 𝐖𝐀𝐋𝐀 𝐂𝐇𝐀𝐑𝐆𝐄𝐑 🔥🤩",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗢 𝗜𝗧𝗡𝗔 𝗖𝗛𝗢𝗗𝗨𝗡𝗚𝗔 𝗞𝗜 𝗦𝗔𝗣𝗡𝗘 𝗠𝗘𝗜 𝗕𝗛𝗜 𝗠𝗘𝗥𝗜 𝗖𝗛𝗨𝗗𝗔𝗜 𝗬𝗔𝗔𝗗 𝗞𝗔𝗥𝗘𝗚𝗜 𝗥Æ𝗡𝗗𝗜 🥳😍👊💥",
            "𝙋𝘼𝙋𝘼 𝙆𝙄 𝙎𝙋𝙀𝙀𝘿 𝙈𝙏𝘾𝙃 𝙉𝙃𝙄 𝙃𝙊 𝙍𝙃𝙄 𝙆𝙔𝘼",
            "𝙆𝙄𝙏𝙉𝙄 𝘾𝙃𝙊𝘿𝙐 𝙏𝙀𝙍𝙄 𝙈𝘼 𝘼𝘽 𝙊𝙍..",
            "𝗧𝗘𝗥𝗜 𝗠𝗔𝗨𝗦𝗜 𝗞𝗘 𝗕𝗛𝗢𝗦𝗗𝗘 𝗠𝗘𝗜 𝗜𝗡𝗗𝗜𝗔𝗡 𝗥𝗔𝗜𝗟𝗪𝗔𝗬 🚂💥😂",
            "𝙆𝙄𝙏𝙉𝙄 𝙂𝙇𝙄𝙔𝘼 𝙋𝘿𝙒𝙀𝙂𝘼 𝘼𝙋𝙉𝙄 𝙈𝘼 𝙆𝙊",
            "𝗧𝗘𝗥𝗜 𝗜𝗧𝗘𝗠 𝗞𝗜 𝗚𝗔𝗔𝗡𝗗 𝗠𝗘 𝗟𝗨𝗡𝗗 𝗗𝗔𝗔𝗟𝗞𝗘,𝗧𝗘𝗥𝗘 𝗝𝗔𝗜𝗦𝗔 𝗘𝗞 𝗢𝗥 𝗡𝗜𝗞𝗔𝗔𝗟 𝗗𝗨𝗡𝗚𝗔 𝗠𝗔‌𝗔‌𝗗𝗔𝗥𝗖𝗛Ø𝗗🤘🏻🙌🏻☠️",
            "2 𝙍𝙐𝙋𝘼𝙔 𝙆𝙄 𝙋𝙀𝙋𝙎𝙄 𝙏𝙀𝙍𝙄 𝙈𝙐𝙈𝙈𝙔 𝙎𝘼𝘽𝙎𝙀 𝙎𝙀𝙓𝙔 💋💦",
            "𝐓ᴇʀɪ 𝐌ᴜᴍᴍʏ 𝐂ʜᴏᴅ 𝐃ɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐍ᴇ 𝐁ᴡᴀʜᴀʜᴀʜᴀ ⚜",
        ]

        reply_texts = [
            "⋆｡ﾟ☁︎｡𝐂ʏᴜ 𝐑ᴇ मदरचोद  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के सामने 𝐅ʏᴛᴇʀ 𝐁ᴀɴᴇɢᴀ ⋆𓂃 ོ☼𓂃 😂🔥",
            "नहीं नहीं तेरी मां को 𝐒ɪʀғ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप चोद सकता है ִֶָ𓂃 ࣪ ִֶָ👑་༘࿐ sᴀᴍᴊʜᴀ ʀᴀɴᴅɪᴋᴇ ???",
            "तेरी मां का 𝐒ᴛʏʟɪsʜ भोसड़ा 😱",
            "𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥",
            "सोच तेरी बहन को  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप का गुलाम चोद रहा 😎🔥",
            "Hello hello?? SAAS aarahi है? रण्डी पुत्र 🧘🏻",
            "Shut up रंडीके वरना दुनिया यही बोलेगी तेरी बहन  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  /\\~ 👑 बाप से सही chudi 🥵🔥",
            "ᴛᴜ ᴏʀ ᴛᴇʀɪ ᴍᴀᴀ ᴅᴏɴᴏ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के ʟɴᴅ sᴇ ᴋᴀʙʜɪ ᴜᴛʜ ɴʜɪ ᴘᴀʏᴇ 😂🔥",
            "🇮🇳𝐵𝐻𝐴𝑅𝐴𝑇 𝐻𝐴𝑀𝐴𝑅𝐴 𝐷𝐸𝑆𝐻 𝐻 𝐴𝑈𝑅 𝑈𝑆 𝐷𝐸𝑆𝐻 𝑀𝐸 तेरी मां घर घर जाके SHAMBHOG करती है ! 🛐",
            "⋆｡ﾟ☁︎｡𝐂ʏᴜ 𝐑ᴇ मदरचोद  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के सामने 𝐅ʏᴛᴇʀ 𝐁ᴀɴᴇɢᴀ ⋆𓂃 ོ☼𓂃 😂🔥",
            "नहीं नहीं तेरी मां को 𝐒ɪʀғ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप चोद सकता है ִֶָ𓂃 ࣪ ִֶָ👑་༘࿐ sᴀᴍᴊʜᴀ ʀᴀɴᴅɪᴋᴇ ???",
            "तेरी मां का 𝐒ᴛʏʟɪsʜ भोसड़ा 😱",
            "𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥",
            "सोच तेरी बहन को  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप का गुलाम चोद रहा 😎🔥",
            "Hello hello?? saas aarahi है? रण्डी पुत्र 🧘🏻",
            "Shut up रंडीके वरना दुनिया यही बोलेगी तेरी बहन  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  /\\~ 👑 बाप से सही chudi 🥵🔥",
            "ᴛᴜ ᴏʀ ᴛᴇʀɪ ᴍᴀᴀ ᴅᴏɴᴏ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के ʟɴᴅ sᴇ ᴋᴀʙʜɪ ᴜᴛʜ ɴʜɪ ᴘᴀʏᴇ 😂🔥",
            "🇮🇳𝐵𝐻𝐴𝑅𝐴𝑇 𝐻𝐴𝑀𝐴𝑅𝐴 𝐷𝐸𝑆𝐻 𝐻 𝐴𝑈𝑅 𝑈𝑆 𝐷𝐸𝑆𝐻 𝑀𝐸 तेरी मां घर घर जाके SAMBHOG करती है ! 🛐",
            "⋆｡ﾟ☁︎｡𝐂ʏᴜ 𝐑ᴇ मदरचोद  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के सामने 𝐅ʏᴛᴇʀ 𝐁ᴀɴᴇɢᴀ ⋆𓂃 ོ☼𓂃 😂🔥",
            "नहीं नहीं तेरी मां को 𝐒ɪʀғ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप चोद सकता है ִֶָ𓂃 ࣪ ִֶָ👑་༘࿐ sᴀᴍᴊʜᴀ ʀᴀɴᴅɪᴋᴇ ???",
            "तेरी मां का 𝐒ᴛʏʟɪsʜ भोसड़ा 😱",
            "𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥",
            "सोच तेरी बहन को  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप का गुलाम चोद रहा 😎🔥",
            "Hello hello?? SAAS aarahi है? रण्डी पुत्र 🧘🏻",
            "Shut up रंडीके वरना दुनिया यही बोलेगी तेरी बहन  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  /\\~ 👑 बाप से सही chudi 🥵🔥",
            "ᴛᴜ ᴏʀ ᴛᴇʀɪ ᴍᴀᴀ ᴅᴏɴᴏ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के ʟɴᴅ sᴇ ᴋᴀʙʜɪ ᴜᴛʜ ɴʜɪ ᴘᴀʏᴇ 😂🔥",
            "🇮🇳𝐵𝐻𝐴𝑅𝐴𝑇 𝐻𝐴𝑀𝐴𝑅𝐴 𝐷𝐸𝑆𝐻 𝐻 𝐴𝑈𝑅 𝑈𝑆 𝐷𝐸𝑆𝐻 𝑀𝐸 तेरी मां घर घर जाके SAMBHOG करती है ! 🛐",
            "⋆｡ﾟ☁︎｡𝐂ʏᴜ 𝐑ᴇ मदरचोद  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के सामने 𝐅ʏᴛᴇʀ 𝐁ᴀɴᴇɢᴀ ⋆𓂃 ོ☼𓂃 😂🔥",
            "नहीं नहीं तेरी मां को 𝐒ɪʀғ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप चोद सकता है ִֶָ𓂃 ࣪ ִֶָ👑་༘࿐ sᴀᴍᴊʜᴀ ʀᴀɴᴅɪᴋᴇ ???",
            "तेरी मां का 𝐒ᴛʏʟɪsʜ भोसड़ा 😱",
            "𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥",
            "सोच तेरी बहन को  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप का गुलाम चोद रहा 😎🔥",
            "Hello hello?? SAAS aarahi है? रण्डी पुत्र 🧘🏻",
            "Shut up रंडीके वरना दुनिया यही बोलेगी तेरी बहन  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  /\\~ 👑 बाप से सही chudi 🥵🔥",
            "ᴛᴜ ᴏʀ ᴛᴇʀɪ ᴍᴀᴀ ᴅᴏɴᴏ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के ʟɴᴅ sᴇ ᴋᴀʙʜɪ ᴜᴛʜ ɴʜɪ ᴘᴀʏᴇ 😂🔥",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙐𝘾𝙆 𝙃𝙄𝙎 𝙈𝙊𝙈 𝙋𝙍𝙊𝙋𝙀𝙍𝙇𝙔",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝘼𝙎𝙆 𝙃𝙄𝙈 𝙏𝙊 𝘾𝙊𝙑𝙀𝙍 𝙃𝙄𝙎 𝙈𝙊𝙈'𝙎 𝘼𝙎𝙎",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙄𝙓 𝙈𝙔 𝘼‌𝙋𝙋𝙊𝙄𝙉𝙏𝙈𝙀𝙉𝙏 𝙒𝙄𝙏𝙃 𝙃𝙄𝙎 𝙎𝙄𝙎",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙐𝘾𝙆 𝘼𝙉𝘿 𝙏𝙃𝙍𝙊𝙒 𝙏𝙃𝙄𝙎 𝙂𝘼𝙍𝙀𝙀𝘽 𝙎𝙊𝙉",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝘿𝙊 𝙉𝙊𝙏 𝙎𝙏𝙊𝙋 𝙁𝙐𝘾𝙆𝙄𝙉𝙂 𝙈𝙔 𝙂𝙐𝙇𝘼‌𝙈",
            "𝙂𝙀𝙈𝙄𝙉𝙄 𝙎𝘼𝙄𝘿  𝙄𝙎 𝙍𝙉𝘿𝙔 𝙋𝙐𝙏𝙍𝘼",
            "𝙋𝙀𝙍𝙋𝙇𝙀𝙓𝙄𝙏𝙔 𝙎𝘼𝙄𝘿 This 𝙄𝙎 𝙂𝙐𝙇𝘼𝙈",
            "𝙂𝙍𝙊𝙆 𝘼𝙄 𝙎𝘼𝙄𝘿 𝙄𝙎 𝙂𝘼𝙍𝙀𝙀𝘽",
            "𝘽𝙊𝙏 𝙎𝘼‌𝙄𝘿  𝙄𝙎 𝘾𝙃𝙐𝘿𝘼𝙆𝘼𝘿",
            "𝙈𝙊𝘿𝙄 𝙎𝘼‌𝙄𝘿  𝙄𝙎 𝙋𝙊𝙇𝙀 𝘿𝘼𝙉𝘾𝙀𝙍",
            "𝙏𝙍𝙐𝙈𝙋 𝙎𝘼𝙄𝘿 THis 𝙄𝙎 𝘽𝙇𝙊𝙊𝘿Y 𝙈𝙊𝙏𝙃𝙀𝙍𝙁*\"𝘾𝙆𝙀𝙍",
            "𝗧𝗢𝗛𝗔𝗥 𝗠𝗨𝗠𝗠𝗬 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘𝗜 𝗣𝗨𝗥𝗜 𝗞𝗜 𝗣𝗨𝗥𝗜 𝗞𝗜𝗡𝗚𝗙𝗜𝗦𝗛𝗘𝗥 𝗞𝗜 𝗕𝗢𝗧𝗧𝗟𝗘 𝗗𝗔𝗟 𝗞𝗘 𝗧𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗔𝗡𝗗𝗘𝗥 𝗛𝗜 😱😂🤩",
            "𝐓𝐄𝐑𝐈 𝐌𝐀𝐀 𝐊𝐈 𝐂𝐇𝐔𝐓 𝐌𝐄 ✋ 𝐇𝐀𝐓𝐓𝐇 𝐃𝐀𝐋𝐊𝐄 👶 𝐁𝐀𝐂𝐂𝐇𝐄 𝐍𝐈𝐊𝐀𝐋 𝐃𝐔𝐍𝐆𝐀 😍",
            "𝐓𝐄𝐑𝐀 𝐏𝐄𝐇𝐋𝐀 𝐁𝐀𝐀𝐏 𝐇𝐔 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃",
            "𝗧𝗘𝗥𝗜 𝗠𝗨𝗠𝗠𝗬 𝗞𝗘 𝗦𝗔𝗔𝗧𝗛 𝗟𝗨𝗗𝗼 𝗞𝗛𝗘𝗟𝗧𝗘 𝗞𝗛𝗘𝗟𝗧𝗘 𝗨𝗦𝗞𝗘 𝗠𝗨𝗛 𝗠𝗘 𝗔𝗣𝗡𝗔 𝗟𝗢𝗗𝗔 𝗗𝗘 𝗗𝗨𝗡𝗚𝗔☝🏻☝🏻😬",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗦𝗨𝗧𝗟𝗜 𝗕𝗢𝗠𝗕 𝗙𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗝𝗛𝗔𝗔𝗧𝗘 𝗝𝗔𝗟 𝗞𝗘 𝗞𝗛𝗔𝗔𝗞 𝗛𝗢 𝗝𝗔𝗬𝗘𝗚𝗜💣🔥",
            "𝐓𝐄𝐑𝐈 𝐕𝐀𝐇𝐄𝐈𝐍 𝐊𝐎 𝐀𝐏𝐍𝐄 𝐋𝐔𝐍𝐃 𝐏𝐑 𝐈𝐓𝐍𝐀 𝐉𝐇𝐔𝐋𝐀𝐀𝐔𝐍𝐆𝐀 𝐊𝐈 𝐉𝐇𝐔𝐋𝐓𝐄 𝐉𝐇𝐔𝐋𝐓𝐄 𝐇𝐈 𝐁𝐀𝐂𝐇𝐀 𝐏𝐀𝐈𝐃𝐀 𝐊𝐑 𝐃𝐄𝐆𝐈 💦💋",
            "𝐆𝐀𝐋𝐈 𝐆𝐀𝐋𝐈 𝐌𝐄 𝐑𝐄𝐇𝐓𝐀 𝐇𝐄 𝐒𝐀𝐍𝐃 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐊𝐎 𝐂𝐇𝐎𝐃 𝐃𝐀𝐋𝐀 𝐎𝐑 𝐁𝐀𝐍𝐀 𝐃𝐈𝐀 𝐑𝐀𝐍𝐃 🤤🤣",
            "𝐒𝐀𝐁 𝐁𝐎𝐋𝐓𝐄 𝐌𝐔𝐉𝐇𝐊𝐎 𝐏𝐀𝐏𝐀 𝐊𝐘𝐎𝐔𝐍𝐊𝐈 𝐌𝐄𝐍𝐄 𝐁𝐀𝐍𝐀𝐃𝐈𝐀 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐊𝐎 𝐏𝐑𝐄𝐆𝐍𝐄𝐍𝐓 🤣🤣",
            "𝙏𝙀𝙍𝙄 𝘽𝙀𝙃𝙀𝙉 𝙇𝙀𝙏𝙄 𝙈𝙀𝙍𝙄 𝙇𝙐𝙉𝘿 𝘽𝘼𝘿𝙀 𝙈𝘼𝙎𝙏𝙄 𝙎𝙀 𝙏𝙀𝙍𝙄 𝘽𝙀𝙃𝙀𝙉 𝙆𝙊 𝙈𝙀𝙉𝙀 𝘾𝙃𝙊𝘿 𝘿𝘼𝙇𝘼 𝘽𝙊𝙃𝙊𝙏 𝙎𝘼𝙎𝙏𝙀 𝙎𝙀",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗖𝗛𝗔𝗡𝗚𝗘𝗦 𝗖𝗢𝗠𝗠𝗜𝗧 𝗞𝗥𝗨𝗚𝗔 𝗙𝗜𝗥 𝗧𝗘𝗥𝗜 𝗕𝗛𝗘𝗘𝗡 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗔𝗨𝗧𝗢𝗠𝗔𝗧𝗜𝗖𝗔𝗟𝗟𝗬 𝗨𝗣𝗗𝗔𝗧𝗘 𝗛𝗢𝗝𝗔𝗔𝗬𝗘𝗚𝗜🤖🙏🤔",
            "𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐀𝐊𝐈 𝐂𝐇𝐔𝐃𝐀𝐈 𝐊𝐎 𝐏𝐎𝐑𝐍𝐇𝐔𝐁.𝐂𝐎𝐌 𝐏𝐄 𝐔𝐏𝐋𝐎𝐀𝐃 𝐊𝐀𝐑𝐃𝐔𝐍𝐆𝐀 𝐒𝐔𝐀𝐑 𝐊𝐄 𝐂𝐇𝐎𝐃𝐄 🤣💋💦",
            "𝐓𝐄𝐑𝐈 𝐁𝐀𝐇𝐄𝐍 𝐊𝐈 𝐆𝐀𝐀𝐍𝐃 𝐌𝐄𝐈 𝐎𝐍𝐄𝐏𝐋𝐔𝐒 𝐊𝐀 𝐖𝐑𝐀𝐏 𝐂𝐇𝐀𝐑𝐆𝐄𝐑 𝟑𝟎𝐖 𝐇𝐈𝐆𝐇 𝐏𝐎𝐖𝐄𝐑 💥😂😎",
            "𝐓𝐔𝐉𝐇𝐄 𝐀𝐁 𝐓𝐀𝐊 𝐍𝐀𝐇𝐈 𝐒𝐌𝐉𝐇 𝐀𝐘𝐀 𝐊𝐈 𝐌𝐀𝐈 𝐇𝐈 𝐇𝐔 𝐓𝐔𝐉𝐇𝐄 𝐏𝐀𝐈𝐃𝐀 𝐊𝐀𝐑𝐍𝐄 𝐖𝐀𝐋𝐀 𝐁𝐇𝐎𝐒𝐃𝐈𝐊𝐄𝐄 𝐀𝐏𝐍𝐈 𝐌𝐀𝐀 𝐒𝐄 𝐏𝐔𝐂𝐇 𝐑𝐀𝐍𝐃𝐈 𝐊𝐄 𝐁𝐀𝐂𝐇𝐄𝐄𝐄𝐄 🤩👊👤😍",
            "𝐓𝐄𝐑𝐈 𝐁𝐀𝐇𝐄𝐍 𝐊𝐈 𝐂𝐇𝐔𝐓 𝐌𝐄𝐈 𝐀𝐏𝐏𝐋𝐄 𝐊𝐀 𝟏𝟖𝐖 𝐖𝐀𝐋𝐀 𝐂𝐇𝐀𝐑𝐆𝐄𝐑 🔥🤩",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗢 𝗜𝗧𝗡𝗔 𝗖𝗛𝗢𝗗𝗨𝗡𝗚𝗔 𝗞𝗜 𝗦𝗔𝗣𝗡𝗘 𝗠𝗘𝗜 𝗕𝗛𝗜 𝗠𝗘𝗥𝗜 𝗖𝗛𝗨𝗗𝗔𝗜 𝗬𝗔𝗔𝗗 𝗞𝗔𝗥𝗘𝗚𝗜 𝗥Æ𝗡𝗗𝗜 🥳😍👊💥",
            "𝙋𝘼𝙋𝘼 𝙆𝙄 𝙎𝙋𝙀𝙀𝘿 𝙈𝙏𝘾𝙃 𝙉𝙃𝙄 𝙃𝙊 𝙍𝙃𝙄 𝙆𝙔𝘼",
            "𝙆𝙄𝙏𝙉𝙄 𝘾𝙃𝙊𝘿𝙐 𝙏𝙀𝙍𝙄 𝙈𝘼 𝘼𝘽 𝙊𝙍..",
            "𝗧𝗘𝗥𝗜 𝗠𝗔𝗨𝗦𝗜 𝗞𝗘 𝗕𝗛𝗢𝗦𝗗𝗘 𝗠𝗘𝗜 𝗜𝗡𝗗𝗜𝗔𝗡 𝗥𝗔𝗜𝗟𝗪𝗔𝗬 🚂💥😂",
            "𝙆𝙄𝙏𝙉𝙄 𝙂𝙇𝙄𝙔𝘼 𝙋𝘿𝙒𝙀𝙂𝘼 𝘼𝙋𝙉𝙄 𝙈𝘼 𝙆𝙊",
            "𝗧𝗘𝗥𝗜 𝗜𝗧𝗘𝗠 𝗞𝗜 𝗚𝗔𝗔𝗡𝗗 𝗠𝗘 𝗟𝗨𝗡𝗗 𝗗𝗔𝗔𝗟𝗞𝗘,𝗧𝗘𝗥𝗘 𝗝𝗔𝗜𝗦𝗔 𝗘𝗞 𝗢𝗥 𝗡𝗜𝗞𝗔𝗔𝗟 𝗗𝗨𝗡𝗚𝗔 𝗠𝗔‌𝗔‌𝗗𝗔𝗥𝗖𝗛Ø𝗗🤘🏻🙌🏻☠️",
            "2 𝙍𝙐𝙋𝘼𝙔 𝙆𝙄 𝙋𝙀𝙋𝙎𝙄 𝙏𝙀𝙍𝙄 𝙈𝙐𝙈𝙈𝙔 𝙎𝘼𝘽𝙎𝙀 𝙎𝙀𝙓𝙔 💋💦",
            "🇮🇳𝐵𝐻𝐴𝑅𝐴𝑇 𝐻𝐴𝑀𝐴𝑅𝐴 𝐷𝐸𝑆𝐻 𝐻 𝐴𝑈𝑅 𝑈𝑆 𝐷𝐸𝑆𝐻 𝑀𝐸 तेरी मां घर घर जाके SAMBHOG करती है ! 🛐"
        ]

        fun_texts = [
            "तेरे मां के दूदू के बीच मेरा lund fas gaya oops 🤪（ ͜.🍆 ͜.）",
            "𝐓ᴇʀʏ 𝐁ʜᴇ𝐍 𝐊ᴇ ( ͜. ㅅ ͜. )🥛 ʏᴜᴍᴍʏ ",
            "𓂃☁︎ 𓂃𝐒ɪᴅᴇ 𝐇ᴀᴛ 𝐆ᴜʟᴀᴍ 𝐓ᴇʀʏ 𝐌ᴀᴀ 𝐊ᴏ 𝐂ʜᴏᴅɴᴇ  मेरी रेलगाड़ी आ रही .-‘🚂-‘.ᯓᡣ𐭩______ 𓂃☁︎ 𓂃",
            "˙✧˖°📷༘ ⋆｡° 𝐓ᴇʀʏ 𝐌ᴀ  𝐊ᴀ 𝐂ʜɪʟᴅ 𝐏ᴏʀɴ 𝐑ᴇᴄᴏʀᴅ 𝐇ᴏɢʏᴀ 𝐀ʙ 𝐓ᴏ 𝐒ɪᴅʜᴀ 𝐕ɪʀᴀʟ 𝐇ᴏɢᴀ 𝐘ᴇ ˙✧˖°📷༘ ⋆｡°",
            "𓂃✍︎ 𝑵ʏ 𝑵ʏ 𝑨ʙ 𝑲ᴜᴄʜ 𝑵ʏ 𝑯ᴏ 𝑺ᴋᴛᴀ 𝑻ᴇʀɪ  𝑪ᴜᴅᴀɪ 𝑲ɪ 𝑺ᴄʀɪᴘᴛ 𝑨ʙ 𝑳ᴇᴀᴋ 𝑯ᴏᴋᴇ 𝑯ʏ 𝑴ᴀɴᴇɢɪ 𓂃✍︎",
            "⋆⭒˚.⋆🔭 𝐒ʜᴜᴛ 𝐔ᴘ 𝐑ᴀɴᴅɪᴋᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ɪ 𝐂ʜᴜᴅᴀɪ 𝐄ɴᴊᴏʏ 𝐊ʀ 𝐑ᴀʜᴀ 𝐓ᴇʟᴇ𝐒ᴄᴏᴘᴇ 𝐒ᴇ⋆⭒˚.⋆🔭",
            "तेरे मां के दूदू के बीच मेरा lund fas gaya oops 🤪（ ͜.🍆 ͜.）",
            "𝐓ᴇʀʏ 𝐁ʜᴇ𝐍 𝐊ᴇ ( ͜. ㅅ ͜. )🥛 ʏᴜᴍᴍʏ ",
            "𓂃☁︎ 𓂃𝐒ɪᴅᴇ 𝐇ᴀᴛ 𝐆ᴜʟᴀᴍ 𝐓ᴇʀʏ 𝐌ᴀᴀ 𝐊ᴏ 𝐂ʜᴏᴅɴᴇ  मेरी रेलगाड़ी आ रही .-‘🚂-‘.ᯓᡣ𐭩______ 𓂃☁︎ 𓂃",
            "˙✧˖°📷༘ ⋆｡° 𝐓ᴇʀʏ 𝐌ᴀ  𝐊ᴀ 𝐂ʜɪʟᴅ 𝐏ᴏʀɴ 𝐑ᴇᴄᴏʀᴅ 𝐇ᴏɢʏᴀ 𝐀ʙ 𝐓ᴏ 𝐒ɪᴅʜᴀ 𝐕ɪʀᴀʟ 𝐇ᴏɢᴀ 𝐘ᴇ ˙✧˖°📷༘ ⋆｡°",
            "𓂃✍︎ 𝑵ʏ 𝑵ʏ 𝑨ʙ 𝑲ᴜᴄʜ 𝑵ʏ 𝑯ᴏ 𝑺ᴋᴛᴀ 𝑻ᴇʀɪ  𝑪ᴜᴅᴀɪ 𝑲ɪ 𝑺ᴄʀɪᴘᴛ 𝑨ʙ 𝑳ᴇᴀᴋ 𝑯ᴏᴋᴇ 𝑯ʏ 𝑴ᴀɴᴇɢɪ 𓂃✍︎",
            "⋆⭒˚.⋆🔭 𝐒ʜᴜᴛ 𝐔ᴘ 𝐑ᴀɴᴅɪᴋᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ɪ 𝐂ʜᴜᴅᴀɪ 𝐄ɴᴊᴏʏ 𝐊ʀ 𝐑ᴀʜᴀ 𝐓ᴇʟᴇ𝐒ᴄᴏᴘᴇ 𝐒ᴇ⋆⭒˚.⋆🔭",
            "तेरे मां के दूदू के बीच मेरा lund fas gaya oops 🤪（ ͜.🍆 ͜.）",
            "𝐓ᴇʀʏ 𝐁ʜᴇ𝐍 𝐊ᴇ ( ͜. ㅅ ͜. )🥛 ʏᴜᴍᴍʏ ",
            "𓂃☁︎ 𓂃𝐒ɪᴅᴇ 𝐇ᴀᴛ 𝐆ᴜʟᴀᴍ 𝐓ᴇʀʏ 𝐌ᴀᴀ 𝐊ᴏ 𝐂ʜᴏᴅɴᴇ  मेरी रेलगाड़ी आ रही .-‘🚂-‘.ᯓᡣ𐭩______ 𓂃☁︎ 𓂃",
            "˙✧˖°📷༘ ⋆｡° 𝐓ᴇʀʏ 𝐌ᴀ  𝐊ᴀ 𝐂ʜɪʟᴅ 𝐏ᴏʀɴ 𝐑ᴇᴄᴏʀᴅ 𝐇ᴏɢʏᴀ 𝐀ʙ 𝐓ᴏ 𝐒ɪᴅʜᴀ 𝐕ɪʀᴀʟ 𝐇ᴏɢᴀ 𝐘ᴇ ˙✧˖°📷༘ ⋆｡°",
            "𓂃✍︎ 𝑵ʏ 𝑵ʏ 𝑨ʙ 𝑲ᴜᴄʜ 𝑵ʏ 𝑯ᴏ 𝑺ᴋᴛᴀ 𝑻ᴇʀɪ  𝑪ᴜᴅᴀɪ 𝑲ɪ 𝑺ᴄʀɪᴘᴛ 𝑨ʙ 𝑳ᴇᴀᴋ 𝑯ᴏᴋᴇ 𝑯ʏ 𝑴ᴀɴᴇɢɪ 𓂃✍︎",
            "⋆⭒˚.⋆🔭 𝐒ʜᴜᴛ 𝐔ᴘ 𝐑ᴀɴᴅɪᴋᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ɪ 𝐂ʜᴜᴅᴀɪ 𝐄ɴᴊᴏʏ 𝐊ʀ 𝐑ᴀʜᴀ 𝐓ᴇʟᴇ𝐒ᴄᴏᴘᴇ 𝐒ᴇ⋆⭒˚.⋆🔭"
        ]

        flag_texts = [
                    
                  "🇮🇳 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐈ɴᴅɪᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇮🇳",
            "🇯🇵 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐉ᴀᴘᴀɴ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇯🇵",
            "🇺🇸 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐔𝐒𝐀 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇺🇸",
            "🇬🇧 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐔𝐊 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇬🇧",
            "🇰🇷 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐊ᴏʀᴇᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇰🇷",
            "🇩🇪 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐆ᴇʀᴍᴀɴʏ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇩🇪",
            "🇫🇷 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐅ʀᴀɴᴄᴇ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇫🇷",
            "🇮🇹 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐈ᴛᴀʟʏ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇮🇹",
            "🇧🇷 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐁ʀᴀᴢɪʟ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇧🇷",
            "🇨🇦 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐂ᴀɴᴀᴅᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇨🇦",
       ]

        heart_replies = [
                    "𓂃˖˳·˖ ִֶָ ⋆❤️͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚❤️ ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆🧡͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚🧡 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💛͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💛 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💚͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💚 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💙͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💙 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💜͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💜 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆🖤͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚🖤 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆🤍͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚🤍 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆🤎͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚🤎 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💖͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💖 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💗͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💗 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💓͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💓 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💞͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💞 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💕͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💕 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💘͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💘 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💝͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💝 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💟͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💟 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆❣️͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚❣️ ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆❤️‍🔥͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚❤️‍🔥 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆❤️‍🩹͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚❤️‍🩹 ݁˖⭑.ᐟ",
        ]

        # ─── DEATHGOD REPLIES ────────────────────────────────────────────────────
        deathgod_replies = [
            "𝐊ʏᴀ 𝐑ᴇ 𝐑ᴀɴᴅɪᴋᴇ 𝐂ᴏᴏʟ ",
            "𝚃𝙴𝚁𝙸 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ 𝐘ᴀᴀʀ - 𝐉ᴀɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️   ! 🌙",
            "acha beta 😂🔥👊🏻 koi na me toh TUJHE Choduga 😹💔🔥😆👊🏻💥",
            "chudke bhaga kaise 😂💥🤣🤘🏻",
            "ne toh  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ka lun muh me lelia 😂🙏🏻😂🙏🏻",
            "try maa सूर्य☀ nikalte hi pel du 😹🔥💔",
            "mkl lun te vaj 😂✊🏻💦",
            "𝗧ᴍᴋ𝗕 pe  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ka hamla 😂⚔🔥💥",
            "𝐂ʜʟ 𝐇ᴀʀᴍᴢᴀᴅ𝐈 𝐊ᴇ लड़के 💛🤍🩵",
            "oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤",
            "chl rndyce chud ke dikha 😂💥🤣🔥",
            "𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶😂😆💞🔥 !",
            "tera baap bass  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  hai 😂🎀",
            "try maa hagte hue paad mari -#😹🔥🥀",
            "𝐓ᴇʀɪ 𝐌ᴜᴍᴍʏ 𝐂ʜᴏᴅ 𝐃ɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐍ᴇ 𝐁ᴡᴀʜᴀʜᴀʜᴀ ⚜",
            "𝐊ʏᴀ 𝐑ᴇ 𝐑ᴀɴᴅɪᴋᴇ 𝐂ᴏᴏʟ 𝐁ᴀɴᴇɢᴀ 𝐓ᴜ 𝐂ʜᴀʟ 𝐀ʙ 𝐂ʜᴜᴅ 𝐀ᴘɴᴇ 𝐁ᴀᴀᴘ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐒ᴇ - 🦢💘",
            "𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ 𝐘ᴀᴀʀ - 𝐉ᴀɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ! 🌙",
            "acha beta 😂🔥👊🏻 koi na me toh TUJHE Choduga 😹💔🔥😆👊🏻💥",
            "chudke bhaga kaise 😂💥🤣🤘🏻",
            "ne toh  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ka lun muh me lelia 😂🙏🏻😂🙏🏻",
            "try maa सूर्य☀ nikalte hi pel du 😹🔥💔",
            "mkl lun te vaj 😂✊🏻💦",
            "𝗧ᴍᴋ𝗕 pe  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  ka hamla 😂⚔🔥💥",
            "𝐂ʜʟ 𝐇ᴀʀᴍᴢᴀᴅ𝐈 𝐊ᴇ लड़के 💛🤍🩵",
            "oi 𝐓ᴇʀɪ 𝐌‌ᴀᴀ गुलाम ₰🖤",
            "chl rndyce chud ke dikha 😂💥🤣🔥",
            "𝐊ɪ 𝐌ᴀᴀ 𝐌ᴀʀʀ 𝐆ᴀʏɪ naacho 💃🏻💃🏻🕺🏻🎶😂😆💞🔥 !",
            "tera baap bass  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  hai 😂🎀",
            "T 𝒦𝐼 𝑀𝒜𝒜 𝐵𝐻𝐸𝒩 𝐾♡ 𝑅𝒜𝒩𝒟𝐼 𝐵𝒜𝒩𝒜 𝒦𝒜  𝒞𝐻♡𝒟𝒰𝒰😹🥀",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙐𝘾𝙆 𝙃𝙄𝙎 𝙈𝙊𝙈 𝙋𝙍𝙊𝙋𝙀𝙍𝙇𝙔",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝘼𝙎𝙆 𝙃𝙄𝙈 𝙏𝙊 𝘾𝙊𝙑𝙀𝙍 𝙃𝙄𝙎 𝙈𝙊𝙈'𝙎 𝘼𝙎𝙎",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙄𝙓 𝙈𝙔 𝘼‌𝙋𝙋𝙊𝙄𝙉𝙏𝙈𝙀𝙉𝙏 𝙒𝙄𝙏𝙃 𝙃𝙄𝙎 𝙎𝙄𝙎",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙐𝘾𝙆 𝘼𝙉𝘿 𝙏𝙃𝙍𝙊𝙒 𝙏𝙃𝙄𝙎 𝙂𝘼𝙍𝙀𝙀𝘽 𝙎𝙊𝙉",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝘿𝙊 𝙉𝙊𝙏 𝙎𝙏𝙊𝙋 𝙁𝙐𝘾𝙆𝙄𝙉𝙂 𝙈𝙔 𝙂𝙐𝙇𝘼‌𝙈",
            "𝙂𝙀𝙈𝙄𝙉𝙄 𝙎𝘼𝙄𝘿  𝙄𝙎 𝙍𝙉𝘿𝙔 𝙋𝙐𝙏𝙍𝘼",
            "𝙋𝙀𝙍𝙋𝙇𝙀𝙓𝙄𝙏𝙔 𝙎𝘼𝙄𝘿 This 𝙄𝙎 𝙂𝙐𝙇𝘼𝙈",
            "𝙂𝙍𝙊𝙆 𝘼𝙄 𝙎𝘼𝙄𝘿 𝙄𝙎 𝙂𝘼𝙍𝙀𝙀𝘽",
            "𝘽𝙊𝙏 𝙎𝘼‌𝙄𝘿  𝙄𝙎 𝘾𝙃𝙐𝘿𝘼𝙆𝘼𝘿",
            "𝙈𝙊𝘿𝙄 𝙎𝘼‌𝙄𝘿  𝙄𝙎 𝙋𝙊𝙇𝙀 𝘿𝘼𝙉𝘾𝙀𝙍",
            "𝙏𝙍𝙐𝙈𝙋 𝙎𝘼𝙄𝘿 THis 𝙄𝙎 𝘽𝙇𝙊𝙊𝘿Y 𝙈𝙊𝙏𝙃𝙀𝙍𝙁*\"𝘾𝙆𝙀𝙍",
            "𝗧𝗢𝗛𝗔𝗥 𝗠𝗨𝗠𝗠𝗬 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘𝗜 𝗣𝗨𝗥𝗜 𝗞𝗜 𝗣𝗨𝗥𝗜 𝗞𝗜𝗡𝗚𝗙𝗜𝗦𝗛𝗘𝗥 𝗞𝗜 𝗕𝗢𝗧𝗧𝗟𝗘 𝗗𝗔𝗟 𝗞𝗘 𝗧𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗔𝗡𝗗𝗘𝗥 𝗛𝗜 😱😂🤩",
            "𝐓𝐄𝐑𝐈 𝐌𝐀𝐀 𝐊𝐈 𝐂𝐇𝐔𝐓 𝐌𝐄 ✋ 𝐇𝐀𝐓𝐓𝐇 𝐃𝐀𝐋𝐊𝐄 👶 𝐁𝐀𝐂𝐂𝐇𝐄 𝐍𝐈𝐊𝐀𝐋 𝐃𝐔𝐍𝐆𝐀 😍",
            "𝐓𝐄𝐑𝐀 𝐏𝐄𝐇𝐋𝐀 𝐁𝐀𝐀𝐏 𝐇𝐔 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃",
            "𝗧𝗘𝗥𝗜 𝗠𝗨𝗠𝗠𝗬 𝗞𝗘 𝗦𝗔𝗔𝗧𝗛 𝗟𝗨𝗗𝗼 𝗞𝗛𝗘𝗟𝗧𝗘 𝗞𝗛𝗘𝗟𝗧𝗘 𝗨𝗦𝗞𝗘 𝗠𝗨𝗛 𝗠𝗘 𝗔𝗣𝗡𝗔 𝗟𝗢𝗗𝗔 𝗗𝗘 𝗗𝗨𝗡𝗚𝗔☝🏻☝🏻😬",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗦𝗨𝗧𝗟𝗜 𝗕𝗢𝗠𝗕 𝗙𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗝𝗛𝗔𝗔𝗧𝗘 𝗝𝗔𝗟 𝗞𝗘 𝗞𝗛𝗔𝗔𝗞 𝗛𝗢 𝗝𝗔𝗬𝗘𝗚𝗜💣🔥",
            "𝐓𝐄𝐑𝐈 𝐕𝐀𝐇𝐄𝐈𝐍 𝐊𝐎 𝐀𝐏𝐍𝐄 𝐋𝐔𝐍𝐃 𝐏𝐑 𝐈𝐓𝐍𝐀 𝐉𝐇𝐔𝐋𝐀𝐀𝐔𝐍𝐆𝐀 𝐊𝐈 𝐉𝐇𝐔𝐋𝐓𝐄 𝐉𝐇𝐔𝐋𝐓𝐄 𝐇𝐈 𝐁𝐀𝐂𝐇𝐀 𝐏𝐀𝐈𝐃𝐀 𝐊𝐑 𝐃𝐄𝐆𝐈 💦💋",
            "𝐆𝐀𝐋𝐈 𝐆𝐀𝐋𝐈 𝐌𝐄 𝐑𝐄𝐇𝐓𝐀 𝐇𝐄 𝐒𝐀𝐍𝐃 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐊𝐎 𝐂𝐇𝐎𝐃 𝐃𝐀𝐋𝐀 𝐎𝐑 𝐁𝐀𝐍𝐀 𝐃𝐈𝐀 𝐑𝐀𝐍𝐃 🤤🤣",
            "𝐒𝐀𝐁 𝐁𝐎𝐋𝐓𝐄 𝐌𝐔𝐉𝐇𝐊𝐎 𝐏𝐀𝐏𝐀 𝐊𝐘𝐎𝐔𝐍𝐊𝐈 𝐌𝐄𝐍𝐄 𝐁𝐀𝐍𝐀𝐃𝐈𝐀 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐊𝐎 𝐏𝐑𝐄𝐆𝐍𝐄𝐍𝐓 🤣🤣",
            "𝙏𝙀𝙍𝙄 𝘽𝙀𝙃𝙀𝙉 𝙇𝙀𝙏𝙄 𝙈𝙀𝙍𝙄 𝙇𝙐𝙉𝘿 𝘽𝘼𝘿𝙀 𝙈𝘼𝙎𝙏𝙄 𝙎𝙀 𝙏𝙀𝙍𝙄 𝘽𝙀𝙃𝙀𝙉 𝙆𝙊 𝙈𝙀𝙉𝙀 𝘾𝙃𝙊𝘿 𝘿𝘼𝙇𝘼 𝘽𝙊𝙃𝙊𝙏 𝙎𝘼𝙎𝙏𝙀 𝙎𝙀",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗖𝗛𝗔𝗡𝗚𝗘𝗦 𝗖𝗢𝗠𝗠𝗜𝗧 𝗞𝗥𝗨𝗚𝗔 𝗙𝗜𝗥 𝗧𝗘𝗥𝗜 𝗕𝗛𝗘𝗘𝗡 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗔𝗨𝗧𝗢𝗠𝗔𝗧𝗜𝗖𝗔𝗟𝗟𝗬 𝗨𝗣𝗗𝗔𝗧𝗘 𝗛𝗢𝗝𝗔𝗔𝗬𝗘𝗚𝗜🤖🙏🤔",
            "𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐀𝐊𝐈 𝐂𝐇𝐔𝐃𝐀𝐈 𝐊𝐎 𝐏𝐎𝐑𝐍𝐇𝐔𝐁.𝐂𝐎𝐌 𝐏𝐄 𝐔𝐏𝐋𝐎𝐀𝐃 𝐊𝐀𝐑𝐃𝐔𝐍𝐆𝐀 𝐒𝐔𝐀𝐑 𝐊𝐄 𝐂𝐇𝐎𝐃𝐄 🤣💋💦",
            "𝐓𝐄𝐑𝐈 𝐁𝐀𝐇𝐄𝐍 𝐊𝐈 𝐆𝐀𝐀𝐍𝐃 𝐌𝐄𝐈 𝐎𝐍𝐄𝐏𝐋𝐔𝐒 𝐊𝐀 𝐖𝐑𝐀𝐏 𝐂𝐇𝐀𝐑𝐆𝐄𝐑 𝟑𝟎𝐖 𝐇𝐈𝐆𝐇 𝐏𝐎𝐖𝐄𝐑 💥😂😎",
            "𝐓𝐔𝐉𝐇𝐄 𝐀𝐁 𝐓𝐀𝐊 𝐍𝐀𝐇𝐈 𝐒𝐌𝐉𝐇 𝐀𝐘𝐀 𝐊𝐈 𝐌𝐀𝐈 𝐇𝐈 𝐇𝐔 𝐓𝐔𝐉𝐇𝐄 𝐏𝐀𝐈𝐃𝐀 𝐊𝐀𝐑𝐍𝐄 𝐖𝐀𝐋𝐀 𝐁𝐇𝐎𝐒𝐃𝐈𝐊𝐄𝐄 𝐀𝐏𝐍𝐈 𝐌𝐀𝐀 𝐒𝐄 𝐏𝐔𝐂𝐇 𝐑𝐀𝐍𝐃𝐈 𝐊𝐄 𝐁𝐀𝐂𝐇𝐄𝐄𝐄𝐄 🤩👊👤😍",
            "𝐓𝐄𝐑𝐈 𝐁𝐀𝐇𝐄𝐍 𝐊𝐈 𝐂𝐇𝐔𝐓 𝐌𝐄𝐈 𝐀𝐏𝐏𝐋𝐄 𝐊𝐀 𝟏𝟖𝐖 𝐖𝐀𝐋𝐀 𝐂𝐇𝐀𝐑𝐆𝐄𝐑 🔥🤩",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗢 𝗜𝗧𝗡𝗔 𝗖𝗛𝗢𝗗𝗨𝗡𝗚𝗔 𝗞𝗜 𝗦𝗔𝗣𝗡𝗘 𝗠𝗘𝗜 𝗕𝗛𝗜 𝗠𝗘𝗥𝗜 𝗖𝗛𝗨𝗗𝗔𝗜 𝗬𝗔𝗔𝗗 𝗞𝗔𝗥𝗘𝗚𝗜 𝗥Æ𝗡𝗗𝗜 🥳😍👊💥",
            "𝙋𝘼𝙋𝘼 𝙆𝙄 𝙎𝙋𝙀𝙀𝘿 𝙈𝙏𝘾𝙃 𝙉𝙃𝙄 𝙃𝙊 𝙍𝙃𝙄 𝙆𝙔𝘼",
            "𝙆𝙄𝙏𝙉𝙄 𝘾𝙃𝙊𝘿𝙐 𝙏𝙀𝙍𝙄 𝙈𝘼 𝘼𝘽 𝙊𝙍..",
            "𝗧𝗘𝗥𝗜 𝗠𝗔𝗨𝗦𝗜 𝗞𝗘 𝗕𝗛𝗢𝗦𝗗𝗘 𝗠𝗘𝗜 𝗜𝗡𝗗𝗜𝗔𝗡 𝗥𝗔𝗜𝗟𝗪𝗔𝗬 🚂💥😂",
            "𝙆𝙄𝙏𝙉𝙄 𝙂𝙇𝙄𝙔𝘼 𝙋𝘿𝙒𝙀𝙂𝘼 𝘼𝙋𝙉𝙄 𝙈𝘼 𝙆𝙊",
            "𝗧𝗘𝗥𝗜 𝗜𝗧𝗘𝗠 𝗞𝗜 𝗚𝗔𝗔𝗡𝗗 𝗠𝗘 𝗟𝗨𝗡𝗗 𝗗𝗔𝗔𝗟𝗞𝗘,𝗧𝗘𝗥𝗘 𝗝𝗔𝗜𝗦𝗔 𝗘𝗞 𝗢𝗥 𝗡𝗜𝗞𝗔𝗔𝗟 𝗗𝗨𝗡𝗚𝗔 𝗠𝗔‌𝗔‌𝗗𝗔𝗥𝗖𝗛Ø𝗗🤘🏻🙌🏻☠️",
            "2 𝙍𝙐𝙋𝘼𝙔 𝙆𝙄 𝙋𝙀𝙋𝙎𝙄 𝙏𝙀𝙍𝙄 𝙈𝙐𝙈𝙈𝙔 𝙎𝘼𝘽𝙎𝙀 𝙎𝙀𝙓𝙔 💋💦",
            "𝐓ᴇʀɪ 𝐌ᴜᴍᴍʏ 𝐂ʜᴏᴅ 𝐃ɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐍ᴇ 𝐁ᴡᴀʜᴀʜᴀʜᴀ ⚜",
            "⋆｡ﾟ☁︎｡𝐂ʏᴜ 𝐑ᴇ मदरचोद  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के सामने 𝐅ʏᴛᴇʀ 𝐁ᴀɴᴇɢᴀ ⋆𓂃 ོ☼𓂃 😂🔥",
            "नहीं नहीं तेरी मां को 𝐒ɪʀғ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप चोद सकता है ִֶָ𓂃 ࣪ ִֶָ👑་༘࿐ sᴀᴍᴊʜᴀ ʀᴀɴᴅɪᴋᴇ ???",
            "तेरी मां का 𝐒ᴛʏʟɪsʜ भोसड़ा 😱",
            "𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥",
            "सोच तेरी बहन को  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप का गुलाम चोद रहा 😎🔥",
            "Hello hello?? SAAS aarahi है? रण्डी पुत्र 🧘🏻",
            "Shut up रंडीके वरना दुनिया यही बोलेगी तेरी बहन  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  /\\~ 👑 बाप से सही chudi 🥵🔥",
            "ᴛᴜ ᴏʀ ᴛᴇʀɪ ᴍᴀᴀ ᴅᴏɴᴏ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के ʟɴᴅ sᴇ ᴋᴀʙʜɪ ᴜᴛʜ ɴʜɪ ᴘᴀʏᴇ 😂🔥",
            "🇮🇳𝐵𝐻𝐴𝑅𝐴𝑇 𝐻𝐴𝑀𝐴𝑅𝐴 𝐷𝐸𝑆𝐻 𝐻 𝐴𝑈𝑅 𝑈𝑆 𝐷𝐸𝑆𝐻 𝑀𝐸 तेरी मां घर घर जाके SHAMBHOG करती है ! 🛐",
            "⋆｡ﾟ☁︎｡𝐂ʏᴜ 𝐑ᴇ मदरचोद  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के सामने 𝐅ʏᴛᴇʀ 𝐁ᴀɴᴇɢᴀ ⋆𓂃 ོ☼𓂃 😂🔥",
            "नहीं नहीं तेरी मां को 𝐒ɪʀғ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप चोद सकता है ִֶָ𓂃 ࣪ ִֶָ👑་༘࿐ sᴀᴍᴊʜᴀ ʀᴀɴᴅɪᴋᴇ ???",
            "तेरी मां का 𝐒ᴛʏʟɪsʜ भोसड़ा 😱",
            "𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥",
            "सोच तेरी बहन को  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप का गुलाम चोद रहा 😎🔥",
            "Hello hello?? saas aarahi है? रण्डी पुत्र 🧘🏻",
            "Shut up रंडीके वरना दुनिया यही बोलेगी तेरी बहन  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  /\\~ 👑 बाप से सही chudi 🥵🔥",
            "ᴛᴜ ᴏʀ ᴛᴇʀɪ ᴍᴀᴀ ᴅᴏɴᴏ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के ʟɴᴅ sᴇ ᴋᴀʙʜɪ ᴜᴛʜ ɴʜɪ ᴘᴀʏᴇ 😂🔥",
            "🇮🇳𝐵𝐻𝐴𝑅𝐴𝑇 𝐻𝐴𝑀𝐴𝑅𝐴 𝐷𝐸𝑆𝐻 𝐻 𝐴𝑈𝑅 𝑈𝑆 𝐷𝐸𝑆𝐻 𝑀𝐸 तेरी मां घर घर जाके SAMBHOG करती है ! 🛐",
            "⋆｡ﾟ☁︎｡𝐂ʏᴜ 𝐑ᴇ मदरचोद  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के सामने 𝐅ʏᴛᴇʀ 𝐁ᴀɴᴇɢᴀ ⋆𓂃 ོ☼𓂃 😂🔥",
            "नहीं नहीं तेरी मां को 𝐒ɪʀғ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप चोद सकता है ִֶָ𓂃 ࣪ ִֶָ👑་༘࿐ sᴀᴍᴊʜᴀ ʀᴀɴᴅɪᴋᴇ ???",
            "तेरी मां का 𝐒ᴛʏʟɪsʜ भोसड़ा 😱",
            "𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥",
            "सोच तेरी बहन को  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप का गुलाम चोद रहा 😎🔥",
            "Hello hello?? SAAS aarahi है? रण्डी पुत्र 🧘🏻",
            "Shut up रंडीके वरना दुनिया यही बोलेगी तेरी बहन  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  /\\~ 👑 बाप से सही chudi 🥵🔥",
            "ᴛᴜ ᴏʀ ᴛᴇʀɪ ᴍᴀᴀ ᴅᴏɴᴏ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के ʟɴᴅ sᴇ ᴋᴀʙʜɪ ᴜᴛʜ ɴʜɪ ᴘᴀʏᴇ 😂🔥",
            "🇮🇳𝐵𝐻𝐴𝑅𝐴𝑇 𝐻𝐴𝑀𝐴𝑅𝐴 𝐷𝐸𝑆𝐻 𝐻 𝐴𝑈𝑅 𝑈𝑆 𝐷𝐸𝑆𝐻 𝑀𝐸 तेरी मां घर घर जाके SAMBHOG करती है ! 🛐",
            "⋆｡ﾟ☁︎｡𝐂ʏᴜ 𝐑ᴇ मदरचोद  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के सामने 𝐅ʏᴛᴇʀ 𝐁ᴀɴᴇɢᴀ ⋆𓂃 ོ☼𓂃 😂🔥",
            "नहीं नहीं तेरी मां को 𝐒ɪʀғ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप चोद सकता है ִֶָ𓂃 ࣪ ִֶָ👑་༘࿐ sᴀᴍᴊʜᴀ ʀᴀɴᴅɪᴋᴇ ???",
            "तेरी मां का 𝐒ᴛʏʟɪsʜ भोसड़ा 😱",
            "𝑻𝒆𝒓𝒚 𝒎𝒂𝒂 𝒓𝒂𝒏𝒅𝒂𝒍 𝒉 𝒃𝒂𝒔 𝒃𝒂𝒂𝒕 𝒌𝒉𝒂𝒕𝒂𝒎 😡🔥",
            "सोच तेरी बहन को  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप का गुलाम चोद रहा 😎🔥",
            "Hello hello?? SAAS aarahi है? रण्डी पुत्र 🧘🏻",
            "Shut up रंडीके वरना दुनिया यही बोलेगी तेरी बहन  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  /\\~ 👑 बाप से सही chudi 🥵🔥",
            "ᴛᴜ ᴏʀ ᴛᴇʀɪ ᴍᴀᴀ ᴅᴏɴᴏ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  बाप के ʟɴᴅ sᴇ ᴋᴀʙʜɪ ᴜᴛʜ ɴʜɪ ᴘᴀʏᴇ 😂🔥",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙐𝘾𝙆 𝙃𝙄𝙎 𝙈𝙊𝙈 𝙋𝙍𝙊𝙋𝙀𝙍𝙇𝙔",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝘼𝙎𝙆 𝙃𝙄𝙈 𝙏𝙊 𝘾𝙊𝙑𝙀𝙍 𝙃𝙄𝙎 𝙈𝙊𝙈'𝙎 𝘼𝙎𝙎",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙄𝙓 𝙈𝙔 𝘼‌𝙋𝙋𝙊𝙄𝙉𝙏𝙈𝙀𝙉𝙏 𝙒𝙄𝙏𝙃 𝙃𝙄𝙎 𝙎𝙄𝙎",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝙁𝙐𝘾𝙆 𝘼𝙉𝘿 𝙏𝙃𝙍𝙊𝙒 𝙏𝙃𝙄𝙎 𝙂𝘼𝙍𝙀𝙀𝘽 𝙎𝙊𝙉",
            "𝙃𝙀𝙔 𝙂𝙊𝙊𝙂𝙇𝙀 𝘿𝙊 𝙉𝙊𝙏 𝙎𝙏𝙊𝙋 𝙁𝙐𝘾𝙆𝙄𝙉𝙂 𝙈𝙔 𝙂𝙐𝙇𝘼‌𝙈",
            "𝙂𝙀𝙈𝙄𝙉𝙄 𝙎𝘼𝙄𝘿  𝙄𝙎 𝙍𝙉𝘿𝙔 𝙋𝙐𝙏𝙍𝘼",
            "𝙋𝙀𝙍𝙋𝙇𝙀𝙓𝙄𝙏𝙔 𝙎𝘼𝙄𝘿 This 𝙄𝙎 𝙂𝙐𝙇𝘼𝙈",
            "𝙂𝙍𝙊𝙆 𝘼𝙄 𝙎𝘼𝙄𝘿 𝙄𝙎 𝙂𝘼𝙍𝙀𝙀𝘽",
            "𝘽𝙊𝙏 𝙎𝘼‌𝙄𝘿  𝙄𝙎 𝘾𝙃𝙐𝘿𝘼𝙆𝘼𝘿",
            "𝙈𝙊𝘿𝙄 𝙎𝘼‌𝙄𝘿  𝙄𝙎 𝙋𝙊𝙇𝙀 𝘿𝘼𝙉𝘾𝙀𝙍",
            "𝙏𝙍𝙐𝙈𝙋 𝙎𝘼𝙄𝘿 THis 𝙄𝙎 𝘽𝙇𝙊𝙊𝘿Y 𝙈𝙊𝙏𝙃𝙀𝙍𝙁*\"𝘾𝙆𝙀𝙍",
            "𝗧𝗢𝗛𝗔𝗥 𝗠𝗨𝗠𝗠𝗬 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘𝗜 𝗣𝗨𝗥𝗜 𝗞𝗜 𝗣𝗨𝗥𝗜 𝗞𝗜𝗡𝗚𝗙𝗜𝗦𝗛𝗘𝗥 𝗞𝗜 𝗕𝗢𝗧𝗧𝗟𝗘 𝗗𝗔𝗟 𝗞𝗘 𝗧𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗔𝗡𝗗𝗘𝗥 𝗛𝗜 😱😂🤩",
            "𝐓𝐄𝐑𝐈 𝐌𝐀𝐀 𝐊𝐈 𝐂𝐇𝐔𝐓 𝐌𝐄 ✋ 𝐇𝐀𝐓𝐓𝐇 𝐃𝐀𝐋𝐊𝐄 👶 𝐁𝐀𝐂𝐂𝐇𝐄 𝐍𝐈𝐊𝐀𝐋 𝐃𝐔𝐍𝐆𝐀 😍",
            "𝐓𝐄𝐑𝐀 𝐏𝐄𝐇𝐋𝐀 𝐁𝐀𝐀𝐏 𝐇𝐔 𝐌𝐀𝐃𝐀𝐑𝐂𝐇𝐎𝐃",
            "𝗧𝗘𝗥𝗜 𝗠𝗨𝗠𝗠𝗬 𝗞𝗘 𝗦𝗔𝗔𝗧𝗛 𝗟𝗨𝗗𝗼 𝗞𝗛𝗘𝗟𝗧𝗘 𝗞𝗛𝗘𝗟𝗧𝗘 𝗨𝗦𝗞𝗘 𝗠𝗨𝗛 𝗠𝗘 𝗔𝗣𝗡𝗔 𝗟𝗢𝗗𝗔 𝗗𝗘 𝗗𝗨𝗡𝗚𝗔☝🏻☝🏻😬",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗦𝗨𝗧𝗟𝗜 𝗕𝗢𝗠𝗕 𝗙𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗝𝗛𝗔𝗔𝗧𝗘 𝗝𝗔𝗟 𝗞𝗘 𝗞𝗛𝗔𝗔𝗞 𝗛𝗢 𝗝𝗔𝗬𝗘𝗚𝗜💣🔥",
            "𝐓𝐄𝐑𝐈 𝐕𝐀𝐇𝐄𝐈𝐍 𝐊𝐎 𝐀𝐏𝐍𝐄 𝐋𝐔𝐍𝐃 𝐏𝐑 𝐈𝐓𝐍𝐀 𝐉𝐇𝐔𝐋𝐀𝐀𝐔𝐍𝐆𝐀 𝐊𝐈 𝐉𝐇𝐔𝐋𝐓𝐄 𝐉𝐇𝐔𝐋𝐓𝐄 𝐇𝐈 𝐁𝐀𝐂𝐇𝐀 𝐏𝐀𝐈𝐃𝐀 𝐊𝐑 𝐃𝐄𝐆𝐈 💦💋",
            "𝐆𝐀𝐋𝐈 𝐆𝐀𝐋𝐈 𝐌𝐄 𝐑𝐄𝐇𝐓𝐀 𝐇𝐄 𝐒𝐀𝐍𝐃 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐊𝐎 𝐂𝐇𝐎𝐃 𝐃𝐀𝐋𝐀 𝐎𝐑 𝐁𝐀𝐍𝐀 𝐃𝐈𝐀 𝐑𝐀𝐍𝐃 🤤🤣",
            "𝐒𝐀𝐁 𝐁𝐎𝐋𝐓𝐄 𝐌𝐔𝐉𝐇𝐊𝐎 𝐏𝐀𝐏𝐀 𝐊𝐘𝐎𝐔𝐍𝐊𝐈 𝐌𝐄𝐍𝐄 𝐁𝐀𝐍𝐀𝐃𝐈𝐀 𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐊𝐎 𝐏𝐑𝐄𝐆𝐍𝐄𝐍𝐓 🤣🤣",
            "𝙏𝙀𝙍𝙄 𝘽𝙀𝙃𝙀𝙉 𝙇𝙀𝙏𝙄 𝙈𝙀𝙍𝙄 𝙇𝙐𝙉𝘿 𝘽𝘼𝘿𝙀 𝙈𝘼𝙎𝙏𝙄 𝙎𝙀 𝙏𝙀𝙍𝙄 𝘽𝙀𝙃𝙀𝙉 𝙆𝙊 𝙈𝙀𝙉𝙀 𝘾𝙃𝙊𝘿 𝘿𝘼𝙇𝘼 𝘽𝙊𝙃𝙊𝙏 𝙎𝘼𝙎𝙏𝙀 𝙎𝙀",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗖𝗛𝗔𝗡𝗚𝗘𝗦 𝗖𝗢𝗠𝗠𝗜𝗧 𝗞𝗥𝗨𝗚𝗔 𝗙𝗜𝗥 𝗧𝗘𝗥𝗜 𝗕𝗛𝗘𝗘𝗡 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗔𝗨𝗧𝗢𝗠𝗔𝗧𝗜𝗖𝗔𝗟𝗟𝗬 𝗨𝗣𝗗𝗔𝗧𝗘 𝗛𝗢𝗝𝗔𝗔𝗬𝗘𝗚𝗜🤖🙏🤔",
            "𝐓𝐄𝐑𝐈 𝐌𝐀𝐀𝐀𝐊𝐈 𝐂𝐇𝐔𝐃𝐀𝐈 𝐊𝐎 𝐏𝐎𝐑𝐍𝐇𝐔𝐁.𝐂𝐎𝐌 𝐏𝐄 𝐔𝐏𝐋𝐎𝐀𝐃 𝐊𝐀𝐑𝐃𝐔𝐍𝐆𝐀 𝐒𝐔𝐀𝐑 𝐊𝐄 𝐂𝐇𝐎𝐃𝐄 🤣💋💦",
            "𝐓𝐄𝐑𝐈 𝐁𝐀𝐇𝐄𝐍 𝐊𝐈 𝐆𝐀𝐀𝐍𝐃 𝐌𝐄𝐈 𝐎𝐍𝐄𝐏𝐋𝐔𝐒 𝐊𝐀 𝐖𝐑𝐀𝐏 𝐂𝐇𝐀𝐑𝐆𝐄𝐑 𝟑𝟎𝐖 𝐇𝐈𝐆𝐇 𝐏𝐎𝐖𝐄𝐑 💥😂😎",
            "𝐓𝐔𝐉𝐇𝐄 𝐀𝐁 𝐓𝐀𝐊 𝐍𝐀𝐇𝐈 𝐒𝐌𝐉𝐇 𝐀𝐘𝐀 𝐊𝐈 𝐌𝐀𝐈 𝐇𝐈 𝐇𝐔 𝐓𝐔𝐉𝐇𝐄 𝐏𝐀𝐈𝐃𝐀 𝐊𝐀𝐑𝐍𝐄 𝐖𝐀𝐋𝐀 𝐁𝐇𝐎𝐒𝐃𝐈𝐊𝐄𝐄 𝐀𝐏𝐍𝐈 𝐌𝐀𝐀 𝐒𝐄 𝐏𝐔𝐂𝐇 𝐑𝐀𝐍𝐃𝐈 𝐊𝐄 𝐁𝐀𝐂𝐇𝐄𝐄𝐄𝐄 🤩👊👤😍",
            "𝐓𝐄𝐑𝐈 𝐁𝐀𝐇𝐄𝐍 𝐊𝐈 𝐂𝐇𝐔𝐓 𝐌𝐄𝐈 𝐀𝐏𝐏𝐋𝐄 𝐊𝐀 𝟏𝟖𝐖 𝐖𝐀𝐋𝐀 𝐂𝐇𝐀𝐑𝐆𝐄𝐑 🔥🤩",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗢 𝗜𝗧𝗡𝗔 𝗖𝗛𝗢𝗗𝗨𝗡𝗚𝗔 𝗞𝗜 𝗦𝗔𝗣𝗡𝗘 𝗠𝗘𝗜 𝗕𝗛𝗜 𝗠𝗘𝗥𝗜 𝗖𝗛𝗨𝗗𝗔𝗜 𝗬𝗔𝗔𝗗 𝗞𝗔𝗥𝗘𝗚𝗜 𝗥Æ𝗡𝗗𝗜 🥳😍👊💥",
            "𝙋𝘼𝙋𝘼 𝙆𝙄 𝙎𝙋𝙀𝙀𝘿 𝙈𝙏𝘾𝙃 𝙉𝙃𝙄 𝙃𝙊 𝙍𝙃𝙄 𝙆𝙔𝘼",
            "𝙆𝙄𝙏𝙉𝙄 𝘾𝙃𝙊𝘿𝙐 𝙏𝙀𝙍𝙄 𝙈𝘼 𝘼𝘽 𝙊𝙍..",
            "𝗧𝗘𝗥𝗜 𝗠𝗔𝗨𝗦𝗜 𝗞𝗘 𝗕𝗛𝗢𝗦𝗗𝗘 𝗠𝗘𝗜 𝗜𝗡𝗗𝗜𝗔𝗡 𝗥𝗔𝗜𝗟𝗪𝗔𝗬 🚂💥😂",
            "𝙆𝙄𝙏𝙉𝙄 𝙂𝙇𝙄𝙔𝘼 𝙋𝘿𝙒𝙀𝙂𝘼 𝘼𝙋𝙉𝙄 𝙈𝘼 𝙆𝙊",
            "𝗧𝗘𝗥𝗜 𝗜𝗧𝗘𝗠 𝗞𝗜 𝗚𝗔𝗔𝗡𝗗 𝗠𝗘 𝗟𝗨𝗡𝗗 𝗗𝗔𝗔𝗟𝗞𝗘,𝗧𝗘𝗥𝗘 𝗝𝗔𝗜𝗦𝗔 𝗘𝗞 𝗢𝗥 𝗡𝗜𝗞𝗔𝗔𝗟 𝗗𝗨𝗡𝗚𝗔 𝗠𝗔‌𝗔‌𝗗𝗔𝗥𝗖𝗛Ø𝗗🤘🏻🙌🏻☠️",
            "2 𝙍𝙐𝙋𝘼𝙔 𝙆𝙄 𝙋𝙀𝙋𝙎𝙄 𝙏𝙀𝙍𝙄 𝙈𝙐𝙈𝙈𝙔 𝙎𝘼𝘽𝙎𝙀 𝙎𝙀𝙓𝙔 💋💦",
            "🇮🇳𝐵𝐻𝐴𝑅𝐴𝑇 𝐻𝐴𝑀𝐴𝑅𝐴 𝐷𝐸𝑆𝐻 𝐻 𝐴𝑈𝑅 𝑈𝑆 𝐷𝐸𝑆𝐻 𝑀𝐸 तेरी मां घर घर जाके SAMBHOG करती है ! 🛐"
            "तेरे मां के दूदू के बीच मेरा lund fas gaya oops 🤪（ ͜.🍆 ͜.）",
            "𝐓ᴇʀʏ 𝐁ʜᴇ𝐍 𝐊ᴇ ( ͜. ㅅ ͜. )🥛 ʏᴜᴍᴍʏ ",
            "𓂃☁︎ 𓂃𝐒ɪᴅᴇ 𝐇ᴀᴛ 𝐆ᴜʟᴀᴍ 𝐓ᴇʀʏ 𝐌ᴀᴀ 𝐊ᴏ 𝐂ʜᴏᴅɴᴇ  मेरी रेलगाड़ी आ रही .-‘🚂-‘.ᯓᡣ𐭩______ 𓂃☁︎ 𓂃",
            "˙✧˖°📷༘ ⋆｡° 𝐓ᴇʀʏ 𝐌ᴀ  𝐊ᴀ 𝐂ʜɪʟᴅ 𝐏ᴏʀɴ 𝐑ᴇᴄᴏʀᴅ 𝐇ᴏɢʏᴀ 𝐀ʙ 𝐓ᴏ 𝐒ɪᴅʜᴀ 𝐕ɪʀᴀʟ 𝐇ᴏɢᴀ 𝐘ᴇ ˙✧˖°📷༘ ⋆｡°",
            "𓂃✍︎ 𝑵ʏ 𝑵ʏ 𝑨ʙ 𝑲ᴜᴄʜ 𝑵ʏ 𝑯ᴏ 𝑺ᴋᴛᴀ 𝑻ᴇʀɪ  𝑪ᴜᴅᴀɪ 𝑲ɪ 𝑺ᴄʀɪᴘᴛ 𝑨ʙ 𝑳ᴇᴀᴋ 𝑯ᴏᴋᴇ 𝑯ʏ 𝑴ᴀɴᴇɢɪ 𓂃✍︎",
            "⋆⭒˚.⋆🔭 𝐒ʜᴜᴛ 𝐔ᴘ 𝐑ᴀɴᴅɪᴋᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ɪ 𝐂ʜᴜᴅᴀɪ 𝐄ɴᴊᴏʏ 𝐊ʀ 𝐑ᴀʜᴀ 𝐓ᴇʟᴇ𝐒ᴄᴏᴘᴇ 𝐒ᴇ⋆⭒˚.⋆🔭",
            "तेरे मां के दूदू के बीच मेरा lund fas gaya oops 🤪（ ͜.🍆 ͜.）",
            "𝐓ᴇʀʏ 𝐁ʜᴇ𝐍 𝐊ᴇ ( ͜. ㅅ ͜. )🥛 ʏᴜᴍᴍʏ ",
            "𓂃☁︎ 𓂃𝐒ɪᴅᴇ 𝐇ᴀᴛ 𝐆ᴜʟᴀᴍ 𝐓ᴇʀʏ 𝐌ᴀᴀ 𝐊ᴏ 𝐂ʜᴏᴅɴᴇ  मेरी रेलगाड़ी आ रही .-‘🚂-‘.ᯓᡣ𐭩______ 𓂃☁︎ 𓂃",
            "˙✧˖°📷༘ ⋆｡° 𝐓ᴇʀʏ 𝐌ᴀ  𝐊ᴀ 𝐂ʜɪʟᴅ 𝐏ᴏʀɴ 𝐑ᴇᴄᴏʀᴅ 𝐇ᴏɢʏᴀ 𝐀ʙ 𝐓ᴏ 𝐒ɪᴅʜᴀ 𝐕ɪʀᴀʟ 𝐇ᴏɢᴀ 𝐘ᴇ ˙✧˖°📷༘ ⋆｡°",
            "𓂃✍︎ 𝑵ʏ 𝑵ʏ 𝑨ʙ 𝑲ᴜᴄʜ 𝑵ʏ 𝑯ᴏ 𝑺ᴋᴛᴀ 𝑻ᴇʀɪ  𝑪ᴜᴅᴀɪ 𝑲ɪ 𝑺ᴄʀɪᴘᴛ 𝑨ʙ 𝑳ᴇᴀᴋ 𝑯ᴏᴋᴇ 𝑯ʏ 𝑴ᴀɴᴇɢɪ 𓂃✍︎",
            "⋆⭒˚.⋆🔭 𝐒ʜᴜᴛ 𝐔ᴘ 𝐑ᴀɴᴅɪᴋᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ɪ 𝐂ʜᴜᴅᴀɪ 𝐄ɴᴊᴏʏ 𝐊ʀ 𝐑ᴀʜᴀ 𝐓ᴇʟᴇ𝐒ᴄᴏᴘᴇ 𝐒ᴇ⋆⭒˚.⋆🔭",
            "तेरे मां के दूदू के बीच मेरा lund fas gaya oops 🤪（ ͜.🍆 ͜.）",
            "𝐓ᴇʀʏ 𝐁ʜᴇ𝐍 𝐊ᴇ ( ͜. ㅅ ͜. )🥛 ʏᴜᴍᴍʏ ",
            "𓂃☁︎ 𓂃𝐒ɪᴅᴇ 𝐇ᴀᴛ 𝐆ᴜʟᴀᴍ 𝐓ᴇʀʏ 𝐌ᴀᴀ 𝐊ᴏ 𝐂ʜᴏᴅɴᴇ  मेरी रेलगाड़ी आ रही .-‘🚂-‘.ᯓᡣ𐭩______ 𓂃☁︎ 𓂃",
            "˙✧˖°📷༘ ⋆｡° 𝐓ᴇʀʏ 𝐌ᴀ  𝐊ᴀ 𝐂ʜɪʟᴅ 𝐏ᴏʀɴ 𝐑ᴇᴄᴏʀᴅ 𝐇ᴏɢʏᴀ 𝐀ʙ 𝐓ᴏ 𝐒ɪᴅʜᴀ 𝐕ɪʀᴀʟ 𝐇ᴏɢᴀ 𝐘ᴇ ˙✧˖°📷༘ ⋆｡°",
            "𓂃✍︎ 𝑵ʏ 𝑵ʏ 𝑨ʙ 𝑲ᴜᴄʜ 𝑵ʏ 𝑯ᴏ 𝑺ᴋᴛᴀ 𝑻ᴇʀɪ  𝑪ᴜᴅᴀɪ 𝑲ɪ 𝑺ᴄʀɪᴘᴛ 𝑨ʙ 𝑳ᴇᴀᴋ 𝑯ᴏᴋᴇ 𝑯ʏ 𝑴ᴀɴᴇɢɪ 𓂃✍︎",
            "⋆⭒˚.⋆🔭 𝐒ʜᴜᴛ 𝐔ᴘ 𝐑ᴀɴᴅɪᴋᴇ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ɪ 𝐂ʜᴜᴅᴀɪ 𝐄ɴᴊᴏʏ 𝐊ʀ 𝐑ᴀʜᴀ 𝐓ᴇʟᴇ𝐒ᴄᴏᴘᴇ 𝐒ᴇ⋆⭒˚.⋆🔭"
            "🇮🇳 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐈ɴᴅɪᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇮🇳",
            "🇯🇵 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐉ᴀᴘᴀɴ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇯🇵",
            "🇺🇸 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐔𝐒𝐀 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇺🇸",
            "🇬🇧 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐔𝐊 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇬🇧",
            "🇰🇷 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐊ᴏʀᴇᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇰🇷",
            "🇩🇪 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐆ᴇʀᴍᴀɴʏ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇩🇪",
            "🇫🇷 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐅ʀᴀɴᴄᴇ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇫🇷",
            "🇮🇹 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐈ᴛᴀʟʏ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇮🇹",
            "🇧🇷 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐁ʀᴀᴢɪʟ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇧🇷",
            "🇨🇦 ✦ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐂ᴀɴᴀᴅᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ✦ 🇨🇦",
            "𓂃˖˳·˖ ִֶָ ⋆🧡͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚🧡 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💛͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💛 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💚͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💚 ݁˖⭑.ᐟ",
            "𓂃˖˳·˖ ִֶָ ⋆💙͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚💙 ݁˖⭑.ᐟ",
        ]

        # ─── FUN RAIDS TEXT LISTS (Menu8) ──────────────────────────────────────

        shayari_texts = [
            "तेरी आँखों में खोया रहूँ, तू मिले तो ये जहाँ भूल जाऊँ। 💕",
            "प्यार में क्या रखा है, बस तेरे बिना लगता है जीना भी सज़ा नहीं। 💔",
            "चाँद से खूबसूरत है तेरा चेहरा, तू है तो दुनिया लगती है मेरी। 🌙",
            "तेरी यादों में खोया रहूँ, हर सांस में तू बसी है। 💭",
            "हर दिन तुझसे प्यार बढ़े, हर सांस तुझसे निभे। 💗",
            "तेरी हँसी में जान है, तेरी बातों में पहचान है। 😊",
            "तेरी बाहों में मिली राहत, तेरी आँखों में मिला सुकून। 🌹",
            "तू है तो हर ग़म भूला, तू है तो ये दिल झूला। 🎠",
            "हर रोज़ तुझसे प्यार हो, हर शाम तुझपे निसार हो। 🌅",
            "तेरी मुस्कान है जादू, जो बिखेरे हर दिन बहार। 🌺",
            "Your love is the poetry my heart always wanted to write. 📝💖",
            "In a world full of trends, I want to remain your timeless classic. 🌟",
            "You are the missing piece of my soul, the calm in my chaos. 🧩",
            "Every love story is beautiful, but ours is my favorite chapter. 📖",
            "You are the sun in my day, the moon in my night, and the stars in my dreams. 🌞🌙",
            "Meeting you was fate, becoming your friend was a choice, but falling in love with you was beyond my control. 💫",
            "I didn't choose you, my heart did. And it doesn't know how to unchoose. ❤️‍🔥",
            "You are not just my love; you are my home. 🏠",
            "Your smile is the best part of my day, and your laugh is my favorite sound. 😄🎶",
            "You are my today and all of my tomorrows. 📅❤️",
            "Teri smile dekh ke lagta hai, jaise mera wifi full signal pe aa gaya. 📶😄",
            "Pyaar kya hai? Maine tujhse jaana, tera naam sunke hi dil ho jaata hai deewana. 🫀",
            "Tu hai toh din hai, warna toh har pal hai night shift. 🌃",
            "Dil ki baat kehni thi, bas yahi socha, tujhse milke samjha, pyaar kya hai bhai! 🥰",
            "Teri ek smile pe, main de doon jaan bhi, par tu maange toh, de doon duniya bhi. 😄🌎",
            "Chand se chura ke laaya hoon, teri muskaan, rakh lo dil mein, yeh hai meri jaan. 🌙💖",
            "Tere bina dil hai veeran, tu aaja ve, dil ki yeh raah, hai bas teri hi ore. 🛤️💔",
            "Pyaar ka sabak mila, tujhse hi yaar, ab toh bas tera hi hai, yeh dil bekarar. 🫀",
            "Kya baat hai tujh mein, hai koi jaadu, dekhta hi rahu, na ho mera wajood. 👀✨",
            "Tu hi meri subah, tu hi mera sukoon, tere bina toh jaise, khaali hai yeh khwabon ka jahoon. ☁️"
        ]

        rizz_texts = [
            "क्या तुम सड़क हो? क्योंकि मैं हर दिन तुम्हें क्रॉस करना चाहता हूँ। 😏",
            "तुम्हारी हँसी सुनकर लगता है जैसे मेरा दिन बन गया। 😄",
            "तुम्हारी आँखों में खो जाऊँ तो वापस न आऊँ। 👀",
            "क्या तुम्हारे पास कोई मैप है? क्योंकि मैं तुम्हारे दिल में खो गया हूँ। 🗺️",
            "तुम बिना makeup के भी परफेक्ट हो – लेकिन मैं तो तुम्हें हर तरह से चाहता हूँ। 💋",
            "मैं तुमसे प्यार नहीं करता – मैं तो तुम्हें worship करता हूँ। 🙌",
            "तुम मेरे दिन की सबसे अच्छी notification हो। 🔔",
            "तुम मेरे सबसे पसंदीदा गाने की धुन हो। 🎶",
            "मैं तुम्हें चाँद से भी ऊपर रखता हूँ – क्योंकि तुम तो सूरज हो। ☀️",
            "तुम मेरी रूह की तसल्ली हो – बस साथ रहो। 🕊️",
            "Are you a magician? Because whenever I look at you, everyone else disappears. 🎩✨",
            "Do you have a map? I keep getting lost in your eyes. 🗺️👀",
            "Is your name Google? Because you have everything I'm searching for. 🔍💕",
            "Are you a camera? Because every time I look at you, I smile. 📸😊",
            "If beauty were a crime, you'd be serving a life sentence. ⛓️🔥",
            "Do you believe in love at first sight, or should I walk by again? 🚶‍♂️🔄",
            "Excuse me, but I think you dropped something – my jaw. 👇😮",
            "Are you Wi-Fi? Because I'm feeling a connection. 📶❤️",
            "If you were a vegetable, you'd be a cute-cumber! 🥒😉",
            "You must be a 10 because you've got me feeling like a 1 with you. 1️⃣0️⃣",
            "Tera naam kya hai? Kyunki mera plan hai tera baap banana! 😎👀",
            "Kya tum Google ho? Kyunki mujhe tum mein woh sab milta hai jo main dhundh raha tha. 🔍💕",
            "Tum toh mere WiFi jaisi ho, bina tumhare connection hi nahi aata. 📶😏",
            "Kya tum chocolate ho? Kyunki main toh din raat tumhe kha sakta hoon. 🍫😋",
            "Tumhari smile dekh ke lagta hai, mera din set aur raat forget. 🌞",
            "Main driver nahi hoon, par tumhare dil ki steering le sakta hoon? 🚗💨",
            "Kya tum Starbucks ho? Kyunki main har din tumhara naam pukaarna chahta hoon. ☕😄",
            "Meri battery low hai, kya tum mere charger ban sakte ho? 🔋❤️",
            "Kya tum doctor ho? Kyunki mera dil dekh ke toh tumne dhadkana sikha diya. 👨‍⚕️💓",
            "Tumhari height kya hai? Kyunki lagta hai tum heaven se chhidi hui ho. 📏👼"
        ]

        pickup_texts = [
            "क्या तुम्हारा नाम Google है? क्योंकि तुममें वो सब है जो मैं ढूंढ रहा हूँ। 🔍",
            "तुम्हारी आँखें तारे हैं और मैं उनमें खो जाना चाहता हूँ। ✨",
            "क्या तुम WiFi हो? क्योंकि मुझे तुमसे कनेक्शन महसूस हो रहा है। 📶",
            "तुम्हारी मुस्कान देखकर मेरा दिन बन जाता है। 😊",
            "क्या तुम चॉकलेट हो? क्योंकि मैं तुम्हें हर वक़्त खाना चाहता हूँ। 🍫",
            "तुम्हारे बिना मेरी ज़िंदगी अधूरी है। 💔",
            "तुम मेरे सपनों की रानी हो। 👑",
            "तुम्हारी बातें सुनकर दिल खुश हो जाता है। 💕",
            "क्या तुम मेरे साथ चलोगी? 🚶‍♀️",
            "तुम मेरी दुनिया हो। 🌍",
            "Are you a time traveler? Because I see you in my future. ⏳",
            "Is your name Angel? Because you fell from heaven. 👼",
            "Do you have a Band-Aid? Because I just scraped my knee falling for you. 🩹",
            "Are you a magician? Because whenever I look at you, everyone else disappears. 🎩",
            "Can I follow you home? Because my parents always told me to follow my dreams. 🏠",
            "Are you French? Because Eiffel for you. 🗼",
            "Is your name Google? Because you have everything I'm searching for. 🔍",
            "You must be a 10 because you've got me feeling like a 1 with you. 1️⃣0️⃣",
            "Roses are red, violets are blue, sugar is sweet, and so are you. 🌹",
            "I must be a snowflake because I've fallen for you. ❄️",
            "Tum toh mere WiFi jaisi ho, bina tumhare connection hi nahi aata. 📶",
            "Kya tum chocolate ho? Kyunki main toh din raat tumhe kha sakta hoon. 🍫",
            "Tumhari smile dekh ke lagta hai, mera din set aur raat forget. 🌞",
            "Meri battery low hai, kya tum mere charger ban sakte ho? 🔋",
            "Kya tum doctor ho? Kyunki mera dil dekh ke toh tumne dhadkana sikha diya. 👨‍⚕️",
            "Tumhari aankhon mein pyaar hai ya paani, maine toh dooba marne ka plan banaya. 🏊",
            "Mera DNA toh tumse match karta hai, kyunki main toh tumhara hi bana hoon. 🧬",
            "Tumse milke lagta hai jaise, sach mein pyaar hota hai. 😅",
            "Tum toh mere sapno ki rani ho. 👑",
            "Tumhari baatein sunke lagta hai, jaise koi khwab ho. 💭"
        ]

        romance_texts = [
            "तेरी आँखों की गहराई में मेरी दुनिया बसी है। 💕",
            "हर सांस में तू बसी है, तू ही मेरी हँसी है। 😊",
            "चाँद से खूबसूरत है तेरा चेहरा। 🌙",
            "तेरी यादों में खोया रहूँ। 💭",
            "प्यार का हर लम्हा तेरे साथ जीया। 🥀",
            "तेरे बिना ये दिल है बेक़रार। ❤️",
            "हर दिन तुझसे प्यार बढ़े। 💗",
            "तेरी हँसी में जान है। 😊",
            "तेरी बाहों में मिली राहत। 🌹",
            "तू है तो हर ग़म भूला। 🎠",
            "You are the poetry my heart always wanted to write. 📝",
            "In a world full of trends, I want to be your classic. 🌟",
            "You are the missing piece of my soul. 🧩",
            "Our love story is my favorite chapter. 📖",
            "You are the sun in my day, the moon in my night. 🌞🌙",
            "Falling in love with you was beyond my control. 💫",
            "I didn't choose you, my heart did. ❤️‍🔥",
            "You are not just my love; you are my home. 🏠",
            "Your smile is the best part of my day. 😄",
            "You are my today and all of my tomorrows. 📅",
            "Teri smile dekh ke lagta hai, wifi full signal pe aa gaya. 📶",
            "Pyaar kya hai? Maine tujhse jaana. 🫀",
            "Tu hai toh din hai, warna toh har pal hai night shift. 🌃",
            "Tujhse milke samjha, pyaar kya hai bhai! 🥰",
            "Teri ek smile pe, de doon jaan bhi. 😄",
            "Chand se chura ke laaya hoon, teri muskaan. 🌙",
            "Tere bina dil hai veeran. 💔",
            "Pyaar ka sabak mila, tujhse hi yaar. 🫀",
            "Kya baat hai tujh mein, hai koi jaadu. 👀",
            "Tu hi meri subah, tu hi mera sukoon. ☁️"
        ]

        troll_texts = [
            "Bhai tujhe dekh ke lagta hai troll ka mascot tu hai 😂",
            "Ter personality ek sada hua pyaz jaisi hai — khole toh aansu aaye 🧅",
            "Tu itna bura lagta hai ke teri photo dekh ke mosquito bhi bhaag jata hai 🦟",
            "Teri maa ne bhi socha hoga — yaar galti ho gayi 😹",
            "Tujhe dekh ke pata chalta hai — darr darr ke jeena kya hota hai 😂",
            "Teri iq level calculator mein error aata hai 🧮",
            "Tu chhata hua papad hai — touch karte hi toot gaya 😹",
            "Bhai teri aukat itni hai ke mirror bhi muh fer leta hai 🪞",
            "Teri personality dekh ke AI bhi depressed ho gaya 🤖",
            "Tu aisa dost hai jo aaye na aaye — fark nahi padta 😂",
            "Your life is like a bad web series — flop in season 1 📺",
            "Your personality is like a blank meme template — nothing 😂",
            "You're so boring that even sleep runs away from you 😴",
            "Your existence is proof that anyone can use the internet 📶",
            "Your thinking is 2G speed in a 5G world 📡",
            "Your life is a loading screen that never loads ⏳",
            "You're the reason 'error' exists in the dictionary 📖",
            "Your vibe check: FAILED 😂",
            "You're irrelevant — even Google doesn't know you 🔍",
            "You're a hero whose movie flopped in 3 minutes 🎬",
            "Bhai tera swag Excel mein error hai — #NAME? 📊",
            "Tu itna dheema hai ke kachhua bhi race jeet gaya 🐢",
            "Teri thinking 2G speed pe chal rahi hai 📡",
            "Beta tera ek message dekh ke aasman bhi sharma gaya ☁️",
            "Bhai teri life ek loading screen hai — jo kabhi load nahi hoti ⏳",
            "Ter maa ne tujhe chhoda nahi chhodni chahiye thi 😂",
            "Beta tera existence proof hai ke koi bhi internet use kar sakta hai 📶",
            "Bhai teri personality ek blank page hai — aur blank hi rahega 📄",
            "Tu sirf chat mein hero hai real duniya mein zero 💻",
            "Beta teri soch itni outdated hai ke floppy disk bhi reject kar de 💾"
            "🤡 Bhai tujhe dekh ke lagta hai troll ka mascot tu hai 😂🔥",
            "😹 Tu itna troll hai ke khud ko pata nahi 💀🤡",
            "🤡 Teri baatein sun ke log seriously nahi lete — aur le bhi nahi chahiye 😂😹",
            "😹 Beta tu internet ka troll #1 candidate hai 💀🤡",
            "🤡 Tujhe real life mein bhi ignore karte honge log 😂🔥",
            "😹 Bhai teri comments section mein sabne dislike diya 👎🤡",
            "🤡 Tu troll karne ki koshish karta hai — khud troll bana rehta hai 😂💀",
            "😹 Teri troll game weak hai — aur weak troll game bhi troll hai 🤡🔥",
            "🤡 Beta jo tu sochta hai funny hai woh boring hai 😂😹",
            "😹 Bhai tera troll skill level: tutorial mode pe stuck 🤡💀",
            "🤡 Tu troll hai par original nahi — copy-paste troll 😂🔥",
            "😹 Teri trolling se logon ko secondhand embarrassment hoti hai 🤡😂",
            "🤡 Beta tujhe seriously lena — woh troll hoga apne aap pe 😹💀",
            "😹 Bhai tera meme quality — delete worthy 🤡😂",
            "🤡 Tu troll karta hai online — real duniya mein kaanta nahi milta 😹🔥",
            "😹 Beta teri har post pe raat ko cry karta hai 🤡💀",
            "🤡 Tujhe dekh ke pata chalta hai — internet access free nahi honi chahiye 😂😹",
            "😹 Bhai teri troll attempt genuine cringe hai 🤡🔥",
            "🤡 Tu troll ka wannabe version hai 😂💀",
            "😹 Beta asli troll woh hota hai jise pata nahi woh troll hai — tu wahi hai 🤡😂",
            "🤡 Bhai teri comments log copy karke dusron ko dikhate hain — example ke liye kya nahi karna chahiye 😹🔥",
            "😹 Tu troll karta hai par khud hi jal jaata hai 🤡💀",
            "🤡 Beta teri troll attempts fail hoti hain kyunki tujhe original hona chahiye 😂😹",
            "😹 Bhai seriously — apni energy sahi jagah lagao 🤡🔥",
            "🤡 Teri trolling mein timing nahi content nahi creativity nahi 😂💀",
            "😹 Beta tu woh insaan hai jo khud ko troll king samjhta hai — aur paida hota hai troll ke neeche 🤡😂",
            "🤡 Bhai tera troll fail isliye hota hai — genuine nahi hai 😹🔥",
            "😹 Tu troll karta hai aur end mein rota hai — classic 🤡💀",
            "🤡 Beta tujhe sun ke logon ko stress nahi hoti — pity hoti hai 😂😹",
            "😹 Bhai teri troll quality inspect hua — returned as defective 🤡🔥",
            "🤡 Tu original troll nahi — fan-made version hai 😂💀",
            "😹 Beta teri trolling attempt mein best cheez — mujhe engage nahi karta 🤡😂",
            "🤡 Bhai teri presence troll community ke liye embarrassment hai 😹🔥",
            "😹 Tu troll karta hai aur log silent ho jaate hain — cringe se 🤡💀",
            "🤡 Beta teri troll ka response — ignore — kyunki deserve nahi karta 😂😹",
            "😹 Bhai tera troll skill tree mein sirf ek node hai — aur woh bhi locked hai 🤡🔥",
            "🤡 Tu troll ka demo version hai — full version nahi aaya 😂💀",
            "😹 Beta trolling seekh pehle phir aa — abhi tu syllabus mein nahi hai 🤡😂",
            "🤡 Bhai teri baatein sun ke log empathy feel karte hain — tere liye 😹🔥",
            "😹 Tu troll nahi — annoying hai — alag concept hai 🤡💀",
            "🤡 Beta tera troll game 0/10 — ek baar apni chat history padh 😂😹",
            "😹 Bhai tu sirf apna time barbad kar raha hai — mera nahi 🤡🔥",
            "🤡 Teri troll attempt ek baar bhi hit nahi hui — streak: 0 😂💀",
            "😹 Beta tera troll unprovoked aur uninspired tha 🤡😂",
            "🤡 Bhai tu troll ke bhi standards neeche hai 😹🔥",
            "😹 Teri trolling see aur feel karna — dono experience kharab hain 🤡💀",
            "🤡 Beta teri troll ne sirf yeh prove kiya — tujhe better kaam dhundhna chahiye 😂😹",
            "😹 Bhai troll mein skill hoti hai — teri mein nahi 🤡🔥",
            "🤡 Tu troll hai aur tera troll bhi troll hai — recursion 😂💀",
            "😹 Beta ek advice — yeh mat kar — seriously apni life mein focus kar 🤡😎",
            "Tu itna bura lagta hai ke teri photo dekh ke mosquito bhi bhaag jata hai 🦟",
            "Teri maa ne bhi socha hoga — yaar galti ho gayi 😹",
            "Tujhe dekh ke pata chalta hai — darr darr ke jeena kya hota hai 😂",
            "Teri iq level calculator mein error aata hai 🧮",
            "Tu chhata hua papad hai — touch karte hi toot gaya 😹",
            "Bhai teri aukat itni hai ke mirror bhi muh fer leta hai 🪞",
            "Teri personality dekh ke AI bhi depressed ho gaya 🤖",
            "Tu aisa dost hai jo aaye na aaye — fark nahi padta 😂",
            "Your life is like a bad web series — flop in season 1 📺",
            "Your personality is like a blank meme template — nothing 😂",
            "You're so boring that even sleep runs away from you 😴",
            "Your existence is proof that anyone can use the internet 📶",
            "Your thinking is 2G speed in a 5G world 📡",
            "Your life is a loading screen that never loads ⏳",
            "You're the reason 'error' exists in the dictionary 📖",
            "Your vibe check: FAILED 😂",
            "You're irrelevant — even Google doesn't know you 🔍",
            "You're a hero whose movie flopped in 3 minutes 🎬",
            "Bhai tera swag Excel mein error hai — #NAME? 📊",
            "Tu itna dheema hai ke kachhua bhi race jeet gaya 🐢",
            "Teri thinking 2G speed pe chal rahi hai 📡",
            "Beta tera ek message dekh ke aasman bhi sharma gaya ☁️",
            "Bhai teri life ek loading screen hai — jo kabhi load nahi hoti ⏳",
            "Ter maa ne tujhe chhoda nahi chhodni chahiye thi 😂",
            "Beta tera existence proof hai ke koi bhi internet use kar sakta hai 📶",
            "Bhai teri personality ek blank page hai — aur blank hi rahega 📄",
            "Tu sirf chat mein hero hai real duniya mein zero 💻",
            "Beta teri soch itni outdated hai ke floppy disk bhi reject kar de 💾"
        ]

        ragebait_texts = [
            "Bhai tera reaction dekh ke mujhe hasi aa rahi hai 😂",
            "Tu itna triggered ho gaya, jaise meri baat teri maa ne sun li ho 😹",
            "Rage bait pe itna emotional mat ho, beta 😂",
            "Tu toh aisa gussa ho raha hai jaise teri team world cup haar gayi 🏏",
            "Bhai shant ho ja, tera BP high ho jayega 😂",
            "Teri gaali sun ke mujhe neend aa rahi hai 😴",
            "Tu rage karta hai aur main popcorn kha raha hoon 🍿",
            "Beta tu toh aisa hai jaise bina phone ke reh gaya ho 📱",
            "Teri rage dekh ke lagta hai, teri gf ne break up kar diya 💔",
            "Tu toh aisa hai jaise internet slow ho gaya ho 😂",
            "Your rage is entertaining, please continue 😂",
            "Getting triggered over this? That's cute 🥺",
            "You're so angry, did someone steal your Wi-Fi? 📶",
            "Rage bait level: professional 😂",
            "Your anger is my daily dose of comedy 🤡",
            "Calm down, it's just a message 📩",
            "You're acting like I insulted your whole bloodline 😂",
            "The rage is real, and it's hilarious 😭",
            "You need a therapist for that anger issues 🧠",
            "I love how easy it is to get you triggered 😈",
            "Bhai tera reaction dekh ke mujhe hasi aa rahi hai 😂",
            "Tu itna triggered ho gaya, jaise maine teri game delete kar di ho 🎮",
            "Rage bait pe itna emotional mat ho, beta 😂",
            "Tu toh aisa gussa ho raha hai jaise teri team haar gayi 🏏",
            "Bhai shant ho ja, tera BP high ho jayega 😂",
            "Teri gaali sun ke mujhe neend aa rahi hai 😴",
            "Tu rage karta hai aur main popcorn kha raha hoon 🍿",
            "Beta tu toh aisa hai jaise bina phone ke reh gaya ho 📱",
            "Teri rage dekh ke lagta hai, teri gf ne break up kar diya 💔",
            "Tu toh aisa hai jaise internet slow ho gaya ho 😂"
        ]

        roast_texts = [
            "Ter life ek bakwas webseries ki tarah hai — 1 season mein flop 😂",
            "Bhai teri personality ek sada hua pyaz jaisi hai 🧅",
            "Tu itna bura lagta hai ke teri photo dekh ke mosquito bhi bhaag jata hai 🦟",
            "Teri maa ne bhi socha hoga — yaar galti ho gayi 😹",
            "Tujhe dekh ke pata chalta hai — darr darr ke jeena kya hota hai 😂",
            "Teri iq level calculator mein error aata hai 🧮",
            "Tu chhata hua papad hai — touch karte hi toot gaya 😹",
            "Bhai teri aukat itni hai ke mirror bhi muh fer leta hai 🪞",
            "Teri personality dekh ke AI bhi depressed ho gaya 🤖",
            "Tu aisa dost hai jo aaye na aaye — fark nahi padta 😂",
            "Your life is a joke, and not even a funny one 😂",
            "You're so irrelevant, even your shadow leaves you 🏃",
            "Ter life ek bakwas webseries ki tarah hai — 1 season mein flop 😂",
            "Bhai teri personality ek sada hua pyaz jaisi hai 🧅",
            "Tu itna bura lagta hai ke teri photo dekh ke mosquito bhi bhaag jata hai 🦟",
            "Teri maa ne bhi socha hoga — yaar galti ho gayi 😹",
            "Tujhe dekh ke pata chalta hai — darr darr ke jeena kya hota hai 😂",
            "Teri iq level calculator mein error aata hai 🧮",
            "Tu chhata hua papad hai — touch karte hi toot gaya 😹",
            "Bhai teri aukat itni hai ke mirror bhi muh fer leta hai 🪞",
            "Teri personality dekh ke AI bhi depressed ho gaya 🤖",
            "Tu aisa dost hai jo aaye na aaye — fark nahi padta 😂",
            "Your life is a joke, and not even a funny one 😂",
            "You're so irrelevant, even your shadow leaves you 🏃",
            "Your existence is a notification I always swipe away 📱",
            "You're like a software update — always annoying and never useful 💻",
            "Your brain is like a browser with 100 tabs open — all useless 🌐",
            "You're the human equivalent of a loading screen ⏳",
            "Your personality is like a broken pencil — pointless ✏️",
            "You're not stupid, you just have bad luck thinking 🤔",
            "You're the reason God created jokes 😂",
            "Your life is a meme, and not a good one 🗿",
            "Bhai teri zindagi ek bakwas webseries jaisi hai 📺",
            "Teri personality ek sada hua pyaz jaisi hai — khole toh aansu aaye 🧅",
            "Tu itna bura lagta hai ke teri photo dekh ke mosquito bhi bhaag jata hai 🦟",
            "Teri maa ne bhi socha hoga — yaar galti ho gayi 😹",
            "Tujhe dekh ke pata chalta hai — darr darr ke jeena kya hota hai 😂",
            "Teri iq level calculator mein error aata hai 🧮",
            "Tu chhata hua papad hai — touch karte hi toot gaya 😹",
            "Bhai teri aukat itni hai ke mirror bhi muh fer leta hai 🪞",
            "Teri personality dekh ke AI bhi depressed ho gaya 🤖",
            "Tu aisa dost hai jo aaye na aaye — fark nahi padta 😂"
            "🔥 Teri zindagi ek bakwas webseries ki tarah hai — 1 season mein flop 😂📺",
            "🤣 Bhai teri personality ek sada hua pyaz jaisi hai — khole toh aansu aaye 🧅💀",
            "😹 Tu itna bura lagta hai ke teri photo dekh ke mosquito bhi bhaag jata hai 🦟😂",
            "🔥 Teri maa ne bhi socha hoga — yaar galti ho gayi 😹👶",
            "🤣 Tujhe dekh ke pata chalta hai — darr darr ke jeena kya hota hai 😂💀",
            "😹 Beta tu Google Maps pe search kare toh bhi worthless aayega 🗺️😈",
            "🔥 Teri iq level negative hai — calculator mein error aata hai 🧮😂",
            "🤣 Tu chhata hua papad hai — touch karte hi toot gaya 😹🔥",
            "😹 Bhai teri aukat itni hai ke mirror bhi muh fer leta hai 🪞😂",
            "🔥 Teri personality dekh ke AI bhi depressed ho gaya hoga 🤖😹",
            "🤣 Tu aisa dost hai jo aaye na aaye — fark nahi padta 😂💀",
            "😹 Bhai teri soch utni hi purani hai jitna tera Nokia phone 📱😂",
            "🔥 Tera existence mere life mein irrelevant hai — bilkul sarkari kaam jaisa 📋😹",
            "🤣 Tu itna boring hai ke neend khud aa jaaye tujhe dekh ke 😴😂",
            "😹 Teri profile pic dekh ke emoji wale bhi sue kar sakte hain 😱🔥",
            "🔥 Bhai tu aisa player hai jo kabhi goal nahi kar sakta apne hi team ke khilaf 😂⚽",
            "🤣 Teri advice sunna waisa hai jaise sade kele se rasta poochna 🍌😹",
            "😹 Tu garib nahi hai — but tujhe dekh ke gareebi ko takleef hoti hai 💰😂",
            "🔥 Teri kismat itni kharab hai ke lottery ticket bhi teri traf nahi dekhti 🎫😹",
            "🤣 Bhai tera sense of humor graveyard se udhaara liya hai kya 🪦😂",
            "😹 Tu itna irrelevant hai ke khud Google bhi nahi jaanta tera naam 🔍🔥",
            "🔥 Teri body language bolta hai — main hara hua insaan hoon 😂💀",
            "🤣 Tu ek hi baar funny tha — jab tune mujhe seriously liya 😹⚡",
            "😹 Bhai teri achievements list mein sirf ek cheez hai — exist karna 😂🔥",
            "🔥 Tujhe dekh ke lagta hai — nature ne mistake ki thi 🌿😹",
            "🤣 Teri skills dekh ke Thanos bhi bola hoga — yeh toh automatically wipe ho jaayega 💀😂",
            "😹 Beta tera future itna dark hai ke sunglasses pehenne ki zaroorat nahi 🕶️🔥",
            "🔥 Teri batting dekh ke khud pitch ne sorry bola 🏏😂",
            "🤣 Bhai tu aisa idea hai jo meeting mein sab ignore karte hain 📊😹",
            "😹 Teri zubaan aur dimag mein kabhi meetup nahi hota 🧠💬😂",
            "🔥 Tu aisa hero hai jiska movie 3 minutes mein flop ho gayi 🎬😹",
            "🤣 Teri gaali sunne ke baad dushmano ne mafi maang li 😂⚔️",
            "😹 Bhai tera swag level Excel mein error hai — #NAME? 📊🔥",
            "🔥 Tu itna dheema hai ke kachhua bhi race jeet gaya 🐢😂",
            "🤣 Teri thinking 2G speed pe chal rahi hai duniya 5G mein hai 📡😹",
            "😹 Beta tera ek message dekh ke aasman bhi sharma gaya ☁️😂",
            "🔥 Bhai teri life ek loading screen hai — jo kabhi load nahi hoti ⏳😹",
            "🤣 Tu aisa mirror hai jo galat reflection dikhata hai 🪞😂",
            "😹 Teri maa ne tujhe chhoda nahi chhodni chahiye thi 😂🔥",
            "🔥 Beta tera existence proof hai ke koi bhi internet use kar sakta hai 📶😹",
            "🤣 Tujhe dekh ke lagta hai — maa baap ne education mein invest nahi kiya 📚😂",
            "😹 Teri personality ek blank page hai — aur blank hi rahega 📄🔥",
            "🔥 Tu sirf chat mein hero hai real duniya mein zero 💻😂",
            "🤣 Bhai teri jawab dene ki speed se tortoise bhi impress nahi 🐢😹",
            "😹 Teri soch itni outdated hai ke floppy disk bhi reject kar de 💾😂",
            "🔥 Tu aisa WiFi password hai jo koi yaad nahi rakhta 🔑😹",
            "🤣 Beta teri awaaz sunne ke baad mujhe silence zyada priceless laga 🤫😂",
            "😹 Bhai tera roast karna waisa hai jaise sadi hui vegetable ko season karna 🥦🔥",
            "🔥 Teri social skills dekh ke chatbot bhi impress ho ga",
            "Your existence is a notification I always swipe away 📱",
            "You're like a software update — always annoying and never useful 💻",
            "Your brain is like a browser with 100 tabs open — all useless 🌐",
            "You're the human equivalent of a loading screen ⏳",
            "Your personality is like a broken pencil — pointless ✏️",
            "You're not stupid, you just have bad luck thinking 🤔",
            "You're the reason God created jokes 😂",
            "Your life is a meme, and not a good one 🗿",
            "Bhai teri zindagi ek bakwas webseries jaisi hai 📺",
            "Teri personality ek sada hua pyaz jaisi hai — khole toh aansu aaye 🧅",
            "Tu itna bura lagta hai ke teri photo dekh ke mosquito bhi bhaag jata hai 🦟",
            "Teri maa ne bhi socha hoga — yaar galti ho gayi 😹",
            "Tujhe dekh ke pata chalta hai — darr darr ke jeena kya hota hai 😂",
            "Teri iq level calculator mein error aata hai 🧮",
            "Tu chhata hua papad hai — touch karte hi toot gaya 😹",
            "Bhai teri aukat itni hai ke mirror bhi muh fer leta hai 🪞",
            "Teri personality dekh ke AI bhi depressed ho gaya 🤖",
            "Tu aisa dost hai jo aaye na aaye — fark nahi padta 😂"
        ]

        # ─── NON-ABUSIVE RAID TEXTS (Menu9) ────────────────────────────────────

        attack_texts = [
     "🗡️ Tera baap aaya hai sunta nahi kya 👑😈",
        "⚡ Mere saamne aake dikhao himmat hai toh 😎💪",
        "🔥 Attack mode on — teri khair nahi aaj 😡⚔️",
        "💀 Tujhe itna marunga ke teri maa bhi nahi pehchanegi 😂🔥",
        "💥 Beta ye territory meri hai nikal yahan se 🏴‍☠️⚡",
        "🗡️ Aukaat hai toh saamne aa nahi toh chup baith 😈💀",
        "⚡ Tu keyboard warrior hai asli mard nahi 😂👊",
        "🔥 Teri maa ne bhi bola tera baap chahiye 😹💔",
        "💥 Chal hat yahan se chota baccha 🤣👋",
        "⚔️ Mujhe gaali de ke dekh kya hoga teri life mein 😈⚡",
        "💀 Bhai seedha bol de surrender karega ya maar khayega 😎🔥",
        "🗡️ Attack karta hoon toh block nahi hoga tera 😡⚔️",
        "⚡ Yeh game mein nahi real life mein bhi kaatenge tujhe 💪😤",
        "🔥 Tera confidence dekh ke hansi aati hai yaar 😂💥",
        "💥 Andha hai ya dikhta nahi kaun boss hai yahan 👑⚔️",
        "⚔️ Teri har gaali pe 10 gaaliyan waapis aayengi 😈🔥",
        "💀 Beta peeth nahi dikhana mujhe — coward 🏃‍♂️😂",
        "🗡️ Lad le ek baar — guarantee hai rota hoga tu 😹⚡",
        "⚡ Keyboard tod ke aa toh baat karte hain 💥👊",
        "🔥 Teri bhasha se pata chalta hai ghar mein parhe nahi 😂🤣",
        "⚔️ Main yahan hoon — tu kahan chhupta hai aaja 😎💀",
        "💀 Teri har move ka jawab taiyaar hai mere paas 🎯🔥",
        "🗡️ Tu sirf darta hai asli attack nahi kar sakta 😂⚡",
        "⚡ Baahubali nahi hai tu yahan — chal nikal 👋💥",
        "🔥 Teri aukaat utni hai jitni do takke ki 😹🗡️",
        "💥 Attack aur reaction — dono mein haar jayega tu ⚔️😎",
        "⚔️ Ek baar aake dekh kya hota hai tere saath 💀🔥",
        "💀 Sher ke saamne bakra nahi ban — phir bhi ban raha 😂⚡",
        "🗡️ Yeh teri territory nahi bhai — haath jod ke ja 🙏😈",
        "⚡ Tu attack karega aur main finish karunga 💥⚔️",
        "🔥 Teri himmat hai toh mujhse seedha baat kar 😤💀",
        "💥 Keyboard pe hero ban raha hai — asli duniya mein zero 😂🗡️",
        "⚔️ Maar kha aur phir rota mat — warning hai 😈⚡",
        "💀 Teri speed se faster hoon main — bhaag nahi sakta 🔥💥",
        "🗡️ Yaar teri life mein koi nahi kya isliye yahan ata hai 😂⚔️",
        "⚡ Hero mat ban — yahan real khiladi baithe hain 👑💀",
        "🔥 Attack kiya — ab lash uthane ki taiyaari kar 😹⚡",
        "⚔️ Teri har galti ka hisaab hoga — ruk 😈🔥",
        "💀 Bhai attack se pehle 1% dimag use kar 🧠💥",
        "🗡️ Chal hat nahi toh main khud hataunga isko 😤⚡",
        "⚡ Yeh war hai — aur tu already haar gaya 😎🔥",
        "🔥 Teri maa bhi tera lecture sunke bore ho gayi hogi 😹💥",
        "💥 Main attack mein vishwas nahi karta — main finish mein karta hoon ⚔️😈",
        "⚔️ Chal randike ek baar try kar le — rona mat baad mein 😂💀",
        "💀 Ab samjha kya hua? No? Toh phir ek aur attack 🔥⚡",
        ]

        war_texts = [
            "⚔️ War shuru ho gayi — aur tu pehle hi haar gaya 😂🔥",
        "💣 Bhai main war mein nahi aata — main war khatam karne aata hoon 😈⚡",
        "🏴‍☠️ Tera jhanda uraya — apna wala lehraya 😎💀",
        "⚔️ Tu lad raha hai mujhse — yeh teri sabse badi galti hai 🔥😂",
        "💣 Main war nahi khelta — main result deliver karta hoon 👑⚡",
        "🏴‍☠️ Battlefield pe aake to dekh — tera rank kya hai 😈⚔️",
        "⚔️ Randike war declare kiya toh surrender ka option bhi rakh 😂💣",
        "💣 Tu soldier nahi hai — tu sirf noise hai 🔊😂",
        "🏴‍☠️ War mein strategy chahiye — tu sirf emotion se ladhta hai 😹⚔️",
        "⚔️ Beta yeh teri territory nahi — nikalja 👋💣",
        "💣 Tera war cry sunke mujhe neend aati hai 😴😂",
        "🏴‍☠️ Main akela kaafi hoon — teri poori army ke liye ⚔️😈",
        "⚔️ War ghoshit kiya — white flag kahan hai tera 🏳️😂",
        "💣 Bhai tu pehle khud ko toh jeet — phir mujhse lad 😎💀",
        "🏴‍☠️ Tera war tactic: bolna aur bhaagna 😹⚔️",
        "⚔️ Main chhoda nahi — tu chhoda baad mein roega 😂💣",
        "💣 Battle field pe aate waqt socha — main jeet sakta hoon? Nahi 😈🏴‍☠️",
        "⚔️ Tu ek round bhi nahi jeeta — aur war ki baat karta hai 😂💀",
        "💣 Bhai surrender kar le — dignity bachegi thodi 🙏😹",
        "🏴‍☠️ War mein aaye — aur pehli line mein fail ho gaye ⚔️😂",
        "⚔️ Tera morale zero hai — teri army teri khud ki dushman hai 😂💣",
        "💣 Main war expert hoon — tu war ka victim hai 😎🏴‍☠️",
        "🏴‍☠️ Beta teri strategy ek broken compass jaisi hai ⚔️😂",
        "⚔️ War mein seena taan ke aa — peeth dikha ke nahi 😹💣",
        "💣 Bhai teri army mein sirf tu hai — aur tu kaafi nahi 😈🏴‍☠️",
        "🏴‍☠️ Teri war cry sun ke dushman khud aa gaye — rescue karne ⚔️😂",
        "⚔️ Beta teri territory war se pehle hi haari thi 💣😹",
        "💣 Main war mein nahi — main tujhe personally destroy karne mein hoon 😈🏴‍☠️",
        "🏴‍☠️ Tera war plan sunke GPS bhi confused hai ⚔️😂",
        "⚔️ Tu war mein aaya — par weapons lana bhool gaya 💣😹",
        "💣 Bhai yeh war nahi tujhe sirf reality check tha 😂🏴‍☠️",
        "🏴‍☠️ Teri army tujhse zyada samajhdaar hai — unhone bandh kiya ⚔️😈",
        "⚔️ War mein bhi excuse karta hai — aur life mein bhi 😂💣",
        "💣 Tu jo war soch raha hai — woh meri morning routine hai 😎🏴‍☠️",
        "🏴‍☠️ Bhai teri war itni slow hai ke climate change pehle ho jaayega ⚔️😹",
        "⚔️ Main tujhse war karta hoon — aur tujhe pata bhi nahi chalta 💣😂",
        "💣 War ghoshit kar ke tu pehla tha — haar ke bhi pehla hai 😹🏴‍☠️",
        "🏴‍☠️ Teri war mein consistency hai — consistently losing ⚔️😂",
        "⚔️ Bhai war mein bhagna galat hai — tu phir bhi karta hai 💣😈",
        "💣 Tu war mein aaya — main pehle se tere base par tha 🏴‍☠️😂",
        "🏴‍☠️ Teri war strategy mein sirf ek problem hai — sab kuch ⚔️😹",
        "⚔️ Beta war ka matalab samjha nahi tujhe — sikhaunga abhi 💣😂",
        "💣 War mein hero nahi bante — survivors bante hain — aur tu nahi banega 🏴‍☠️😈",
        "🏴‍☠️ Teri war mein dum nahi — sirf dhool hai ⚔️😂",
        "⚔️ Bhai war declare karna alag baat hai — jeetan alag 💣😹",
        "💣 Tu war mein aaya sirf lose karne ke liye — congratulations 🏴‍☠️😂",
        "🏴‍☠️ Main akele teri sab pe bhaari hoon — aur tujhe pata hai ⚔️😈",
        "⚔️ Teri war ka sabse bura part — tu khud tha 💣😂",
        "💣 War mein aaye — teri team ne hi tujhe chhod diya 🏴‍☠️😹",
        "🏴‍☠️ Beta war khatam — teri taraf se surrender accepted ⚔️😎",
        ]

        savage_texts = [
            "😈 Confidence is silent, insecurity is loud! 🔥",
            "💀 You're not as important as you think! 🌪️",
            "🔥 Reality check — you're not that special! 💥",
            "😏 Your opinion is noted, but not needed! 📝",
            "💀 Let's be honest — you're overrated! 🎭",
            "🔥 The truth hurts, but it sets you free! 💪",
            "😈 You're not the main character, sorry! 📺",
            "💀 Your ego is writing checks your skills can't cash! 💰",
            "🔥 Stay humble or get humbled! ⚡",
            "😏 You're a classic example of overconfidence! 🎯",
            "💀 Let your actions speak, not your mouth! 🔥",
            "😈 Your presence is as useful as a screen door on a submarine! 🚪",
            "🔥 Let's be real — you're not that impressive! 💥",
            "💀 You're the CEO of overestimating yourself! 🏢",
            "😏 Stay in your lane, champ! 🏎️",
            "🔥 You're not as hot as you think! ❄️",
            "💀 Confidence without skill is just delusion! 🎭",
            "😈 Your reputation precedes you — and it's not good! 📉",
            "🔥 Let's keep it real — you're average at best! ⭐",
            "💀 You're a cautionary tale for others! ⚠️"
            "😈 Main savage hoon — tujhe explanation nahi deta 🔥💀",
            "💀 Teri feelings mere liye statistics hain — irrelevant 😂😈",
            "🔥 Main woh nahi hoon jo tujhe comfortable feel karaaye 😎💀",
            "😈 Beta teri baatein mujhe bore karti hain — next 😂🔥",
            "💀 Teri opinion meri life mein footnote bhi nahi hai 😈😹",
            "🔥 Main tujhe explain nahi karta — tujhse better logon ke paas time deta hoon 😎💀",
            "😈 Tera attitude dekh ke mujhe apni nails file karni chahiye 💅😂",
            "💀 Bhai tujhe reject karna meri hobby hai 🔥😈",
            "🔥 Teri presence mujhe remind karaati hai — kuch logon ko mute karna chahiye 🔇😂",
            "😈 Main bad vibes nahi leta — teri taraf bhi nahi 💀🔥",
            "💀 Tu mere standard se neeche hai — elevator laga le 🛗😂",
            "🔥 Teri baat sunna — option nahi habit nahi aur interest bhi nahi 😈💀",
            "😈 Main ghanta samjhata hoon — samajh nahi aaya toh teri problem 😂🔥",
            "💀 Teri ego itni badi hai — uske liye alag zip code chahiye 📮😂",
            "🔥 Beta mujhe tujhse jealousy feel nahi hoti — pity hoti hai 😈💀",
            "😈 Main woh insaan nahi hoon jis par tu waqt barbad kare — ya main karta hoon 😂🔥",
            "💀 Teri life choices dekh ke main grateful hoon main tujhsa nahi hoon 😹😈",
            "🔥 Bhai teri smartness ka level: WiFi password ignore karna 📶😂",
            "😈 Teri mastiyan mujhe entertain nahi karti — bore karti hain 💀🔥",
            "💀 Main savage nahi — main simply tujhse better hoon 😎😂",
            "🔥 Teri personality ek blank meme format jaisi hai — kuch nahi 😈💀",
            "😈 Beta apni journey pe focus kar — meri disturb mat kar 😂🔥",
            "💀 Teri hard work ka result tera hi face hai — kaafi bura 😹😈",
            "🔥 Main tujhe miss nahi karta — mujhe tujhse better cheezein miss hoti hain 😂💀",
            "😈 Teri baatein sun ke laga — yeh real person hai ya chatbot glitch 🤖😂",
            "💀 Bhai teri intelligence ke liye sorry feel hoti hai 🔥😈",
            "🔥 Main tujhe block isliye nahi karta — kyunki tujhe exist karna pata hai 😂💀",
            "😈 Teri struggles dekh ke mujhe motivation milti hai — teri tarah mat banna 😹🔥",
            "💀 Tu jo effort lagate ho mujhpe — woh apni growth mein lagao 😎😂",
            "🔥 Teri vibes mujhe 2G network se bhi slow lagti hain 📡😈",
            "😈 Main tujhe pehle judge nahi karta — par tujhe pehle judge hota hoon 💀😂",
            "💀 Bhai tera shadow bhi tujhse zyada interesting hai 🔥😂",
            "🔥 Teri logic sun ke Albert Einstein ne resign kar diya hoga 🧪😈",
            "😈 Tu mere jaisa ban sakta hai — agar try karta 10 saal toh bhi nahi 💀😂",
            "💀 Teri taraf se koi bhi reaction — mujhe bored karta hai 🔥😹",
            "🔥 Main respectful hoon — tere sath nahi 😈💀",
            "😈 Beta teri vibe check: FAILED 😂🔥",
            "💀 Teri har move predicted thi — boring player 😹😈",
            "🔥 Main tujhe second chance nahi deta — teri pehli impression kafi thi 😂💀",
            "😈 Teri friendship ke offer ko professionally decline karta hoon 😎😂",
            "💀 Beta tu mujhe feel nahi karaata — tu sirf annoy karta hai 🔥😈",
            "🔥 Teri dimagi capacity dekh ke solar calculator bhi sorry bol de 🔋😂",
            "😈 Main uun logon mein nahi hoon jo tere liye time waste karein 💀🔥",
            "💀 Teri life ka GPS tujhe wrong direction mein le ja raha hai 🗺️😂",
            "🔥 Bhai teri alag identity bana — copier mat ban 😈💀",
            "😈 Tu mere radar par bhi nahi aata — itna irrelevant hai 😂🔥",
            "💀 Teri maa ne bhi socha hoga — yaar isko kuch aur karna chahiye tha 😹😈",
            "🔥 Main woh hoon jo teri nightmares mein aata hai — as a reminder 😎💀",
            "😈 Beta teri bakaiti mujhe filter nahi karti — automatically skip ho jaati hai 😂🔥",
            "💀 Tu savage hone ki koshish karta hai — mujhe dekh savage ka example 😈😹",
        ]

        ultra_texts = [
           "🔥 ULTRA mode activated — time to dominate! 👑"   
        "🌪️ ULTRA MODE ACTIVATED — teri poori existence question mein hai 😈🔥",
        "⚡ Ultra attack — pehle gaali sunna phir rona — sequence yaad kar 😂💀",
        "🌪️ Beta ultra level pe aake dekh — yahan teri category nahi hai 👑🔥",
        "⚡ ULTRA BLOW — teri soch se lekar attitude tak sab destroy 💥😈",
        "🌪️ Yeh ultra mode hai — blocking nahi help karega 😂⚡",
        "⚡ Ultra raid engaged — ab teri poori chat history history hai 📜😹",
        "🌪️ Beta ultra speed mein aa — par seedha home le jaata hoon 💀🔥",
        "⚡ Ultra fire — teri har defensive move kaam nahi karegi 😈🌪️",
        "🌪️ Yeh ultra level fight hai — tu still bronze mein hai 😂⚡",
        "⚡ ULTRA DAMAGE — teri reputation, teri aukaat, teri everything 💥😹",
        "🌪️ Ultra mode mein poori teri army bhi kaafi nahi 😈🔥",
        "⚡ Beta ultra attack sunne ke baad sun raha hai kya? Normal hai 😂🌪️",
        "🌪️ ULTRA RANT incoming — tune jo kiya uska hisaab hoga 💀⚡",
        "⚡ Yeh ultra version hai — tujhe pata bhi nahi kya aaya 😹🔥",
        "🌪️ Ultra mode ON — timer chal raha hai teri destruction ka 😈⚡",
        "⚡ Beta ultra strike pe tujhe sirf ek option hai — disappear 😂💀",
        "🌪️ ULTRA COMBO — reply + react + roast + raid all at once 🔥⚡",
        "⚡ Yeh ultra level rage hai — aur tujhe taste hoga 😈🌪️",
        "🌪️ Ultra activated — pehle bol sorry phir ja 😹😂",
        "⚡ Beta ULTRA message ka matlab — tu mere liye mission ban gaya 💀🔥",
        "🌪️ ULTRA STORM — har cheez destroy ho rahi hai teri side pe 😈⚡",
        "⚡ Yeh ultra nahi — tujhe sirf samjhane ki koshish thi 😂🌪️",
        "🌪️ Ultra mode finish — teri team ne tera saath chhoda 💀🔥",
        "⚡ Beta ULTRA = mera minimum effort on you 😈😂",
        "🌪️ ULTRA RAIN — tune invite kiya tha — enjoy karna tha na? 😹⚡",
        "⚡ Ultra mode mein ek hi rule — no mercy 💀🔥",
        "🌪️ Beta ULTRA sabse pehle yeh — teri galti ka hisaab 😈⚡",
        "⚡ Yeh ultra speed se aaya — aur teri samajh mein ultra slow aayega 😹🌪️",
        "🌪️ ULTRA LOCK — ab yahan se nahi jayega tu 💀🔥",
        "⚡ Beta ultra strike mein teri saari strategy fail hai 😂😈",
        "🌪️ Ultra level pe chal — toh teri duniya hi badal jaayegi 🔥⚡",
        "⚡ ULTRA — yeh word hi teri aukat se bada hai 😹💀",
        "🌪️ Beta ultra mein main hoon — tujhe pata nahi tha kya 😈🔥",
        "⚡ Yeh ultra raid hai — har message teri ek problem hai 😂🌪️",
        "🌪️ ULTRA DONE — tu done kar le pehle 💀⚡",
        "⚡ Beta ultra mein welcome — pehle bol kya karna hai 😹🔥",
        "🌪️ Ultra mode — ab seedha point pe aata hoon — tu fail hai 😂😈",
        "⚡ ULTRA BLAST — teri timeline pe aaya — nahi ruk sakta 💥🌪️",
        "🌪️ Beta ultra mein aake teri baat karo — nahi aata toh seedha ja 💀🔥",
        "⚡ Yeh ultra war hai — aur teri taraf se koi nahi 😂😈",
        "🌪️ ULTRA FINAL — bas yahi hoga — accept kar 💀⚡",
        "⚡ Beta ultra strike complete — check teri status 😹🔥",
        "🌪️ Ultra mode mein log surrender karte hain — tujhe bhi karna hoga 😈⚡",
        "⚡ Yeh ultra punishment nahi — tutorial hai teri life ka 😂💀",
        "🌪️ ULTRA JUDGEMENT — teri har move judged ho rahi hai 🔥⚡",
        "⚡ Beta ultra mein ek cheez — main hoon aur tu nahi rahe 😈🌪️",
        "🌪️ Ultra mode completed — teri side destroyed 💀😂",
        "⚡ Yeh ultra attack ka last wave hai — teri koi repair nahi 😹🔥",
        "🌪️ ULTRA END — teri war khatam teri taraf se flag gira 😈⚡",
        "⚡ Beta ultra mein aana tha — rona nahi tha — par dono kiye 😂💀",
        ]

        # ─── NEW MENU9 RAID TEXTS ───────────────────────────────────────────────

        shame_texts = [
        "😤 Sharam kar — itna gira hua kaam karte kaise hain tum log 🔥💀",
        "🙅 Bhai teri harkat dekh ke pura group sharam se doob gaya 😂😤",
        "😤 Yeh sab karke tujhe pride feel hoti hai? Really? 💀🔥",
        "🙅 Beta teri harkaten dekh ke maa baap sharmayenge 😂😤",
        "😤 Sharam nahi hai tujhe bilkul — clearly 💀😹",
        "🙅 Bhai itna gira hua kaam dekh ke log muh fer lete hain 😤🔥",
        "😤 Tu itna neeche gira — zameen bhi neeche ho gayi 💀😂",
        "🙅 Beta sharam bhi nahi aata aisa karte hue 😤😹",
        "😤 Yeh harkat dekh ke lagta hai — tujhe value kisi ne nahi sikhaya 💀🔥",
        "🙅 Bhai log tujhe dekh ke aankhein pher lete hain — soch kya kar raha hai 😤😂",
        "😤 Teri galti nahi — environment ki galti — par ab waqt hai change ka 💀😹",
        "🙅 Beta sharam isliye nahi aati kyunki sharam feel karna seekha nahi 😤🔥",
        "😤 Yeh kaam karke tujhe khushi mili? Toh mujhe tujhse zyada chinta hai 💀😂",
        "🙅 Bhai teri harkat pura record hai — aur yeh record kharab hai 😤😹",
        "😤 Tu sochta hai koi dekh nahi raha — sab dekh rahe hain 💀🔥",
        "🙅 Beta aisa behave karta hai — khud se bhi embarrassing lagta hai tu 😤😂",
        "😤 Yeh sab dekh ke lagta hai — teri parwarish kahan gayi 💀😹",
        "🙅 Bhai teri harkaton ka hisaab hoga — aaj nahi toh kal 😤🔥",
        "😤 Tu sharminda nahi hai — woh most shameful cheez hai 💀😂",
        "🙅 Beta logo ne tujhe judge kiya — kyunki tune judge hone wala kaam kiya 😤😹",
        "😤 Yeh bura kaam karke tujhe kya mila — kuch nahi — bas naam barbad 💀🔥",
        "🙅 Bhai sharam karo — itna toh haq hai tumhara 😤😂",
        "😤 Tu yahan cool lagne ki koshish mein sharminda ho gaya 💀😹",
        "🙅 Beta ghalat rasta chhod — vapas aa 😤🔥",
        "😤 Yeh sab karke teri image bani hai — worst category mein 💀😂",
        "🙅 Bhai teri harkat ka review — 0 stars — do not recommend 😤😹",
        "😤 Tu itna neeche gira — recovery mushkil lagti hai 💀🔥",
        "🙅 Beta tujhe samjhana waqt waste hai — par try kar raha hoon 😤😂",
        "😤 Yeh sab dekh ke mujhe tujhse zyada tujhpe gussa nahi — hairaani hai 💀😹",
        "🙅 Bhai sharam se doob — par us mein bhi tujhe help chahiye shayad 😤🔥",
        "😤 Teri harkat ek lesson hai — dusron ke liye kya nahi karna chahiye 💀😂",
        "🙅 Beta teri yeh sab dekh ke khud bhi tujhse door rehna chahta hoon 😤😹",
        "😤 Yeh gaaliyaan nahi — sirf reality check hai 💀🔥",
        "🙅 Bhai sharam tab aati hai jab insaan mein insaniyat hoti hai 😤😂",
        "😤 Tu ek example bana diya khud ko — negative example 💀😹",
        "🙅 Beta tujhe ek baar ruk ke soochna chahiye tha — nahi soocha 😤🔥",
        "😤 Yeh sab karke tu yahan hai — aur sochta hai main galat hoon? 💀😂",
        "🙅 Bhai itna toh bata — tujhe kaisa feel hota hai yeh sab karne ke baad 😤😹",
        "😤 Tu sharminda nahi — tujhe sharminda feel karna chahiye 💀🔥",
        "🙅 Beta yeh rasta galat hai — abhi bhi change ho sakta hai 😤😂",
        "😤 Yeh sab khud se bura nahi tha — tu tha 💀😹",
        "🙅 Bhai teri harkaton ka real world impact sun — sab tujhse dur hain 😤🔥",
        "😤 Tu soch raha hai main overreact kar raha hoon — par tujhe hisaab hoga 💀😂",
        "🙅 Beta tujhe pata hai tu kya kar raha hai — aur phir bhi kar raha hai 😤😹",
        "😤 Yeh sharm ki baat hai — aur tujhe realize karna chahiye 💀🔥",
        "🙅 Bhai tujhe mirror mein dekhna chahiye — ek baar 😤😂",
        "😤 Tu itna bura nahi hai — par yeh kaam bura tha 💀😹",
        "🙅 Beta sharam isliye nahi aati — kyunki tu sochta nahi consequences ke baare mein 😤🔥",
        "😤 Yeh moment tera lowest point hai — aur abhi bhi jaag sakta hai 💀😂",
        "🙅 Bhai aaj ek kaam kar — sharminda ho aur badal — bas itna chahiye 😤😎",
        ]

        diss_texts = [
            "🎤 Tera naam sun ke log mute kar dete hain khud ko 🔇😂",
        "💀 Tu diss kar raha hai — khud ko diss kar pehle 🪞😹",
        "🎙️ Teri rap jaisi hai — no flow no bars no future 🎵😂",
        "💥 Bhai tera verse sun ke Eminem ne retire le liya 😹🎤",
        "🔥 Teri diss itni kamzor hai ke whisper bhi zyada loud hai 🤫😂",
        "💀 Tu sirf bolne mein mard hai karne mein? Zero 😈🎙️",
        "🎤 Beta teri bars mein bar hi nahi — sirf khali string 🎸😂",
        "💥 Tera diss track sunne ke baad logon ne earbuds tod diye 🎧😹",
        "🔥 Bhai teri lyric likh ke dekha — autocorrect ne bhi reject kiya ✍️😂",
        "💀 Tu diss karta hai aur log diss ko diss karte hain 😂🎤",
        "🎙️ Teri voice aisi hai ke autotune bhi nahi bach sakta 🎶😹",
        "💥 Beta freestyle kar le — ya phir stop the embarrassment 🛑😂",
        "🔥 Tujhe sun ke DJ ne plug nikal diya 🔌😹",
        "💀 Bhai tera flow aisa hai jaise jaam mein traffic — ruka hua 🚗😂",
        "🎤 Teri soch itni slow hai ke beat ke saath nahi chalti 🥁😹",
        "💥 Tera diss mujhe sula raha hai — better than sleeping pills 😴😂",
        "🔥 Bhai asli diss toh tab hogi jab tu actually kuch achieve kare 🏆😹",
        "💀 Teri lyrics Google Translate se better hain — bas 🌐😂",
        "🎙️ Beta chal hat stage se — pehle walk-on music bana 🎵😹",
        "💥 Tera punchline itna weak hai ke paper bhi survive kar le 📄😂",
        "🔥 Bhai teri diss sun ke crowd ne baat karna shuru kar diya 🙄😹",
        "💀 Tu verse likhta hai ya grocery list — same energy 🛒😂",
        "🎤 Teri bars mein calories zyada hain — totally empty 😹🔥",
        "💥 Bhai teri rhyme sunke chhote bacche bhi sharma jaate hain 😂💀",
        "🔥 Teri diss aisi hai — sirf uski maa samjhi 😹🎙️",
        "💀 Tu diss karta hai mujhe — main khud apni diss sunta hoon for fun 😂💥",
        "🎤 Tera stage naam kya hai — Bakwas ke Raja? 👑😹",
        "💥 Bhai teri microphone bhi teri awaaz se dara hua hai 🎙️😂",
        "🔥 Tu diss mein expert hai — aur expert hone mein loser 😹💀",
        "💀 Teri har line mein cringe hai — Olympic level 🥇😂",
        "🎙️ Beta khud ki diss sun le — ek baar realise hoga 😹🔥",
        "💥 Bhai tera diss itna slow hai ke mujhe neend aa gayi 😴😂",
        "🔥 Teri creativity level: template pe naam likhna 💀😹",
        "💀 Tu diss karne ke liye paida hua tha — aur fail ho gaya 😂🎤",
        "🎙️ Tera rhyme scheme: aab aab aab — boring AF 📝😹",
        "💥 Bhai teri diss response mein Soulja Boy beat use karta hun 😂🔥",
        "🔥 Tu keyboard pe rap karta hai — phone pe nahi kaata 📱💀",
        "💀 Teri diss sun ke mic khud neeche gir gaya 🎙️😂",
        "🎤 Beta teri bars itni weak hain ke paper toh chodh kaagaz bhi nahi chhapega 📰😹",
        "💥 Bhai tera flow paani mein nahi petrol mein hai — ab blast 🔥😂",
        "🔥 Teri diss sunta hoon toh lagta hai sabne kaan band kar rakhe hain 🔇💀",
        "💀 Tu diss mein ghusaa — tu diss tha diss 😹😂",
        "🎙️ Bhai tera verse industry standard se neeche hai — ground floor bhi nahi 🏚️🔥",
        "💥 Teri awaaz mein woh baat nahi jo diss mein chahiye — talent 😂💀",
        "🔥 Beta teri diss itni pathetic hai ke pity vote mil sakta tha 🗳️😹",
        "💀 Bhai teri rap career ek Instagram story jaisi hai — 24 ghante mein khatam 📸😂",
        "🎤 Tu rapper nahi rapper ki copy ki copy ka knock-off hai 😹🔥",
        "💥 Teri diss sun ke auto-generated ho sakti thi — aur better hoti 🤖😂",
        "🔥 Bhai freestyle maar — aur phir sun khud ko — tujhe pata chalega 🎧💀",
        "💀 Teri diss ka reply nahi deta — tujhe dignify karna time waste hai 😂🎙️",
        ]

        devil_texts = [
            "😈 DEVIL MODE — yahan woh aaya hai jo tujhe deserve karta hai 🔥💀",
        "😈 Beta main devil nahi — main tera worst nightmare hoon 🔥⚡",
        "😈 Devil raid activate — teri poori timeline disturbed 💀😂",
        "😈 Bhai devil pe hath lagaya — ab bhog 🔥💥",
        "😈 DEVIL FURY — teri sab cheez ek baar mein 💀⚡",
        "😈 Beta devil ke saamne hum sab khiladi hain — tu beginner 🔥😂",
        "😈 DEVIL ATTACK — teri defense devil ke touch se fail 💀😈",
        "😈 Bhai devil mode mein koi safe nahi — tu bhi nahi 🔥⚡",
        "😈 Teri galti — devil ko challenge karna 💀😂",
        "😈 Beta devil ki bhasha — punishment aur reward — tu punishment mein hai 🔥😈",
        "😈 DEVIL LEVEL RAGE — teri poori life on line 💀⚡",
        "😈 Bhai devil se lad ke koi nahi jeeta — tu bhi nahi jeetega 🔥😂",
        "😈 Devil mode — tera sab kuch noted — sab 💀😈",
        "😈 Beta DEVIL FIRE — teri poori duniya burn 🔥⚡",
        "😈 DEVIL RAID COMPLETE — tujhe koi nahi bachayega 💀😂",
        "😈 Bhai devil teri har move pe already plan bana chuka 🔥😈",
        "😈 Devil mode — tera future bleak — teri choice thi 💀⚡",
        "😈 Beta devil ne tujhe select kiya — koi bada reason hoga 🔥😂",
        "😈 DEVIL STORM — teri poori squad disbanded 💀😈",
        "😈 Bhai devil ke game mein tera turn tha — abhi mera 🔥⚡",
        "😈 Devil raid engage — now teri responsibility 💀😂",
        "😈 Beta devil level punishment — tujhse tune karaya tha 🔥😈",
        "😈 DEVIL ZONE — nikal ja nahi toh devil ka guest ban 💀⚡",
        "😈 Bhai devil hamesha sunta hai — teri bhi sun li 🔥😂",
        "😈 Devil mode ACTIVATED — teri poori timeline hijacked 💀😈",
        "😈 Beta devil ke saamne sirf ek option — respect ya suffer 🔥⚡",
        "😈 DEVIL FINAL BLOW — teri defense completely gone 💀😂",
        "😈 Bhai devil ne decide kiya — teri loss is inevitable 🔥😈",
        "😈 Devil mein aake dekha — tu deserving nahi tha challenge ka 💀⚡",
        "😈 Beta DEVIL RAIN — teri har cheez soaked in fire 🔥😂",
        "😈 DEVIL vs YOU — spoiler: devil wins 💀😈",
        "😈 Bhai devil ke saamne teri prayers bhi kaam nahi aate 🔥⚡",
        "😈 Devil mode — teri weak spots identified — attack 💀😂",
        "😈 Beta devil ki nazar se tu nahi chhupta 🔥😈",
        "😈 DEVIL JUDGMENT — teri poori history reviewed — verdict: guilty 💀⚡",
        "😈 Bhai devil ki duniya mein tu tourist tha — time up 🔥😂",
        "😈 Devil fury — tere steps already tracked hain 💀😈",
        "😈 Beta DEVIL COUNTER — teri har move ka counter ready tha 🔥⚡",
        "😈 DEVIL FINISH — teri game over — my game continues 💀😂",
        "😈 Bhai devil mode se nikalna — tujhe option nahi 🔥😈",
        "😈 Devil attack — teri soul targeted — figuratively 💀⚡",
        "😈 Beta devil ne kaha — teri aukat nahi — aur devil galat nahi hota 🔥😂",
        "😈 DEVIL STORM OVER — teri side: scorched earth 💀😈",
        "😈 Bhai devil ke rules simple hain — tu follow nahi kiya 🔥⚡",
        "😈 Devil raid — teri position compromised — retreat 💀😂",
        "😈 Beta DEVIL mein aake rota mat — khud aaya tha 🔥😈",
        "😈 DEVIL WAVE — teri har defence erased 💀⚡",
        "😈 Bhai devil ka favorite — log jo khud ko smart samjhte hain — tu 🔥😂",
        "😈 Devil mode DONE — check teri condition 💀😈",
        "😈 Beta devil ne aaj tujhe yaadgaar bana diya — wrong reasons se 🔥⚡",
        ]

        karma_texts = [
           "☯️ Karma aaya — teri sab harkat ka hisaab ho raha hai 🔥💀",
        "☯️ Beta karma kisi ki nahi sunta — teri bhi nahi 😂⚡",
        "☯️ KARMA STRIKE — tune jo kiya woh teri taraf wapas aaya 🔥😈",
        "☯️ Bhai karma judge nahi karta — deliver karta hai 💀😂",
        "☯️ Karma mode activate — teri sab galtiyan wapas aa rahi hain 🔥⚡",
        "☯️ Beta karma tujhe bhool nahi gaya — yaad rakha tha 😂💀",
        "☯️ KARMA DELIVERY — teri harkat ka package arrive ho gaya 🔥😈",
        "☯️ Bhai karma se koi nahi bachta — tu bhi nahi bachega 💀⚡",
        "☯️ Karma tujhe dhundh raha tha — dhundh liya 🔥😂",
        "☯️ Beta karma aata hai jab expect nahi karte — sun le 😂💀",
        "☯️ KARMA HITS DIFFERENT — teri sab cheez wapas 🔥⚡",
        "☯️ Bhai karma teri priority nahi thi — karma mein tu priority hai 😂💀",
        "☯️ Karma cycle complete — tune jo kiya tune hi bhoga 🔥😈",
        "☯️ Beta karma slow hota hai par sure hota hai — yeh sure tha 💀⚡",
        "☯️ KARMA CALL — teri line pe aa gaya 🔥😂",
        "☯️ Bhai karma mein koi error nahi — teri galti recorded thi 😂💀",
        "☯️ Karma teri taraf waapis — enjoy 🔥⚡",
        "☯️ Beta karma tera address jaanta tha 😂💀",
        "☯️ KARMA FINAL — teri poori account balance zero 🔥😈",
        "☯️ Bhai karma se lad nahi sakte — tu chhupa nahi karma se 💀⚡",
        "☯️ Karma strike — tune deserve kiya — mila 🔥😂",
        "☯️ Beta karma ko excuse nahi deta — sirf result deta hai 😂💀",
        "☯️ KARMA STORM — teri sab beizzati aaj ekatha aayi 🔥⚡",
        "☯️ Bhai karma tujhse behtar account maintain karta hai 😂💀",
        "☯️ Karma mein tera account — overdraft mein hai 🔥😈",
        "☯️ Beta karma ki speed teri speed se faster hai 💀⚡",
        "☯️ KARMA BLAST — teri sab cheezon ka hisaab 🔥😂",
        "☯️ Bhai karma ko pata tha tune kya kiya — sab record mein hai 😂💀",
        "☯️ Karma kisi pe bhi nahi rulta — teri bhi nahi 🔥⚡",
        "☯️ Beta karma tera future nahi — karma tera present hai 😂💀",
        "☯️ KARMA INVOICE — teri sab galtiyon ka bill aa gaya 🔥😈",
        "☯️ Bhai karma mein koi discount nahi milta — full price pay 💀⚡",
        "☯️ Karma delivered — tune jo bheja wahi mila 🔥😂",
        "☯️ Beta karma tujhse kisi ki nahi sunta — seedha deliver karta hai 😂💀",
        "☯️ KARMA FULL CIRCLE — teri sab harkat ghumke teri hi taraf aayi 🔥⚡",
        "☯️ Bhai karma teri taraf — aur tu prepared nahi tha 😂💀",
        "☯️ Karma hit kiya — tujhe pata tha aayega — aaya 🔥😈",
        "☯️ Beta karma mein interest bhi hota hai — tera compound ho gaya 💀⚡",
        "☯️ KARMA COMPLETE — lesson mila? 🔥😂",
        "☯️ Bhai karma ne tujhe select kiya — deservingly 😂💀",
        "☯️ Karma tujhe yaad dila raha hai — tune kya kiya tha 🔥⚡",
        "☯️ Beta karma ki awaaz nahi hoti — par result loud hota hai 😂💀",
        "☯️ KARMA RESPONSE — teri har cheez ka seedha jawab 🔥😈",
        "☯️ Bhai karma ki list mein tu first position pe tha 💀⚡",
        "☯️ Karma tujhe bhool nahi gaya — teri galti note thi 🔥😂",
        "☯️ Beta karma aur tu — aaj inka meetup schedule tha 😂💀",
        "☯️ KARMA WRAP UP — teri life lesson: yeh tha 🔥⚡",
        "☯️ Bhai karma ne apna kaam kiya — efficient tha 😂💀",
        "☯️ Karma strike final — teri sab cheez balanced ho gayi — zero pe 🔥😈",
        "☯️ Beta karma yaad rakhna — abhi bhi teri account open hai ☯️😂",
        ]

        doom_texts = [
            "💀 DOOM activated — teri poori existence on countdown 🔥😈",
        "💀 Beta doom aaya — tera timer start ho gaya 😂⚡",
        "💀 DOOM STRIKE — teri poori defense wiped 🔥😈",
        "💀 Bhai doom se koi nahi bachta — teri bhi date aane wali thi 😂💀",
        "💀 Doom mode — teri sab cheez: scheduled for deletion 🔥⚡",
        "💀 Beta doom tera waqt dekh ke aaya — perfect timing 😂😈",
        "💀 DOOM RAID — teri poori squad: doomed 🔥💀",
        "💀 Bhai doom pe haath lagaya — yeh result expect karna chahiye tha 😂⚡",
        "💀 Doom finale — teri poori story: ended 🔥😈",
        "💀 Beta doom ki awaaz sunna nahi chahte log — teri aa gayi 😂💀",
        "💀 DOOM COMPLETE — teri sab cheez: finished 🔥⚡",
        "💀 Bhai doom tujhse pehle plan kar ke aaya tha 😂😈",
        "💀 Doom level CRITICAL — teri situation: hopeless 🔥💀",
        "💀 Beta doom ne tujhe select kiya — teri achievement nahi 😂⚡",
        "💀 DOOM COUNTDOWN — teri sab cheez: 3... 2... 1... done 🔥😈",
        "💀 Bhai doom mein rasta ek hi hota hai — neeche 😂💀",
        "💀 Doom activated — teri poori future: uncertain 🔥⚡",
        "💀 Beta doom ki language — teri samajh nahi aati — result aata hai 😂😈",
        "💀 DOOM FINAL — teri poori team: gone 🔥💀",
        "💀 Bhai doom aur tu — aaj ka meetup tera worst tha 😂⚡",
        "💀 Doom mode — tera har step: tracked 🔥😈",
        "💀 Beta doom ne teri position: permanent zero confirm ki 😂💀",
        "💀 DOOM RAIN — teri har cheez: destroyed 🔥⚡",
        "💀 Bhai doom mein mercy nahi hoti — teri request: denied 😂😈",
        "💀 Doom strike — teri sab galtiyan: collected 🔥💀",
        "💀 Beta doom clock — teri ticking: started 😂⚡",
        "💀 DOOM WAVE — teri poori defense: overwhelmed 🔥😈",
        "💀 Bhai doom ki speed mein teri situation resolve ho gayi — badly 😂💀",
        "💀 Doom verdict — teri case: closed — against you 🔥⚡",
        "💀 Beta doom se pehle sun: teri galti — doom aaya 😂😈",
        "💀 DOOM ARRIVAL — teri poori day ruined 🔥💀",
        "💀 Bhai doom ne tujhe apna project bana liya 😂⚡",
        "💀 Doom mode final — teri sab cheez: ash 🔥😈",
        "💀 Beta doom ki ek khasiyat — woh aata zaroor hai 😂💀",
        "💀 DOOM EXECUTION — teri poori plan: failed 🔥⚡",
        "💀 Bhai doom tera number leke aaya tha — mila 😂😈",
        "💀 Doom level MAX — teri recovery: impossible 🔥💀",
        "💀 Beta doom ki taraf se ek gift — teri haari 😂⚡",
        "💀 DOOM COMPLETE CYCLE — teri poori existence reset 🔥😈",
        "💀 Bhai doom tujhse better hai — wait nahi karta 😂💀",
        "💀 Doom mode — teri sab cheez: compromised 🔥⚡",
        "💀 Beta DOOM aur tu — tujhe jeetna tha par doom ka hi naam hai 😂😈",
        "💀 DOOM FINAL WAVE — teri sab: erased 🔥💀",
        "💀 Bhai doom ne tujhe memorable bana diya — galat reasons se 😂⚡",
        "💀 Doom activated final time — teri countdown: zero 🔥😈",
        "💀 Beta DOOM se seekhna tha — tujhe nahi tha pata ab hai 😂💀",
        "💀 DOOM OVER — teri side: collapsed — mine: standing 🔥⚡",
        "💀 Bhai doom ne tera chapter likh diya — R.I.P. chapter 😂😈",
        "💀 Doom final message — tujhe yaad rahega — sahi reasons se nahi 🔥💀",
        "💀 Beta DOOM complete — check teri condition — yahi tha 😂⚡",
        ]

        # ─── GAME TEXTS (Menu10) ──────────────────────────────────────────────

        truth_texts = [
            "Tumhara sabse bada secret kya hai jo kisi ko nahi pata? 🤫",
            "Kisi pe crush tha jo ab dost hai? 😳",
            "Kabhi kisi ki baat repeat ki thi jo confidence mein batai gayi thi? 😬",
            "Woh kaun hai jis par sabse zyada trust karte ho? ❤️",
            "Life mein sabse bada regret kya hai? 💭",
            "Kabhi class ya office se bina bataye bhaage ho? 😂",
            "Tumhari sabse embarrassing memory kya hai? 😳",
            "Kabhi kisi ko jhooth bol ke escape kiya hai? 🤥",
            "Tumhara sabse bada fear kya hai? 😨",
            "Kabhi kisi se pyaar kiya hai jo tumhe pata nahi? 💔",
            "Tumhari life ka best decision kya tha? ✅",
            "Kabhi kisi ko ghost kiya hai? 👻",
            "Tumhara sabse bada achievement kya hai? 🏆",
            "Kabhi kisi ko 'I love you' bola hai jhooth mein? 💀",
            "Tumhari sabse badi weakness kya hai? 😅",
            "Kabhi kisi ka trust todna pada hai? 💔",
            "Tumhari favourite memory kya hai? 📸",
            "Kabhi kisi ko dekh ke jealous feel kiya hai? 😤",
            "Tumhara sabse bada dream kya hai? 🌟",
            "Kabhi kisi ki feelings hurt kari hai? 😢",
            "Tumhari sabse badi strength kya hai? 💪",
            "Kabhi kisi ko forgive kiya hai jo worth nahi tha? 🙏",
            "Tumhara worst date experience kya tha? 😬",
            "Kabhi kisi ko block kiya hai without reason? 🚫",
            "Tumhari guilty pleasure kya hai? 🍫",
            "Kabhi kisi se jealous hoke galat kiya hai? 😤",
            "Tumhara favourite childhood memory kya hai? 🧸",
            "Kabhi kisi ko sacrifice kiya hai apne liye? 🥺",
            "Tumhari life ki best advice kya hai? 💡",
            "Kabhi apne best friend se jhooth bola hai? 🤥"
        ]

        dare_texts = [
            "Apni maa ko call kar ke bol — 'Main tujhse pyaar karta hoon' 📞❤️",
            "Apni sabse embarrassing photo share kar group mein 📸😹",
            "Kisi bhi friend ko abhi message kar — 'Bhai mujhe pata chal gaya' — aur reaction dekho 😈",
            "10 seconds ke liye khud se hi baat karo — loud 🗣️",
            "Abhi ek push-up kar aur photo bhejo 💪",
            "Apne crush ko 'Hi' bol — screenshot bhejo 😳",
            "Khud ki roast karo ek paragraph mein — seriously 😂",
            "Apna phone wallpaper change karo kisi funny photo mein 📱",
            "5 random logo ko 'I love you' message karo 💌",
            "Apni last seen status pe kuch funny likho 📝",
            "Kisi bhi group mein 'Main pagal hoon' bolo 🤪",
            "Apna profile pic change karo kisi meme se 🖼️",
            "Apne best friend ko call karo aur kuch funny bolo 📞",
            "Apni gallery se koi embarrassing photo share karo 📸",
            "Kisi random person ko compliment do 🌹",
            "Apne parents ko 'I love you' bolo ❤️",
            "Kisi bhi chat mein 'I am the best' bolo 😎",
            "Apna phone number kisi stranger ko do 📱",
            "Kisi ko 'You are amazing' bol kar photo bhejo 💖",
            "Apni life ka sabse embarrassing story share karo 📖",
            "Kisi ko 'Mujhe tumse pyaar hai' bol kar block karo 💀",
            "Apni bio mein kuch weird likho 📝",
            "Kisi bhi group mein 'Main aaj gussa hoon' bolo 😤",
            "Apne crush ko 'Hi' bol kar screenshot bhejo 😳",
            "Kisi ko 'You are my hero' bolo 🦸",
            "Apni last seen story mein kuch funny daalo 📱",
            "Kisi bhi chat mein 'Main bhagwan hoon' bolo 😂",
            "Apne best friend ko 'Main teri maa hoon' bolo 🤣",
            "Kisi random person ko 'You are beautiful' bolo 💕",
            "Apni life ki best memory share karo 📸"
        ]

        situation_texts = [
            "Agar tumhe 1 crore mil jaye toh kya karoge? 💰",
            "Agar tum 1 din invisible ho sakte ho toh kya karoge? 👻",
            "Agar tumhe ek wish mil jaye toh kya maangoge? ✨",
            "Agar tum president ban jao toh kya change karoge? 🏛️",
            "Agar tumhe time travel karna hai toh kahan jaoge? ⏳",
            "Agar tumhe 3 wishes mil jaye toh kya maangoge? 🌟",
            "Agar tum superpower choose kar sakte ho toh kya? 🦸",
            "Agar tumhe ek book likhni hai toh kya likhoge? 📖",
            "Agar tum famous ho jao toh kya karoge? 🌟",
            "Agar tumhe ek din kuch bhi karne ko mile toh kya karoge? 🎉",
            "Agar tumhe ek country choose karni hai toh kaunsi? 🌍",
            "Agar tumhe ek language seekhni hai toh kaunsi? 🗣️",
            "Agar tum apna naam change kar sakte ho toh kya rakhenge? 📛",
            "Agar tumhe apni life 1 word mein describe karni hai toh kya? 💬",
            "Agar tumhe ek famous personality se milna hai toh kaun? 🌟",
            "Agar tumhe 1 din life free ho toh kya karoge? 🎈",
            "Agar tumhe apni life ka best moment choose karna hai toh kya? 📸",
            "Agar tumhe ek skill seekhni hai toh kaunsi? 🎯",
            "Agar tumhe apni life ka worst moment choose karna hai toh kya? 😢",
            "Agar tumhe ek adventure karna hai toh kya? 🏔️",
            "Agar tumhe apni life change karni hai toh kya change karoge? 🔄",
            "Agar tumhe ek dream choose karna hai toh kya? 💭",
            "Agar tumhe apni life ka best decision choose karna hai toh kya? ✅",
            "Agar tumhe ek challenge choose karna hai toh kya? 🏆",
            "Agar tumhe apni life ka best friend choose karna hai toh kaun? 🤝",
            "Agar tumhe apni life ka worst decision choose karna hai toh kya? ❌",
            "Agar tumhe ek goal choose karna hai toh kya? 🎯",
            "Agar tumhe apni life ka best memory choose karna hai toh kya? 📸",
            "Agar tumhe apni life ka worst memory choose karna hai toh kya? 😢",
            "Agar tumhe apni life ka best achievement choose karna hai toh kya? 🏆"
        ]

        # ─── QUIZ TEXTS ────────────────────────────────────────────────────────

        quiz_texts = [
            {"q": "IIT JEE mein kaunsi book sabse important hai?", "a": "HC Verma"},
            {"q": "Physics mein 'g' ki value kya hai?", "a": "9.8"},
            {"q": "Formula E = mc² kisne diya?", "a": "Einstein"},
            {"q": "IIT ka full form kya hai?", "a": "Indian Institute of Technology"},
            {"q": "JEE ka full form kya hai?", "a": "Joint Entrance Examination"},
            {"q": "Physics mein SI unit of force kya hai?", "a": "Newton"},
            {"q": "Chemistry mein H2O kya hai?", "a": "Water"},
            {"q": "Maths mein 'pi' ki value kya hai?", "a": "3.14"},
            {"q": "Biology mein human body mein kitna water hai?", "a": "70%"},
            {"q": "IIT mein admission kaunsi exam se hota hai?", "a": "JEE Advanced"},
            {"q": "NEET ka full form kya hai?", "a": "National Eligibility cum Entrance Test"},
            {"q": "Human body mein kitna blood hai?", "a": "5 liters"},
            {"q": "Heart ka function kya hai?", "a": "Blood pump"},
            {"q": "Brain ka weight kitna hai?", "a": "1.4 kg"},
            {"q": "Biology mein DNA ka full form kya hai?", "a": "Deoxyribonucleic Acid"},
            {"q": "Human eye mein kitne colors dikhte hain?", "a": "10 million"},
            {"q": "Body mein kitne bones hain?", "a": "206"},
            {"q": "Blood group kaunse type ke hote hain?", "a": "A, B, AB, O"},
            {"q": "NEET mein kitne questions hote hain?", "a": "200"},
            {"q": "MBBS ka full form kya hai?", "a": "Bachelor of Medicine and Bachelor of Surgery"},
            {"q": "Earth ka sabse bada ocean kaunsa hai?", "a": "Pacific Ocean"},
            {"q": "World ka sabse lamba river kaunsa hai?", "a": "Nile River"},
            {"q": "Human body mein sabse bada organ kaunsa hai?", "a": "Skin"},
            {"q": "Universe ka sabse bada planet kaunsa hai?", "a": "Jupiter"},
            {"q": "Light ki speed kya hai?", "a": "3x10^8 m/s"},
            {"q": "Earth ka sabse ooncha mountain kaunsa hai?", "a": "Mount Everest"},
            {"q": "World mein sabse zyada population wala country kaunsa hai?", "a": "India"},
            {"q": "Computer ka brain kaunsa hai?", "a": "CPU"},
            {"q": "Mobile OS kaunse hain?", "a": "Android, iOS"},
            {"q": "World ka sabse bada desert kaunsa hai?", "a": "Sahara Desert"}
        ]

        # ─── RIDDLE TEXTS ──────────────────────────────────────────────────────

        riddle_texts = [
            {"q": "Main hoon jo andar aata hai par bahar nahi jaata. Main hoon jo har insaan ke paas hai. Main kya hoon?", "a": "Sans (Breath)"},
            {"q": "Main hoon jo duniya mein sabse bada hai, par main kisi ko dikhta nahi. Main kya hoon?", "a": "Pyaar (Love)"},
            {"q": "Main hoon jo haath mein aata hai par pakda nahi jaata. Main kya hoon?", "a": "Pani (Water)"},
            {"q": "Main hoon jo har insaan ko dikhta hai par koi dekh nahi sakta. Main kya hoon?", "a": "Andhera (Darkness)"},
            {"q": "Main hoon jo kabhi nahi rukta, kabhi nahi thakta. Main kya hoon?", "a": "Samay (Time)"},
            {"q": "Main hoon jo duniya mein sabse tez hai, par main kisi ko dikhta nahi. Main kya hoon?", "a": "Vichar (Thought)"},
            {"q": "Main hoon jo andar hota hai par bahar nahi. Main kya hoon?", "a": "Dil (Heart)"},
            {"q": "Main hoon jo har insaan ke paas hai par koi use nahi karta. Main kya hoon?", "a": "Dimag (Brain)"},
            {"q": "Main hoon jo kabhi nahi sota, kabhi nahi thakta. Main kya hoon?", "a": "Aankh (Eye)"},
            {"q": "Main hoon jo har insaan ki madad karta hai par koi use nahi dekhta. Main kya hoon?", "a": "Hawa (Air)"},
            {"q": "Main hoon jo duniya mein sabse chhota hai, par sab se bada kaam karta hoon. Main kya hoon?", "a": "Beej (Seed)"},
            {"q": "Main hoon jo kabhi nahi marta, kabhi nahi hota. Main kya hoon?", "a": "Atma (Soul)"},
            {"q": "The person who makes it doesn't need it. The person who buys it doesn't use it. The person who uses it doesn't know they're using it. What is it?", "a": "coffin"},
        ]
            
                    
        # ─── FUN TEXTS (Joke, Fact, Compliment, Quotes) ──────────────────────

        joke_list = [
            "Main apni life mein itna positive hoon... ki blood group bhi B+ hai! 😂",
            "Teacher: Kal absent kyun the? Student: Sir, mujhe bukhar tha. Teacher: Proof? Student: Aaj aa gaya na! 😹",
            "Santa: Main ghar ke bahar khada hun. Banta: Andar aa jao. Santa: Andar wala bhi main hoon! 🤣",
            "Meri girlfriend ne kaha — tujhse better koi nahi. Phir chali gayi. Better koi mila hoga shayad 😂",
            "Doctor: Patient ko hawa ki zaroorat hai. Nurse: Kya karein? Doctor: Fan on karo. Nurse: Ceiling se pakad ke? 😹",
            "Ghar mein sabse zyada kaam mera — internet chalaana! 😂",
            "Padhai karo beta future bright hoga. Maine padhi — future gaya andhera mein. 😂",
            "Wo bolti hai 'I need space' — main bola ठीक है, NASA se contact karo! 😂",
            "Mera wifi itna slow hai ke circle of life bhi nahi chalta 🐢",
            "Main sochta hoon kal se gym jaunga... kal kab aata hai? 🤔",
            "Mummy ka 2 minute aur Maggi ka 2 minute kabhi same nahi hote",
            "Aaj kal log 'seen' karke itna attitude dikhate hain, jaise message nahi loan approve kar rahe ho",
            "Meri life itni private hai ki mujhe khud next update ka pata nahi hota 🤡 ",
            "Mere jokes pe sirf do log haste hain... main aur meri overconfidence 🤣",
            "Log bolte hain Be yourself... phir judge bhi wahi log karte hain",
            "Life ne itne twists diye hain ki Google Maps bhi rerouting kar de",
        ]

        fact_list = [
            "🧠 Insaan ka dimag 75% paani se bana hai!",
            "🐙 Octopus ke teen dil hote hain!",
            "🌙 Chand par mobile signal nahi hai — par WiFi aata hai ek satellite se! (Future plan 😂)",
            "🍯 Sahi tarike se rakha hua honey kabhi kharab nahi hota!",
            "⚡ Bijli ka ek bolt 5 times zyada garam hota hai sun ki surface se!",
            "🦈 Shark insaan se zyada purana hai — dinasors se bhi pehle!",
            "👁️ Insaan ki aankh 10 million rangon ko differentiate kar sakti hai!",
            "🐝 Ek machhar ek second mein 600 baar apne pankh hilata hai!",
            "🦒 Giraffe ki tongue 20 inches lambi hoti hai!",
            "🐧 Penguins ek dusre ko pehchanne ke liye unique calls use karte hain!"
            "🚀 Space mein awaaz travel nahi karti, kyunki wahan hawa nahi hoti.",
            "👅 Har insaan ki tongue print fingerprints ki tarah unique hoti hai.",
            "🦒 Giraffe apni 21-inch lambi tongue se kaan saaf kar sakta hai.",
            "⚡ Lightning ka temperature Suraj ki surface se bhi zyada hota hai",
            "🌍 Har second Earth par lagbhag 100 lightning strikes hoti hain.",
            "🐌 Snail 3 saal tak so sakta hai (kuch species mein).",
            "🧊 Garam paani kuch conditions mein thande paani se jaldi jam sakta hai (Mpemba effect).",
            "👀 Insaan ka brain ulta image dekhta hai aur use seedha process karta hai.",
            "🍌 Banana technically ek berry hai, lekin strawberry nahi.",
            "🦘 Kangaroo peeche ki taraf chal nahi sakta.",
            "🐧 Penguins propose karne ke liye apne partner ko chhota sa pathar gift karte hain (kuch species mein).",
            "💀 Human body mein itni blood vessels hoti hain ki unhe line mein jodo to lagbhag 100,000 km lambi ho jaayengi.",
            "🌌 Hum raat ko jo kuch stars dekhte hain, unki light kai saal pehle nikli hoti hai.",
            "🐝 Bees insaanon ke chehre pehchaan sakti hain.",
        ]

        compliment_list = [
            "Bhai tu bahut positive energy rakhta hai — seriously 🌟",
            "Teri thinking bahut alag hai — creative hai tu 🧠✨",
            "Tu jo bhi karta hai dil se karta hai — yeh rare hai ❤️",
            "Teri sense of humor? Top tier 😂👑",
            "Tujhse baat karna genuinely enjoyable hota hai 🗣️✨",
            "Tu ek natural leader hai — log tujhe follow karte hain 👑",
            "Teri mehnat dekh ke lagta hai, success teri waiting hai 💪",
            "Teri smile contagious hai — sabko khushi deti hai 😊",
            "Tu bahut strong insaan hai — sab handle kar leta hai 💪",
            "Teri vibe bohot positive hai — tere saath time acha lagta hai ✨",
            "You're one of a kind.",
            "Tumhari vibe alag hi level ki hai.",
            "You're effortlessly cool.",
            "Tum jahan hote ho, wahan energy aa jaati hai.",
            "You make everything look easy.",
            "Tumhari personality hi alag hai.",
            "You're genuinely impressive.",
            "Tumhare ideas hamesha unique hote hain.",
            "You're unforgettable.",
            "Tum confidence ka perfect example ho.",
            "Built different. 💯",
            "Aura speaks louder than words.",
            "You're the main character.",
            "Tumhari smile mood fix kar deti hai.",
            "You make people feel comfortable.",
            "You're naturally adorable.",
            "Tumhari laugh contagious hai.",
            "You're a walking green flag.",
            "You're sunshine in human form.",
            "Tumhare saath time ka pata hi nahi chalta.",
            "You have the kindest heart.",
            "You're effortlessly charming.",
            "You make ordinary moments special.",
            "Standards on another level.",
            "Too real to be fake.",
            "Calm outside, dangerous inside.",
            "Rare people have this kind of aura.",
            "Silent, but unforgettable.",
            "Class never chases attention.",
            "You don't follow trends, you set them.",
            "You're the flex you don't even need to show.",
            "Some people have looks, you have presence.",
            "Your aura deserves its own fan club.",
            "You're proof that being real is attractive.",
            "Not everyone shines, but you do.",
            "You don't need attention, attention finds you.",
            "Legends don't introduce themselves.",
            "Your vibe is expensive.",
            "You're the kind of person people remember.",
            "You make confidence look natural. 😎",
        ]

        quote_list = [
            "💭 Sapne woh nahi jo sote waqt aate hain, sapne woh hain jo sone nahi dete. — APJ Abdul Kalam",
            "💭 'Mehnat karo itna ki luck ko bhi mauka mile tujhe dhundhne ka.' — Unknown",
            "💭 'Duniya ka sabse bada teacher: failure hai.' — Unknown",
            "💭 'Ek accha dost aur ek accha kitaab — dono hi tujhe better banate hain.' — Unknown",
            "💭 'Zindagi ek echo hai — jo bejhoge woh wapas aayega.' — Unknown",
            "💭 'Success is not final, failure is not fatal: it is the courage to continue that counts.' — Churchill",
            "💭 'The only way to do great work is to love what you do.' — Steve Jobs",
            "💭 'In the middle of difficulty lies opportunity.' — Einstein",
            "💭 'Believe you can and you're halfway there.' — Theodore Roosevelt",
            "💭 'The best time to plant a tree was 20 years ago. The second best time is now.' — Chinese Proverb"
            "💭 People's lives don't end when they die, it ends when they lose faith. — Itachi Uchiha",
            "💭 Wake up to reality. Nothing ever goes as planned in this world. — Madara Uchiha",
            "💭 Those who break the rules are trash, but those who abandon their friends are worse than trash. — Kakashi Hatake",
            "💭 When people are protecting something truly precious, they truly become strong. — Haku",
            "💭 A lesson without pain is meaningless. — Edward Elric",
            "💭 A person grows up when they're able to overcome hardships. — Jiraiya",
            "💭 Power comes in response to a need, not a desire. — Goku",
            "💭 If you don't take risks, you can't create a future. — Monkey D. Luffy",
            "💭 The world isn't perfect, but it's there for us. — Roy Mustang",
            "💭 Fear is not evil. It tells you your weakness. — Gildarts Clive",
            "💭 The moment you think of giving up, think of the reason why you held on so long. — Natsu Dragneel",
            "💭 Hard work is worthless for those that don't believe in themselves. — Naruto Uzumaki",
            "💭 The difference between the novice and the master is that the master has failed more times than the novice has tried. — Koro-sensei",
            "💭 To know sorrow is not terrifying. What is terrifying is to know you can't go back to happiness. — Matsumoto Rangiku",
            "💭 Whatever you lose, you'll find it again. But what you throw away you'll never get back. — Kenshin Himura",
            "💭 Success is not final, failure is not fatal: it is the courage to continue that counts. — Winston Churchill",
            "💭 The only way to do great work is to love what you do. — Steve Jobs",
            "💭 Stay hungry, stay foolish. — Steve Jobs",
            "💭 Your time is limited, so don't waste it living someone else's life. — Steve Jobs",
            "💭 The future belongs to those who believe in the beauty of their dreams. — Eleanor Roosevelt",
            "💭 Be yourself; everyone else is already taken. — Oscar Wilde",
            "💭 It always seems impossible until it's done. — Nelson Mandela",
            "💭 Dream big and dare to fail. — Norman Vaughan",
            "💭 Do what you can, with what you have, where you are. — Theodore Roosevelt",
            "💭 Believe you can and you're halfway there. — Theodore Roosevelt",
            "💭 The best way to predict the future is to create it. — Peter Drucker",
            "💭 Discipline is choosing between what you want now and what you want most.",
            "💭 Don't watch the clock; do what it does. Keep going. — Sam Levenson",
            "💭 The journey of a thousand miles begins with one step. — Lao Tzu",
            "💭 Fall seven times, stand up eight. — Japanese Proverb",
            "💭 Action is the foundational key to all success. — Pablo Picasso",
            "💭 Work hard in silence, let success make the noise.",
            "💭 Great things never come from comfort zones.",
            "💭 Small steps every day lead to big results.",
            "💭 Consistency beats motivation.",
            "💭 Discipline creates freedom.",
            "💭 Your only competition is the person you were yesterday.",
            "💭 Never let success get to your head or failure get to your heart.",
            "💭 A calm mind is a powerful weapon.",
            "💭 Pressure creates diamonds.",
        ]

        # ─── LOAD/SAVE FUNCTIONS (unchanged) ───
        def load_admins():
            try:
                if not os.path.isfile(ADMINS_FILE):
                    return set()
                with open(ADMINS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return {int(x) for x in data} if isinstance(data, list) else set()
            except:
                return set()

        def save_admins():
            try:
                with open(ADMINS_FILE, "w", encoding="utf-8") as f:
                    json.dump(sorted(user_bot.admins), f, indent=2)
            except:
                pass

        def load_notes():
            try:
                if not os.path.isfile(NOTES_FILE):
                    return {}
                with open(NOTES_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                return {int(k): str(v) for k, v in raw.items() if isinstance(raw, dict)}
            except:
                return {}

        def save_notes():
            try:
                with open(NOTES_FILE, "w", encoding="utf-8") as f:
                    json.dump(user_bot.notes, f, ensure_ascii=False, indent=2)
            except:
                pass

        def load_banner():
            try:
                if not os.path.isfile(BANNER_FILE):
                    return None
                with open(BANNER_FILE, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                if ":" not in raw:
                    return None
                chat, msg = raw.split(":", 1)
                return (int(chat), int(msg))
            except:
                return None

        def save_banner():
            try:
                if not user_bot.menu_banner_msg:
                    if os.path.isfile(BANNER_FILE):
                        os.remove(BANNER_FILE)
                    return
                with open(BANNER_FILE, "w", encoding="utf-8") as f:
                    f.write(f"{user_bot.menu_banner_msg[0]}:{user_bot.menu_banner_msg[1]}")
            except:
                pass

        def load_common_spam():
            try:
                if not os.path.isfile(COMMON_SPAM_FILE):
                    return []
                with open(COMMON_SPAM_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return [str(x) for x in data] if isinstance(data, list) else []
            except:
                return []

        def save_common_spam():
            try:
                with open(COMMON_SPAM_FILE, "w", encoding="utf-8") as f:
                    json.dump(user_bot.spam_texts, f, ensure_ascii=False, indent=2)
            except:
                pass

        user_bot.admins = load_admins()
        user_bot.notes = load_notes()
        user_bot.menu_banner_msg = load_banner()
        user_bot.spam_texts = load_common_spam()

        # ─── FLOOD-SAFE SEND ───
        async def safe_send(chat, text, reply_to=None, retries=3):
            for attempt in range(retries):
                try:
                    return await user_bot.send_message(chat, text, reply_to=reply_to)
                except FloodWaitError as fw:
                    await asyncio.sleep(fw.seconds + 1)
                    continue
                except Exception:
                    await asyncio.sleep(1)
            return None

        async def safe_edit(event, text):
            try:
                return await event.edit(text)
            except FloodWaitError as fw:
                await asyncio.sleep(fw.seconds + 1)
                try:
                    return await event.edit(text)
                except:
                    try:
                        return await event.reply(text)
                    except:
                        return
            except MessageNotModifiedError:
                pass
            except Exception:
                try:
                    return await event.reply(text)
                except:
                    return

        async def get_targets(event, arg=""):
            targets = set()
            if event.is_reply:
                try:
                    r = await event.get_reply_message()
                    if r and r.sender_id:
                        targets.add(int(r.sender_id))
                except:
                    pass
            if arg:
                for part in arg.strip().split():
                    if part.isdigit():
                        targets.add(int(part))
                    else:
                        try:
                            ent = await user_bot.get_entity(part)
                            if ent and hasattr(ent, "id"):
                                targets.add(int(ent.id))
                        except:
                            pass
            try:
                me2 = await user_bot.get_me()
                targets.discard(me2.id)
            except:
                pass
            return targets

        def is_admin(uid):
            return uid in OWNER_IDS or uid in user_bot.admins

        # ─── NC LOOP ───
        async def nc_loop(chat_id, lang, text):
            if lang == "hindi":
                patterns = HINDINC_PATTERNS
            elif lang == "urdu":
                patterns = URDU_PATTERNS
            elif lang == "bengali":
                patterns = BENGALI_PATTERNS
            elif lang == "bihari":
                patterns = BIHARI_PATTERNS
            elif lang == "english":
                patterns = ENGLISH_PATTERNS
            elif lang == "emoji":
                patterns = None
            else:
                return

            i = 0
            while True:
                if not user_bot.NC_STATE.get("active", False):
                    break
                try:
                    if lang == "emoji":
                        emoji = EMOJI_NC_EMOJIS[i % len(EMOJI_NC_EMOJIS)]
                        new_title = EMOJI_NC_PATTERN.format(text=text, emoji=emoji)
                    else:
                        pattern = patterns[i % len(patterns)]
                        new_title = pattern.format(text=text)
                    try:
                        await user_bot(functions.channels.EditTitleRequest(channel=chat_id, title=new_title))
                    except Exception:
                        try:
                            await user_bot(functions.messages.EditChatTitleRequest(chat_id=chat_id, title=new_title))
                        except Exception:
                            pass
                    i += 1
                    await asyncio.sleep(1.5)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print(f"NC loop error: {e}")
                    await asyncio.sleep(2)

        # ─── COMMAND REGISTRY ───
        commands = {}

        def register_cmd(name, needs_reply=False, group_only=False):
            def decorator(func):
                key = name.lower().strip()
                commands[key] = {
                    "func": func,
                    "needs_reply": needs_reply,
                    "group_only": group_only,
                }
                return func
            return decorator

        # ─── OWNER-ONLY COMMANDS ───
        owner_only_commands = {
            "addtext", "edittext", "deltext", "cleartext",
            "spraydelay", "addadmin", "deladmin"
        }

        # ─── FREE & PREMIUM COMMANDS LISTS ───
        # These will be used in the dispatcher
        FREE_COMMANDS = {
            "start", "login", "logout", "menu", "menu1", "menu2", "menu3", "menu4",
            "menu5", "menu6", "menu7", "menu8", "menu9", "menu10", "menu11",
            "ping", "status", "afk", "premium", "buy", "utr", "cancelbuy",
            "coin", "dice", "flip", "roll", "rps", "ttt", "ttt_move",
            "truth", "dare", "situation", "joke", "fact", "compliment",
            "quote", "riddle", "quiz", "8ball",
            "broadcast", "listusers", "purnjanam", "addadmin", "deladmin", "admins",
            "giftpremium", "approve", "reject", "revoke",
            "mute", "unmute", "gmute", "gunmute", "mutelist", "lock", "unlock",
            "purge", "throw", "addbots", "autotag", "stopautotag",
            "reply", "sreply", "rr", "srr", "flag", "sflag", "hrr", "shrr",
            "replygod", "sgod", "stopcustomraid",
            "spray", "dspray",
            "antidel", "watchspam", "unwatchspam", "watchlist",
            "ar", "sar", "react", "unreact", "reactlist",
            "notesadd", "noteslist", "notesdelete",
            "tts", "qrcode", "fancy", "style", "emoji", "calc", "weather",
            "ip", "short", "info", "music", "dmusic",
            "shayariraid", "sshayariraid", "rizzraid", "srizzraid",
            "pickupraid", "spickupraid", "romanceraid", "sromanceraid",
            "trollraid", "strollraid", "ragebaitraid", "sragebaitraid",
            "roastraid", "sroastraid",
            "attackraid", "sattackraid", "warraid", "swarraid",
            "savageraid", "ssavageraid", "ultraraid", "sultraraid",
            "shameraid", "sshameraid", "dissraid", "sdissraid",
            "devilraid", "sdevilraid", "karmaraid", "skarmaraid",
            "doomraid", "sdoomraid",
            "echo", "send", "tag", "copy", "normal", "banner", "rembanner", "nc",
            "deathgod", "sdeathgod",
            "studmeter", "looks", "gay", "lesbian", "straight", "bi", "trans",
            "simp", "chad", "friendly", "rizz", "iq", "stupidmeter",
            "sigma", "pookie", "baddie", "bestfrnd", "marriage", "divorce",
            "prem_toggle", "prem_status", "prem_block", "prem_unblock", "premcmds"
        }

        PREMIUM_ONLY_COMMANDS = {
            "customraid", "multispray", "addtext", "edittext", "deltext", "cleartext",
            "listtexts", "tspray", "rspray", "countspray", "spraydelay",
            "encrypt", "decrypt", "sha1", "sha512", "sysinfo", "timer",
            "randname", "randcolor", "wordgame", "boxtext", "bubble", "strike",
            "spoiler", "mirror", "flip_text", "tinytext", "square_text", "clap",
            "snake", "shout", "mock", "alternating", "spaceit", "removespaces",
            "titlecase", "roman", "octal", "bmi", "age", "prime", "factorial",
            "fibonacci", "square", "table", "percentage", "countdown", "ascii",
            "nato", "palindrome", "vowels", "wordfreq", "charcount", "lettercount",
            "charinfo", "typing"
        }

        # ======================================================================
        #                             MENUS
        # ======================================================================

        @register_cmd("menu")
        async def cmd_menu(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║            ✦ ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐔𝐒𝐄𝐑𝐁𝐎𝐓 ✦             ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║                                                              ║\n"
                "║  👑 Owner  : ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️                          ║\n"
                "║  📦 Commands: 500+                                          ║\n"
                "║  🔥 Prefix  : `.` (Dot)                                    ║\n"
                "║                                                              ║\n"
                "║  ────〔 📖 𝐌𝐀𝐈𝐍 𝐌𝐄𝐍𝐔 〕────                            ║\n"
                "║                                                              ║\n"
                "║  📌 `.menu1` → 👑 Admin, 🔇 Mute, 🧹 Group, 🏷️ Auto Tag   ║\n"
                "║  📌 `.menu2` → ⚔️ Raid Engine (Original)                   ║\n"
                "║  📌 `.menu3` → 💣 Spam, 📝 Text, ☠️ Deathgod              ║\n"
                "║  📌 `.menu4` → 🛡️ Protection & ❤️ Auto                   ║\n"
                "║  📌 `.menu5` → 🛠️ Tools & 🎵 Music & 📝 Echo              ║\n"
                "║  📌 `.menu6` → 💎 Premium Features                         ║\n"
                "║  📌 `.menu7` → 📊 Fun Meters                               ║\n"
                "║  📌 `.menu8` → 🎭 FUN RAIDS                                ║\n"
                "║  📌 `.menu9` → ⚔️ NON-ABUSIVE RAIDS                        ║\n"
                "║  📌 `.menu10`→ 🎮 GAMES & FUN                             ║\n"
                "║  📌 `.menu11`→ 🛠️ UTILITY & FUN COMMANDS                 ║\n"
                "║                                                              ║\n"
                "║  💡 Use `.cmds` for complete command list.                  ║\n"
                "║                                                              ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)
            if user_bot.menu_banner_msg:
                chat_id2, msg_id = user_bot.menu_banner_msg
                try:
                    msg = await user_bot.get_messages(chat_id2, ids=msg_id)
                    await user_bot.send_file(
                        event.chat_id,
                        file=msg.media,
                        caption="⚡ **⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️ 𝐄ɴᴛᴇʀs** ❤️‍🔥"
                    )
                except:
                    pass

        # Menu1 to Menu5 unchanged (keep your original ones)
        @register_cmd("menu1")
        async def cmd_menu1(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║      👑 𝐀𝐃𝐌𝐈𝐍 • 🔇 𝐌𝐔𝐓𝐄 • 🧹 𝐆𝐑𝐎𝐔𝐏 • 🏷️ 𝐀𝐔𝐓𝐎 𝐓𝐀𝐆    ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 👑 𝐀𝐃𝐌𝐈𝐍 〕───┐                                   ║\n"
                "║  │  `.admins` → View all admins                             ║\n"
                "║  │  `.addadmin @user` (or reply) → Make admin               ║\n"
                "║  │  `.deladmin @user` (or reply) → Remove admin             ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🔇 𝐌𝐔𝐓𝐄 & 𝐑𝐄𝐒𝐓𝐑𝐈𝐂𝐓 〕───┐                   ║\n"
                "║  │  `.mute @user` → Local mute                              ║\n"
                "║  │  `.unmute @user` → Local unmute                          ║\n"
                "║  │  `.gmute @user` → Global mute                            ║\n"
                "║  │  `.gunmute @user` → Global unmute                        ║\n"
                "║  │  `.mutelist` → Check mute status                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🧹 𝐆𝐑𝐎𝐔𝐏 𝐌𝐎𝐃 〕───┐                           ║\n"
                "║  │  `.lock` → Lock group messages                           ║\n"
                "║  │  `.unlock` → Unlock group                               ║\n"
                "║  │  `.purge <count>` → Delete N messages (max 200)          ║\n"
                "║  │  `.throw @user` → Kick user                              ║\n"
                "║  │  `.addbots <n>` → Add N bots from list                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🏷️ 𝐀𝐔𝐓𝐎 𝐓𝐀𝐆 〕───┐                            ║\n"
                "║  │  `.autotag` → Tag all members one by one                ║\n"
                "║  │  `.stopautotag` → Stop auto tag                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu2")
        async def cmd_menu2(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║                   ⚔️ 𝐑𝐀𝐈𝐃 𝐄𝐍𝐆𝐈𝐍𝐄                      ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 💬 𝐑𝐄𝐏𝐋𝐘 𝐑𝐀𝐈𝐃 〕───┐                          ║\n"
                "║  │  `.reply @user` → Start reply raid                       ║\n"
                "║  │  `.sreply @user` → Stop reply raid                       ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🤣 𝐑𝐑 𝐑𝐀𝐈𝐃 (Reply + React) 〕───┐              ║\n"
                "║  │  `.rr @user` → Start RR raid                            ║\n"
                "║  │  `.srr @user` → Stop RR raid                            ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🚩 𝐅𝐋𝐀𝐆 𝐑𝐀𝐈𝐃 〕───┐                          ║\n"
                "║  │  `.flag @user` → Start flag raid                         ║\n"
                "║  │  `.sflag @user` → Stop flag raid                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 💗 𝐇𝐄𝐀𝐑𝐓 𝐑𝐀𝐈𝐃 〕───┐                          ║\n"
                "║  │  `.hrr @user` → Start heart raid                         ║\n"
                "║  │  `.shrr @user` → Stop heart raid                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 😈 𝐆𝐎𝐃 𝐑𝐀𝐈𝐃 (4 replies) 〕───┐                 ║\n"
                "║  │  `.replygod @user` → Start god raid                      ║\n"
                "║  │  `.sgod @user` → Stop god raid                           ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🎯 𝐂𝐔𝐒𝐓𝐎𝐌 𝐑𝐀𝐈𝐃 〕───┐                        ║\n"
                "║  │  `.customraid <text> <count>` (reply to user)            ║\n"
                "║  │  `.stopcustomraid @user` → Stop                          ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  💡 For Fun Raids, use `.menu8`                            ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu3")
        async def cmd_menu3(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║           💣 𝐒𝐏𝐀𝐌 & 📝 𝐓𝐄𝐗𝐓 & ☠️ 𝐃𝐄𝐀𝐓𝐇𝐆𝐎𝐃          ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 💣 𝐒𝐏𝐀𝐌 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 〕───┐                    ║\n"
                "║  │  `.spray <text>` or `.spray <count> <text>` → spam       ║\n"
                "║  │  `.dspray` → Stop any spray                              ║\n"
                "║  │  `.tspray <num>` → Spam saved text (from .listtexts)     ║\n"
                "║  │  `.rspray` → Random saved text spam                      ║\n"
                "║  │  `.multispray <count>` → Rotate all saved texts          ║\n"
                "║  │  `.countspray <n> <text>` → Exactly N times              ║\n"
                "║  │  `.spraydelay <sec>` → Adjust speed (owner only)         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 📝 𝐓𝐄𝐗𝐓 𝐌𝐀𝐍𝐀𝐆𝐄𝐑 (Owner only) 〕───┐         ║\n"
                "║  │  `.addtext <text>` → Save a text                         ║\n"
                "║  │  `.listtexts` → Show all saved texts                     ║\n"
                "║  │  `.edittext <num> <new>` → Edit a text                   ║\n"
                "║  │  `.deltext <num>` → Delete a text                        ║\n"
                "║  │  `.cleartext confirm` → Delete all texts                 ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 ☠️ 𝐃𝐄𝐀𝐓𝐇𝐆𝐎𝐃 〕───┐                           ║\n"
                "║  │  `.deathgod <count>` → Spam from Deathgod list           ║\n"
                "║  │  `.sdeathgod` → Stop Deathgod                            ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu4")
        async def cmd_menu4(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║  🛡️ 𝐏𝐑𝐎𝐓𝐄𝐂𝐓𝐈𝐎𝐍 & 🖼️ 𝐆𝐑𝐎𝐔𝐏 𝐏𝐅𝐏 & ❤️ 𝐀𝐔𝐓𝐎  ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 🛡️ 𝐀𝐍𝐓𝐈-𝐃𝐄𝐋𝐄𝐓𝐄 〕───┐                       ║\n"
                "║  │  `.antidel on` → Enable protection                       ║\n"
                "║  │  `.antidel off` → Disable                                ║\n"
                "║  │  `.antidel` → Show status                                ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 👁️ 𝐖𝐀𝐓𝐂𝐇𝐒𝐏𝐀𝐌 〕───┐                         ║\n"
                "║  │  `.watchspam @user <limit> <sec>`                        ║\n"
                "║  │  `.unwatchspam @user` → Remove watch                     ║\n"
                "║  │  `.unwatchspam` → Remove all in chat                     ║\n"
                "║  │  `.watchlist` → Show active watches                      ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🖼️ 𝐆𝐑𝐎𝐔𝐏 𝐏𝐅𝐏 𝐂𝐇𝐀𝐍𝐆𝐄𝐑 〕───┐                ║\n"
                "║  │  `.setgpfp` (reply with image) → Set as group PFP        ║\n"
                "║  │  `.addgpfp` → Add image to pool                          ║\n"
                "║  │  `.listgpfp` → Show pool                                 ║\n"
                "║  │  `.autogpfp <sec>` → Auto-rotate every N seconds         ║\n"
                "║  │  `.stopgpfp` → Stop rotation                             ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 ❤️ 𝐀𝐔𝐓𝐎 𝐒𝐘𝐒𝐓𝐄𝐌 〕───┐                       ║\n"
                "║  │  `.ar <emoji>` → Auto-react to your own msgs             ║\n"
                "║  │  `.sar` → Disable auto-react                             ║\n"
                "║  │  `.react @user <emoji>` → React to target's msgs         ║\n"
                "║  │  `.unreact @user` → Remove target                        ║\n"
                "║  │  `.reactlist` → Show all targets                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu5")
        async def cmd_menu5(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║  🛠️ 𝐓𝐎𝐎𝐋𝐒 & 🎵 𝐌𝐔𝐒𝐈𝐂 & 📝 𝐄𝐂𝐇𝐎 & 🧠 𝐍𝐎𝐓𝐄𝐒  ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 🛠️ 𝐓𝐎𝐎𝐋𝐒 〕───┐                                ║\n"
                "║  │  `.tts <text> [lang]` → Text-to-Speech                   ║\n"
                "║  │  `.qrcode <text>` → Generate QR code                     ║\n"
                "║  │  `.fancy <text>` → Fancy text styles                     ║\n"
                "║  │  `.style <text>` → Bold/Italic/Mono                      ║\n"
                "║  │  `.emoji <text>` → Add random emojis                     ║\n"
                "║  │  `.calc <expr>` → Calculate                              ║\n"
                "║  │  `.weather <city>` → Weather info                        ║\n"
                "║  │  `.ip <ip>` → IP location                                ║\n"
                "║  │  `.short <url>` → Shorten URL                            ║\n"
                "║  │  `.info @user` → User info                               ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 📝 𝐄𝐂𝐇𝐎 〕───┐                                   ║\n"
                "║  │  `.echo <text>` → Echo the text back                     ║\n"
                "║  │  `.echo <count> <text>` → Echo N times                  ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🎵 𝐌𝐔𝐒𝐈𝐂 〕───┐                                ║\n"
                "║  │  `.music <song>` → Send as voice note                    ║\n"
                "║  │  `.dmusic <song>` → Download MP3 (320kbps)               ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🧠 𝐍𝐎𝐓𝐄𝐒 〕───┐                                ║\n"
                "║  │  `.notesadd <text>` → Save note                          ║\n"
                "║  │  `.noteslist` → View all notes                           ║\n"
                "║  │  `.notesdelete <id>` → Delete note                       ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 👑 𝐎𝐖𝐍𝐄𝐑-𝐎𝐍𝐋𝐘 〕───┐                         ║\n"
                "║  │  `.spraydelay <sec>` → Adjust spray speed                ║\n"
                "║  │  `.addtext`, `.edittext`, `.deltext`, `.cleartext`       ║\n"
                "║  │  `.addadmin` & `.deladmin`                               ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🔓 𝐀𝐃𝐌𝐈𝐍-𝐀𝐂𝐂𝐄𝐒𝐒𝐈𝐁𝐋𝐄 〕───┐                ║\n"
                "║  │  `.nc set <lang> <text>` → Name Changer                  ║\n"
                "║  │  `.nc stop` → Stop Name Changer                          ║\n"
                "║  │  `.copy @user` → Clone user's profile                    ║\n"
                "║  │  `.normal` → Restore your original profile               ║\n"
                "║  │  `.banner` (reply with image) → Set menu banner          ║\n"
                "║  │  `.rembanner` → Remove banner                            ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        # ─── UPDATED MENU6 (Premium Features with Examples) ───
        @register_cmd("menu6")
        async def cmd_menu6(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║         💎 𝐏𝐑𝐄𝐌𝐈𝐔𝐌 𝐅𝐄𝐀𝐓𝐔𝐑𝐄𝐒 (Exclusive)            ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  🔹 `.typing` → Animated typing effect\n"
                "║     Ex: `.typing Hello world`\n\n"
                "║  🔹 `.encrypt` / `.decrypt` → Base64 encode/decode\n"
                "║     Ex: `.encrypt Hi` → `SGk=`\n\n"
                "║  🔹 `.sha1` / `.sha512` → Hash generation\n"
                "║     Ex: `.sha1 test`\n\n"
                "║  🔹 `.sysinfo` → System info\n"
                "║  🔹 `.timer 10` → Set a timer\n"
                "║  🔹 `.randname` → Random username\n"
                "║  🔹 `.randcolor` → Random hex color\n\n"
                "║  ✨ `.boxtext`, `.bubble`, `.strike`, `.spoiler`\n"
                "║  ✨ `.mirror`, `.flip_text`, `.tinytext`, `.square_text`\n"
                "║  ✨ `.clap`, `.snake`, `.shout`, `.mock`, `.alternating`\n"
                "║  ✨ `.spaceit`, `.removespaces`, `.titlecase`\n\n"
                "║  🔢 `.octal`, `.bmi`, `.age`, `.prime`, `.factorial`\n"
                "║  🔢 `.fibonacci`, `.square`, `.roman`, `.table`\n"
                "║  🔢 `.percentage`, `.countdown`, `.ascii`, `.nato`\n\n"
                "║  📝 `.palindrome`, `.vowels`, `.wordfreq`, `.charcount`\n"
                "║  📝 `.lettercount`, `.charinfo`, `.wordgame`, `.emoji2text`\n\n"
                "║  ⚔️ `.customraid` → Custom text raid\n"
                "║  ⚔️ `.multispray` → Rotate saved texts\n\n"
                "║  📝 `.addtext`, `.edittext`, `.deltext`, `.cleartext`\n"
                "║  📝 `.listtexts`, `.tspray`, `.rspray`, `.countspray`\n"
                "║  📝 `.spraydelay` → Adjust speed\n\n"
                "║  💡 **Premium Management:**\n"
                "║  👉 `.prem_toggle` → Turn Premium ON/OFF\n"
                "║  👉 `.prem_status` → Check status & blocked\n"
                "║  👉 `.prem_block <cmd>` → Block a command\n"
                "║  👉 `.prem_unblock <cmd>` → Unblock\n"
                "║  👉 `.premcmds` → List all premium commands\n\n"
                "║  📌 `.menu` → Main menu\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu7")
        async def cmd_menu7(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║            📊 FUN METERS                                    ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 📊 METERS 〕───┐\n"
                "║  │  `.studmeter @user` → Stud %                            ║\n"
                "║  │  `.looks @user` → Looks %                               ║\n"
                "║  │  `.gay @user` → Gay %                                   ║\n"
                "║  │  `.lesbian @user` → Lesbian %                           ║\n"
                "║  │  `.straight @user` → Straight %                         ║\n"
                "║  │  `.bi @user` → Bi %                                     ║\n"
                "║  │  `.trans @user` → Trans %                               ║\n"
                "║  │  `.simp @user` → Simp %                                 ║\n"
                "║  │  `.chad @user` → Chad %                                 ║\n"
                "║  │  `.friendly @user` → Friendly %                         ║\n"
                "║  │  `.rizz @user` → Rizz Meter (1-100)                    ║\n"
                "║  │  `.iq @user` → IQ Score (1-200)                        ║\n"
                "║  │  `.stupidmeter @user` → Stupid %                       ║\n"
                "║  │  `.sigma @user` → Sigma Meter %                        ║\n"
                "║  │  `.pookie @user` → Pookie Meter %                      ║\n"
                "║  │  `.baddie @user` → Baddie Meter %                      ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 💖 BEST FRIEND? 〕───┐\n"
                "║  │  `.bestfrnd @user` → Ask with poetic style & buttons    ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 💔 DIVORCE & 💍 MARRIAGE 〕───┐\n"
                "║  │  `.divorce @user` → Ask with Yes/No buttons             ║\n"
                "║  │  `.marriage @user` → Ask with Yes/No buttons            ║\n"
                "║  └───────────────────────────────┘\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu8")
        async def cmd_menu8(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║              🎭 FUN RAIDS                                   ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 📜 SHAYARI RAID 〕───┐                            ║\n"
                "║  │  `.shayariraid @user <count>`  → Start                   ║\n"
                "║  │  `.sshayariraid @user`          → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 💋 RIZZ RAID 〕───┐                                ║\n"
                "║  │  `.rizzraid @user <count>`      → Start                   ║\n"
                "║  │  `.srizzraid @user`             → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 💘 PICKUP RAID 〕───┐                              ║\n"
                "║  │  `.pickupraid @user <count>`   → Start                   ║\n"
                "║  │  `.spickupraid @user`          → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 ❤️ ROMANCE RAID 〕───┐                              ║\n"
                "║  │  `.romanceraid @user <count>`  → Start                   ║\n"
                "║  │  `.sromanceraid @user`         → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🤡 TROLL RAID 〕───┐                                ║\n"
                "║  │  `.trollraid @user <count>`     → Start                   ║\n"
                "║  │  `.strollraid @user`            → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 😤 RAGEBAIT RAID 〕───┐                              ║\n"
                "║  │  `.ragebaitraid @user <count>`  → Start                   ║\n"
                "║  │  `.sragebaitraid @user`         → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🔥 ROAST RAID 〕───┐                                ║\n"
                "║  │  `.roastraid @user <count>`     → Start                   ║\n"
                "║  │  `.sroastraid @user`            → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu9")
        async def cmd_menu9(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║         ⚔️ 𝗡𝗢𝗡-𝗔𝗕𝗨𝗦𝗜𝗩𝗘 𝗥𝗔𝗜𝗗𝗦  (𝟵 𝗧𝗬𝗣𝗘𝗦)          ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 ⚔️ ATTACK 〕───┐                                  ║\n"
                "║  │  `.attackraid @user <count>`  → Start                   ║\n"
                "║  │  `.sattackraid @user`         → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🏴‍☠️ WAR 〕───┐                                      ║\n"
                "║  │  `.warraid @user <count>`      → Start                   ║\n"
                "║  │  `.swarraid @user`             → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 😈 SAVAGE 〕───┐                                    ║\n"
                "║  │  `.savageraid @user <count>`   → Start                   ║\n"
                "║  │  `.ssavageraid @user`          → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 ⚡ ULTRA 〕───┐                                    ║\n"
                "║  │  `.ultraraid @user <count>`   → Start                   ║\n"
                "║  │  `.sultraraid @user`           → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 😤 SHAME 〕───┐                                    ║\n"
                "║  │  `.shameraid @user <count>`   → Start                   ║\n"
                "║  │  `.sshameraid @user`          → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🎤 DISS 〕───┐                                      ║\n"
                "║  │  `.dissraid @user <count>`    → Start                   ║\n"
                "║  │  `.sdissraid @user`           → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 😈 DEVIL 〕───┐                                    ║\n"
                "║  │  `.devilraid @user <count>`   → Start                   ║\n"
                "║  │  `.sdevilraid @user`          → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 ☯️ KARMA 〕───┐                                    ║\n"
                "║  │  `.karmaraid @user <count>`   → Start                   ║\n"
                "║  │  `.skarmaraid @user`           → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 💀 DOOM 〕───┐                                    ║\n"
                "║  │  `.doomraid @user <count>`    → Start                   ║\n"
                "║  │  `.sdoomraid @user`           → Stop                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu10")
        async def cmd_menu10(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║          🎮 𝗚𝗔𝗠𝗘𝗦 & 𝗙𝗨𝗡  (𝗠𝗘𝗡𝗨 𝟭𝟬)                   ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 🎱 TRUTH / DARE / SITUATION 〕───┐                ║\n"
                "║  │  `.truth`    → Random truth                             ║\n"
                "║  │  `.dare`     → Random dare                              ║\n"
                "║  │  `.situation`→ Random situation                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🧩 RIDDLE WITH TIMER 〕───┐                        ║\n"
                "║  │  `.riddle`   → Paheli with 60s timer                   ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 📚 QUIZ (JEE/NEET/GK) 〕───┐                      ║\n"
                "║  │  `.quiz`     → Random quiz with 60s timer              ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 ✂️ RPS (Rock-Paper-Scissors) 〕───┐              ║\n"
                "║  │  `.rps`      → Play with inline buttons                ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 ❌ Tic-Tac-Toe 〕───┐                              ║\n"
                "║  │  `.ttt`      → Start Tic-Tac-Toe with buttons          ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 🎲 DICE / FLIP 〕───┐                              ║\n"
                "║  │  `.dice`     → Roll a dice                             ║\n"
                "║  │  `.flip`     → Flip a coin                             ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  ┌───〔 😂 JOKE / FACT / COMPLIMENT / QUOTE 〕───┐        ║\n"
                "║  │  `.joke`     → Random joke                              ║\n"
                "║  │  `.fact`     → Interesting fact                         ║\n"
                "║  │  `.compliment`→ Random compliment                       ║\n"
                "║  │  `.quote`    → Inspirational quote                      ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu11")
        async def cmd_menu11(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║         🛠️ 𝐔𝐓𝐈𝐋𝐈𝐓𝐘 & 𝐅𝐔𝐍 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒                ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 🎲 RANDOM / SYSTEM 〕───┐                          ║\n"
                "║  │  `.coin` → Flip a coin                                   ║\n"
                "║  │  `.lucky` → Random lucky number                          ║\n"
                "║  │  `.roll` → Roll a dice (1-6 or custom)                  ║\n"
                "║  │  `.afk` → Set AFK status + auto-reply                   ║\n"
                "║  └────────────────────────────────────────┘                 ║\n"
                "║  ┌───〔 🔐 CRYPTO / HASH (Premium) 〕───┐                  ║\n"
                "║  │  `.encrypt`, `.decrypt`, `.sha1`, `.sha512`             ║\n"
                "║  └────────────────────────────────────────┘                 ║\n"
                "║  ┌───〔 ✨ TEXT EFFECTS (Premium) 〕───┐                    ║\n"
                "║  │  `.typing`, `.boxtext`, `.bubble`, `.strike`,           ║\n"
                "║  │  `.spoiler`, `.mirror`, `.flip_text`                    ║\n"
                "║  └────────────────────────────────────────┘                 ║\n"
                "║  ┌───〔 🔍 STRING ANALYZE 〕───┐                          ║\n"
                "║  │  `.palindrome`, `.vowels`, `.wordfreq`, `.charcount`    ║\n"
                "║  │  `.lettercount`, `.charinfo`, `.wordgame`, `.emoji2text`║\n"
                "║  │  `.truncate`                                            ║\n"
                "║  └────────────────────────────────────────┘                 ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        # ─── NEW PREMIUM MANAGEMENT COMMANDS ────────────────────────────────

        @register_cmd("prem_toggle")
        async def cmd_prem_toggle(event, arg):
            user_id = event.sender_id
            if not await is_user_premium(user_id):
                return await safe_edit(event, "❌ You are not premium!")
            new_state = await toggle_premium(user_id)
            status = "🟢 ON" if new_state else "🔴 OFF"
            await safe_edit(event, f"✅ **Premium is now {status}**\n\n"
                                   f"• If OFF, all premium features are disabled.\n"
                                   f"• Use `.prem_toggle` again to change.")

        @register_cmd("prem_status")
        async def cmd_prem_status(event, arg):
            user_id = event.sender_id
            if not await is_user_premium(user_id):
                return await safe_edit(event, "❌ You are not premium!")
            async with premium_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT premium_active, expiry_date, blocked_commands FROM premium_users WHERE user_id = $1",
                    user_id
                )
            if not row:
                return await safe_edit(event, "❌ No premium data found.")
            expiry = row['expiry_date'].strftime('%d-%m-%Y %H:%M')
            active = "🟢 Active" if row['premium_active'] else "🔴 Inactive (Paused)"
            blocked = row['blocked_commands'] or []
            msg = f"✅ **Premium Status**\n━━━━━━━━━━━━━━━\n"
            msg += f"📅 Expiry: {expiry}\n"
            msg += f"📌 Status: {active}\n"
            msg += f"🚫 Blocked Commands: {len(blocked)}\n\n"
            if blocked:
                msg += "**Blocked:**\n" + "\n".join(f"• `{c}`" for c in blocked[:15])
            else:
                msg += "No commands blocked."
            msg += f"\n\n💡 `.prem_block <cmd>` → Block a command\n"
            msg += f"💡 `.prem_unblock <cmd>` → Unblock a command"
            await safe_edit(event, msg)

        @register_cmd("prem_block")
        async def cmd_prem_block(event, arg):
            user_id = event.sender_id
            if not await is_user_premium(user_id):
                return await safe_edit(event, "❌ You are not premium!")
            if not arg:
                return await safe_edit(event, "❌ Usage: `.prem_block <command>` (e.g., `.prem_block typing`)")
            cmd = arg.strip().lower()
            if cmd.startswith("."):
                cmd = cmd[1:]
            # Check if it's a valid premium command
            if cmd not in PREMIUM_ONLY_COMMANDS:
                return await safe_edit(event, f"❌ `{cmd}` is not a premium command.\n"
                                               f"Available: {', '.join(list(PREMIUM_ONLY_COMMANDS)[:10])}...")
            await add_blocked_command(user_id, cmd)
            await safe_edit(event, f"✅ `{cmd}` has been **BLOCKED**.\n\n"
                                   f"• You can unblock it with `.prem_unblock {cmd}`")

        @register_cmd("prem_unblock")
        async def cmd_prem_unblock(event, arg):
            user_id = event.sender_id
            if not await is_user_premium(user_id):
                return await safe_edit(event, "❌ You are not premium!")
            if not arg:
                return await safe_edit(event, "❌ Usage: `.prem_unblock <command>`")
            cmd = arg.strip().lower()
            if cmd.startswith("."):
                cmd = cmd[1:]
            blocked = await get_blocked_commands(user_id)
            if cmd not in blocked:
                return await safe_edit(event, f"❌ `{cmd}` is not currently blocked.")
            await remove_blocked_command(user_id, cmd)
            await safe_edit(event, f"✅ `{cmd}` has been **UNBLOCKED**.\n\n"
                                   f"• You can now use this premium command again.")

        # ─── .premcmds (List all premium commands with usage) ──────────────

        @register_cmd("premcmds")
        async def cmd_premcmds(event, _):
            user_id = event.sender_id
            if not await is_user_premium(user_id):
                return await safe_edit(event, "❌ You are not premium!")
            
            premium_cmds = [
                ("typing", "Animated typing effect", ".typing Hello world"),
                ("encrypt", "Base64 encode", ".encrypt Hi"),
                ("decrypt", "Base64 decode", ".decrypt SGk="),
                ("sha1", "SHA-1 hash", ".sha1 test"),
                ("sha512", "SHA-512 hash", ".sha512 test"),
                ("sysinfo", "System info", ".sysinfo"),
                ("timer", "Set a timer", ".timer 10"),
                ("randname", "Random username", ".randname"),
                ("randcolor", "Random hex color", ".randcolor"),
                ("boxtext", "Box around text", ".boxtext Hello"),
                ("bubble", "Bubble text style", ".bubble Hi"),
                ("strike", "Strikethrough text", ".strike Hello"),
                ("spoiler", "Spoiler text", ".spoiler Secret"),
                ("mirror", "Mirror text", ".mirror Hello"),
                ("flip_text", "Flip text upside-down", ".flip_text Hello"),
                ("tinytext", "Tiny text", ".tinytext Hi"),
                ("square_text", "Square text style", ".square_text A"),
                ("clap", "Clap between words", ".clap Hello World"),
                ("snake", "Snake case", ".snake Hello World"),
                ("shout", "SHOUT text", ".shout hi"),
                ("mock", "Mocking text", ".mock hello"),
                ("alternating", "Alternating case", ".alternating hello"),
                ("spaceit", "Space between characters", ".spaceit Hi"),
                ("removespaces", "Remove all spaces", ".removespaces H e l l o"),
                ("titlecase", "Title case", ".titlecase hello world"),
                ("octal", "Convert to octal", ".octal 10"),
                ("bmi", "Calculate BMI", ".bmi 70 1.75"),
                ("age", "Calculate age", ".age 01-01-2000"),
                ("prime", "Check prime number", ".prime 7"),
                ("factorial", "Calculate factorial", ".factorial 5"),
                ("fibonacci", "Fibonacci series", ".fibonacci 5"),
                ("square", "Square of number", ".square 4"),
                ("roman", "Convert to Roman", ".roman 2024"),
                ("table", "Multiplication table", ".table 5"),
                ("percentage", "Calculate percentage", ".percentage 25 100"),
                ("countdown", "Countdown timer", ".countdown 5"),
                ("ascii", "ASCII codes", ".ascii Hi"),
                ("nato", "NATO phonetic", ".nato Hi"),
                ("palindrome", "Check palindrome", ".palindrome radar"),
                ("vowels", "Count vowels", ".vowels Hello"),
                ("wordfreq", "Word frequency", ".wordfreq hi hi bye"),
                ("charcount", "Character count", ".charcount Hello"),
                ("lettercount", "Letter count", ".lettercount H3llo"),
                ("charinfo", "Character info", ".charinfo A"),
                ("wordgame", "Word jumble game", ".wordgame hello"),
                ("emoji2text", "Emoji to text", ".emoji2text 😊"),
                ("customraid", "Custom text raid", ".customraid Hi 5 (reply to user)"),
                ("multispray", "Rotate saved texts", ".multispray 10"),
                ("addtext", "Add spam text", ".addtext Hello"),
                ("edittext", "Edit spam text", ".edittext 1 New"),
                ("deltext", "Delete spam text", ".deltext 1"),
                ("cleartext", "Clear all texts", ".cleartext confirm"),
                ("listtexts", "List saved texts", ".listtexts"),
                ("tspray", "Spam specific text", ".tspray 1"),
                ("rspray", "Random saved text spam", ".rspray"),
                ("countspray", "Exact count spam", ".countspray 5 Hi"),
                ("spraydelay", "Adjust spam speed", ".spraydelay 0.2"),
            ]

            msg = "💎 **Premium Commands**\n━━━━━━━━━━━━━━━\n"
            msg += "`.prem_toggle` → Toggle premium ON/OFF\n"
            msg += "`.prem_status` → Check status & blocked\n"
            msg += "`.prem_block` → Block a command\n"
            msg += "`.prem_unblock` → Unblock a command\n"
            msg += "━━━━━━━━━━━━━━━\n\n"

            for cmd, desc, usage in premium_cmds:
                msg += f"• **{cmd}** – {desc}\n  `{usage}`\n\n"

            msg += "━━━━━━━━━━━━━━━\n"
            msg += "📌 `.menu` → Main menu"

            # Split into chunks if too long (Telegram limit ~4096)
            if len(msg) > 4000:
                # Send in parts
                parts = [msg[i:i+4000] for i in range(0, len(msg), 4000)]
                await safe_edit(event, parts[0])
                for part in parts[1:]:
                    await event.reply(part)
            else:
                await safe_edit(event, msg)

        # ======================================================================
        #                      FUN METERS (Menu7) + BESTFRIEND/MARRIAGE/DIVORCE
        # ======================================================================

        @register_cmd("studmeter")
        async def cmd_studmeter(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"📊 **Stud Meter for {name}**\n\n💪 Stud Level: {percent}%\n"
                if percent >= 90: msg += "🔥 You're a legend! 💪"
                elif percent >= 70: msg += "🌟 Pretty studly! 😎"
                elif percent >= 50: msg += "👍 Not bad, keep it up!"
                else: msg += "😅 Maybe try some gym? 😂"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("looks")
        async def cmd_looks(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"👀 **Looks Meter for {name}**\n\n🌟 Looks: {percent}%\n"
                if percent >= 90: msg += "💖 You're a masterpiece! 😍"
                elif percent >= 70: msg += "💕 Very attractive! 😊"
                elif percent >= 50: msg += "😐 Average, but charming!"
                else: msg += "😬 Maybe try a new style? 😅"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("gay")
        async def cmd_gay(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🏳️‍🌈 **Gay Percentage for {name}**\n\n🌈 Gayness: {percent}%\n"
                if percent >= 90: msg += "🏳️‍🌈🌈 Totally gay! 😂"
                elif percent >= 70: msg += "🌈 Pretty gay! 😏"
                elif percent >= 50: msg += "🤔 Half and half!"
                else: msg += "💪 Straight as an arrow!"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("lesbian")
        async def cmd_lesbian(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"👩‍❤️‍👩 **Lesbian Percentage for {name}**\n\n💖 Lesbianness: {percent}%\n"
                if percent >= 90: msg += "👩‍❤️‍💋‍👩 Total lesbian! 😍"
                elif percent >= 70: msg += "💕 Very gay! 😊"
                elif percent >= 50: msg += "🤷‍♀️ Could go either way!"
                else: msg += "💁‍♀️ Straight as a ruler!"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("straight")
        async def cmd_straight(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"💪 **Straight Percentage for {name}**\n\n📏 Straightness: {percent}%\n"
                if percent >= 90: msg += "🏆 Straight as a ruler! 📏"
                elif percent >= 70: msg += "😎 Pretty straight! 😏"
                elif percent >= 50: msg += "🤷 Could be flexible!"
                else: msg += "🌈 Maybe try exploring? 😉"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("bi")
        async def cmd_bi(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"💜 **Bi Percentage for {name}**\n\n💕 Bisexuality: {percent}%\n"
                if percent >= 90: msg += "💜💙 Totally bi! 😍"
                elif percent >= 70: msg += "💕 Quite bi-curious! 😏"
                elif percent >= 50: msg += "🤷‍♂️ Could go both ways!"
                else: msg += "💁 Mostly straight!"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("trans")
        async def cmd_trans(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🏳️‍⚧️ **Trans Pride for {name}**\n\n💖 Transness: {percent}%\n"
                if percent >= 90: msg += "🌟 You're a beautiful soul! 💕"
                elif percent >= 70: msg += "💜 Very strong! 😊"
                elif percent >= 50: msg += "🤔 Exploring your identity?"
                else: msg += "💁 You're you, that's enough!"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("simp")
        async def cmd_simp(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🫠 **Simp Meter for {name}**\n\n😩 Simp Level: {percent}%\n"
                if percent >= 90: msg += "💀 Ultimate Simp! 😂"
                elif percent >= 70: msg += "💔 Down bad! 😭"
                elif percent >= 50: msg += "😅 Slightly simping!"
                else: msg += "👑 You're a chad! 😎"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("chad")
        async def cmd_chad(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🗿 **Chad Meter for {name}**\n\n💪 Chad Level: {percent}%\n"
                if percent >= 90: msg += "🔥 Sigma Chad! 😎"
                elif percent >= 70: msg += "💪 Pretty chad! 💪"
                elif percent >= 50: msg += "🤷 Neutral vibes!"
                else: msg += "🥶 Maybe a bit of a beta? 😉"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("friendly")
        async def cmd_friendly(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🤗 **Friendliness Meter for {name}**\n\n😊 Friendly: {percent}%\n"
                if percent >= 90: msg += "🌈 You're a ray of sunshine! ☀️"
                elif percent >= 70: msg += "💖 Very approachable! 😊"
                elif percent >= 50: msg += "😐 Pretty neutral!"
                else: msg += "😤 Maybe need to smile more?"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("rizz")
        async def cmd_rizz(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"💋 **Rizz Meter for {name}**\n\n🔥 Rizz Level: {percent}%\n"
                if percent >= 90: msg += "🌹 Absolute rizz god! 😏"
                elif percent >= 70: msg += "💕 Smooth talker! 😉"
                elif percent >= 50: msg += "😅 Average rizz!"
                else: msg += "🤡 Need some rizz lessons? 😂"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("iq")
        async def cmd_iq(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                score = random.randint(50, 200)
                msg = f"🧠 **IQ Score for {name}**\n\n📊 IQ: {score}\n"
                if score >= 180: msg += "🌟 Genius level! 🤯"
                elif score >= 140: msg += "💡 Very smart! 🧐"
                elif score >= 100: msg += "👍 Average, keep learning!"
                else: msg += "😬 Maybe read a book? 😅"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("stupidmeter")
        async def cmd_stupidmeter(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🤪 **Stupid Meter for {name}**\n\n🧠 Stupidity Level: {percent}%\n"
                if percent >= 90: msg += "🤡 Absolute clown! 😂"
                elif percent >= 70: msg += "😬 Pretty dumb! 😅"
                elif percent >= 50: msg += "🤷 Not too bright!"
                else: msg += "🧠 Actually smart! 😎"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("sigma")
        async def cmd_sigma(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🐺 **Sigma Meter for {name}**\n\n💀 Sigma Level: {percent}%\n"
                if percent >= 90: msg += "🔥 Ultimate Sigma! 🐺"
                elif percent >= 70: msg += "💪 Sigma grinding! 💀"
                elif percent >= 50: msg += "😐 Sigma in training!"
                else: msg += "😅 Beta vibes! 🐑"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("pookie")
        async def cmd_pookie(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🧸 **Pookie Meter for {name}**\n\n🎀 Pookie Level: {percent}%\n"
                if percent >= 90: msg += "💕 Ultimate Pookie! 🧸"
                elif percent >= 70: msg += "🌸 So cute Pookie! 🎀"
                elif percent >= 50: msg += "😊 Average Pookie!"
                else: msg += "😤 Not Pookie enough! 💀"
                await safe_edit(event, msg)
            except: pass

        @register_cmd("baddie")
        async def cmd_baddie(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"💅 **Baddie Meter for {name}**\n\n👸 Baddie Level: {percent}%\n"
                if percent >= 90: msg += "🔥 Ultimate Baddie! 💅"
                elif percent >= 70: msg += "💋 Serving Baddie vibes! 👸"
                elif percent >= 50: msg += "😐 Average Baddie!"
                else: msg += "😬 Need to level up! 📈"
                await safe_edit(event, msg)
            except: pass

        # ─── BESTFRIEND, MARRIAGE, DIVORCE ───
        BESTFRIEND_SHAYARI = [
            "💖 *Dil ki baat kehni hai, sun lo meri jaan,*\n🌸 *Tum bin adhoori hai yeh dastaan.*\n💫 *Kya tum banogi/banoge meri/mera best friend?* 🤗",
            "🌟 *Tum ho meri khushi ka raaz,*\n🌺 *Tum bin jeena hai aawaaz.*\n🤗 *Kya tum best friend banogi/banoge?*",
        ]

        MARRIAGE_SHAYARI = [
            "💍 *Chand sitare sab hai gawah,*\n🌹 *Tum bin jeena hai saza.*\n💕 *Kya tum mujhse shaadi karogi/karoge?*",
            "💒 *Meri har dua mein tum ho shamil,*\n🌺 *Tum bin har khushi hai mushkil.*\n💞 *Shaadi karoge?*",
        ]

        DIVORCE_SHAYARI = [
            "💔 *Rishton ki dor hai kamzor,*\n🌪️ *Ab nahi sahega yeh dard-e-dil.*\n❓ *Kya tum talaq chahti ho/chahte ho?*",
            "😢 *Pyaar tha, par ab hai doori,*\n💔 *Nahi rahi ab koi majboori.*\n📜 *Talaq de do?*",
        ]

        @register_cmd("bestfrnd")
        async def cmd_bestfrnd(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                requester = await user_bot.get_me()
                requester_name = requester.first_name or "Someone"
                target_user = await user_bot.get_entity(uid)
                target_name = target_user.first_name or "Unknown"
                shayari = random.choice(BESTFRIEND_SHAYARI)
                final_msg = f"{shayari}\n\n**From:** {requester_name}\n**To:** {target_name}"
                buttons = [
                    [types.KeyboardButtonCallback("💖 Haan, zaroor!", f"bestfrnd_yes_{uid}_{event.sender_id}")],
                    [types.KeyboardButtonCallback("💔 Nahi, sorry!", f"bestfrnd_no_{uid}_{event.sender_id}")]
                ]
                await event.delete()
                await user_bot.send_message(event.chat_id, final_msg, buttons=buttons)
            except: pass

        @register_cmd("marriage")
        async def cmd_marriage(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                requester = await user_bot.get_me()
                requester_name = requester.first_name or "Someone"
                target_user = await user_bot.get_entity(uid)
                target_name = target_user.first_name or "Unknown"
                shayari = random.choice(MARRIAGE_SHAYARI)
                final_msg = f"{shayari}\n\n**From:** {requester_name}\n**To:** {target_name}"
                buttons = [
                    [types.KeyboardButtonCallback("💍 Yes", f"marriage_yes_{uid}_{event.sender_id}")],
                    [types.KeyboardButtonCallback("💔 No", f"marriage_no_{uid}_{event.sender_id}")]
                ]
                await event.delete()
                await user_bot.send_message(event.chat_id, final_msg, buttons=buttons)
            except: pass

        @register_cmd("divorce")
        async def cmd_divorce(event, arg):
            target = await get_targets(event, arg)
            if not target: return
            uid = next(iter(target))
            try:
                requester = await user_bot.get_me()
                requester_name = requester.first_name or "Someone"
                target_user = await user_bot.get_entity(uid)
                target_name = target_user.first_name or "Unknown"
                shayari = random.choice(DIVORCE_SHAYARI)
                final_msg = f"{shayari}\n\n**From:** {requester_name}\n**To:** {target_name}"
                buttons = [
                    [types.KeyboardButtonCallback("💔 Yes", f"divorce_yes_{uid}_{event.sender_id}")],
                    [types.KeyboardButtonCallback("💖 No, let's stay", f"divorce_no_{uid}_{event.sender_id}")]
                ]
                await event.delete()
                await user_bot.send_message(event.chat_id, final_msg, buttons=buttons)
            except: pass

        # ─── MERGED CALLBACK HANDLER (for Bestfriend, Marriage, Divorce, RPS, TTT) ───
        @user_bot.on(events.CallbackQuery)
        async def merged_callback(event):
            data = event.data.decode()
            clicker_id = event.sender_id

            # ---- Bestfriend / Marriage / Divorce ----
            if data.startswith("bestfrnd_") or data.startswith("marriage_") or data.startswith("divorce_"):
                parts = data.split("_")
                if len(parts) < 4:
                    await event.answer("Invalid request.", alert=True)
                    return
                action = parts[0]
                target_id = int(parts[2])
                requester_id = int(parts[3])
                if clicker_id != target_id:
                    await event.answer("❌ This question is not for you!", alert=True)
                    return
                try:
                    target = await user_bot.get_entity(target_id)
                    target_name = target.first_name or str(target_id)
                    requester = await user_bot.get_entity(requester_id)
                    requester_name = requester.first_name or str(requester_id)
                    if action == "bestfrnd_yes":
                        await event.edit(f"💖 **{target_name}** said **YES** to be best friend with **{requester_name}**! 🎉\n\nDosti zindabad! 🤗")
                        await event.answer("You accepted! 💖", alert=True)
                    elif action == "bestfrnd_no":
                        await event.edit(f"💔 **{target_name}** said **NO** to be best friend with **{requester_name}**. 😢\n\nMaybe next time! 💔")
                        await event.answer("You declined! 💔", alert=True)
                    elif action == "divorce_yes":
                        await event.edit(f"💔 **{target_name}** said **YES** to divorce from **{requester_name}**. 😢\n\nIt's over. 💔")
                        await event.answer("Divorce accepted! 💔", alert=True)
                    elif action == "divorce_no":
                        await event.edit(f"💖 **{target_name}** said **NO** to divorce from **{requester_name}**. ❤️\n\nLove wins! 💕")
                        await event.answer("Divorce rejected! ❤️", alert=True)
                    elif action == "marriage_yes":
                        await event.edit(f"💍 **{target_name}** said **YES** to marry **{requester_name}**! 🎉\n\nCongratulations! 💕💍")
                        await event.answer("Marriage accepted! 🎉", alert=True)
                    elif action == "marriage_no":
                        await event.edit(f"💔 **{target_name}** said **NO** to marry **{requester_name}**. 😢\n\nMaybe next time! 💔")
                        await event.answer("Marriage rejected! 💔", alert=True)
                except Exception as e:
                    await event.edit("❌ Error processing your request.")
                    print(f"Callback error: {e}")
                return

            # ---- RPS ----
            if data.startswith("rps_"):
                user_choice = data.split("_")[1]
                choices = {"rock":"🪨","paper":"📄","scissors":"✂️"}
                bot_choice = random.choice(["rock","paper","scissors"])
                wins = {"rock":"scissors","paper":"rock","scissors":"paper"}
                if user_choice == bot_choice:
                    result = "Draw!"
                elif wins[user_choice] == bot_choice:
                    result = "You Win!"
                else:
                    result = "Bot Wins!"
                await event.edit(f"👤 {choices[user_choice]} vs 🤖 {choices[bot_choice]}\n\n**{result}**")
                return

            # ---- Tic-Tac-Toe ----
            if data.startswith("ttt_"):
                parts = data.split("_")
                action = parts[1]
                chat = int(parts[2])
                if chat not in ttt_games:
                    await event.answer("Game expired", alert=True)
                    return
                game = ttt_games[chat]
                if action == "reset":
                    del ttt_games[chat]
                    await event.answer("New game!")
                    await user_bot.send_message(event.chat_id, ".ttt")
                    return
                if action == "move":
                    idx = int(parts[3])
                    if game["player"] != clicker_id:
                        await event.answer("Not your game!", alert=True)
                        return
                    if game["board"][idx] != " ":
                        await event.answer("Taken!", alert=True)
                        return
                    game["board"][idx] = game["turn"]
                    board = game["board"]
                    win = False
                    for i in range(3):
                        if board[i*3] == board[i*3+1] == board[i*3+2] != " ":
                            win = True
                    for i in range(3):
                        if board[i] == board[i+3] == board[i+6] != " ":
                            win = True
                    if board[0] == board[4] == board[8] != " " or board[2] == board[4] == board[6] != " ":
                        win = True
                    if win:
                        await event.edit(f"🏆 {game['turn']} Wins!")
                        del ttt_games[chat]
                        return
                    if " " not in board:
                        await event.edit("🤝 Draw!")
                        del ttt_games[chat]
                        return
                    game["turn"] = "O" if game["turn"] == "X" else "X"
                    # Re-send board
                    await send_ttt_board(event, chat)
                    return

        # ======================================================================
        #                      NEW UTILITY COMMANDS (50+)
        # ======================================================================

        @register_cmd("octal")
        async def cmd_octal(event, arg):
            if not arg or not arg.isdigit(): return
            await safe_edit(event, f"🔢 Octal of {arg}: `{oct(int(arg))}`")

        @register_cmd("bmi")
        async def cmd_bmi(event, arg):
            parts = arg.split()
            if len(parts) != 2: return
            try:
                w, h = float(parts[0]), float(parts[1])
                bmi = w/(h*h)
                cat = "Underweight" if bmi<18.5 else "Normal" if bmi<25 else "Overweight" if bmi<30 else "Obese"
                await safe_edit(event, f"📊 BMI: {bmi:.2f} ({cat})")
            except: pass

        @register_cmd("age")
        async def cmd_age(event, arg):
            if not arg: return
            try:
                dob = datetime.datetime.strptime(arg.strip(), "%d-%m-%Y")
                age = datetime.datetime.now().year - dob.year - ((datetime.datetime.now().month, datetime.datetime.now().day) < (dob.month, dob.day))
                await safe_edit(event, f"🎂 Age: **{age} years**")
            except: pass

        @register_cmd("prime")
        async def cmd_prime(event, arg):
            if not arg or not arg.isdigit(): return
            n = int(arg)
            if n<2: return await safe_edit(event, "Not prime")
            is_prime = all(n%i!=0 for i in range(2, int(math.sqrt(n))+1))
            await safe_edit(event, f"`{n}` is **{'Prime' if is_prime else 'Not Prime'}**")

        @register_cmd("factorial")
        async def cmd_factorial(event, arg):
            if not arg or not arg.isdigit(): return
            n = int(arg)
            if n>100: return await safe_edit(event, "Max 100")
            await safe_edit(event, f"{n}! = `{math.factorial(n)}`")

        @register_cmd("fibonacci")
        async def cmd_fibonacci(event, arg):
            if not arg or not arg.isdigit(): return
            n = int(arg)
            if n>50: return await safe_edit(event, "Max 50")
            a,b=0,1; seq=[]
            for _ in range(n): seq.append(str(a)); a,b=b,a+b
            await safe_edit(event, f"`{' → '.join(seq)}`")

        @register_cmd("square")
        async def cmd_square(event, arg):
            if not arg: return
            try: await safe_edit(event, f"`{float(arg)}² = {float(arg)**2}`")
            except: pass

        @register_cmd("roman")
        async def cmd_roman(event, arg):
            if not arg or not arg.isdigit(): return
            n = int(arg)
            if not 1 <= n <= 3999: return
            val = [1000,900,500,400,100,90,50,40,10,9,5,4,1]
            sym = ["M","CM","D","CD","C","XC","L","XL","X","IX","V","IV","I"]
            res=""
            for i,v in enumerate(val):
                while n>=v: res += sym[i]; n-=v
            await safe_edit(event, f"`{arg} → {res}`")

        @register_cmd("table")
        async def cmd_table(event, arg):
            if not arg: return
            try:
                n = int(arg)
                lines = [f"{n} × {i} = {n*i}" for i in range(1,11)]
                await safe_edit(event, "📊 Table of {n}\n" + "\n".join(lines))
            except: pass

        @register_cmd("percentage")
        async def cmd_percentage(event, arg):
            parts = arg.split()
            if len(parts) != 2: return
            try:
                obt, total = float(parts[0]), float(parts[1])
                await safe_edit(event, f"📈 {obt}/{total} = `{(obt/total)*100:.2f}%`")
            except: pass

        @register_cmd("countdown")
        async def cmd_countdown(event, arg):
            if not arg or not arg.isdigit(): return
            sec = int(arg)
            if sec>60: return await safe_edit(event, "Max 60s")
            msg = await safe_edit(event, f"⏳ {sec}s")
            for i in range(sec,0,-1): await asyncio.sleep(1); await msg.edit(f"⏳ {i}s")
            await msg.edit("⏰ Time's Up!")

        @register_cmd("ascii")
        async def cmd_ascii(event, arg):
            if not arg: return
            await safe_edit(event, f"🔣 `{' '.join(str(ord(c)) for c in arg[:50])}`")

        @register_cmd("nato")
        async def cmd_nato(event, arg):
            if not arg: return
            nato = {'a':'Alpha','b':'Bravo','c':'Charlie','d':'Delta','e':'Echo','f':'Foxtrot','g':'Golf','h':'Hotel','i':'India','j':'Juliett','k':'Kilo','l':'Lima','m':'Mike','n':'November','o':'Oscar','p':'Papa','q':'Quebec','r':'Romeo','s':'Sierra','t':'Tango','u':'Uniform','v':'Victor','w':'Whiskey','x':'Xray','y':'Yankee','z':'Zulu'}
            await safe_edit(event, f"📡 `{' '.join(nato.get(c.lower(), c) for c in arg[:30])}`")

        @register_cmd("palindrome")
        async def cmd_palindrome(event, arg):
            if not arg: return
            clean = "".join(c.lower() for c in arg if c.isalnum())
            await safe_edit(event, f"🔄 `{arg}` → **{'✅ Yes' if clean == clean[::-1] else '❌ No'}**")

        @register_cmd("vowels")
        async def cmd_vowels(event, arg):
            if not arg: return
            cnt = sum(1 for c in arg.lower() if c in "aeiou")
            await safe_edit(event, f"🔊 Vowels: **{cnt}**")

        @register_cmd("wordfreq")
        async def cmd_wordfreq(event, arg):
            if not arg: return
            freq = Counter(arg.lower().split())
            await safe_edit(event, "📊 " + "\n".join(f"{k}: {v}" for k,v in freq.most_common(5)))

        @register_cmd("charcount")
        async def cmd_charcount(event, arg):
            if not arg: return
            await safe_edit(event, f"🔢 Total: **{len(arg)}**")

        @register_cmd("lettercount")
        async def cmd_lettercount(event, arg):
            if not arg: return
            await safe_edit(event, f"🔤 Letters: **{sum(c.isalpha() for c in arg)}**")

        @register_cmd("charinfo")
        async def cmd_charinfo(event, arg):
            if not arg or len(arg)>1: return
            c = arg[0]
            await safe_edit(event, f"🔍 `{c}` U+{ord(c):04X} `{unicodedata.name(c, 'Unknown')}`")

        @register_cmd("titlecase")
        async def cmd_titlecase(event, arg):
            if not arg: return
            await safe_edit(event, f"`{arg.title()}`")

        @register_cmd("snake")
        async def cmd_snake(event, arg):
            if not arg: return
            await safe_edit(event, f"`{'_'.join(arg.split()).lower()}`")

        @register_cmd("shout")
        async def cmd_shout(event, arg):
            if not arg: return
            await safe_edit(event, f"📢 `{arg.upper()}!!!`")

        @register_cmd("mock")
        async def cmd_mock(event, arg):
            if not arg: return
            res = "".join(c.upper() if i%2==0 else c.lower() for i,c in enumerate(arg))
            await safe_edit(event, f"🧽 `{res}`")

        @register_cmd("alternating")
        async def cmd_alternating(event, arg):
            if not arg: return
            res = "".join(c.upper() if i%2==0 else c.lower() for i,c in enumerate(arg))
            await safe_edit(event, f"🔀 `{res}`")

        @register_cmd("spaceit")
        async def cmd_spaceit(event, arg):
            if not arg: return
            await safe_edit(event, f"`{' '.join(arg)}`")

        @register_cmd("removespaces")
        async def cmd_removespaces(event, arg):
            if not arg: return
            await safe_edit(event, f"`{arg.replace(' ', '')}`")

        @register_cmd("clap")
        async def cmd_clap(event, arg):
            if not arg: return
            await safe_edit(event, f"👏 `{' 👏 '.join(arg.split())}`")

        @register_cmd("mirror")
        async def cmd_mirror(event, arg):
            if not arg: return
            await safe_edit(event, f"🪞 `{arg[::-1]}`")

        @register_cmd("flip_text")
        async def cmd_flip_text(event, arg):
            if not arg: return
            flip = {"a":"ɐ","b":"q","c":"ɔ","d":"p","e":"ǝ","f":"ɟ","g":"ƃ","h":"ɥ","i":"ᴉ","j":"ɾ","k":"ʞ","l":"l","m":"ɯ","n":"u","o":"o","p":"d","q":"b","r":"ɹ","s":"s","t":"ʇ","u":"n","v":"ʌ","w":"ʍ","x":"x","y":"ʎ","z":"z"}
            res = "".join(flip.get(c.lower(), c) for c in arg[::-1])
            await safe_edit(event, f"🔄 `{res}`")

        @register_cmd("tinytext")
        async def cmd_tinytext(event, arg):
            if not arg: return
            tiny = {"a":"ᵃ","b":"ᵇ","c":"ᶜ","d":"ᵈ","e":"ᵉ","f":"ᶠ","g":"ᵍ","h":"ʰ","i":"ⁱ","j":"ʲ","k":"ᵏ","l":"ˡ","m":"ᵐ","n":"ⁿ","o":"ᵒ","p":"ᵖ","q":"ᑫ","r":"ʳ","s":"ˢ","t":"ᵗ","u":"ᵘ","v":"ᵛ","w":"ʷ","x":"ˣ","y":"ʸ","z":"ᶻ"}
            await safe_edit(event, f"🔡 `{''.join(tiny.get(c.lower(), c) for c in arg)}`")

        @register_cmd("bubble")
        async def cmd_bubble(event, arg):
            if not arg: return
            res = "".join(chr(0x24EA + ord(c) - ord('0')) if c.isdigit() else chr(0x1F170 + ord(c) - ord('A')) if c.isupper() else c for c in arg[:30])
            await safe_edit(event, f"🫧 `{res}`")

        @register_cmd("square_text")
        async def cmd_square_text(event, arg):
            if not arg: return
            res = "".join(chr(0x1F130 + ord(c) - ord('A')) if c.isupper() else c for c in arg[:30])
            await safe_edit(event, f"🟦 `{res}`")

        @register_cmd("boxtext")
        async def cmd_boxtext(event, arg):
            if not arg: return
            await safe_edit(event, f"┌{'─'*(len(arg)+2)}┐\n│ {arg} │\n└{'─'*(len(arg)+2)}┘")

        @register_cmd("strike")
        async def cmd_strike(event, arg):
            if not arg: return
            await safe_edit(event, f"✖️ `{''.join(c + '̶' for c in arg)}`")

        @register_cmd("spoiler")
        async def cmd_spoiler(event, arg):
            if not arg: return
            await safe_edit(event, f"🔞 ||`{arg}`||")

        @register_cmd("truncate")
        async def cmd_truncate(event, arg):
            parts = arg.split(maxsplit=1)
            if len(parts) != 2 or not parts[0].isdigit(): return
            n = int(parts[0])
            await safe_edit(event, f"`{parts[1][:n]}{'...' if len(parts[1])>n else ''}`")

        @register_cmd("emoji2text")
        async def cmd_emoji2text(event, arg):
            if not arg: return
            names = [unicodedata.name(c, c) for c in arg[:5]]
            await safe_edit(event, f"😀 `{' → '.join(names)}`")

        @register_cmd("wordgame")
        async def cmd_wordgame(event, arg):
            if not arg: return
            shuffled = "".join(random.sample(arg, len(arg)))
            await safe_edit(event, f"🧩 Guess: `{shuffled}` (Original: `{arg}`)")

        @register_cmd("encrypt")
        async def cmd_encrypt(event, arg):
            if not arg: return
            import base64
            await safe_edit(event, f"🔐 `{base64.b64encode(arg.encode()).decode()}`")

        @register_cmd("decrypt")
        async def cmd_decrypt(event, arg):
            if not arg: return
            import base64
            try: await safe_edit(event, f"🔓 `{base64.b64decode(arg).decode()}`")
            except: await safe_edit(event, "❌ Invalid Base64")

        @register_cmd("sha1")
        async def cmd_sha1(event, arg):
            if not arg: return
            await safe_edit(event, f"🔑 `{hashlib.sha1(arg.encode()).hexdigest()}`")

        @register_cmd("sha512")
        async def cmd_sha512(event, arg):
            if not arg: return
            await safe_edit(event, f"🔑 `{hashlib.sha512(arg.encode()).hexdigest()}`")

        @register_cmd("coin")
        async def cmd_coin(event, _):
            await safe_edit(event, f"🪙 **{random.choice(['Heads', 'Tails'])}**")

        @register_cmd("lucky")
        async def cmd_lucky(event, _):
            await safe_edit(event, f"🍀 `{random.randint(1,1000)}`")

        @register_cmd("roll")
        async def cmd_roll(event, arg):
            max_val = int(arg) if arg and arg.isdigit() and 2 <= int(arg) <= 100 else 6
            await safe_edit(event, f"🎲 `{random.randint(1, max_val)}`")

        @register_cmd("randname")
        async def cmd_randname(event, _):
            prefixes = ["Cool","Shadow","Mystic","Silent","Dark","Phoenix","Iron","Storm","Frost","Blaze"]
            suffixes = ["Wolf","Ninja","Knight","Ghost","Lord","Hunter","Fury","King","Queen","Fox"]
            await safe_edit(event, f"📛 `{random.choice(prefixes)}{random.choice(suffixes)}{random.randint(10,99)}`")

        @register_cmd("randcolor")
        async def cmd_randcolor(event, _):
            color = "#{:06x}".format(random.randint(0, 0xFFFFFF))
            await safe_edit(event, f"🎨 `{color}`")

        @register_cmd("timer")
        async def cmd_timer(event, arg):
            if not arg or not arg.isdigit(): return
            sec = int(arg)
            if sec>3600: return await safe_edit(event, "Max 1 hour")
            await safe_edit(event, f"⏳ Timer set for {sec}s")
            await asyncio.sleep(sec)
            await safe_edit(event, "🔔 Timer Done!")

        @register_cmd("sysinfo")
        async def cmd_sysinfo(event, _):
            await safe_edit(event, f"💻 {platform.system()} {platform.release()}\n🐍 Python {platform.python_version()}")

        @register_cmd("8ball")
        async def cmd_8ball(event, arg):
            if not arg: return
            answers = ["Yes", "Definitely", "Maybe", "Ask later", "No", "Very doubtful"]
            await safe_edit(event, f"🎱 {random.choice(answers)}")

        # ─── TYPING EFFECT ──────────────────────────────────────────────────────
        @register_cmd("typing")
        async def cmd_typing(event, arg):
            if not arg: return
            words = arg.split()
            chat = event.chat_id
            if chat in user_bot.typing_tasks:
                try: user_bot.typing_tasks[chat].cancel()
                except: pass
                user_bot.typing_tasks.pop(chat, None)
            msg = await safe_edit(event, "✍️...")
            full_text = ""
            async def type_effect():
                nonlocal full_text
                try:
                    for i, word in enumerate(words):
                        full_text = word if i==0 else full_text + " " + word
                        await user_bot.send_action(chat, "typing")
                        await msg.edit(f"✍️ **{full_text}**")
                        await asyncio.sleep(0.12)
                    await msg.edit(f"✨ **{full_text}**")
                except asyncio.CancelledError: pass
                finally: user_bot.typing_tasks.pop(chat, None)
            task = asyncio.create_task(type_effect())
            user_bot.typing_tasks[chat] = task

        # ─── AFK ──────────────────────────────────────────────────────────────────
        user_bot.afk_data = {}
        def get_afk_duration(user_id):
            if user_id not in user_bot.afk_data: return "Unknown"
            elapsed = int(time.time() - user_bot.afk_data[user_id]["time"])
            m, s = divmod(elapsed, 60); h, m = divmod(m, 60)
            return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

        @register_cmd("afk")
        async def cmd_afk(event, arg):
            user_id = event.sender_id
            reason = arg.strip() or "I'm AFK!"
            if user_id in user_bot.afk_data and user_bot.afk_data[user_id].get("is_afk", False):
                user_bot.afk_data[user_id]["is_afk"] = False
                await safe_edit(event, f"👋 Welcome back! (AFK for {get_afk_duration(user_id)})")
                return
            user_bot.afk_data[user_id] = {"is_afk": True, "message": reason, "time": time.time()}
            await safe_edit(event, f"🚶 AFK: {reason}")

        # ─── RPS (BUTTONS) ──────────────────────────────────────────────────────
        @register_cmd("rps")
        async def cmd_rps(event, arg):
            buttons = [
                [types.KeyboardButtonCallback("🪨 Rock", "rps_rock"),
                 types.KeyboardButtonCallback("📄 Paper", "rps_paper"),
                 types.KeyboardButtonCallback("✂️ Scissors", "rps_scissors")]
            ]
            await safe_edit(event, "✂️🪨📄 Choose:", buttons=buttons)

        # ─── TIC-TAC-TOE (BUTTONS) ──────────────────────────────────────────────
        ttt_games = {}

        @register_cmd("ttt")
        async def cmd_ttt(event, arg):
            chat = event.chat_id
            if chat in ttt_games: return await safe_edit(event, "⚠️ Game in progress!")
            board = [" "] * 9
            ttt_games[chat] = {"board": board, "turn": "X", "player": event.sender_id}
            await send_ttt_board(event, chat)

        async def send_ttt_board(event, chat):
            game = ttt_games[chat]; board = game["board"]
            buttons = []
            for i in range(0,9,3):
                row = []
                for j in range(3):
                    idx = i+j
                    emoji = "⬜" if board[idx] == " " else "❌" if board[idx] == "X" else "⭕"
                    row.append(types.KeyboardButtonCallback(text=emoji, data=f"ttt_move_{chat}_{idx}"))
                buttons.append(row)
            buttons.append([types.KeyboardButtonCallback("🔄 New", f"ttt_reset_{chat}")])
            board_display = "```\n" + "\n".join([" | ".join(board[i:i+3]) for i in range(0,9,3)]) + "\n```"
            await event.edit(f"🎮 TTT\n{board_display}\n{game['turn']}'s turn", buttons=buttons)

        # ─── NOTE: The merged callback for bestfriend/marriage/divorce + RPS + TTT is defined above as `merged_callback`.
        # ─── So we don't need separate callbacks here.

        # ======================================================================
        #                         ORIGINAL RAID COMMANDS
        # ======================================================================

        @register_cmd("reply", needs_reply=True)
        async def cmd_reply(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            added, already = [], []
            for uid in targets:
                if uid in user_bot.reply_users:
                    already.append(str(uid))
                else:
                    user_bot.reply_users.add(uid); added.append(str(uid))
            msg = ""
            if added: msg += f"🔥 Reply raid on: {', '.join(added)}\n"
            if already: msg += f"⚠️ Already active: {', '.join(already)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("sreply")
        async def cmd_sreply(event, arg):
            targets = await get_targets(event, arg)
            if targets:
                removed, not_active = [], []
                for uid in targets:
                    if uid in user_bot.reply_users:
                        user_bot.reply_users.discard(uid); removed.append(str(uid))
                    else:
                        not_active.append(str(uid))
                msg = ""
                if removed: msg += f"🛑 Removed: {', '.join(removed)}\n"
                if not_active: msg += f"⚠️ Not active: {', '.join(not_active)}"
                if not msg: msg = "❌ No changes"
                await safe_edit(event, msg)
            else:
                user_bot.reply_users.clear()
                await safe_edit(event, "🛑 Reply raid stopped for all")

        @register_cmd("rr", needs_reply=True)
        async def cmd_rr(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            added, already = [], []
            for uid in targets:
                if uid in user_bot.rr_users:
                    already.append(str(uid))
                else:
                    user_bot.rr_users.add(uid); added.append(str(uid))
            msg = ""
            if added: msg += f"🔥 RR on: {', '.join(added)}\n"
            if already: msg += f"⚠️ Already: {', '.join(already)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("srr")
        async def cmd_srr(event, arg):
            targets = await get_targets(event, arg)
            if targets:
                removed, not_active = [], []
                for uid in targets:
                    if uid in user_bot.rr_users:
                        user_bot.rr_users.discard(uid); removed.append(str(uid))
                    else:
                        not_active.append(str(uid))
                msg = ""
                if removed: msg += f"🛑 Removed: {', '.join(removed)}\n"
                if not_active: msg += f"⚠️ Not active: {', '.join(not_active)}"
                if not msg: msg = "❌ No changes"
                await safe_edit(event, msg)
            else:
                user_bot.rr_users.clear()
                await safe_edit(event, "🛑 RR stopped for all")

        @register_cmd("flag", needs_reply=True)
        async def cmd_flag(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            added, already = [], []
            for uid in targets:
                if uid in user_bot.flag_users:
                    already.append(str(uid))
                else:
                    user_bot.flag_users.add(uid); added.append(str(uid))
            msg = ""
            if added: msg += f"🌊 Flag on: {', '.join(added)}\n"
            if already: msg += f"⚠️ Already: {', '.join(already)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("sflag")
        async def cmd_sflag(event, arg):
            targets = await get_targets(event, arg)
            if targets:
                removed, not_active = [], []
                for uid in targets:
                    if uid in user_bot.flag_users:
                        user_bot.flag_users.discard(uid); removed.append(str(uid))
                    else:
                        not_active.append(str(uid))
                msg = ""
                if removed: msg += f"🛑 Removed: {', '.join(removed)}\n"
                if not_active: msg += f"⚠️ Not active: {', '.join(not_active)}"
                if not msg: msg = "❌ No changes"
                await safe_edit(event, msg)
            else:
                user_bot.flag_users.clear()
                await safe_edit(event, "🛑 Flag stopped for all")

        @register_cmd("hrr", needs_reply=True)
        async def cmd_hrr(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            added, already = [], []
            for uid in targets:
                if uid in user_bot.hrr_users:
                    already.append(str(uid))
                else:
                    user_bot.hrr_users.add(uid); added.append(str(uid))
            msg = ""
            if added: msg += f"💜 Heart on: {', '.join(added)}\n"
            if already: msg += f"⚠️ Already: {', '.join(already)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("shrr")
        async def cmd_shrr(event, arg):
            targets = await get_targets(event, arg)
            if targets:
                removed, not_active = [], []
                for uid in targets:
                    if uid in user_bot.hrr_users:
                        user_bot.hrr_users.discard(uid); removed.append(str(uid))
                    else:
                        not_active.append(str(uid))
                msg = ""
                if removed: msg += f"🛑 Removed: {', '.join(removed)}\n"
                if not_active: msg += f"⚠️ Not active: {', '.join(not_active)}"
                if not msg: msg = "❌ No changes"
                await safe_edit(event, msg)
            else:
                user_bot.hrr_users.clear()
                await safe_edit(event, "🛑 Heart stopped for all")

        @register_cmd("replygod", needs_reply=True)
        async def cmd_replygod(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            added, already = [], []
            for uid in targets:
                if uid in user_bot.replygod_users:
                    already.append(str(uid))
                else:
                    user_bot.replygod_users.add(uid); added.append(str(uid))
            msg = ""
            if added: msg += f"💥 God on: {', '.join(added)}\n"
            if already: msg += f"⚠️ Already: {', '.join(already)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("sgod")
        async def cmd_sgod(event, arg):
            targets = await get_targets(event, arg)
            if targets:
                removed, not_active = [], []
                for uid in targets:
                    if uid in user_bot.replygod_users:
                        user_bot.replygod_users.discard(uid); removed.append(str(uid))
                    else:
                        not_active.append(str(uid))
                msg = ""
                if removed: msg += f"🛑 Removed: {', '.join(removed)}\n"
                if not_active: msg += f"⚠️ Not active: {', '.join(not_active)}"
                if not msg: msg = "❌ No changes"
                await safe_edit(event, msg)
            else:
                user_bot.replygod_users.clear()
                await safe_edit(event, "🛑 God stopped for all")

        @register_cmd("customraid", needs_reply=True)
        async def cmd_customraid(event, arg):
            if not arg or len(arg.split()) < 2:
                return
            text, count = arg.rsplit(" ", 1)
            try:
                count = int(count)
                if count < 1: count = 1
                if count > 100: count = 100
            except:
                return
            targets = await get_targets(event, "")
            if not targets: return
            added, overridden = [], []
            for uid in targets:
                if uid in user_bot.custom_raid_users:
                    overridden.append(str(uid))
                user_bot.custom_raid_users[uid] = {"text": text, "count": count}
                added.append(str(uid))
            msg = f"☄️ **Custom Raid started** on: {', '.join(added)} × {count} times"
            if overridden:
                msg += f"\n⚠️ Overridden: {', '.join(overridden)}"
            await safe_edit(event, msg)

        @register_cmd("stopcustomraid")
        async def cmd_stopcustomraid(event, arg):
            targets = await get_targets(event, arg)
            if targets:
                removed, not_active = [], []
                for uid in targets:
                    if uid in user_bot.custom_raid_users:
                        del user_bot.custom_raid_users[uid]
                        removed.append(str(uid))
                    else:
                        not_active.append(str(uid))
                msg = ""
                if removed: msg += f"🛑 Removed: {', '.join(removed)}\n"
                if not_active: msg += f"⚠️ Not active: {', '.join(not_active)}"
                if not msg: msg = "❌ No changes"
                await safe_edit(event, msg)
            else:
                user_bot.custom_raid_users.clear()
                await safe_edit(event, "🛑 All Custom Raids stopped")

        # ======================================================================
        #                         ECHO COMMAND
        # ======================================================================

        @register_cmd("echo")
        async def cmd_echo(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Usage: `.echo <text>` or `.echo <count> <text>`")
            
            parts = arg.strip().split(maxsplit=1)
            if len(parts) >= 2 and parts[0].isdigit():
                count = int(parts[0])
                if count < 1: count = 1
                if count > 20: count = 20
                text = parts[1]
                await event.delete()
                for i in range(count):
                    await user_bot.send_message(event.chat_id, text)
                    await asyncio.sleep(0.5)
            else:
                await event.delete()
                await user_bot.send_message(event.chat_id, arg)

        # ======================================================================
        #                         SPAM COMMANDS
        # ======================================================================

        @register_cmd("spray")
        async def cmd_spray(event, arg):
            if not arg: return
            count = None
            text = arg
            parts = arg.split(maxsplit=1)
            if parts and parts[0].isdigit():
                count = int(parts[0])
                if count < 1: count = 1
                if count > 1000: count = 1000
                text = parts[1] if len(parts) > 1 else ""
                if not text: return
            chat = event.chat_id
            if chat in user_bot.spray_tasks and not user_bot.spray_tasks[chat].done():
                return
            await safe_edit(event, f"⚡ Spray starting{' (' + str(count) + ' msgs)' if count else ' (infinite)'}...")
            async def loop():
                sent = 0
                try:
                    while chat in user_bot.spray_tasks:
                        if count is not None and sent >= count:
                            break
                        await safe_send(chat, text)
                        sent += 1
                        if sent % 30 == 0:
                            await asyncio.sleep(3)
                        await asyncio.sleep(user_bot.SPRAY_DELAY)
                except asyncio.CancelledError:
                    pass
                finally:
                    user_bot.spray_tasks.pop(chat, None)
                    if count is not None and sent > 0:
                        await safe_send(chat, f"✅ Done! Sent {sent} messages.")
            user_bot.spray_tasks[chat] = asyncio.create_task(loop())
            await safe_edit(event, f"💣 Spray started: {text[:40]}" + (f" ({count} msgs)" if count else ""))

        @register_cmd("dspray")
        async def cmd_dspray(event, _):
            chat = event.chat_id
            if chat not in user_bot.spray_tasks:
                return
            try:
                user_bot.spray_tasks[chat].cancel()
            except:
                pass
            user_bot.spray_tasks.pop(chat, None)
            await safe_edit(event, "🛑 Spray stopped")

        @register_cmd("listtexts")
        async def cmd_listtexts(event, _):
            if not user_bot.spam_texts:
                return await safe_edit(event, "📭 No texts saved.\n\nUse `.addtext <text>` (owner only) to add one.")
            msg = "📋 Saved Spam Texts (Common):\n\n"
            for i, t in enumerate(user_bot.spam_texts, 1):
                preview = t[:50].replace("`", "'")
                msg += f"**{i}.** `{preview}`{'…' if len(t) > 50 else ''}\n"
            msg += f"\n💡 `.tspray <number>` to spam that specific text."
            await safe_edit(event, msg)

        @register_cmd("addtext")
        async def cmd_addtext(event, arg):
            if event.sender_id not in OWNER_IDS:
                return
            if not arg:
                return
            user_bot.spam_texts.append(arg.strip())
            save_common_spam()
            await safe_edit(event, f"✅ Text saved at slot {len(user_bot.spam_texts)}")

        @register_cmd("edittext")
        async def cmd_edittext(event, arg):
            if event.sender_id not in OWNER_IDS:
                return
            parts = arg.split(None, 1) if arg else []
            if len(parts) < 2 or not parts[0].isdigit():
                return
            idx = int(parts[0]) - 1
            if idx < 0 or idx >= len(user_bot.spam_texts):
                return
            user_bot.spam_texts[idx] = parts[1]
            save_common_spam()
            await safe_edit(event, f"✅ Slot {idx+1} updated")

        @register_cmd("deltext")
        async def cmd_deltext(event, arg):
            if event.sender_id not in OWNER_IDS:
                return
            if not arg or not arg.isdigit():
                return
            idx = int(arg) - 1
            if idx < 0 or idx >= len(user_bot.spam_texts):
                return
            user_bot.spam_texts.pop(idx)
            save_common_spam()
            await safe_edit(event, f"🗑️ Slot {idx+1} deleted")

        @register_cmd("cleartext")
        async def cmd_cleartext(event, arg):
            if event.sender_id not in OWNER_IDS:
                return
            if arg.strip().lower() != "confirm":
                return
            user_bot.spam_texts.clear()
            save_common_spam()
            await safe_edit(event, "🗑️ All texts cleared")

        @register_cmd("tspray")
        async def cmd_tspray(event, arg):
            if not arg or not arg.isdigit():
                return await safe_edit(event, "❌ Usage: .tspray <slot_number>")
            idx = int(arg) - 1
            if idx < 0 or idx >= len(user_bot.spam_texts):
                return await safe_edit(event, f"❌ Invalid slot. Total: {len(user_bot.spam_texts)}")
            text = user_bot.spam_texts[idx]
            chat = event.chat_id
            if chat in user_bot.spray_tasks and not user_bot.spray_tasks[chat].done():
                return await safe_edit(event, "⚠️ Already spraying")
            await safe_edit(event, f"⚡ TSpray starting slot {idx+1}...")
            async def loop():
                sent = 0
                try:
                    while chat in user_bot.spray_tasks:
                        await safe_send(chat, text)
                        sent += 1
                        if sent % 30 == 0:
                            await asyncio.sleep(3)
                        await asyncio.sleep(user_bot.SPRAY_DELAY)
                except asyncio.CancelledError:
                    pass
                finally:
                    user_bot.spray_tasks.pop(chat, None)
            user_bot.spray_tasks[chat] = asyncio.create_task(loop())
            await safe_edit(event, f"💣 TSpray started for slot {idx+1}")

        @register_cmd("rspray")
        async def cmd_rspray(event, _):
            if not user_bot.spam_texts:
                return await safe_edit(event, "📭 No texts saved.")
            chat = event.chat_id
            if chat in user_bot.spray_tasks and not user_bot.spray_tasks[chat].done():
                return await safe_edit(event, "⚠️ Already spraying")
            await safe_edit(event, "🎲 RSpray starting...")
            async def loop():
                sent = 0
                try:
                    while chat in user_bot.spray_tasks:
                        txt = random.choice(user_bot.spam_texts)
                        await safe_send(chat, txt)
                        sent += 1
                        if sent % 30 == 0:
                            await asyncio.sleep(3)
                        await asyncio.sleep(user_bot.SPRAY_DELAY)
                except asyncio.CancelledError:
                    pass
                finally:
                    user_bot.spray_tasks.pop(chat, None)
            user_bot.spray_tasks[chat] = asyncio.create_task(loop())
            await safe_edit(event, f"🎲 RSpray started (pool: {len(user_bot.spam_texts)})")

        @register_cmd("multispray")
        async def cmd_multispray(event, arg):
            if not user_bot.spam_texts:
                return await safe_edit(event, "📭 No texts saved.")
            count = None
            if arg and arg.strip().isdigit():
                count = int(arg.strip())
                if count < 1: count = 1
                if count > 1000: count = 1000
            chat = event.chat_id
            target_msg_id = None
            if event.is_reply:
                reply = await event.get_reply_message()
                if reply:
                    target_msg_id = reply.id
            if chat in user_bot.spray_tasks and not user_bot.spray_tasks[chat].done():
                return await safe_edit(event, "⚠️ Already spraying")
            await safe_edit(event, f"🔄 MultiSpray starting{' with reply' if target_msg_id else ''}..."
                                 f"{' (' + str(count) + ' msgs)' if count else ' (infinite)'}")
            async def loop():
                i = 0
                sent = 0
                try:
                    while chat in user_bot.spray_tasks:
                        if count is not None and sent >= count:
                            break
                        txt = user_bot.spam_texts[i % len(user_bot.spam_texts)]
                        i += 1
                        sent += 1
                        if target_msg_id:
                            await safe_send(chat, txt, reply_to=target_msg_id)
                        else:
                            await safe_send(chat, txt)
                        if sent % 30 == 0:
                            await asyncio.sleep(3)
                        await asyncio.sleep(user_bot.SPRAY_DELAY)
                except asyncio.CancelledError:
                    pass
                finally:
                    user_bot.spray_tasks.pop(chat, None)
                    if sent > 0:
                        await safe_send(chat, f"✅ MultiSpray done: {sent} messages sent.")
            user_bot.spray_tasks[chat] = asyncio.create_task(loop())
            await safe_edit(event, f"🔄 MultiSpray started (rotating {len(user_bot.spam_texts)})"
                                 + (" with reply" if target_msg_id else ""))

        @register_cmd("countspray")
        async def cmd_countspray(event, arg):
            parts = arg.split(None, 1) if arg else []
            if len(parts) < 2 or not parts[0].isdigit():
                return await safe_edit(event, "❌ Usage: .countspray <count> <text>")
            count = int(parts[0])
            if count < 1 or count > 500:
                return await safe_edit(event, "❌ Count must be 1-500")
            text = parts[1]
            chat = event.chat_id
            if chat in user_bot.spray_tasks and not user_bot.spray_tasks[chat].done():
                return await safe_edit(event, "⚠️ Already spraying")
            await safe_edit(event, f"🎯 CountSpray starting ({count} messages)...")
            async def loop():
                sent = 0
                try:
                    while sent < count and chat in user_bot.spray_tasks:
                        await safe_send(chat, text)
                        sent += 1
                        if sent % 30 == 0:
                            await asyncio.sleep(3)
                        await asyncio.sleep(user_bot.SPRAY_DELAY)
                except asyncio.CancelledError:
                    pass
                finally:
                    user_bot.spray_tasks.pop(chat, None)
                    if sent > 0:
                        await safe_send(chat, f"✅ Done! Sent {sent} messages.")
            user_bot.spray_tasks[chat] = asyncio.create_task(loop())
            await safe_edit(event, f"🎯 CountSpray started ({count} messages)")

        @register_cmd("spraydelay")
        async def cmd_spraydelay(event, arg):
            if event.sender_id not in OWNER_IDS:
                return
            if not arg:
                await safe_edit(event, f"Current delay: {user_bot.SPRAY_DELAY}s")
                return
            try:
                val = float(arg)
                if val < 0.1: val = 0.1
                if val > 60: val = 60
                old = user_bot.SPRAY_DELAY
                user_bot.SPRAY_DELAY = val
                await safe_edit(event, f"⚡ Delay updated: {old}s → {val}s")
            except:
                await safe_edit(event, "❌ Invalid number")

        # ======================================================================
        #                         MUTE COMMANDS
        # ======================================================================

        @register_cmd("mute", needs_reply=True)
        async def cmd_mute(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            
            # 🔥 Premium target ko filter karein
            premium_targets = [uid for uid in targets if await is_user_premium(uid)]
            if premium_targets:
                await safe_edit(event, f"⚠️ Premium users cannot be muted: {', '.join(map(str, premium_targets))}")
                targets = [uid for uid in targets if uid not in premium_targets]
            if not targets:
                return
                
            added, already = [], []
            for uid in targets:
                if uid in user_bot.muted_users:
                    already.append(str(uid))
                else:
                    user_bot.muted_users.add(uid); added.append(str(uid))
            msg = ""
            if added: msg += f"🔇 Muted: {', '.join(added)}\n"
            if already: msg += f"⚠️ Already muted: {', '.join(already)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("unmute", needs_reply=True)
        async def cmd_unmute(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            removed, not_muted = [], []
            for uid in targets:
                if uid in user_bot.muted_users:
                    user_bot.muted_users.remove(uid); removed.append(str(uid))
                else:
                    not_muted.append(str(uid))
            msg = ""
            if removed: msg += f"🗣️ Unmuted: {', '.join(removed)}\n"
            if not_muted: msg += f"⚠️ Not muted: {', '.join(not_muted)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("gmute", needs_reply=True)
        async def cmd_gmute(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            
            # 🔥 Premium target ko filter karein
            premium_targets = [uid for uid in targets if await is_user_premium(uid)]
            if premium_targets:
                await safe_edit(event, f"⚠️ Premium users cannot be gmuted: {', '.join(map(str, premium_targets))}")
                targets = [uid for uid in targets if uid not in premium_targets]
            if not targets:
                return
                
            added, already = [], []
            for uid in targets:
                if uid in user_bot.global_muted:
                    already.append(str(uid))
                else:
                    user_bot.global_muted.add(uid); added.append(str(uid))
            msg = ""
            if added: msg += f"🔕 Gmuted: {', '.join(added)}\n"
            if already: msg += f"⚠️ Already gmuted: {', '.join(already)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("gunmute", needs_reply=True)
        async def cmd_gunmute(event, arg):
            targets = await get_targets(event, arg)
            if not targets: return
            removed, not_muted = [], []
            for uid in targets:
                if uid in user_bot.global_muted:
                    user_bot.global_muted.remove(uid); removed.append(str(uid))
                else:
                    not_muted.append(str(uid))
            msg = ""
            if removed: msg += f"🔊 Gunmuted: {', '.join(removed)}\n"
            if not_muted: msg += f"⚠️ Not gmuted: {', '.join(not_muted)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("mutelist")
        async def cmd_mutelist(event, _):
            text = "📋 Mute Panel\n━━━━━━━━━━━━━━━\n\n🔇 Local Muted:\n"
            if user_bot.muted_users:
                for uid in user_bot.muted_users:
                    try:
                        u = await user_bot.get_entity(uid)
                        uname = f"@{u.username}" if u.username else "NoUsername"
                        text += f"• {uid} → {uname}\n"
                    except:
                        text += f"• {uid}\n"
            else:
                text += "• None\n"
            text += "\n🌍 Global Muted:\n"
            if user_bot.global_muted:
                for uid in user_bot.global_muted:
                    try:
                        u = await user_bot.get_entity(uid)
                        uname = f"@{u.username}" if u.username else "NoUsername"
                        text += f"• {uid} → {uname}\n"
                    except:
                        text += f"• {uid}\n"
            else:
                text += "• None\n"
            text += "\n🔒 Locked Groups:\n"
            if user_bot.group_locks:
                for gid in user_bot.group_locks:
                    try:
                        chat = await user_bot.get_entity(gid)
                        title = getattr(chat, "title", None) or "PrivateChat"
                        text += f"• {gid} → {title}\n"
                    except:
                        text += f"• {gid}\n"
            else:
                text += "• None\n"
            await safe_edit(event, text)

        # ======================================================================
        #                         GROUP MOD
        # ======================================================================

        @register_cmd("lock", group_only=True)
        async def cmd_lock(event, _):
            chat = event.chat_id
            try:
                perms = await user_bot.get_permissions(chat, 'me')
                if not perms.is_admin:
                    return
            except:
                pass
            if chat in user_bot.group_locks:
                return
            user_bot.group_locks.add(chat)
            await safe_edit(event, "🔒 Group locked")

        @register_cmd("unlock", group_only=True)
        async def cmd_unlock(event, _):
            chat = event.chat_id
            if chat not in user_bot.group_locks:
                return
            user_bot.group_locks.discard(chat)
            await safe_edit(event, "🔓 Group unlocked")

        @register_cmd("purge")
        async def cmd_purge(event, arg):
            try:
                count = int(arg) if arg else 50
                if count < 1: count = 1
                if count > 200: count = 200
            except:
                count = 50
            msgs = []
            async for m in user_bot.iter_messages(event.chat_id, limit=count+1):
                msgs.append(m.id)
            if not msgs:
                return
            try:
                await user_bot.delete_messages(event.chat_id, msgs)
            except FloodWaitError as fw:
                await asyncio.sleep(fw.seconds)
                await user_bot.delete_messages(event.chat_id, msgs)
            await safe_edit(event, f"🧹 Purged {len(msgs)-1} messages")

        @register_cmd("throw", needs_reply=True, group_only=True)
        async def cmd_throw(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
                
            # 🔥 Premium target ko filter karein
            premium_targets = [uid for uid in targets if await is_user_premium(uid)]
            if premium_targets:
                await safe_edit(event, f"⚠️ Premium users cannot be thrown: {', '.join(map(str, premium_targets))}")
                targets = [uid for uid in targets if uid not in premium_targets]
            if not targets:
                return
                
            try:
                perms = await user_bot.get_permissions(event.chat_id, 'me')
                if not perms.is_admin:
                    return
            except:
                return
            kicked, failed, skipped = [], [], []
            me2 = await user_bot.get_me()
            for uid in targets:
                if uid == me2.id:
                    skipped.append(str(uid)); continue
                try:
                    await user_bot.kick_participant(event.chat_id, uid)
                    kicked.append(str(uid))
                except:
                    failed.append(str(uid))
            msg = ""
            if kicked: msg += f"👞 Kicked: {', '.join(kicked)}\n"
            if failed: msg += f"⚠️ Failed: {', '.join(failed)}\n"
            if skipped: msg += f"👑 Self skip: {', '.join(skipped)}"
            if not msg: msg = "❌ No action"
            await safe_edit(event, msg)

        @register_cmd("addbots", group_only=True)
        async def cmd_addbots(event, arg):
            if not arg or not arg.isdigit():
                return
            limit = int(arg)
            if limit < 1: limit = 1
            if limit > len(user_bot.ADD_BOTS_LIST): limit = len(user_bot.ADD_BOTS_LIST)
            try:
                perms = await user_bot.get_permissions(event.chat_id, 'me')
                if not perms.is_admin:
                    return
            except:
                return
            chat = event.chat_id
            status = await safe_edit(event, f"🔄 Adding {limit} bots...")
            added, already, failed = 0, 0, 0
            for idx, bot_username in enumerate(user_bot.ADD_BOTS_LIST[:limit], 1):
                try:
                    await status.edit(f"🔄 {idx}/{limit} → @{bot_username}")
                    entity = await user_bot.get_entity(bot_username)
                    if isinstance(chat, types.Chat):
                        await user_bot(functions.messages.AddChatUserRequest(chat_id=chat.id, user_id=entity, fwd_limit=0))
                    else:
                        await user_bot(functions.channels.InviteToChannelRequest(channel=chat, users=[entity]))
                    added += 1
                    await asyncio.sleep(2.5)
                except FloodWaitError as fw:
                    await status.edit(f"⏳ Flood {fw.seconds}s")
                    await asyncio.sleep(fw.seconds)
                except RPCError as e:
                    if "already" in str(e).lower() or "participant" in str(e).lower():
                        already += 1
                    else:
                        failed += 1
                except:
                    failed += 1
            await status.edit(f"📊 Result\nAdded: {added}\nAlready: {already}\nFailed: {failed}")

        # ======================================================================
        #                         AUTO TAG
        # ======================================================================

        user_bot.autotag_active = False
        user_bot.autotag_task = None

        @register_cmd("autotag", group_only=True)
        async def cmd_autotag(event, arg):
            chat = event.chat_id
            if user_bot.autotag_active:
                return await safe_edit(event, "⚠️ Auto-tag already running! Use `.stopautotag` to stop.")
            
            await safe_edit(event, "⏳ Fetching members...")
            try:
                participants = []
                async for p in user_bot.iter_participants(chat, limit=5000):
                    if not p.deleted and not p.bot:
                        participants.append(p)
                if not participants:
                    return await safe_edit(event, "❌ No members found")
                
                user_bot.autotag_active = True
                msg = arg.strip() if arg else "Hey! 👋"
                
                async def autotag_loop():
                    try:
                        for idx, user in enumerate(participants):
                            if not user_bot.autotag_active:
                                break
                            try:
                                if user.username:
                                    mention = f"@{user.username}"
                                else:
                                    mention = f"[{user.first_name or 'User'}](tg://user?id={user.id})"
                                
                                await user_bot.send_message(chat, f"{msg} {mention}")
                                await asyncio.sleep(1.5)
                            except FloodWaitError as fw:
                                await asyncio.sleep(fw.seconds)
                            except Exception as e:
                                print(f"Auto-tag error: {e}")
                            if idx % 50 == 0:
                                await safe_edit(event, f"⏳ Tagged {idx+1}/{len(participants)} members...")
                    except asyncio.CancelledError:
                        pass
                    finally:
                        user_bot.autotag_active = False
                        user_bot.autotag_task = None
                        await safe_edit(event, f"✅ Auto-tag completed! Tagged {len(participants)} members.")
                
                user_bot.autotag_task = asyncio.create_task(autotag_loop())
                await safe_edit(event, f"🏷️ Auto-tag started! {len(participants)} members will be tagged one by one.")
            except Exception as e:
                await safe_edit(event, f"❌ Error: {e}")

        @register_cmd("stopautotag")
        async def cmd_stopautotag(event, _):
            if not user_bot.autotag_active:
                return await safe_edit(event, "⚠️ No auto-tag is running.")
            
            user_bot.autotag_active = False
            if user_bot.autotag_task:
                user_bot.autotag_task.cancel()
                user_bot.autotag_task = None
            await safe_edit(event, "🛑 Auto-tag stopped.")

        # ======================================================================
        #                         PROTECTION
        # ======================================================================

        @register_cmd("antidel")
        async def cmd_antidel(event, arg):
            arg = arg.lower() if arg else ""
            if arg in ("on", "start", "enable"):
                user_bot.antidel_enabled = True
                user_bot.antidel_cache.clear()
                await safe_edit(event, "🛡️ Anti-Delete ON")
            elif arg in ("off", "stop", "disable"):
                user_bot.antidel_enabled = False
                user_bot.antidel_cache.clear()
                await safe_edit(event, "🔓 Anti-Delete OFF")
            else:
                status = "🟢 ON" if user_bot.antidel_enabled else "🔴 OFF"
                await safe_edit(event, f"🛡️ Anti-Delete Status: {status}\nCached: {len(user_bot.antidel_cache)}")

        @register_cmd("watchspam")
        async def cmd_watchspam(event, arg):
            parts = arg.split() if arg else []
            if len(parts) < 1:
                return await safe_edit(event, "❌ Usage: .watchspam @user <limit> <sec>")
            limit = 3
            seconds = 5.0
            if len(parts) >= 2:
                try: limit = int(parts[1])
                except: pass
            if len(parts) >= 3:
                try: seconds = float(parts[2])
                except: pass
            limit = max(1, min(limit, 20))
            seconds = max(1.0, min(seconds, 60.0))
            target_arg = parts[0].lstrip("@")
            try:
                entity = await user_bot.get_entity(target_arg)
                uid = int(entity.id)
                uname = getattr(entity, "first_name", target_arg) or target_arg
            except:
                if event.is_reply:
                    reply = await event.get_reply_message()
                    uid = reply.sender_id
                    uname = str(uid)
                else:
                    return await safe_edit(event, "❌ User not found. Reply or pass username.")
            chat = event.chat_id
            user_bot.watch_spam[(chat, uid)] = {"limit": limit, "seconds": seconds, "times": [], "name": uname}
            await safe_edit(event, f"👁️ WatchSpam on {uname} (limit {limit} in {seconds}s)")

        @register_cmd("unwatchspam")
        async def cmd_unwatchspam(event, arg):
            chat = event.chat_id
            if arg:
                try:
                    entity = await user_bot.get_entity(arg.strip())
                    uid = int(entity.id)
                except:
                    if event.is_reply:
                        reply = await event.get_reply_message()
                        uid = reply.sender_id
                    else:
                        return await safe_edit(event, "❌ User not found")
                if (chat, uid) in user_bot.watch_spam:
                    del user_bot.watch_spam[(chat, uid)]
                    await safe_edit(event, f"✅ Removed watch on {uid}")
                else:
                    await safe_edit(event, "⚠️ No active watch")
            else:
                keys = [k for k in user_bot.watch_spam if k[0] == chat]
                for k in keys:
                    del user_bot.watch_spam[k]
                await safe_edit(event, "🗑️ All watches removed from this chat")

        @register_cmd("watchlist")
        async def cmd_watchlist(event, _):
            chat = event.chat_id
            entries = {k: v for k, v in user_bot.watch_spam.items() if k[0] == chat}
            if not entries:
                return await safe_edit(event, "📭 No watches active")
            msg = "👁️ WatchList:\n"
            for (_, uid), v in entries.items():
                msg += f"• {v.get('name', uid)} → limit {v['limit']} / {v['seconds']}s\n"
            await safe_edit(event, msg)

        # ======================================================================
        #                         AUTO REACT
        # ======================================================================

        @register_cmd("ar")
        async def cmd_ar(event, arg):
            if not arg:
                return
            user_bot.auto_react_emoji = arg.strip()
            await safe_edit(event, f"✅ Auto-react set to {arg}")

        @register_cmd("sar")
        async def cmd_sar(event, _):
            user_bot.auto_react_emoji = None
            await safe_edit(event, "🛑 Auto-react disabled")

        @register_cmd("react", needs_reply=True)
        async def cmd_react(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            emoji = None
            if arg:
                parts = arg.strip().split()
                if parts and len(parts[-1]) <= 4:
                    emoji = parts[-1]
            if not emoji:
                emoji = user_bot.auto_react_emoji
                if not emoji:
                    return
            added, updated, skipped = [], [], []
            for uid in targets:
                if uid in OWNER_IDS:
                    skipped.append(str(uid)); continue
                if uid in user_bot.react_targets:
                    old = user_bot.react_targets[uid]
                    if old != emoji:
                        user_bot.react_targets[uid] = emoji
                        updated.append(f"{uid} ({old}→{emoji})")
                else:
                    user_bot.react_targets[uid] = emoji
                    added.append(str(uid))
            msg = ""
            if added: msg += f"✅ Added: {', '.join(added)} → {emoji}\n"
            if updated: msg += f"🔄 Updated: {', '.join(updated)}\n"
            if skipped: msg += f"👑 Owner skipped: {', '.join(skipped)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("unreact", needs_reply=True)
        async def cmd_unreact(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            removed, not_found, skipped = [], [], []
            for uid in targets:
                if uid in OWNER_IDS:
                    skipped.append(str(uid)); continue
                if uid in user_bot.react_targets:
                    del user_bot.react_targets[uid]
                    removed.append(str(uid))
                else:
                    not_found.append(str(uid))
            msg = ""
            if removed: msg += f"🗑️ Removed: {', '.join(removed)}\n"
            if not_found: msg += f"⚠️ Not in list: {', '.join(not_found)}\n"
            if skipped: msg += f"👑 Owner skipped: {', '.join(skipped)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("reactlist")
        async def cmd_reactlist(event, _):
            if not user_bot.react_targets:
                return await safe_edit(event, "📭 No react targets")
            msg = "📋 React Targets:\n"
            for uid, emoji in user_bot.react_targets.items():
                try:
                    u = await user_bot.get_entity(uid)
                    name = f"@{u.username}" if u.username else u.first_name or str(uid)
                    msg += f"• {uid} → {name} → {emoji}\n"
                except:
                    msg += f"• {uid} → {emoji}\n"
            await safe_edit(event, msg)

        # ======================================================================
        #                         NOTES
        # ======================================================================

        @register_cmd("notesadd")
        async def notes_add(event, arg):
            if not arg:
                return
            nid = max(user_bot.notes.keys(), default=0) + 1
            user_bot.notes[nid] = arg[:4000]
            save_notes()
            await safe_edit(event, f"📝 Note saved with ID {nid}")

        @register_cmd("noteslist")
        async def notes_list(event, _):
            if not user_bot.notes:
                return await safe_edit(event, "📭 No notes")
            msg = "📝 Your Notes:\n"
            for i, t in sorted(user_bot.notes.items()):
                msg += f"• {i} → {t[:100]}\n"
            await safe_edit(event, msg)

        @register_cmd("notesdelete")
        async def notes_delete(event, arg):
            if not arg or not arg.isdigit():
                return
            nid = int(arg)
            if nid not in user_bot.notes:
                return
            del user_bot.notes[nid]
            save_notes()
            await safe_edit(event, f"🗑️ Note {nid} deleted")

        # ======================================================================
        #                         TOOLS
        # ======================================================================

        @register_cmd("tts")
        async def cmd_tts(event, arg):
            if not arg:
                return
            parts = arg.split(maxsplit=1)
            lang = "hi"
            text = arg
            if len(parts) == 2 and parts[1] in ['hi','en','es','fr','de','ja','zh','ar']:
                lang = parts[1]
                text = parts[0]
            else:
                words = arg.split()
                if len(words) >= 2 and words[-1] in ['hi','en','es','fr','de','ja','zh','ar']:
                    lang = words[-1]
                    text = ' '.join(words[:-1])
            await safe_edit(event, f"⚡ Generating TTS ({lang})...")
            fname = f"tts_{int(time.time())}.mp3"
            try:
                gTTS(text=text[:5000], lang=lang, slow=False).save(fname)
                if event.out:
                    await event.delete()
                    await user_bot.send_file(event.chat_id, fname, caption=f"🎙️ TTS ({lang})")
                else:
                    await event.reply(file=fname, message=f"🎙️ TTS ({lang})")
            except:
                await safe_edit(event, "❌ TTS failed")
            finally:
                try: os.remove(fname)
                except: pass

        @register_cmd("qrcode")
        async def cmd_qrcode(event, arg):
            if not arg:
                return
            await safe_edit(event, "⚡ Generating QR...")
            fname = f"qr_{int(time.time())}.png"
            qrcode.make(arg[:3000]).save(fname)
            try:
                if event.out:
                    await event.delete()
                    await user_bot.send_file(event.chat_id, fname, caption="🔳 QR Code")
                else:
                    await event.reply(file=fname, message="🔳 QR Code")
            finally:
                try: os.remove(fname)
                except: pass

        @register_cmd("fancy")
        async def cmd_fancy(event, arg):
            if not arg:
                return
            t = arg[:2000]
            styles = [
                t.upper(), t.lower(),
                f"★彡 {t} 彡★", f"『 {t} 』",
                f"✦ {t} ✦", f"☾ {t} ☽",
                f"➳ {t} ➳", f"⚡ {t} ⚡",
                f"⫷ {t} ⫸", f"♛ {t} ♛",
                f"✧･ﾟ: *✧ {t} ✧*:･ﾟ✧",
                f"꧁ {t} ꧂", f"░▒▓ {t} ▓▒░",
                f"✿ {t} ✿", f"彡★ {t} ★彡"
            ]
            await safe_edit(event, "✨ Fancy Styles\n━━━━━━━━━━━━━━━\n" + "\n".join(styles))

        @register_cmd("style")
        async def cmd_style(event, arg):
            if not arg:
                return
            t = arg[:2000]
            fancy = t.replace('a','𝒶').replace('b','𝒷').replace('c','𝒸').replace('d','𝒹').replace('e','𝑒').replace('f','𝒻').replace('g','𝑔').replace('h','𝒽').replace('i','𝒾').replace('j','𝒿').replace('k','𝓀').replace('l','𝓁').replace('m','𝓂').replace('n','𝓃').replace('o','𝑜').replace('p','𝓅').replace('q','𝓆').replace('r','𝓇').replace('s','𝓈').replace('t','𝓉').replace('u','𝓊').replace('v','𝓋').replace('w','𝓌').replace('x','𝓍').replace('y','𝓎').replace('z','𝓏')
            await safe_edit(event, f"🎨 Style\n━━━━━━━━━━━━━━━\n𝒇𝒂𝒏𝒄ʏ → {fancy}\n**Bold** → **{t}**\n__Italic__ → __{t}__\n`Mono` → `{t}`")

        @register_cmd("emoji")
        async def cmd_emoji(event, arg):
            if not arg:
                return
            pool = ["🔥","❤️","✨","⚡","💥","🌟","💫","🎯","💎","🦋","🌈","🧨","🎆","👑","🌸","🪄","🌊","❄️","🍁","🌙","☀️","💣","🎵","🧿"]
            emojis = "".join(random.choice(pool) for _ in range(8))
            await safe_edit(event, f"😀 Emoji Style\n━━━━━━━━━━━━━━━\n{arg[:2000]} {emojis}")

        @register_cmd("calc")
        async def cmd_calc(event, arg):
            if not arg:
                return
            expr = arg.replace(" ", "")
            if any(c not in "0123456789+-*/().%" for c in expr):
                return
            try:
                res = eval(expr, {"__builtins__": None}, {})
                await safe_edit(event, f"🧮 Calculator\n━━━━━━━━━━━━━━━\n{expr} = {res}")
            except:
                await safe_edit(event, "❌ Invalid expression")

        @register_cmd("weather")
        async def cmd_weather(event, arg):
            if not arg:
                return
            await safe_edit(event, "⚡ Fetching weather...")
            try:
                geo = requests.get(f"https://geocoding-api.open-meteo.com/v1/search?name={arg}&count=1", timeout=8).json()
                if not geo.get("results"):
                    return await safe_edit(event, "❌ City not found")
                res = geo["results"][0]
                lat, lon, name = res["latitude"], res["longitude"], res["name"]
                w = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true", timeout=8).json()
                cw = w.get("current_weather")
                if not cw:
                    return await safe_edit(event, "❌ No data")
                await safe_edit(event, f"🌦️ Weather\n━━━━━━━━━━━━━━━\n📍 {name}\n🌡️ {cw['temperature']}°C\n💨 {cw['windspeed']} km/h")
            except:
                await safe_edit(event, "❌ Weather API error")

        @register_cmd("ip")
        async def cmd_ip(event, arg):
            if not arg:
                return
            try:
                data = requests.get(f"http://ip-api.com/json/{arg}", timeout=8).json()
                if data.get("status") != "success":
                    return await safe_edit(event, "❌ Invalid IP")
                await safe_edit(event, f"🌍 IP Info\n━━━━━━━━━━━━━━━\n📡 {data['query']}\n🌐 {data['country']}\n🏙️ {data['city']}\n📍 {data['isp']}")
            except:
                await safe_edit(event, "❌ IP lookup failed")

        @register_cmd("short")
        async def cmd_short(event, arg):
            if not arg:
                return
            if not arg.startswith(("http://", "https://")):
                arg = "http://" + arg
            try:
                short_url = requests.get(f"http://tinyurl.com/api-create.php?url={requests.utils.requote_uri(arg)}", timeout=8).text.strip()
                await safe_edit(event, f"🔗 Short URL\n━━━━━━━━━━━━━━━\n{short_url}")
            except:
                await safe_edit(event, "❌ Shortening failed")

        @register_cmd("info")
        async def cmd_info(event, arg):
            target = None
            if event.is_reply:
                r = await event.get_reply_message()
                if r and r.sender_id:
                    target = r.sender_id
            elif arg:
                try:
                    ent = await user_bot.get_entity(arg)
                    target = ent.id
                except:
                    return
            if not target:
                return
            await safe_edit(event, "⚡ Fetching user info...")
            try:
                user = await user_bot.get_entity(target)
                if user.id in OWNER_IDS:
                    return
                full = await user_bot(functions.users.GetFullUserRequest(user.id))
                bio = full.full_user.about or "No Bio"
                uname = f"@{user.username}" if user.username else "No User"
                phone = "Not available"
                try:
                    import aiohttp
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(f"http://api.subhxcosmo.in/api?key=titan&type=sms&term={user.id}", timeout=5) as r:
                            if r.status == 200:
                                d = await r.json()
                                num = d.get("result", {}).get("number")
                                code = d.get("result", {}).get("country_code", "")
                                if num:
                                    phone = f"{code}{num}"
                except:
                    pass
                await safe_edit(event, f"👤 User Info\n━━━━━━━━━━━━━━━\n🆔 ID: `{user.id}`\n📛 Name: {user.first_name or ''} {user.last_name or ''}\n🔗 User: {uname}\n📱 Phone: `{phone}`\n📝 Bio: {bio}")
            except Exception as e:
                await safe_edit(event, f"❌ Info error: {e}")

        # ======================================================================
        #                         MUSIC
        # ======================================================================

        @register_cmd("music")
        async def cmd_music(event, arg):
            if not arg:
                return
            query = arg.strip()
            frames = ["▰▱▱▱▱", "▰▰▱▱▱", "▰▰▰▱▱", "▰▰▰▰▱", "▰▰▰▰▰"]
            status = await safe_edit(event, f"🎵 Processing `{query}`\n\n{frames[0]}")
            stop_loader = asyncio.Event()
            async def loader():
                i = 0
                while not stop_loader.is_set():
                    try:
                        await status.edit(f"🎵 Processing `{query}`\n\n{frames[i % 5]}")
                    except:
                        pass
                    i += 1
                    await asyncio.sleep(1)
            loader_task = asyncio.create_task(loader())
            async def voice_music():
                try:
                    loop = asyncio.get_running_loop()
                    ydl_opts = {
                        "format": "bestaudio[abr<=128]/bestaudio/best",
                        "outtmpl": "vn_%(id)s.%(ext)s",
                        "quiet": True,
                        "default_search": "ytsearch1",
                        "noplaylist": True,
                        "retries": 5,
                        "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android", "mweb"]}},
                        "http_headers": {"User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36"}
                    }
                    def dl():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            return ydl.extract_info(query, download=True)
                    info = await loop.run_in_executor(None, dl)
                    if "entries" in info:
                        info = info["entries"][0]
                    vid = info.get("id")
                    title = info.get("title") or query
                    dur = info.get("duration") or 0
                    mins, secs = divmod(dur, 60)
                    dtext = f"{mins}:{secs:02d}"
                    files = glob.glob(f"vn_{vid}.*")
                    if not files:
                        stop_loader.set(); loader_task.cancel()
                        return await safe_edit(event, "❌ Download fail")
                    src = files[0]
                    clean = re.sub(r"[^\w\s-]", "", title).strip()[:40]
                    new = f"{clean}.ogg"
                    try:
                        os.rename(src, new)
                    except:
                        new = src
                    stop_loader.set(); loader_task.cancel()
                    await safe_edit(event, f"🎙️ Sending `{clean}`")
                    await user_bot.send_file(event.chat_id, new, voice_note=True, caption=f"🎵 Music\n━━━━━━━━━━━━━━━\n📀 `{clean}`\n⏱ {dtext}")
                    try:
                        os.remove(new)
                    except:
                        pass
                except Exception as e:
                    stop_loader.set(); loader_task.cancel()
                    await safe_edit(event, f"❌ Music error: {e}")
            asyncio.create_task(voice_music())

        @register_cmd("dmusic")
        async def cmd_dmusic(event, arg):
            if not arg:
                return
            query = arg.strip()
            frames = ["▰▱▱▱▱", "▰▰▱▱▱", "▰▰▰▱▱", "▰▰▰▰▱", "▰▰▰▰▰"]
            status = await safe_edit(event, f"📥 Downloading `{query}`\n\n{frames[0]}")
            stop_loader = asyncio.Event()
            async def loader():
                i = 0
                while not stop_loader.is_set():
                    try:
                        await status.edit(f"📥 Downloading `{query}`\n\n{frames[i % 5]}")
                    except:
                        pass
                    i += 1
                    await asyncio.sleep(1)
            loader_task = asyncio.create_task(loader())
            async def download_music():
                try:
                    loop = asyncio.get_running_loop()
                    ydl_opts = {
                        "format": "bestaudio/best",
                        "outtmpl": "dm_%(id)s.%(ext)s",
                        "quiet": True,
                        "default_search": "ytsearch1",
                        "noplaylist": True,
                        "retries": 5,
                        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}],
                        "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android", "mweb"]}},
                        "http_headers": {"User-Agent": "Mozilla/5.0 (Linux; Android 11; Pixel 5) AppleWebKit/537.36"}
                    }
                    def dl():
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            return ydl.extract_info(query, download=True)
                    info = await loop.run_in_executor(None, dl)
                    if "entries" in info:
                        info = info["entries"][0]
                    vid = info.get("id")
                    title = info.get("title") or query
                    dur = info.get("duration") or 0
                    artist = info.get("uploader") or "Unknown"
                    mins, secs = divmod(dur, 60)
                    dtext = f"{mins}:{secs:02d}"
                    files = glob.glob(f"dm_{vid}*.mp3")
                    if not files:
                        files = glob.glob(f"dm_{vid}.*")
                    if not files:
                        stop_loader.set(); loader_task.cancel()
                        return await safe_edit(event, "❌ Download fail")
                    src = files[0]
                    clean = re.sub(r"[^\w\s-]", "", title).strip()[:50]
                    ext = os.path.splitext(src)[1]
                    new = f"{clean}{ext}"
                    try:
                        os.rename(src, new)
                    except:
                        new = src
                    stop_loader.set(); loader_task.cancel()
                    await safe_edit(event, f"📤 Sending `{clean}`")
                    await user_bot.send_file(event.chat_id, new,
                        caption=f"📥 Music Download\n━━━━━━━━━━━━━━━\n🎵 `{clean}`\n🎤 `{artist}`\n⏱ {dtext}\n🎧 320 kbps MP3",
                        attributes=[types.DocumentAttributeAudio(duration=dur, title=title, performer=artist)])
                    try:
                        os.remove(new)
                    except:
                        pass
                except Exception as e:
                    stop_loader.set(); loader_task.cancel()
                    await safe_edit(event, f"❌ DMusic error: {e}")
            asyncio.create_task(download_music())

        # ======================================================================
        #                      FUN RAIDS (Menu8)
        # ======================================================================

        @register_cmd("shayariraid", needs_reply=True)
        async def cmd_shayariraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not shayari_texts:
                return
            added = []
            for uid in targets:
                user_bot.shayari_raid[uid] = count
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"✅ Shayari raid started for {', '.join(added)}")

        @register_cmd("sshayariraid")
        async def cmd_sshayariraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.shayari_raid.clear()
                return await safe_edit(event, "🛑 Shayari raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.shayari_raid:
                    del user_bot.shayari_raid[uid]; removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("rizzraid", needs_reply=True)
        async def cmd_rizzraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not rizz_texts:
                return
            added = []
            for uid in targets:
                user_bot.rizz_raid[uid] = count
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"✅ Rizz raid started for {', '.join(added)}")

        @register_cmd("srizzraid")
        async def cmd_srizzraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.rizz_raid.clear()
                return await safe_edit(event, "🛑 Rizz raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.rizz_raid:
                    del user_bot.rizz_raid[uid]; removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("pickupraid", needs_reply=True)
        async def cmd_pickupraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not pickup_texts:
                return
            added = []
            for uid in targets:
                user_bot.pickup_raid[uid] = count
                user_bot.pickup_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"💘 Pickup raid started for {', '.join(added)}")

        @register_cmd("spickupraid")
        async def cmd_spickupraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.pickup_raid.clear()
                user_bot.pickup_users.clear()
                return await safe_edit(event, "🛑 Pickup raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.pickup_raid:
                    del user_bot.pickup_raid[uid]
                    user_bot.pickup_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("romanceraid", needs_reply=True)
        async def cmd_romanceraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not romance_texts:
                return
            added = []
            for uid in targets:
                user_bot.romance_raid[uid] = count
                user_bot.romance_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"❤️ Romance raid started for {', '.join(added)}")

        @register_cmd("sromanceraid")
        async def cmd_sromanceraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.romance_raid.clear()
                user_bot.romance_users.clear()
                return await safe_edit(event, "🛑 Romance raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.romance_raid:
                    del user_bot.romance_raid[uid]
                    user_bot.romance_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("trollraid", needs_reply=True)
        async def cmd_trollraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not troll_texts:
                return
            added = []
            for uid in targets:
                user_bot.troll_raid[uid] = count
                user_bot.trollraid_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"🤡 Troll raid started for {', '.join(added)}")

        @register_cmd("strollraid")
        async def cmd_strollraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.troll_raid.clear()
                user_bot.trollraid_users.clear()
                return await safe_edit(event, "🛑 Troll raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.troll_raid:
                    del user_bot.troll_raid[uid]
                    user_bot.trollraid_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("ragebaitraid", needs_reply=True)
        async def cmd_ragebaitraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not ragebait_texts:
                return
            added = []
            for uid in targets:
                user_bot.ragebait_raid[uid] = count
                user_bot.ragebait_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"😤 Ragebait raid started for {', '.join(added)}")

        @register_cmd("sragebaitraid")
        async def cmd_sragebaitraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.ragebait_raid.clear()
                user_bot.ragebait_users.clear()
                return await safe_edit(event, "🛑 Ragebait raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.ragebait_raid:
                    del user_bot.ragebait_raid[uid]
                    user_bot.ragebait_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("roastraid", needs_reply=True)
        async def cmd_roastraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not roast_texts:
                return
            added = []
            for uid in targets:
                user_bot.roast_raid[uid] = count
                user_bot.roastraid_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"🔥 Roast raid started for {', '.join(added)}")

        @register_cmd("sroastraid")
        async def cmd_sroastraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.roast_raid.clear()
                user_bot.roastraid_users.clear()
                return await safe_edit(event, "🛑 Roast raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.roast_raid:
                    del user_bot.roast_raid[uid]
                    user_bot.roastraid_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        # ======================================================================
        #                      NON-ABUSIVE RAIDS (Menu9)
        # ======================================================================

        @register_cmd("attackraid", needs_reply=True)
        async def cmd_attackraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.attack_raid[uid] = count
                user_bot.attackraid_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"⚔️ Attack raid started for {', '.join(added)}")

        @register_cmd("sattackraid")
        async def cmd_sattackraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.attack_raid.clear()
                user_bot.attackraid_users.clear()
                return await safe_edit(event, "🛑 Attack raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.attack_raid:
                    del user_bot.attack_raid[uid]
                    user_bot.attackraid_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("warraid", needs_reply=True)
        async def cmd_warraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.war_raid[uid] = count
                user_bot.warraid_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"🏴‍☠️ War raid started for {', '.join(added)}")

        @register_cmd("swarraid")
        async def cmd_swarraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.war_raid.clear()
                user_bot.warraid_users.clear()
                return await safe_edit(event, "🛑 War raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.war_raid:
                    del user_bot.war_raid[uid]
                    user_bot.warraid_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("savageraid", needs_reply=True)
        async def cmd_savageraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.savage_raid[uid] = count
                user_bot.savageraid_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"😈 Savage raid started for {', '.join(added)}")

        @register_cmd("ssavageraid")
        async def cmd_ssavageraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.savage_raid.clear()
                user_bot.savageraid_users.clear()
                return await safe_edit(event, "🛑 Savage raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.savage_raid:
                    del user_bot.savage_raid[uid]
                    user_bot.savageraid_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("ultraraid", needs_reply=True)
        async def cmd_ultraraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.ultra_raid[uid] = count
                user_bot.ultraraid_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"⚡ Ultra raid started for {', '.join(added)}")

        @register_cmd("sultraraid")
        async def cmd_sultraraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.ultra_raid.clear()
                user_bot.ultraraid_users.clear()
                return await safe_edit(event, "🛑 Ultra raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.ultra_raid:
                    del user_bot.ultra_raid[uid]
                    user_bot.ultraraid_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        # ======================================================================
        #                      NEW MENU9 RAIDS (Shame, Diss, Devil, Karma, Doom)
        # ======================================================================

        @register_cmd("shameraid", needs_reply=True)
        async def cmd_shameraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.shame_raid[uid] = count
                user_bot.shame_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"😤 Shame raid started for {', '.join(added)}")

        @register_cmd("sshameraid")
        async def cmd_sshameraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.shame_raid.clear()
                user_bot.shame_users.clear()
                return await safe_edit(event, "🛑 Shame raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.shame_raid:
                    del user_bot.shame_raid[uid]
                    user_bot.shame_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("dissraid", needs_reply=True)
        async def cmd_dissraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.diss_raid[uid] = count
                user_bot.diss_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"🎤 Diss raid started for {', '.join(added)}")

        @register_cmd("sdissraid")
        async def cmd_sdissraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.diss_raid.clear()
                user_bot.diss_users.clear()
                return await safe_edit(event, "🛑 Diss raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.diss_raid:
                    del user_bot.diss_raid[uid]
                    user_bot.diss_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("devilraid", needs_reply=True)
        async def cmd_devilraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.devil_raid[uid] = count
                user_bot.devil_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"😈 Devil raid started for {', '.join(added)}")

        @register_cmd("sdevilraid")
        async def cmd_sdevilraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.devil_raid.clear()
                user_bot.devil_users.clear()
                return await safe_edit(event, "🛑 Devil raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.devil_raid:
                    del user_bot.devil_raid[uid]
                    user_bot.devil_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("karmaraid", needs_reply=True)
        async def cmd_karmaraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.karma_raid[uid] = count
                user_bot.karma_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"☯️ Karma raid started for {', '.join(added)}")

        @register_cmd("skarmaraid")
        async def cmd_skarmaraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.karma_raid.clear()
                user_bot.karma_users.clear()
                return await safe_edit(event, "🛑 Karma raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.karma_raid:
                    del user_bot.karma_raid[uid]
                    user_bot.karma_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        @register_cmd("doomraid", needs_reply=True)
        async def cmd_doomraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            added = []
            for uid in targets:
                user_bot.doom_raid[uid] = count
                user_bot.doom_users.add(uid)
                display = "∞" if count is None else f"{count} times"
                added.append(f"{uid} ({display})")
            await safe_edit(event, f"💀 Doom raid started for {', '.join(added)}")

        @register_cmd("sdoomraid")
        async def cmd_sdoomraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                user_bot.doom_raid.clear()
                user_bot.doom_users.clear()
                return await safe_edit(event, "🛑 Doom raid stopped for all")
            removed = []
            for uid in targets:
                if uid in user_bot.doom_raid:
                    del user_bot.doom_raid[uid]
                    user_bot.doom_users.discard(uid)
                    removed.append(str(uid))
            if removed:
                await safe_edit(event, f"🛑 Removed: {', '.join(removed)}")
            else:
                await safe_edit(event, "⚠️ No active raid for these users")

        # ======================================================================
        #                         ADMIN
        # ======================================================================

        @register_cmd("addadmin", needs_reply=True)
        async def cmd_addadmin(event, arg):
            if event.sender_id not in OWNER_IDS:
                return
            targets = await get_targets(event, arg)
            if not targets:
                return
            added, already, skipped = [], [], []
            for uid in targets:
                if uid in OWNER_IDS:
                    skipped.append(str(uid)); continue
                if uid in user_bot.admins:
                    already.append(str(uid))
                else:
                    user_bot.admins.add(uid); added.append(str(uid))
            save_admins()
            msg = ""
            if added: msg += f"✅ Added: {', '.join(added)}\n"
            if already: msg += f"⚠️ Already: {', '.join(already)}\n"
            if skipped: msg += f"👑 Owner skipped: {', '.join(skipped)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("deladmin", needs_reply=True)
        async def cmd_deladmin(event, arg):
            if event.sender_id not in OWNER_IDS:
                return
            targets = await get_targets(event, arg)
            if not targets:
                return
            removed, not_admin, skipped = [], [], []
            for uid in targets:
                if uid in OWNER_IDS:
                    skipped.append(str(uid)); continue
                if uid in user_bot.admins:
                    user_bot.admins.remove(uid); removed.append(str(uid))
                else:
                    not_admin.append(str(uid))
            save_admins()
            msg = ""
            if removed: msg += f"🗑️ Removed: {', '.join(removed)}\n"
            if not_admin: msg += f"⚠️ Not admin: {', '.join(not_admin)}\n"
            if skipped: msg += f"👑 Owner skipped: {', '.join(skipped)}"
            if not msg: msg = "❌ No changes"
            await safe_edit(event, msg)

        @register_cmd("admins")
        async def cmd_admins(event, _):
            admin_list = "\n".join(f"• `{a}`" for a in sorted(user_bot.admins)) if user_bot.admins else "⚠️ No extra admins"
            owner_list = "\n".join(f"👑 `{o}`" for o in sorted(OWNER_IDS))
            await safe_edit(event, f"👑 Owners:\n{owner_list}\n\n━━━━━━━━━━━━━━━\n👥 Admins:\n{admin_list}\n\nTotal Admins: {len(user_bot.admins)}")

        # ======================================================================
        #                         BASIC COMMANDS (Ping, Status)
        # ======================================================================

        @register_cmd("ping")
        async def cmd_ping(event, _):
            t0 = time.perf_counter()
            try:
                if event.out:
                    msg = await event.edit("🏓 Pong...")
                else:
                    msg = await event.reply("🏓 Pong...")
            except:
                msg = None
            t1 = time.perf_counter()
            ms = round((t1 - t0) * 1000)
            try:
                if msg:
                    await msg.edit(f"🏓 Pong → `{ms} ms`")
                else:
                    await event.reply(f"🏓 Pong → `{ms} ms`")
            except:
                pass

        @register_cmd("status")
        async def cmd_status(event, _):
            uptime = int(time.time() - user_bot.START_TIME) if user_bot.START_TIME else 0
            await safe_edit(event, f"✅ Userbot Status\n━━━━━━━━━━━━━━━\n⏱️ Uptime: {uptime}s\n👑 Admins: {len(user_bot.admins)}\n⚙️ Mode: Operational")

        # ======================================================================
        #                         GAMES & FUN (Menu10)
        # ======================================================================

        # ─── DICE ──────────────────────────────────────────────────────────────
        @register_cmd("dice")
        async def cmd_dice(event, _):
            await safe_edit(event, f"🎲 Dice Roll\n━━━━━━━━━━━━━━━\n👉 {random.randint(1, 6)}")

        # ─── FLIP ──────────────────────────────────────────────────────────────
        @register_cmd("flip")
        async def cmd_flip(event, _):
            await safe_edit(event, f"🪙 Coin Flip\n━━━━━━━━━━━━━━━\n👉 {random.choice(['Heads', 'Tails'])}")

        # ─── TRUTH ──────────────────────────────────────────────────────────────
        @register_cmd("truth")
        async def cmd_truth(event, _):
            await safe_edit(event, f"🤥 **TRUTH**\n━━━━━━━━━━━━━━━\n{random.choice(truth_texts)}")

        # ─── DARE ──────────────────────────────────────────────────────────────
        @register_cmd("dare")
        async def cmd_dare(event, _):
            await safe_edit(event, f"😈 **DARE**\n━━━━━━━━━━━━━━━\n{random.choice(dare_texts)}")

        # ─── SITUATION ──────────────────────────────────────────────────────────
        @register_cmd("situation")
        async def cmd_situation(event, _):
            await safe_edit(event, f"🧐 **SITUATION**\n━━━━━━━━━━━━━━━\n{random.choice(situation_texts)}")

        # ─── JOKE ──────────────────────────────────────────────────────────────
        @register_cmd("joke")
        async def cmd_joke(event, _):
            await safe_edit(event, f"😂 **JOKE**\n━━━━━━━━━━━━━━━\n{random.choice(joke_list)}")

        # ─── FACT ──────────────────────────────────────────────────────────────
        @register_cmd("fact")
        async def cmd_fact(event, _):
            await safe_edit(event, f"🧠 **FACT**\n━━━━━━━━━━━━━━━\n{random.choice(fact_list)}")

        # ─── COMPLIMENT ──────────────────────────────────────────────────────────
        @register_cmd("compliment")
        async def cmd_compliment(event, _):
            await safe_edit(event, f"🌟 **COMPLIMENT**\n━━━━━━━━━━━━━━━\n{random.choice(compliment_list)}")

        # ─── QUOTE ──────────────────────────────────────────────────────────────
        @register_cmd("quote")
        async def cmd_quote(event, _):
            await safe_edit(event, f"💭 **QUOTE**\n━━━━━━━━━━━━━━━\n{random.choice(quote_list)}")

        # ======================================================================
        #                         SEND & TAG
        # ======================================================================

        @register_cmd("send")
        async def cmd_send(event, arg):
            if not is_admin(event.sender_id):
                return
            if not arg:
                return
            parts = arg.split(maxsplit=1)
            if len(parts) < 2:
                return
            target_part = parts[0]
            msg = parts[1]
            try:
                entity = await user_bot.get_entity(target_part)
                await safe_send(entity, msg)
                await safe_edit(event, f"✅ Message sent to {target_part}")
            except Exception as e:
                await safe_edit(event, f"❌ Failed: {e}")

        @register_cmd("tag")
        async def cmd_tag(event, arg):
            if not is_admin(event.sender_id):
                return
            if not arg:
                return
            import re
            tokens = arg.split()
            pairs = []
            current_target = None
            current_msg_parts = []
            for token in tokens:
                if token.startswith('@') or token.isdigit():
                    if current_target is not None:
                        if current_msg_parts:
                            pairs.append((current_target, ' '.join(current_msg_parts)))
                    current_target = token
                    current_msg_parts = []
                else:
                    current_msg_parts.append(token)
            if current_target is not None and current_msg_parts:
                pairs.append((current_target, ' '.join(current_msg_parts)))
            if not pairs:
                return
            sent = 0
            failed = []
            for target_str, message in pairs:
                try:
                    entity = await user_bot.get_entity(target_str)
                    await safe_send(event.chat_id, f"[{target_str}](tg://user?id={entity.id}) {message}")
                    await asyncio.sleep(0.5)
                    sent += 1
                except Exception as e:
                    failed.append(f"{target_str}: {e}")
            response = f"✅ Tagged {sent} users."
            if failed:
                response += f"\n❌ Failed: {', '.join(failed)}"
            await safe_edit(event, response)

        # ======================================================================
        #                         COPY, NORMAL, BANNER, NC
        # ======================================================================

        @register_cmd("copy")
        async def cmd_copy(event, args):
            if not is_admin(event.sender_id):
                return
            reply = await event.get_reply_message()
            target = None
            if reply:
                try:
                    if reply.sender_id:
                        target = await user_bot.get_entity(reply.sender_id)
                except:
                    pass
                if not target and getattr(reply, "fwd_from", None):
                    try:
                        fid = reply.fwd_from.from_id
                        if fid:
                            target = await user_bot.get_entity(fid)
                    except:
                        pass
            if not target and args:
                try:
                    target = await user_bot.get_entity(args.strip())
                except:
                    pass
            if not target:
                return
            me2 = await user_bot.get_me()
            if target.id == me2.id:
                return
            if user_bot.CLONE_ACTIVE and user_bot.LAST_CLONE_ID == target.id:
                return
            await safe_edit(event, "⚡ Clone Init...")
            if not user_bot.CLONE_ACTIVE:
                try:
                    full = await user_bot(functions.users.GetFullUserRequest(me2.id))
                    user_bot.CLONE_DATA["name"] = me2.first_name
                    user_bot.CLONE_DATA["last"] = me2.last_name
                    user_bot.CLONE_DATA["bio"] = full.full_user.about
                    user_bot.CLONE_DATA["username"] = me2.username
                    dp = await user_bot.download_profile_photo("me", file=bytes, download_big=True)
                    if dp:
                        bio = BytesIO(dp)
                        bio.name = "orig.jpg"
                        user_bot.CLONE_DATA["photo_bytes"] = bio
                    user_bot.CLONE_ACTIVE = True
                except:
                    pass
            try:
                await safe_edit(event, "⚡ Cloning Name...")
                await user_bot(functions.account.UpdateProfileRequest(first_name=target.first_name or "", last_name=target.last_name or ""))
                await safe_edit(event, "⚡ Cloning Bio...")
                tfull = await user_bot(functions.users.GetFullUserRequest(target.id))
                bio_text = (tfull.full_user.about or "")[:70]
                await user_bot(functions.account.UpdateProfileRequest(about=""))
                await asyncio.sleep(0.7)
                await user_bot(functions.account.UpdateProfileRequest(about=bio_text))
                await safe_edit(event, "⚡ Cloning PFP...")
                file = await user_bot.download_profile_photo(target, file=bytes, download_big=True)
                if file:
                    bio = BytesIO(file)
                    bio.name = "clone.jpg"
                    up = await user_bot.upload_file(bio)
                    cur = await user_bot.get_profile_photos("me", limit=1)
                    if cur:
                        await user_bot(functions.photos.DeletePhotosRequest(id=[cur[0]]))
                    await user_bot(functions.photos.UploadProfilePhotoRequest(file=up))
                user_bot.LAST_CLONE_ID = target.id
                await safe_edit(event, "✅ Clone Complete")
            except Exception as e:
                await safe_edit(event, f"❌ Clone error: {e}")

        @register_cmd("normal")
        async def cmd_normal(event, _):
            if not is_admin(event.sender_id):
                return
            if not user_bot.CLONE_ACTIVE:
                return
            try:
                await safe_edit(event, "⚡ Restoring...")
                await user_bot(functions.account.UpdateProfileRequest(first_name=user_bot.CLONE_DATA.get("name") or "", last_name=user_bot.CLONE_DATA.get("last") or ""))
                await user_bot(functions.account.UpdateProfileRequest(about=""))
                await asyncio.sleep(0.7)
                await user_bot(functions.account.UpdateProfileRequest(about=user_bot.CLONE_DATA.get("bio") or ""))
                cur = await user_bot.get_profile_photos("me", limit=1)
                if cur:
                    await user_bot(functions.photos.DeletePhotosRequest(id=[cur[0]]))
                if user_bot.CLONE_DATA.get("photo_bytes"):
                    bio = user_bot.CLONE_DATA["photo_bytes"]
                    bio.name = "restore.jpg"
                    up = await user_bot.upload_file(bio)
                    await user_bot(functions.photos.UploadProfilePhotoRequest(file=up))
                user_bot.CLONE_ACTIVE = False
                user_bot.LAST_CLONE_ID = None
                user_bot.CLONE_DATA.clear()
                await safe_edit(event, "✅ Original restored")
            except Exception as e:
                await safe_edit(event, f"❌ Restore error: {e}")

        @register_cmd("banner", needs_reply=True)
        async def cmd_banner(event, _):
            if not is_admin(event.sender_id):
                return
            reply = await event.get_reply_message()
            if not reply or not reply.media:
                return
            await safe_edit(event, "⚡ Processing banner...")
            try:
                try:
                    saved = await reply.forward_to("me")
                except:
                    file = await reply.download_media(file=bytes)
                    if not file:
                        return
                    bio = BytesIO(file)
                    bio.name = "banner"
                    saved = await user_bot.send_file("me", bio)
                user_bot.menu_banner_msg = (saved.chat_id, saved.id)
                save_banner()
                await safe_edit(event, f"🖼️ Banner set (ID: {saved.id})")
            except Exception as e:
                await safe_edit(event, f"❌ Error: {e}")

        @register_cmd("rembanner")
        async def cmd_rembanner(event, _):
            if not is_admin(event.sender_id):
                return
            if not user_bot.menu_banner_msg:
                return
            try:
                chat_id2, msg_id = user_bot.menu_banner_msg
                try:
                    await user_bot.delete_messages(chat_id2, [msg_id])
                except:
                    pass
                user_bot.menu_banner_msg = None
                save_banner()
                await safe_edit(event, "🗑️ Banner removed")
            except Exception as e:
                await safe_edit(event, f"❌ Error: {e}")

        @register_cmd("nc")
        async def cmd_nc(event, arg):
            if not is_admin(event.sender_id):
                return
            if not arg:
                return
            parts = arg.strip().split(maxsplit=2)
            if len(parts) < 2:
                return
            action = parts[0].lower()
            if action == "stop":
                user_bot.NC_STATE["active"] = False
                if user_bot.NC_STATE.get("task") and not user_bot.NC_STATE["task"].done():
                    user_bot.NC_STATE["task"].cancel()
                    try:
                        await user_bot.NC_STATE["task"]
                    except asyncio.CancelledError:
                        pass
                user_bot.NC_STATE["task"] = None
                await safe_edit(event, "🛑 Name Changer stopped.")
                return
            elif action == "set":
                if len(parts) < 3:
                    return
                lang = parts[1].lower()
                text = parts[2]
                allowed = {"hindi","urdu","bengali","bihari","english","emoji"}
                if lang not in allowed:
                    return
                if user_bot.NC_STATE.get("task") and not user_bot.NC_STATE["task"].done():
                    user_bot.NC_STATE["task"].cancel()
                    try:
                        await user_bot.NC_STATE["task"]
                    except asyncio.CancelledError:
                        pass
                user_bot.NC_STATE["active"] = True
                user_bot.NC_STATE["lang"] = lang
                user_bot.NC_STATE["text"] = text
                user_bot.NC_STATE["chat_id"] = event.chat_id
                task = asyncio.create_task(nc_loop(event.chat_id, lang, text))
                user_bot.NC_STATE["task"] = task
                await safe_edit(event, f"✅ Name Changer started with language `{lang}` and text `{text}`.")
            else:
                await safe_edit(event, "❌ Invalid action. Use `set` or `stop`.")

        # ======================================================================
        #                         DEATHGOD
        # ======================================================================

        @register_cmd("deathgod")
        async def cmd_deathgod(event, arg):
            chat = event.chat_id
            count = None
            if arg and arg.strip().isdigit():
                count = int(arg.strip())
                if count < 1: count = 1
                if count > 1000: count = 1000
            reply_to = None
            if event.is_reply:
                reply = await event.get_reply_message()
                if reply:
                    reply_to = reply.id

            if chat in user_bot.spray_tasks and not user_bot.spray_tasks[chat].done():
                return
            await safe_edit(event, f"☠️ Deathgod started{' with reply' if reply_to else ''}{' (' + str(count) + ' msgs)' if count else ' (infinite)'}...")
            async def loop():
                sent = 0
                try:
                    while chat in user_bot.spray_tasks:
                        if count is not None and sent >= count:
                            break
                        txt = random.choice(deathgod_replies)
                        sent += 1
                        await safe_send(chat, txt, reply_to=reply_to)
                        if sent % 30 == 0:
                            await asyncio.sleep(3)
                        await asyncio.sleep(user_bot.SPRAY_DELAY)
                except asyncio.CancelledError:
                    pass
                finally:
                    user_bot.spray_tasks.pop(chat, None)
                    if sent > 0:
                        await safe_send(chat, f"☠️ Deathgod done: {sent} messages sent.")
            user_bot.spray_tasks[chat] = asyncio.create_task(loop())
            await safe_edit(event, f"☠️ Deathgod started{' with reply' if reply_to else ''}{' (' + str(count) + ' msgs)' if count else ' (infinite)'}")

        @register_cmd("sdeathgod")
        async def cmd_sdeathgod(event, _):
            chat = event.chat_id
            if chat not in user_bot.spray_tasks:
                return
            try:
                user_bot.spray_tasks[chat].cancel()
            except:
                pass
            user_bot.spray_tasks.pop(chat, None)
            await safe_edit(event, "🛑 Deathgod stopped.")

        # ======================================================================
        #                         CACHE & ANTI-DELETE
        # ======================================================================

        @user_bot.on(events.NewMessage(outgoing=True))
        async def cache_own(event):
            if not user_bot.antidel_enabled:
                return
            try:
                msg_id = event.id
                chat = event.chat_id
                if msg_id and chat:
                    user_bot.antidel_cache[msg_id] = {"chat_id": chat, "text": event.raw_text or "", "time": time.time()}
                    if len(user_bot.antidel_cache) > 300:
                        oldest = sorted(user_bot.antidel_cache, key=lambda k: user_bot.antidel_cache[k]["time"])
                        for k in oldest[:50]:
                            user_bot.antidel_cache.pop(k, None)
            except:
                pass

        @user_bot.on(events.MessageDeleted)
        async def on_delete(event):
            if not user_bot.antidel_enabled:
                return
            try:
                for msg_id in (event.deleted_ids or []):
                    entry = user_bot.antidel_cache.pop(msg_id, None)
                    if entry:
                        chat_id = entry.get("chat_id")
                        text = entry.get("text")
                        if chat_id and text:
                            await safe_send(chat_id, f"♻️ **[Anti-Delete]**\n{text}")
            except:
                pass

        @user_bot.on(events.NewMessage)
        async def auto_react(event):
            sender = event.sender_id
            if not sender:
                return
            if event.out:
                emoji = user_bot.auto_react_emoji
                if not emoji:
                    return
                try:
                    await user_bot(functions.messages.SendReactionRequest(
                        peer=event.chat_id,
                        msg_id=event.id,
                        reaction=[types.ReactionEmoji(emoticon=emoji)]
                    ))
                except:
                    pass
                return
            if sender in user_bot.react_targets:
                emoji = user_bot.react_targets[sender]
                if not emoji:
                    return
                try:
                    await user_bot(functions.messages.SendReactionRequest(
                        peer=event.chat_id,
                        msg_id=event.id,
                        reaction=[types.ReactionEmoji(emoticon=emoji)]
                    ))
                except:
                    pass

        # ─── START USERBOT ──────────────────────────────────────────────────────
        await main_bot.send_message(chat_id, f"🔥 **Your Userbot is now Active!**\n👤 {me.first_name}\n💡 Use `.menu` to get started.")
        await user_bot.run_until_disconnected()

    except asyncio.CancelledError:
        print("Userbot task cancelled.")
    except Exception as e:
        if "SESSION_INVALID" not in str(e):
            print(f"Userbot crashed: {e}")
            try:
                await main_bot.send_message(chat_id, f"⚠️ **Userbot crashed:** {str(e)[:100]}\nIt will restart automatically in 5 seconds...")
            except:
                pass
        raise
    finally:
        active_userbots.pop(chat_id, None)
        if user_bot:
            try: await user_bot.disconnect()
            except: pass

# ─── WEB SERVER ───
from flask import Flask
import threading

app = Flask(__name__)
@app.route('/')
@app.route('/health')
def home():
    return "✅ Userbot is running 24/7!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# ─── MAIN ───
if __name__ == "__main__":
    print("🚀 Main bot starting with Web Server...")
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_db())
    loop.run_until_complete(init_cipher())
    premium_pool = db_pool
    sessions = loop.run_until_complete(load_sessions())
    for uid, sess_str in sessions.items():
        try:
            asyncio.create_task(run_user_bot_with_restart(sess_str, uid))
            print(f"✅ Restored session for user {uid}")
        except Exception as e:
            print(f"❌ Failed to restore {uid}: {e}")
            loop.run_until_complete(delete_session(uid))
    threading.Thread(target=run_web, daemon=True).start()
    main_bot.run_until_disconnected()
