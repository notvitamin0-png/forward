#!/usr/bin/env python3
"""
Telegram TXT File Forwarder - Railway Ready (Class-Based)
Login via bot, select channels, auto-forward .txt files
"""

import os
import sys
import json
import asyncio
import sqlite3
import logging
import threading
import traceback
from datetime import datetime
from typing import Dict, List, Optional, Any

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import SessionPasswordNeededError
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
SCAN_INTERVAL = 300  # 5 minutes

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
    logger.info("Database initialized")

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
        "settings": {
            "forward_to_saved": True,
            "forward_to_bot": True
        }
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

def remove_channel(channel_id: int):
    config = load_config()
    config["channels"] = [ch for ch in config["channels"] if ch["id"] != channel_id]
    save_config(config)

def toggle_channel(channel_id: int, enabled: bool):
    config = load_config()
    for ch in config["channels"]:
        if ch["id"] == channel_id:
            ch["enabled"] = enabled
            break
    save_config(config)

def get_enabled_channels() -> List[dict]:
    config = load_config()
    return [ch for ch in config["channels"] if ch.get("enabled", True)]

def clear_channels():
    config = load_config()
    config["channels"] = []
    save_config(config)

# ============================================================
# FORWARDER BOT CLASS (Encapsulates Telethon client)
# ============================================================

class ForwarderBot:
    def __init__(self):
        self.client: Optional[TelegramClient] = None
        self.scanner_task: Optional[asyncio.Task] = None
        self.running = True

    async def initialize_client(self) -> bool:
        """Start Telethon client using saved credentials or session"""
        api_id, api_hash = get_credentials()
        if not api_id or not api_hash:
            logger.warning("No API credentials found")
            return False

        self.client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
        try:
            await self.client.start()
            if await self.client.is_user_authorized():
                set_logged_in(True)
                me = await self.client.get_me()
                logger.info(f"Loaded existing session for: {me.first_name}")
                return True
            else:
                set_logged_in(False)
                return False
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
            set_logged_in(False)
            return False

    async def start_scanner(self):
        """Start background scanner loop"""
        if self.scanner_task and not self.scanner_task.done():
            return
        self.scanner_task = asyncio.create_task(self._scanner_loop())
        logger.info("Scanner started")

    async def _scanner_loop(self):
        """Background loop scanning channels every SCAN_INTERVAL seconds"""
        while self.running:
            try:
                await self.scan_and_forward()
            except Exception as e:
                logger.error(f"Scanner loop error: {e}\n{traceback.format_exc()}")
            await asyncio.sleep(SCAN_INTERVAL)

    async def scan_and_forward(self):
        """Scan all enabled channels for new .txt files"""
        if not self.client or not self.client.is_connected():
            logger.warning("Telethon client not connected, skipping scan")
            return

        enabled_channels = get_enabled_channels()
        if not enabled_channels:
            return

        for channel_info in enabled_channels:
            try:
                await self._scan_one_channel(channel_info)
            except Exception as e:
                logger.error(f"Error scanning channel {channel_info.get('name')}: {e}")

    async def _scan_one_channel(self, channel_info: dict):
        channel_id = channel_info["id"]
        channel_name = channel_info["name"]

        try:
            channel = await self.client.get_entity(channel_id)
            last_msg_id = get_last_scan(channel_id)

            messages = await self.client.get_messages(channel, limit=50)
            if not messages:
                return

            # Find new messages
            new_files = []
            for msg in messages:
                if last_msg_id is None or msg.id > last_msg_id:
                    if msg.document:
                        file_name = self._extract_filename(msg)
                        if file_name and file_name.endswith('.txt'):
                            new_files.append(msg)

            # Update last scan ID
            latest_id = max(m.id for m in messages)
            update_last_scan(channel_id, latest_id)

            # Process files in chronological order
            for msg in reversed(new_files):
                await self._process_file(msg, channel_name)

        except Exception as e:
            logger.error(f"Error in channel {channel_name}: {e}")

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

        logger.info(f"New .txt file: {file_name} from {channel_name}")

        # Download
        temp_path = os.path.join(DATA_DIR, f"temp_{msg.id}_{file_name}")
        try:
            await self.client.download_file(msg.media, temp_path)

            config = load_config()
            # Forward to Saved Messages
            if config["settings"].get("forward_to_saved", True):
                caption = f"Forwarded from: {channel_name}\nOriginal message: {msg.id}"
                await self._forward_to_saved(temp_path, caption)

            # Forward to target bot
            if config["settings"].get("forward_to_bot", True):
                await self._forward_to_target_bot(temp_path, file_name, channel_name)

            # Mark as forwarded
            mark_file_forwarded(file_id, msg.id, channel_id, file_name)

        except Exception as e:
            logger.error(f"Error processing file {file_name}: {e}")
        finally:
            # Cleanup
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass

    async def _forward_to_saved(self, file_path: str, caption: str):
        try:
            await self.client.send_file('me', file_path, caption=caption)
            logger.info(f"Forwarded to Saved Messages")
        except Exception as e:
            logger.error(f"Failed to forward to Saved Messages: {e}")

    async def _forward_to_target_bot(self, file_path: str, file_name: str, source: str):
        try:
            import requests
            url = f"https://api.telegram.org/bot{TARGET_BOT_TOKEN}/sendDocument"
            with open(file_path, 'rb') as f:
                files = {'document': (file_name, f)}
                data = {
                    'chat_id': TARGET_CHAT_ID,
                    'caption': f"📁 {file_name}\n📡 Source: {source}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                }
                response = requests.post(url, data=data, files=files, timeout=30)
                if response.status_code == 200:
                    logger.info(f"Forwarded to target bot: {file_name}")
                else:
                    logger.warning(f"Target bot returned {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to forward to target bot: {e}")

    async def manual_scan(self):
        """Trigger a manual scan"""
        await self.scan_and_forward()

    async def shutdown(self):
        """Clean shutdown"""
        self.running = False
        if self.scanner_task:
            self.scanner_task.cancel()
            try:
                await self.scanner_task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.disconnect()
        logger.info("ForwarderBot shut down")

# ============================================================
# TELEGRAM BOT COMMAND HANDLERS (use ForwarderBot instance)
# ============================================================

forwarder_bot: ForwarderBot = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_logged_in():
        await show_main_menu(update)
    else:
        msg = (
            "🤖 **Telegram TXT File Forwarder**\n\n"
            "This bot monitors channels and forwards .txt files.\n\n"
            "**First, you need to log in with your Telegram account:**\n\n"
            "1. Get your API credentials from https://my.telegram.org/apps\n"
            "2. Send `/login [api_id] [api_hash]`\n\n"
            "Example: `/login 1234567 abcdef1234567890`\n\n"
            "⚠️ Your credentials are stored locally and never shared."
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "❌ **Usage:** `/login [api_id] [api_hash]`\n\n"
            "Get your credentials from: https://my.telegram.org/apps",
            parse_mode='Markdown'
        )
        return

    api_id, api_hash = args[0], args[1]

    await update.message.reply_text("🔄 **Logging in...**\n\nPlease enter your phone number (with country code) in the next message.")

    # Store credentials temporarily
    context.user_data['pending_login'] = (api_id, api_hash)
    context.user_data['login_step'] = 'phone'

async def handle_login_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'pending_login' not in context.user_data:
        return

    step = context.user_data.get('login_step')
    api_id, api_hash = context.user_data['pending_login']
    phone = context.user_data.get('login_phone')
    password = None

    if step == 'phone':
        phone = update.message.text.strip()
        context.user_data['login_phone'] = phone
        context.user_data['login_step'] = 'code'

        # Request code via Telethon
        client = TelegramClient(None, int(api_id), api_hash)
        try:
            await client.connect()
            await client.send_code_request(phone)
            await client.disconnect()
            await update.message.reply_text("✅ **Code sent!**\n\nPlease enter the OTP code you received:")
        except Exception as e:
            await update.message.reply_text(f"❌ **Error sending code:** `{str(e)}`", parse_mode='Markdown')
            context.user_data.pop('pending_login', None)
            context.user_data.pop('login_step', None)
            context.user_data.pop('login_phone', None)

    elif step == 'code':
        code = update.message.text.strip()
        await update.message.reply_text("🔄 **Verifying code...**")

        client = TelegramClient(None, int(api_id), api_hash)
        try:
            await client.connect()
            await client.sign_in(phone, code)

            if await client.is_user_authorized():
                # Save session and credentials
                await client.disconnect()
                # Create permanent client
                perm_client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
                await perm_client.start()
                me = await perm_client.get_me()
                await perm_client.disconnect()

                save_credentials(api_id, api_hash)
                set_logged_in(True)

                # Initialize the global forwarder bot with this client
                global forwarder_bot
                forwarder_bot = ForwarderBot()
                forwarder_bot.client = perm_client
                await forwarder_bot.start_scanner()

                await update.message.reply_text(
                    f"✅ **Login successful!**\n\n"
                    f"Welcome, {me.first_name}! (@{me.username or 'no username'})\n\n"
                    f"Now fetching your channels..."
                )

                # Fetch channels and show selection
                await fetch_and_show_channels(update, perm_client)

            else:
                context.user_data['login_step'] = 'password'
                await update.message.reply_text("🔐 **2FA Enabled**\n\nPlease enter your password:")
                await client.disconnect()
                return

        except SessionPasswordNeededError:
            context.user_data['login_step'] = 'password'
            await update.message.reply_text("🔐 **2FA Enabled**\n\nPlease enter your password:")
            await client.disconnect()
            return
        except Exception as e:
            await update.message.reply_text(f"❌ **Login failed:** `{str(e)}`", parse_mode='Markdown')
            context.user_data.pop('pending_login', None)
            context.user_data.pop('login_step', None)
            context.user_data.pop('login_phone', None)
            return

        # Clean up login state
        context.user_data.pop('pending_login', None)
        context.user_data.pop('login_step', None)
        context.user_data.pop('login_phone', None)

    elif step == 'password':
        password = update.message.text.strip()
        client = TelegramClient(None, int(api_id), api_hash)
        try:
            await client.connect()
            await client.sign_in(password=password)

            if await client.is_user_authorized():
                # Save session and credentials
                await client.disconnect()
                perm_client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
                await perm_client.start()
                me = await perm_client.get_me()
                await perm_client.disconnect()

                save_credentials(api_id, api_hash)
                set_logged_in(True)

                global forwarder_bot
                forwarder_bot = ForwarderBot()
                forwarder_bot.client = perm_client
                await forwarder_bot.start_scanner()

                await update.message.reply_text(
                    f"✅ **Login successful!**\n\n"
                    f"Welcome, {me.first_name}! (@{me.username or 'no username'})\n\n"
                    f"Now fetching your channels..."
                )

                await fetch_and_show_channels(update, perm_client)

            else:
                await update.message.reply_text("❌ Login failed.")
        except Exception as e:
            await update.message.reply_text(f"❌ **Login failed:** `{str(e)}`", parse_mode='Markdown')
        finally:
            context.user_data.pop('pending_login', None)
            context.user_data.pop('login_step', None)
            context.user_data.pop('login_phone', None)
            await client.disconnect()

async def fetch_and_show_channels(update: Update, client: TelegramClient):
    try:
        dialogs = await client.get_dialogs()
        channels = []
        for dialog in dialogs:
            if dialog.is_channel:
                channels.append({
                    'id': dialog.id,
                    'name': dialog.name,
                    'username': dialog.entity.username,
                })

        # Save to config
        clear_channels()
        for ch in channels:
            add_channel(ch['id'], ch['name'], enabled=True)

        # Show selection UI
        await show_channel_selection(update, channels)

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to fetch channels: {str(e)}")

async def show_channel_selection(update: Update, channels: List[dict]):
    if not channels:
        await update.message.reply_text("📭 No channels found. Make sure you're a member of some channels.")
        return

    config = load_config()
    enabled_ids = {ch["id"] for ch in config["channels"] if ch.get("enabled", True)}

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
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_channels")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"📋 **Select Channels to Monitor**\n\n"
        f"Found {len(channels)} channels.\n"
        f"Tap to toggle monitoring on/off.\n\n"
        f"✅ = Active | ⏸️ = Disabled",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_main_menu(update: Update):
    config = load_config()
    enabled_count = len(get_enabled_channels())
    total_count = len(config["channels"])

    msg = (
        f"✅ **Logged In**\n\n"
        f"📡 **Channels:** {enabled_count}/{total_count} active\n"
        f"📁 **Scan Interval:** {SCAN_INTERVAL // 60} minutes\n"
        f"📤 **Forward to Saved:** {'ON' if config['settings'].get('forward_to_saved', True) else 'OFF'}\n"
        f"🤖 **Forward to Bot:** {'ON' if config['settings'].get('forward_to_bot', True) else 'OFF'}\n\n"
        f"**Commands:**\n"
        f"/channels - Manage channels\n"
        f"/settings - Change forwarding settings\n"
        f"/status - View statistics\n"
        f"/scan - Manual scan\n"
        f"/logout - Log out"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first using /login")
        return

    config = load_config()
    channels = config["channels"]
    if not channels:
        await update.message.reply_text("📭 No channels configured. Run /refresh to fetch your channels.")
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

    keyboard.append([InlineKeyboardButton("🔄 Refresh Channel List", callback_data="refresh_channels")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "📋 **Your Channels**\n\nTap to toggle monitoring on/off:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first using /login")
        return

    config = load_config()
    settings = config["settings"]

    keyboard = [
        [
            InlineKeyboardButton(
                f"📤 Saved: {'ON' if settings.get('forward_to_saved', True) else 'OFF'}",
                callback_data="toggle_saved"
            )
        ],
        [
            InlineKeyboardButton(
                f"🤖 Bot: {'ON' if settings.get('forward_to_bot', True) else 'OFF'}",
                callback_data="toggle_bot"
            )
        ],
        [InlineKeyboardButton("🔄 Manual Scan", callback_data="manual_scan")],
        [InlineKeyboardButton("Close", callback_data="close")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⚙️ **Settings**\n\nSelect an option to toggle:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first using /login")
        return

    config = load_config()
    enabled_count = len(get_enabled_channels())

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM forwarded_files")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM forwarded_files WHERE forwarded_at > datetime('now', '-24 hours')")
    today = c.fetchone()[0]
    conn.close()

    msg = (
        f"📊 **Status**\n\n"
        f"📡 **Active Channels:** {enabled_count}\n"
        f"📁 **Files Forwarded:** {total} total, {today} today\n"
        f"⏱️ **Scan Interval:** {SCAN_INTERVAL // 60} minutes\n"
        f"🤖 **Scanner:** {'Running' if forwarder_bot and forwarder_bot.running else 'Not started'}\n\n"
        f"Use /scan to trigger manual scan."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global forwarder_bot
    if forwarder_bot:
        await forwarder_bot.shutdown()
        forwarder_bot = None
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    set_logged_in(False)
    clear_channels()
    await update.message.reply_text(
        "✅ **Logged out**\n\nYour session has been cleared.\nUse /login to log in again."
    )

async def manual_scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first using /login")
        return
    if forwarder_bot:
        await update.message.reply_text("🔄 Manual scan triggered...")
        asyncio.create_task(forwarder_bot.manual_scan())
    else:
        await update.message.reply_text("❌ Forwarder bot not initialized.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("toggle_"):
        channel_id = int(data.split("_")[1])
        config = load_config()
        for ch in config["channels"]:
            if ch["id"] == channel_id:
                ch["enabled"] = not ch.get("enabled", True)
                break
        save_config(config)
        # Refresh channel list
        await channels_command(update, context)

    elif data.startswith("sel_"):
        channel_id = int(data.split("_")[1])
        config = load_config()
        for ch in config["channels"]:
            if ch["id"] == channel_id:
                ch["enabled"] = not ch.get("enabled", True)
                break
        save_config(config)
        await refresh_selection_menu(update, query.message)

    elif data == "confirm_channels":
        await query.edit_message_text(
            "✅ **Channels saved!**\n\nMonitoring will now begin.\n\nUse /status to check progress."
        )

    elif data == "refresh_channels":
        await query.edit_message_text("🔄 Refreshing channel list...")
        # Re-fetch channels using existing client
        if forwarder_bot and forwarder_bot.client:
            await fetch_and_show_channels(update, forwarder_bot.client)
        else:
            await query.edit_message_text("❌ Not connected. Please log in again.")

    elif data == "toggle_saved":
        config = load_config()
        config["settings"]["forward_to_saved"] = not config["settings"].get("forward_to_saved", True)
        save_config(config)
        await settings_command(update, context)

    elif data == "toggle_bot":
        config = load_config()
        config["settings"]["forward_to_bot"] = not config["settings"].get("forward_to_bot", True)
        save_config(config)
        await settings_command(update, context)

    elif data == "manual_scan":
        await query.edit_message_text("🔄 Manual scan triggered...")
        if forwarder_bot:
            await forwarder_bot.manual_scan()

    elif data == "close":
        await query.delete_message()

async def refresh_selection_menu(update: Update, message):
    config = load_config()
    channels = config["channels"]
    enabled_ids = {ch["id"] for ch in channels if ch.get("enabled", True)}

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
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_channels")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await message.edit_text(
        f"📋 **Select Channels to Monitor**\n\n"
        f"Found {len(channels)} channels.\n"
        f"Tap to toggle monitoring on/off.\n\n"
        f"✅ = Active | ⏸️ = Disabled",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

# ============================================================
# MAIN
# ============================================================

async def main():
    global forwarder_bot

    init_db()

    # Try to restore previous session
    forwarder_bot = ForwarderBot()
    if await forwarder_bot.initialize_client():
        await forwarder_bot.start_scanner()
    else:
        logger.info("No valid session, waiting for user login")

    # Create command bot application
    app = Application.builder().token(COMMAND_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("channels", channels_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("scan", manual_scan_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_input))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Command bot started")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await app.stop()
        if forwarder_bot:
            await forwarder_bot.shutdown()

if __name__ == "__main__":
    asyncio.run(main())