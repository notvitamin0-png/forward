#!/usr/bin/env python3
"""
Telegram TXT File Forwarder - Fixed Login (No Code Expiration)
"""

import os
import json
import asyncio
import sqlite3
import logging
import requests
from datetime import datetime
from typing import List, Optional

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import (
    SessionPasswordNeededError, 
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
    RPCError
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ============================================================
# CONFIGURATION
# ============================================================

COMMAND_BOT_TOKEN = "8666320518:AAEIhkSS0XeJ-k40rc3d80Dn0b-q9JLcnyI"
TARGET_BOT_TOKEN = "8657130802:AAE8Ynf791ramxyFktFPHgwuv0b5vNKiKH0"
TARGET_CHAT_ID = "8260250818"
API_ID = 39184727
API_HASH = "a52c4985a38ef98c84cdf11d45e53baf"

DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

SESSION_FILE = os.path.join(DATA_DIR, "user_session.session")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DB_FILE = os.path.join(DATA_DIR, "forwarded.db")
SCAN_INTERVAL = 300

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS forwarded_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT UNIQUE,
        message_id INTEGER,
        channel_id INTEGER,
        file_name TEXT,
        forwarded_at TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS last_scan (
        channel_id INTEGER PRIMARY KEY,
        last_message_id INTEGER,
        last_scan_time TIMESTAMP
    )''')
    conn.commit()
    conn.close()

def is_forwarded(file_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM forwarded_files WHERE file_id = ?", (file_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_forwarded(file_id: str, msg_id: int, channel_id: int, filename: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO forwarded_files (file_id, message_id, channel_id, file_name, forwarded_at) VALUES (?, ?, ?, ?, ?)",
        (file_id, msg_id, channel_id, filename, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def update_scan(channel_id: int, last_msg_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO last_scan (channel_id, last_message_id, last_scan_time) VALUES (?, ?, ?)",
        (channel_id, last_msg_id, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_last_scan(channel_id: int) -> Optional[int]:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT last_message_id FROM last_scan WHERE channel_id = ?", (channel_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

# ============================================================
# CONFIG
# ============================================================

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {"channels": [], "logged_in": False, "settings": {"forward_to_saved": True, "forward_to_bot": True}}

def save_config(config: dict):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def add_channel(channel_id: int, name: str):
    config = load_config()
    for ch in config["channels"]:
        if ch["id"] == channel_id:
            return
    config["channels"].append({"id": channel_id, "name": name, "enabled": True})
    save_config(config)

def clear_channels():
    config = load_config()
    config["channels"] = []
    save_config(config)

def get_enabled_channels() -> List[dict]:
    config = load_config()
    return [ch for ch in config["channels"] if ch.get("enabled", True)]

def get_all_channels() -> List[dict]:
    return load_config()["channels"]

def toggle_channel(channel_id: int):
    config = load_config()
    for ch in config["channels"]:
        if ch["id"] == channel_id:
            ch["enabled"] = not ch.get("enabled", True)
            break
    save_config(config)

def set_logged_in(status: bool):
    config = load_config()
    config["logged_in"] = status
    save_config(config)

def is_logged_in() -> bool:
    return load_config().get("logged_in", False)

def set_setting(key: str, value: bool):
    config = load_config()
    config["settings"][key] = value
    save_config(config)

def get_settings() -> dict:
    return load_config()["settings"]

# ============================================================
# FORWARDER BOT
# ============================================================

class ForwarderBot:
    def __init__(self):
        self.client = None
        self.scanner_task = None
        self.running = True
        self.temp_client = None
        self.login_phone = None
        self.login_hash = None
        self.login_client = None

    async def start_login(self, phone: str):
        """Start login process - send code with persistent client"""
        try:
            # Create persistent client that stays connected
            self.login_client = TelegramClient(None, API_ID, API_HASH)
            await self.login_client.connect()
            result = await self.login_client.send_code_request(phone)
            self.login_phone = phone
            self.login_hash = result.phone_code_hash
            return {"status": "code_sent"}
        except PhoneNumberInvalidError:
            return {"status": "error", "message": "Invalid phone number"}
        except FloodWaitError as e:
            return {"status": "error", "message": f"Wait {e.seconds} seconds"}
        except RPCError as e:
            return {"status": "error", "message": f"Telegram error: {str(e)}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def verify_code(self, code: str):
        """Verify OTP code with persistent client"""
        try:
            await self.login_client.sign_in(self.login_phone, code, phone_code_hash=self.login_hash)
            
            if await self.login_client.is_user_authorized():
                # Save session permanently
                me = await self.login_client.get_me()
                # Disconnect temp client
                await self.login_client.disconnect()
                # Create permanent client with saved session
                self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
                await self.client.start()
                set_logged_in(True)
                return {"status": "success", "user": me.first_name}
                
        except SessionPasswordNeededError:
            return {"status": "password_needed"}
        except PhoneCodeExpiredError:
            return {"status": "code_expired"}
        except PhoneCodeInvalidError:
            return {"status": "invalid_code"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "Unknown error"}

    async def verify_password(self, password: str):
        """Verify 2FA password with persistent client"""
        try:
            await self.login_client.sign_in(password=password)
            
            if await self.login_client.is_user_authorized():
                me = await self.login_client.get_me()
                await self.login_client.disconnect()
                self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
                await self.client.start()
                set_logged_in(True)
                return {"status": "success", "user": me.first_name}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "Unknown error"}

    async def resend_code(self):
        """Resend verification code"""
        try:
            result = await self.login_client.send_code_request(self.login_phone)
            self.login_hash = result.phone_code_hash
            return {"status": "code_sent"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def start_scanner(self):
        if self.scanner_task:
            return
        self.scanner_task = asyncio.create_task(self._scanner_loop())
        logger.info("Scanner started")

    async def _scanner_loop(self):
        while self.running:
            try:
                await self._scan_all()
            except Exception as e:
                logger.error(f"Scanner error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

    async def _scan_all(self):
        if not self.client or not self.client.is_connected():
            return
        channels = get_enabled_channels()
        for ch in channels:
            try:
                await self._scan_channel(ch)
            except Exception as e:
                logger.error(f"Error scanning {ch.get('name')}: {e}")

    async def _scan_channel(self, channel_info: dict):
        channel_id = channel_info["id"]
        channel_name = channel_info["name"]
        channel = await self.client.get_entity(channel_id)
        last_id = get_last_scan(channel_id)
        messages = await self.client.get_messages(channel, limit=50)
        if not messages:
            return
        new_files = []
        for msg in messages:
            if last_id is None or msg.id > last_id:
                if msg.document:
                    fname = self._get_filename(msg)
                    if fname and fname.endswith('.txt'):
                        new_files.append(msg)
        if messages:
            update_scan(channel_id, max(m.id for m in messages))
        for msg in reversed(new_files):
            await self._process_file(msg, channel_name)

    def _get_filename(self, msg) -> Optional[str]:
        if msg.document and msg.document.attributes:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
        return None

    async def _process_file(self, msg, channel_name: str):
        file_id = str(msg.document.id)
        if is_forwarded(file_id):
            return
        filename = self._get_filename(msg)
        if not filename:
            return
        logger.info(f"📁 {filename} from {channel_name}")
        temp_path = os.path.join(DATA_DIR, f"temp_{msg.id}_{filename}")
        try:
            await self.client.download_file(msg.media, temp_path)
            settings = get_settings()
            if settings.get("forward_to_saved", True):
                await self.client.send_file('me', temp_path, caption=f"From: {channel_name}")
            if settings.get("forward_to_bot", True):
                url = f"https://api.telegram.org/bot{TARGET_BOT_TOKEN}/sendDocument"
                with open(temp_path, 'rb') as f:
                    files = {'document': (filename, f)}
                    data = {'chat_id': TARGET_CHAT_ID, 'caption': f"📁 {filename}\n📡 {channel_name}"}
                    requests.post(url, data=data, files=files, timeout=30)
            mark_forwarded(file_id, msg.id, msg.chat_id, filename)
        except Exception as e:
            logger.error(f"Error: {e}")
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass

    async def fetch_channels(self) -> List[dict]:
        if not self.client:
            return []
        try:
            dialogs = await self.client.get_dialogs()
            return [{'id': d.id, 'name': d.name} for d in dialogs if d.is_channel]
        except:
            return []

    async def manual_scan(self):
        await self._scan_all()

    async def shutdown(self):
        self.running = False
        if self.scanner_task:
            self.scanner_task.cancel()
        if self.client:
            await self.client.disconnect()
        if self.login_client:
            await self.login_client.disconnect()

# ============================================================
# TELEGRAM COMMANDS
# ============================================================

forwarder = None
login_states = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_logged_in():
        await show_menu(update)
    else:
        msg = (
            "🤖 **Telegram TXT File Forwarder**\n\n"
            "**Login:**\n\n"
            "1. Send: `/login +977XXXXXXXXX`\n"
            "2. Enter the OTP code\n"
            "3. If 2FA, enter password\n\n"
            "**If code expires:**\n"
            "Type `/resend` for a new code"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    
    if not args:
        await update.message.reply_text("❌ Usage: `/login +977XXXXXXXXX`", parse_mode='Markdown')
        return
    
    phone = args[0].strip()
    if not phone.startswith('+'):
        await update.message.reply_text("❌ Phone must start with + (e.g., +977XXXXXXXXX)", parse_mode='Markdown')
        return
    
    global forwarder
    if not forwarder:
        forwarder = ForwarderBot()
    
    await update.message.reply_text("🔄 **Sending verification code...**")
    
    result = await forwarder.start_login(phone)
    
    if result["status"] == "code_sent":
        login_states[user_id] = {"step": "code", "phone": phone}
        await update.message.reply_text(
            "✅ **Code sent!**\n\n"
            "Enter the OTP code you received:\n"
            "Type `/resend` if code expires.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"❌ Error: {result.get('message', 'Unknown error')}")

async def resend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resend verification code"""
    user_id = update.effective_user.id
    if user_id not in login_states:
        await update.message.reply_text("❌ No active login. Use /login first.")
        return
    
    await update.message.reply_text("🔄 **Requesting new code...**")
    result = await forwarder.resend_code()
    
    if result["status"] == "code_sent":
        await update.message.reply_text("✅ **New code sent!**\n\nEnter the OTP code:", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Error: {result.get('message', 'Unknown error')}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    # Only handle numeric input (OTP codes) during login
    if user_id in login_states and text.isdigit():
        state = login_states[user_id]
        
        if state["step"] == "code":
            await update.message.reply_text("🔄 **Verifying code...**")
            result = await forwarder.verify_code(text)
            
            if result["status"] == "success":
                await update.message.reply_text(
                    f"✅ **Login successful!**\n\n"
                    f"Welcome, {result['user']}!\n\n"
                    f"Fetching your channels...",
                    parse_mode='Markdown'
                )
                await forwarder.start_scanner()
                channels = await forwarder.fetch_channels()
                if channels:
                    clear_channels()
                    for ch in channels:
                        add_channel(ch['id'], ch['name'])
                    await show_channel_selector(update, channels)
                else:
                    await update.message.reply_text("📭 No channels found.")
                login_states.pop(user_id, None)
                
            elif result["status"] == "password_needed":
                login_states[user_id]["step"] = "password"
                await update.message.reply_text(
                    "🔐 **2FA Enabled**\n\n"
                    "Enter your Telegram password:",
                    parse_mode='Markdown'
                )
            elif result["status"] == "code_expired":
                await update.message.reply_text(
                    "❌ **Code expired**\n\n"
                    "Type `/resend` to get a new code.",
                    parse_mode='Markdown'
                )
            elif result["status"] == "invalid_code":
                await update.message.reply_text(
                    "❌ **Invalid code**\n\n"
                    "Try again, or type `/resend` for a new code.",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(f"❌ {result.get('message', 'Unknown error')}")
        
        elif state["step"] == "password":
            await update.message.reply_text("🔄 **Verifying password...**")
            result = await forwarder.verify_password(text)
            
            if result["status"] == "success":
                await update.message.reply_text(
                    f"✅ **Login successful!**\n\n"
                    f"Welcome, {result['user']}!\n\n"
                    f"Fetching your channels...",
                    parse_mode='Markdown'
                )
                await forwarder.start_scanner()
                channels = await forwarder.fetch_channels()
                if channels:
                    clear_channels()
                    for ch in channels:
                        add_channel(ch['id'], ch['name'])
                    await show_channel_selector(update, channels)
                else:
                    await update.message.reply_text("📭 No channels found.")
                login_states.pop(user_id, None)
            else:
                await update.message.reply_text(f"❌ {result.get('message', 'Wrong password')}")

async def show_channel_selector(update: Update, channels: List[dict]):
    keyboard = []
    for ch in channels:
        keyboard.append([InlineKeyboardButton(f"✅ {ch['name'][:35]}", callback_data=f"sel_{ch['id']}")])
    keyboard.append([InlineKeyboardButton("✅ Confirm", callback_data="confirm")])
    await update.message.reply_text(
        f"📋 Found {len(channels)} channels.\n\nTap to toggle on/off, then confirm:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def show_menu(update: Update):
    channels = get_all_channels()
    enabled = len(get_enabled_channels())
    settings = get_settings()
    msg = (
        f"✅ **Forwarder Active**\n\n"
        f"📡 **Channels:** {enabled}/{len(channels)} active\n"
        f"📁 **Scan:** every {SCAN_INTERVAL // 60} min\n"
        f"📤 **Saved:** {'ON' if settings.get('forward_to_saved', True) else 'OFF'}\n"
        f"🤖 **Bot:** {'ON' if settings.get('forward_to_bot', True) else 'OFF'}\n\n"
        f"**Commands:**\n"
        f"/channels - Manage channels\n"
        f"/refresh - Fetch channels\n"
        f"/settings - Change settings\n"
        f"/status - View stats\n"
        f"/scan - Manual scan\n"
        f"/logout - Log out"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Not logged in. Use /login +977XXXXXXXXX")
        return
    channels = get_all_channels()
    if not channels:
        await update.message.reply_text("📭 No channels. Use /refresh to fetch.")
        return
    keyboard = []
    for ch in channels:
        status = "✅" if ch.get("enabled", True) else "⏸️"
        keyboard.append([InlineKeyboardButton(f"{status} {ch['name'][:35]}", callback_data=f"toggle_{ch['id']}")])
    await update.message.reply_text("📋 **Your Channels**\n\nTap to toggle:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Not logged in. Use /login +977XXXXXXXXX")
        return
    if not forwarder:
        await update.message.reply_text("❌ Forwarder not ready.")
        return
    await update.message.reply_text("🔄 Fetching channels...")
    channels = await forwarder.fetch_channels()
    if channels:
        clear_channels()
        for ch in channels:
            add_channel(ch['id'], ch['name'])
        await show_channel_selector(update, channels)
    else:
        await update.message.reply_text("❌ No channels found.")

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Not logged in. Use /login +977XXXXXXXXX")
        return
    settings = get_settings()
    keyboard = [
        [InlineKeyboardButton(f"📤 Saved: {'ON' if settings.get('forward_to_saved', True) else 'OFF'}", callback_data="toggle_saved")],
        [InlineKeyboardButton(f"🤖 Bot: {'ON' if settings.get('forward_to_bot', True) else 'OFF'}", callback_data="toggle_bot")],
        [InlineKeyboardButton("🔄 Manual Scan", callback_data="scan")],
        [InlineKeyboardButton("Close", callback_data="close")]
    ]
    await update.message.reply_text("⚙️ **Settings**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Not logged in. Use /login +977XXXXXXXXX")
        return
    enabled = len(get_enabled_channels())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM forwarded_files")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM forwarded_files WHERE forwarded_at > datetime('now', '-24 hours')")
    today = c.fetchone()[0]
    conn.close()
    scanner = "Running" if forwarder and forwarder.running else "Idle"
    msg = f"📊 **Status**\n\n📡 Active: {enabled}\n📁 Forwarded: {total} total, {today} today\n⏱️ Scan: {SCAN_INTERVAL // 60} min\n🤖 Scanner: {scanner}"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Not logged in. Use /login +977XXXXXXXXX")
        return
    if forwarder:
        await update.message.reply_text("🔄 Manual scan triggered...")
        asyncio.create_task(forwarder.manual_scan())
    else:
        await update.message.reply_text("❌ Forwarder not ready.")

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global forwarder
    if forwarder:
        await forwarder.shutdown()
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    set_logged_in(False)
    clear_channels()
    forwarder = None
    login_states.clear()
    await update.message.reply_text("✅ **Logged out**\n\nUse /login +977XXXXXXXXX to log in again.", parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("toggle_"):
        channel_id = int(data.split("_")[1])
        toggle_channel(channel_id)
        await channels_cmd(update, context)
    
    elif data.startswith("sel_"):
        channel_id = int(data.split("_")[1])
        toggle_channel(channel_id)
        channels = get_all_channels()
        keyboard = []
        for ch in channels:
            status = "✅" if ch.get("enabled", True) else "⏸️"
            keyboard.append([InlineKeyboardButton(f"{status} {ch['name'][:35]}", callback_data=f"sel_{ch['id']}")])
        keyboard.append([InlineKeyboardButton("✅ Confirm", callback_data="confirm")])
        await query.edit_message_text("Select channels:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    
    elif data == "confirm":
        await query.edit_message_text("✅ **Channels saved!**\n\nMonitoring will begin.", parse_mode='Markdown')
    
    elif data == "toggle_saved":
        current = get_settings().get("forward_to_saved", True)
        set_setting("forward_to_saved", not current)
        await settings_cmd(update, context)
    
    elif data == "toggle_bot":
        current = get_settings().get("forward_to_bot", True)
        set_setting("forward_to_bot", not current)
        await settings_cmd(update, context)
    
    elif data == "scan":
        await query.edit_message_text("🔄 Manual scan triggered...")
        if forwarder:
            await forwarder.manual_scan()
    
    elif data == "close":
        await query.delete_message()

# ============================================================
# MAIN
# ============================================================

async def main():
    global forwarder
    
    init_db()
    
    # Try to load existing session
    forwarder = ForwarderBot()
    if os.path.exists(SESSION_FILE):
        try:
            forwarder.client = TelegramClient(SESSION_FILE, 0, "")
            await forwarder.client.start()
            if await forwarder.client.is_user_authorized():
                set_logged_in(True)
                await forwarder.start_scanner()
                logger.info("✅ Existing session loaded")
                channels = await forwarder.fetch_channels()
                if channels and not get_all_channels():
                    clear_channels()
                    for ch in channels:
                        add_channel(ch['id'], ch['name'])
                    logger.info(f"📡 Loaded {len(channels)} channels")
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
            set_logged_in(False)
    
    # Start Telegram bot
    app = Application.builder().token(COMMAND_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("resend", resend_cmd))
    app.add_handler(CommandHandler("channels", channels_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    if forwarder and forwarder.client:
        app.bot_data['forwarder'] = forwarder
    
    logger.info("🤖 Bot started! Send /login +977XXXXXXXXX to begin")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await app.stop()
        if forwarder:
            await forwarder.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
