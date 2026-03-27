#!/usr/bin/env python3
"""
Telegram TXT File Forwarder - Railway Ready (Fixed Login with Resend)
"""

import os
import sys
import json
import asyncio
import sqlite3
import logging
import traceback
import requests
from datetime import datetime
from typing import Dict, List, Optional

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import (
    SessionPasswordNeededError, 
    PhoneCodeExpiredError, 
    PhoneCodeInvalidError,
    PhoneNumberInvalidError
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ============================================================
# CONFIGURATION
# ============================================================

COMMAND_BOT_TOKEN = "8666320518:AAEIhkSS0XeJ-k40rc3d80Dn0b-q9JLcnyI"
TARGET_BOT_TOKEN = "8657130802:AAE8Ynf791ramxyFktFPHgwuv0b5vNKiKH0"
TARGET_CHAT_ID = "8260250818"

DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

SESSION_FILE = os.path.join(DATA_DIR, "user_session.session")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
DB_FILE = os.path.join(DATA_DIR, "forwarded.db")
SCAN_INTERVAL = 300

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# DATABASE UTILITIES
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

def is_file_forwarded(file_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM forwarded_files WHERE file_id = ?", (file_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_file_forwarded(file_id: str, message_id: int, channel_id: int, file_name: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO forwarded_files (file_id, message_id, channel_id, file_name, forwarded_at) VALUES (?, ?, ?, ?, ?)",
        (file_id, message_id, channel_id, file_name, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def update_last_scan(channel_id: int, last_message_id: int):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO last_scan (channel_id, last_message_id, last_scan_time) VALUES (?, ?, ?)",
        (channel_id, last_message_id, datetime.now().isoformat())
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
# CONFIGURATION UTILITIES
# ============================================================

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        "channels": [],
        "api_id": None,
        "api_hash": None,
        "logged_in": False,
        "settings": {"forward_to_saved": True, "forward_to_bot": True}
    }

def save_config(config: dict):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def save_credentials(api_id: str, api_hash: str):
    config = load_config()
    config["api_id"] = api_id
    config["api_hash"] = api_hash
    save_config(config)

def get_credentials():
    config = load_config()
    return config.get("api_id"), config.get("api_hash")

def set_logged_in(status: bool):
    config = load_config()
    config["logged_in"] = status
    save_config(config)

def is_logged_in() -> bool:
    config = load_config()
    return config.get("logged_in", False)

def add_channel(channel_id: int, channel_name: str, enabled: bool = True):
    config = load_config()
    for ch in config["channels"]:
        if ch["id"] == channel_id:
            return False
    config["channels"].append({
        "id": channel_id,
        "name": channel_name,
        "enabled": enabled,
        "added_at": datetime.now().isoformat()
    })
    save_config(config)
    return True

def clear_channels():
    config = load_config()
    config["channels"] = []
    save_config(config)

def get_enabled_channels() -> List[dict]:
    config = load_config()
    return [ch for ch in config["channels"] if ch.get("enabled", True)]

def get_all_channels() -> List[dict]:
    config = load_config()
    return config["channels"]

def toggle_channel_enabled(channel_id: int):
    config = load_config()
    for ch in config["channels"]:
        if ch["id"] == channel_id:
            ch["enabled"] = not ch.get("enabled", True)
            break
    save_config(config)

def set_setting(key: str, value: bool):
    config = load_config()
    config["settings"][key] = value
    save_config(config)

def get_settings() -> dict:
    config = load_config()
    return config["settings"]

# ============================================================
# FORWARDER BOT CLASS
# ============================================================

class ForwarderBot:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.scanner_task: Optional[asyncio.Task] = None
        self.running = True

    async def initialize_from_session(self) -> bool:
        api_id, api_hash = get_credentials()
        if not api_id or not api_hash:
            return False
        self.client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
        try:
            await self.client.start()
            if await self.client.is_user_authorized():
                set_logged_in(True)
                me = await self.client.get_me()
                logger.info(f"Loaded session: {me.first_name}")
                return True
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
        return False

    async def create_permanent_client(self, api_id: str, api_hash: str):
        self.client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
        await self.client.start()
        set_logged_in(True)
        me = await self.client.get_me()
        save_credentials(api_id, api_hash)
        logger.info(f"Created permanent client: {me.first_name}")
        return me

    async def start_scanner(self):
        if self.scanner_task and not self.scanner_task.done():
            return
        self.scanner_task = asyncio.create_task(self._scanner_loop())
        logger.info("Scanner started")

    async def _scanner_loop(self):
        while self.running:
            try:
                await self.scan_and_forward()
            except Exception as e:
                logger.error(f"Scanner error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

    async def scan_and_forward(self):
        if not self.client or not self.client.is_connected():
            return
        enabled_channels = get_enabled_channels()
        for channel_info in enabled_channels:
            try:
                await self._scan_one_channel(channel_info)
            except Exception as e:
                logger.error(f"Error scanning {channel_info.get('name')}: {e}")

    async def _scan_one_channel(self, channel_info: dict):
        channel_id = channel_info["id"]
        channel_name = channel_info["name"]
        try:
            channel = await self.client.get_entity(channel_id)
            last_msg_id = get_last_scan(channel_id)
            messages = await self.client.get_messages(channel, limit=50)
            if not messages:
                return

            new_files = []
            for msg in messages:
                if last_msg_id is None or msg.id > last_msg_id:
                    if msg.document:
                        fname = self._extract_filename(msg)
                        if fname and fname.endswith('.txt'):
                            new_files.append(msg)

            latest_id = max(m.id for m in messages)
            update_last_scan(channel_id, latest_id)

            for msg in reversed(new_files):
                await self._process_file(msg, channel_name)
        except Exception as e:
            logger.error(f"Error in {channel_name}: {e}")

    def _extract_filename(self, msg) -> Optional[str]:
        if msg.document and msg.document.attributes:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
        return None

    async def _process_file(self, msg, channel_name: str):
        file_id = str(msg.document.id)
        if is_file_forwarded(file_id):
            return
        file_name = self._extract_filename(msg)
        if not file_name:
            return

        logger.info(f"New file: {file_name} from {channel_name}")
        temp_path = os.path.join(DATA_DIR, f"temp_{msg.id}_{file_name}")
        try:
            await self.client.download_file(msg.media, temp_path)
            settings = get_settings()

            if settings.get("forward_to_saved", True):
                await self.client.send_file('me', temp_path, caption=f"From: {channel_name}\nMsg: {msg.id}")

            if settings.get("forward_to_bot", True):
                url = f"https://api.telegram.org/bot{TARGET_BOT_TOKEN}/sendDocument"
                with open(temp_path, 'rb') as f:
                    files = {'document': (file_name, f)}
                    data = {'chat_id': TARGET_CHAT_ID, 'caption': f"📁 {file_name}\n📡 {channel_name}"}
                    requests.post(url, data=data, files=files, timeout=30)

            mark_file_forwarded(file_id, msg.id, msg.chat_id, file_name)
        except Exception as e:
            logger.error(f"Error processing {file_name}: {e}")
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
            return [{'id': d.id, 'name': d.name, 'username': d.entity.username} for d in dialogs if d.is_channel]
        except Exception as e:
            logger.error(f"Failed to fetch channels: {e}")
            return []

    async def manual_scan(self):
        await self.scan_and_forward()

    async def shutdown(self):
        self.running = False
        if self.scanner_task:
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.disconnect()

# ============================================================
# LOGIN STATE MANAGEMENT
# ============================================================

class LoginState:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.api_id = None
        self.api_hash = None
        self.phone = None
        self.client = None
        self.step = "start"  # start -> phone -> code -> password -> done
        self.password_needed = False

login_states: Dict[int, LoginState] = {}

async def cleanup_login_state(user_id: int):
    state = login_states.pop(user_id, None)
    if state and state.client:
        try:
            await state.client.disconnect()
        except:
            pass

# ============================================================
# COMMAND HANDLERS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_logged_in():
        await show_main_menu(update, context)
    else:
        msg = (
            "🤖 **Telegram TXT File Forwarder**\n\n"
            "Monitors channels and forwards .txt files.\n\n"
            "**Login:**\n"
            "1. Get API creds: https://my.telegram.org/apps\n"
            "2. Send `/login [api_id] [api_hash]`\n\n"
            "Example: `/login 1234567 abcdef1234567890`"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if len(args) != 2:
        await update.message.reply_text(
            "❌ Usage: `/login [api_id] [api_hash]`\nGet credentials from: https://my.telegram.org/apps",
            parse_mode='Markdown'
        )
        return

    api_id, api_hash = args[0], args[1]

    # Clean up any existing state
    await cleanup_login_state(user_id)

    state = LoginState(user_id)
    state.api_id = api_id
    state.api_hash = api_hash
    state.step = "phone"
    login_states[user_id] = state

    await update.message.reply_text(
        "📱 **Enter your phone number** (with country code)\nExample: `+1234567890`\n\n"
        "Type `/cancel` to abort.",
        parse_mode='Markdown'
    )

async def resend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resend verification code"""
    user_id = update.effective_user.id
    state = login_states.get(user_id)

    if not state or state.step not in ["code", "password"]:
        await update.message.reply_text("❌ No active login session. Use /login to start.")
        return

    if state.step == "code" and state.client:
        await update.message.reply_text("🔄 **Requesting new code...**")
        try:
            result = await state.client.send_code_request(state.phone)
            state.phone_code_hash = result.phone_code_hash
            await update.message.reply_text("✅ **New code sent!**\n\nPlease enter the OTP code:")
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to resend: {str(e)}")
    else:
        await update.message.reply_text("❌ Cannot resend code at this step.")

async def cancel_login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current login session"""
    user_id = update.effective_user.id
    await cleanup_login_state(user_id)
    await update.message.reply_text("✅ **Login cancelled.** Use /login to start over.")

async def handle_login_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = login_states.get(user_id)

    if not state:
        return

    text = update.message.text.strip()

    if state.step == "phone":
        state.phone = text
        state.step = "requesting"

        await update.message.reply_text("🔄 **Requesting verification code...**")

        try:
            state.client = TelegramClient(None, int(state.api_id), state.api_hash)
            await state.client.connect()

            result = await state.client.send_code_request(state.phone)
            state.phone_code_hash = result.phone_code_hash
            state.step = "code"

            await update.message.reply_text(
                "✅ **Code sent!**\n\n"
                "Enter the OTP code from Telegram:\n"
                "Type `/resend` if you don't receive it.\n"
                "Type `/cancel` to abort.",
                parse_mode='Markdown'
            )

        except PhoneNumberInvalidError:
            await update.message.reply_text("❌ Invalid phone number. Please try again.")
            state.step = "phone"
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
            await cleanup_login_state(user_id)

    elif state.step == "code":
        code = text
        await update.message.reply_text("🔄 **Verifying...**")

        try:
            await state.client.sign_in(state.phone, code, phone_code_hash=state.phone_code_hash)

            if await state.client.is_user_authorized():
                me = await state.client.get_me()
                await state.client.disconnect()

                # Create permanent client
                forwarder = ForwarderBot()
                await forwarder.create_permanent_client(state.api_id, state.api_hash)
                await forwarder.start_scanner()
                context.bot_data['forwarder'] = forwarder

                await update.message.reply_text(
                    f"✅ **Login successful!**\n\n"
                    f"Welcome, {me.first_name}! (@{me.username or 'no username'})\n\n"
                    f"Fetching your channels...",
                    parse_mode='Markdown'
                )

                channels = await forwarder.fetch_channels()
                if channels:
                    clear_channels()
                    for ch in channels:
                        add_channel(ch['id'], ch['name'], enabled=True)
                    await show_channel_selection(update, channels, context)
                else:
                    await update.message.reply_text("📭 No channels found.")

                await cleanup_login_state(user_id)

            else:
                await update.message.reply_text("❌ Login failed.")
                await cleanup_login_state(user_id)

        except SessionPasswordNeededError:
            state.step = "password"
            state.password_needed = True
            await update.message.reply_text(
                "🔐 **2FA Enabled**\n\n"
                "Enter your Telegram 2FA password:\n"
                "Type `/cancel` to abort.",
                parse_mode='Markdown'
            )

        except PhoneCodeInvalidError:
            await update.message.reply_text(
                "❌ **Invalid code**\n\n"
                "Try again, or type `/resend` for a new code:",
                parse_mode='Markdown'
            )

        except PhoneCodeExpiredError:
            await update.message.reply_text(
                "❌ **Code expired**\n\n"
                "Type `/resend` to get a new code:",
                parse_mode='Markdown'
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
            await cleanup_login_state(user_id)

    elif state.step == "password":
        password = text
        await update.message.reply_text("🔄 **Verifying password...**")

        try:
            await state.client.sign_in(password=password)

            if await state.client.is_user_authorized():
                me = await state.client.get_me()
                await state.client.disconnect()

                forwarder = ForwarderBot()
                await forwarder.create_permanent_client(state.api_id, state.api_hash)
                await forwarder.start_scanner()
                context.bot_data['forwarder'] = forwarder

                await update.message.reply_text(
                    f"✅ **Login successful!**\n\n"
                    f"Welcome, {me.first_name}! (@{me.username or 'no username'})\n\n"
                    f"Fetching your channels...",
                    parse_mode='Markdown'
                )

                channels = await forwarder.fetch_channels()
                if channels:
                    clear_channels()
                    for ch in channels:
                        add_channel(ch['id'], ch['name'], enabled=True)
                    await show_channel_selection(update, channels, context)
                else:
                    await update.message.reply_text("📭 No channels found.")

                await cleanup_login_state(user_id)

            else:
                await update.message.reply_text("❌ Login failed.")
                await cleanup_login_state(user_id)

        except Exception as e:
            await update.message.reply_text(f"❌ Error: {str(e)}")
            await cleanup_login_state(user_id)

async def show_channel_selection(update: Update, channels: List[dict], context: ContextTypes.DEFAULT_TYPE):
    if not channels:
        await update.message.reply_text("📭 No channels found.")
        return

    enabled_ids = {ch["id"] for ch in get_all_channels() if ch.get("enabled", True)}

    keyboard = []
    for ch in channels:
        status = "✅" if ch['id'] in enabled_ids else "⏸️"
        keyboard.append([
            InlineKeyboardButton(
                f"{status} {ch['name'][:35]}",
                callback_data=f"sel_{ch['id']}"
            )
        ])

    keyboard.append([InlineKeyboardButton("✅ Confirm Selection", callback_data="confirm_channels")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📋 **Select Channels to Monitor**\n\n"
        f"Found {len(channels)} channels.\n"
        f"Tap to toggle on/off.\n\n"
        f"✅ = Active | ⏸️ = Disabled",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    enabled_count = len(get_enabled_channels())
    total_count = len(config["channels"])
    settings = get_settings()

    msg = (
        f"✅ **Logged In**\n\n"
        f"📡 **Channels:** {enabled_count}/{total_count} active\n"
        f"📁 **Scan Interval:** {SCAN_INTERVAL // 60} min\n"
        f"📤 **Saved:** {'ON' if settings.get('forward_to_saved', True) else 'OFF'}\n"
        f"🤖 **Bot:** {'ON' if settings.get('forward_to_bot', True) else 'OFF'}\n\n"
        f"**Commands:**\n"
        f"/channels - Manage channels\n"
        f"/settings - Change settings\n"
        f"/status - View stats\n"
        f"/scan - Manual scan\n"
        f"/logout - Log out"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first using /login")
        return

    channels = get_all_channels()
    if not channels:
        await update.message.reply_text("📭 No channels configured.")
        return

    keyboard = []
    for ch in channels:
        status = "✅" if ch.get("enabled", True) else "⏸️"
        keyboard.append([
            InlineKeyboardButton(
                f"{status} {ch['name'][:35]}",
                callback_data=f"toggle_{ch['id']}"
            )
        ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📋 **Your Channels**\n\nTap to toggle monitoring:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first")
        return

    settings = get_settings()
    keyboard = [
        [InlineKeyboardButton(f"📤 Saved: {'ON' if settings.get('forward_to_saved', True) else 'OFF'}", callback_data="toggle_saved")],
        [InlineKeyboardButton(f"🤖 Bot: {'ON' if settings.get('forward_to_bot', True) else 'OFF'}", callback_data="toggle_bot")],
        [InlineKeyboardButton("🔄 Manual Scan", callback_data="manual_scan")],
        [InlineKeyboardButton("Close", callback_data="close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("⚙️ **Settings**", reply_markup=reply_markup, parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first")
        return

    enabled_count = len(get_enabled_channels())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM forwarded_files")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM forwarded_files WHERE forwarded_at > datetime('now', '-24 hours')")
    today = c.fetchone()[0]
    conn.close()

    forwarder = context.bot_data.get('forwarder')
    scanner = "Running" if forwarder and forwarder.running else "Idle"

    msg = f"📊 **Status**\n\n📡 Active Channels: {enabled_count}\n📁 Files Forwarded: {total} total, {today} today\n⏱️ Scan: every {SCAN_INTERVAL // 60} min\n🤖 Scanner: {scanner}"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    forwarder = context.bot_data.get('forwarder')
    if forwarder:
        await forwarder.shutdown()
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    set_logged_in(False)
    clear_channels()
    context.bot_data.pop('forwarder', None)
    await update.message.reply_text("✅ **Logged out**\n\nUse /login to log in again.")

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first")
        return
    forwarder = context.bot_data.get('forwarder')
    if forwarder:
        await update.message.reply_text("🔄 Manual scan triggered...")
        asyncio.create_task(forwarder.manual_scan())
    else:
        await update.message.reply_text("❌ Forwarder not initialized.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("toggle_"):
        channel_id = int(data.split("_")[1])
        toggle_channel_enabled(channel_id)
        await channels_command(update, context)

    elif data.startswith("sel_"):
        channel_id = int(data.split("_")[1])
        toggle_channel_enabled(channel_id)
        channels = get_all_channels()
        enabled_ids = {ch["id"] for ch in channels if ch.get("enabled", True)}
        keyboard = []
        for ch in channels:
            status = "✅" if ch['id'] in enabled_ids else "⏸️"
            keyboard.append([InlineKeyboardButton(f"{status} {ch['name'][:35]}", callback_data=f"sel_{ch['id']}")])
        keyboard.append([InlineKeyboardButton("✅ Confirm Selection", callback_data="confirm_channels")])
        await query.edit_message_text(
            f"📋 Select Channels\n\nFound {len(channels)} channels.\nTap to toggle.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    elif data == "confirm_channels":
        await query.edit_message_text("✅ **Channels saved!**\n\nMonitoring will begin.\nUse /status to check.")

    elif data == "toggle_saved":
        current = get_settings().get("forward_to_saved", True)
        set_setting("forward_to_saved", not current)
        await settings_command(update, context)

    elif data == "toggle_bot":
        current = get_settings().get("forward_to_bot", True)
        set_setting("forward_to_bot", not current)
        await settings_command(update, context)

    elif data == "manual_scan":
        await query.edit_message_text("🔄 Manual scan triggered...")
        forwarder = context.bot_data.get('forwarder')
        if forwarder:
            await forwarder.manual_scan()

    elif data == "close":
        await query.delete_message()

# ============================================================
# MAIN
# ============================================================

async def main():
    init_db()

    app = Application.builder().token(COMMAND_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("resend", resend_command))
    app.add_handler(CommandHandler("cancel", cancel_login_command))
    app.add_handler(CommandHandler("channels", channels_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("scan", manual_scan_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_input))
    app.add_handler(CallbackQueryHandler(button_callback))

    forwarder = ForwarderBot()
    if await forwarder.initialize_from_session():
        await forwarder.start_scanner()
        app.bot_data['forwarder'] = forwarder
        logger.info("Restored session")
    else:
        logger.info("No session, waiting for login")

    logger.info("Bot started")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await app.stop()
        if 'forwarder' in app.bot_data:
            await app.bot_data['forwarder'].shutdown()

if __name__ == "__main__":
    asyncio.run(main())
