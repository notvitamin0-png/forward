#!/usr/bin/env python3
"""
TELEGRAM TXT FILE FORWARDER - COMPLETE WORKING VERSION
Fixed login - no more code expired errors
Detailed error codes for debugging
"""

import os
import asyncio
import sqlite3
import logging
import time
import json
import requests
from datetime import datetime
from typing import List, Dict, Optional
from enum import Enum

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

CONTROL_BOT_TOKEN = "8666320518:AAEIhkSS0XeJ-k40rc3d80Dn0b-q9JLcnyI"
TARGET_BOT_TOKEN = "8657130802:AAE8Ynf791ramxyFktFPHgwuv0b5vNKiKH0"
TARGET_CHAT_ID = "8260250818"
API_ID = 39184727
API_HASH = "a52c4985a38ef98c84cdf11d45e53baf"

DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

SESSION_FILE = os.path.join(DATA_DIR, "forwarder_session.session")
DB_FILE = os.path.join(DATA_DIR, "forwarded.db")
CONFIG_FILE = os.path.join(DATA_DIR, "channels.json")

SCAN_INTERVAL = 300

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# ERROR CODES
# ============================================================

class ErrorCode(Enum):
    SUCCESS = ("✅", "Success")
    CODE_EXPIRED = ("❌ CODE_EXPIRED", "The confirmation code has expired. Use /resend to get a new code.")
    CODE_INVALID = ("❌ CODE_INVALID", "The code you entered is incorrect. Please try again.")
    PHONE_INVALID = ("❌ PHONE_INVALID", "Invalid phone number format. Use + followed by country code.")
    FLOOD_WAIT = ("⏱️ FLOOD_WAIT", "Too many attempts. Please wait {} seconds.")
    NETWORK_ERROR = ("🌐 NETWORK_ERROR", "Network connection failed. Check your internet.")
    SESSION_ERROR = ("🔐 SESSION_ERROR", "Session error. Please restart login with /login")
    AUTH_ERROR = ("🔒 AUTH_ERROR", "Authentication failed. Please try again.")
    UNKNOWN_ERROR = ("⚠️ UNKNOWN_ERROR", "An unknown error occurred: {}")
    PASSWORD_NEEDED = ("🔐 PASSWORD_NEEDED", "2FA is enabled. Please enter your password.")
    PASSWORD_INVALID = ("❌ PASSWORD_INVALID", "Incorrect password. Please try again.")
    ALREADY_LOGGED_IN = ("✅ ALREADY_LOGGED_IN", "Already logged in as: {}")
    NOT_LOGGED_IN = ("❌ NOT_LOGGED_IN", "Not logged in. Please use /login first.")
    CHANNEL_NOT_FOUND = ("❌ CHANNEL_NOT_FOUND", "Channel not found or you don't have access.")
    PERMISSION_DENIED = ("🚫 PERMISSION_DENIED", "Permission denied. Cannot access this channel.")

    def format(self, *args):
        icon, msg = self.value
        return f"{icon} **{msg.format(*args)}**"

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
    logger.info("Database initialized")

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

def update_last_scan(channel_id: int, last_msg_id: int):
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
# CHANNEL CONFIGURATION
# ============================================================

def load_channels() -> List[dict]:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return []

def save_channels(channels: List[dict]):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(channels, f, indent=2)

def add_channel(channel_id: int, channel_name: str, enabled: bool = True):
    channels = load_channels()
    for ch in channels:
        if ch["id"] == channel_id:
            return False
    channels.append({
        "id": channel_id,
        "name": channel_name,
        "enabled": enabled,
        "added_at": datetime.now().isoformat()
    })
    save_channels(channels)
    return True

def remove_channel(channel_id: int):
    channels = load_channels()
    channels = [ch for ch in channels if ch["id"] != channel_id]
    save_channels(channels)

def toggle_channel(channel_id: int, enabled: bool):
    channels = load_channels()
    for ch in channels:
        if ch["id"] == channel_id:
            ch["enabled"] = enabled
            break
    save_channels(channels)

def get_enabled_channels() -> List[dict]:
    return [ch for ch in load_channels() if ch.get("enabled", True)]

# ============================================================
# LOGIN STATE MANAGEMENT (PERSISTENT CLIENT)
# ============================================================

class LoginSession:
    def __init__(self):
        self.client = None
        self.phone = None
        self.phone_code_hash = None
        self.step = None
        self.user_id = None

login_sessions: Dict[int, LoginSession] = {}

async def get_or_create_login_session(user_id: int) -> LoginSession:
    if user_id not in login_sessions:
        login_sessions[user_id] = LoginSession()
    return login_sessions[user_id]

async def cleanup_login_session(user_id: int):
    if user_id in login_sessions:
        session = login_sessions[user_id]
        if session.client:
            try:
                await session.client.disconnect()
            except:
                pass
        del login_sessions[user_id]

# ============================================================
# FORWARDER ENGINE
# ============================================================

class TelegramForwarder:
    def __init__(self):
        self.client = None
        self.scanner_task = None
        self.running = True

    async def load_existing_session(self) -> bool:
        """Load existing session file"""
        if not os.path.exists(SESSION_FILE):
            return False
        
        try:
            self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
            await self.client.start()
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                logger.info(f"✅ Loaded existing session: {me.first_name}")
                return True
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
        return False

    async def start_login(self, user_id: int, phone: str):
        """Step 1: Send code to phone"""
        session = await get_or_create_login_session(user_id)
        
        try:
            session.client = TelegramClient(None, API_ID, API_HASH)
            await session.client.connect()
            session.phone = phone
            session.step = "code"
            session.user_id = user_id
            
            result = await session.client.send_code_request(phone)
            session.phone_code_hash = result.phone_code_hash
            
            return {"status": "code_sent", "error_code": ErrorCode.SUCCESS}
            
        except PhoneNumberInvalidError:
            await cleanup_login_session(user_id)
            return {"status": "error", "error_code": ErrorCode.PHONE_INVALID}
        except FloodWaitError as e:
            await cleanup_login_session(user_id)
            return {"status": "error", "error_code": ErrorCode.FLOOD_WAIT, "wait": e.seconds}
        except Exception as e:
            await cleanup_login_session(user_id)
            return {"status": "error", "error_code": ErrorCode.UNKNOWN_ERROR, "details": str(e)}

    async def verify_code(self, user_id: int, code: str):
        """Step 2: Verify OTP code"""
        session = login_sessions.get(user_id)
        if not session or not session.client:
            return {"status": "error", "error_code": ErrorCode.SESSION_ERROR}
        
        try:
            await session.client.sign_in(
                session.phone, 
                code, 
                phone_code_hash=session.phone_code_hash
            )
            
            if await session.client.is_user_authorized():
                me = await session.client.get_me()
                
                # Save session permanently
                await session.client.disconnect()
                
                # Create permanent client with saved session
                self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
                await self.client.start()
                
                await cleanup_login_session(user_id)
                return {"status": "success", "user": me.first_name, "error_code": ErrorCode.SUCCESS}
                
        except SessionPasswordNeededError:
            session.step = "password"
            return {"status": "password_needed", "error_code": ErrorCode.PASSWORD_NEEDED}
            
        except PhoneCodeExpiredError:
            return {"status": "error", "error_code": ErrorCode.CODE_EXPIRED}
            
        except PhoneCodeInvalidError:
            return {"status": "error", "error_code": ErrorCode.CODE_INVALID}
            
        except Exception as e:
            return {"status": "error", "error_code": ErrorCode.UNKNOWN_ERROR, "details": str(e)}
        
        return {"status": "error", "error_code": ErrorCode.UNKNOWN_ERROR}

    async def verify_password(self, user_id: int, password: str):
        """Step 3: Verify 2FA password"""
        session = login_sessions.get(user_id)
        if not session or not session.client:
            return {"status": "error", "error_code": ErrorCode.SESSION_ERROR}
        
        try:
            await session.client.sign_in(password=password)
            
            if await session.client.is_user_authorized():
                me = await session.client.get_me()
                await session.client.disconnect()
                
                self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
                await self.client.start()
                
                await cleanup_login_session(user_id)
                return {"status": "success", "user": me.first_name, "error_code": ErrorCode.SUCCESS}
                
        except Exception as e:
            return {"status": "error", "error_code": ErrorCode.PASSWORD_INVALID}
        
        return {"status": "error", "error_code": ErrorCode.UNKNOWN_ERROR}

    async def resend_code(self, user_id: int):
        """Resend verification code"""
        session = login_sessions.get(user_id)
        if not session or not session.client:
            return {"status": "error", "error_code": ErrorCode.SESSION_ERROR}
        
        try:
            result = await session.client.send_code_request(session.phone)
            session.phone_code_hash = result.phone_code_hash
            return {"status": "code_sent", "error_code": ErrorCode.SUCCESS}
        except FloodWaitError as e:
            return {"status": "error", "error_code": ErrorCode.FLOOD_WAIT, "wait": e.seconds}
        except Exception as e:
            return {"status": "error", "error_code": ErrorCode.UNKNOWN_ERROR, "details": str(e)}

    async def start_scanner(self):
        if self.scanner_task:
            return
        self.scanner_task = asyncio.create_task(self._scanner_loop())
        logger.info("Scanner started")

    async def _scanner_loop(self):
        while self.running:
            try:
                await self._scan_all_channels()
            except Exception as e:
                logger.error(f"Scanner error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

    async def _scan_all_channels(self):
        if not self.client or not self.client.is_connected():
            return
        
        channels = get_enabled_channels()
        for channel_info in channels:
            try:
                await self._scan_channel(channel_info)
            except Exception as e:
                logger.error(f"Error scanning {channel_info.get('name')}: {e}")

    async def _scan_channel(self, channel_info: dict):
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
                        file_name = self._get_filename(msg)
                        if file_name and file_name.endswith('.txt'):
                            new_files.append(msg)
            
            latest_id = max(m.id for m in messages)
            update_last_scan(channel_id, latest_id)
            
            for msg in reversed(new_files):
                await self._forward_file(msg, channel_name)
                
        except Exception as e:
            logger.error(f"Error in channel {channel_name}: {e}")

    def _get_filename(self, msg) -> Optional[str]:
        if msg.document and msg.document.attributes:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
        return None

    async def _forward_file(self, msg, channel_name: str):
        file_id = str(msg.document.id)
        
        if is_forwarded(file_id):
            return
        
        file_name = self._get_filename(msg)
        if not file_name:
            return
        
        logger.info(f"📁 New file: {file_name} from {channel_name}")
        
        temp_path = os.path.join(DATA_DIR, f"temp_{msg.id}_{file_name}")
        try:
            await self.client.download_file(msg.media, temp_path)
            
            url = f"https://api.telegram.org/bot{TARGET_BOT_TOKEN}/sendDocument"
            with open(temp_path, 'rb') as f:
                files = {'document': (file_name, f)}
                data = {
                    'chat_id': TARGET_CHAT_ID,
                    'caption': f"📁 {file_name}\n📡 Source: {channel_name}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                }
                response = requests.post(url, data=data, files=files, timeout=30)
                
                if response.status_code == 200:
                    logger.info(f"✅ Forwarded: {file_name}")
                    mark_forwarded(file_id, msg.id, msg.chat_id, file_name)
                    
        except Exception as e:
            logger.error(f"Error forwarding {file_name}: {e}")
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass

    async def fetch_my_channels(self) -> List[dict]:
        if not self.client:
            return []
        try:
            dialogs = await self.client.get_dialogs()
            channels = []
            for dialog in dialogs:
                if dialog.is_channel:
                    channels.append({
                        'id': dialog.id,
                        'name': dialog.name,
                        'username': dialog.entity.username if dialog.entity.username else None,
                    })
            return channels
        except Exception as e:
            logger.error(f"Failed to fetch channels: {e}")
            return []

    async def manual_scan(self):
        await self._scan_all_channels()

    async def shutdown(self):
        self.running = False
        if self.scanner_task:
            self.scanner_task.cancel()
        if self.client:
            await self.client.disconnect()

# ============================================================
# TELEGRAM BOT COMMANDS
# ============================================================

forwarder = None

async def send_error_message(update: Update, error_code: ErrorCode, *args):
    """Send formatted error message"""
    msg = error_code.format(*args)
    await update.message.reply_text(msg, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if forwarder and forwarder.client and forwarder.client.is_connected():
        await show_main_menu(update)
    else:
        msg = (
            "🤖 **Telegram TXT File Forwarder**\n\n"
            "**Commands:**\n"
            "• `/login` - Login to your Telegram account\n"
            "• `/resend` - Resend OTP code (if expired)\n"
            "• `/status` - Check login status\n\n"
            "**Login Steps:**\n"
            "1. Send `/login`\n"
            "2. Enter your phone number with country code\n"
            "3. Enter the OTP code you receive\n"
            "4. If 2FA enabled, enter your password\n\n"
            "After login, you can select channels to monitor."
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Check if already logged in
    if forwarder and forwarder.client and forwarder.client.is_connected():
        me = await forwarder.client.get_me()
        msg = ErrorCode.ALREADY_LOGGED_IN.format(me.first_name)
        await update.message.reply_text(msg, parse_mode='Markdown')
        return
    
    session = await get_or_create_login_session(user_id)
    session.step = "phone"
    
    await update.message.reply_text(
        "📱 **Enter your phone number**\n\n"
        "Format: `+977XXXXXXXXX`\n"
        "(Include country code with +)\n\n"
        "Type `/cancel` to abort.",
        parse_mode='Markdown'
    )

async def resend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resend OTP code"""
    user_id = update.effective_user.id
    
    if not forwarder:
        await send_error_message(update, ErrorCode.NOT_LOGGED_IN)
        return
    
    result = await forwarder.resend_code(user_id)
    
    if result["status"] == "code_sent":
        await update.message.reply_text(
            "✅ **New code sent!**\n\n"
            "Enter the OTP code you received:",
            parse_mode='Markdown'
        )
    else:
        error_code = result.get("error_code", ErrorCode.UNKNOWN_ERROR)
        if error_code == ErrorCode.FLOOD_WAIT:
            await send_error_message(update, error_code, result.get("wait", 60))
        else:
            await send_error_message(update, error_code)

async def cancel_login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel current login"""
    user_id = update.effective_user.id
    await cleanup_login_session(user_id)
    await update.message.reply_text(
        "✅ **Login cancelled**\n\n"
        "Use `/login` to start over.",
        parse_mode='Markdown'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = login_sessions.get(user_id)
    
    if not session or not session.step:
        return
    
    text = update.message.text.strip()
    
    if session.step == "phone":
        if not text.startswith('+'):
            await update.message.reply_text(
                "❌ **Invalid format**\n\n"
                "Phone number must start with `+`\n"
                "Example: `+977XXXXXXXXX`",
                parse_mode='Markdown'
            )
            return
        
        await update.message.reply_text("🔄 **Sending verification code...**")
        
        global forwarder
        if not forwarder:
            forwarder = TelegramForwarder()
        
        result = await forwarder.start_login(user_id, text)
        
        if result["status"] == "code_sent":
            await update.message.reply_text(
                "✅ **Code sent!**\n\n"
                "Enter the OTP code you received in Telegram.\n"
                "Type `/resend` if code expires.\n"
                "Type `/cancel` to abort.",
                parse_mode='Markdown'
            )
        else:
            error_code = result.get("error_code", ErrorCode.UNKNOWN_ERROR)
            if error_code == ErrorCode.FLOOD_WAIT:
                await send_error_message(update, error_code, result.get("wait", 60))
            else:
                await send_error_message(update, error_code)
            await cleanup_login_session(user_id)
    
    elif session.step == "code":
        await update.message.reply_text("🔄 **Verifying code...**")
        
        result = await forwarder.verify_code(user_id, text)
        
        if result["status"] == "success":
            await update.message.reply_text(
                f"✅ **Login successful!**\n\n"
                f"Welcome, {result['user']}!\n\n"
                f"Fetching your channels...",
                parse_mode='Markdown'
            )
            
            await forwarder.start_scanner()
            
            channels = await forwarder.fetch_my_channels()
            if channels:
                for ch in channels:
                    add_channel(ch['id'], ch['name'], enabled=False)
                await show_channel_selector(update, channels)
            else:
                await update.message.reply_text("📭 No channels found.")
            
        elif result["status"] == "password_needed":
            session.step = "password"
            await update.message.reply_text(
                "🔐 **2FA Enabled**\n\n"
                "Enter your Telegram password:\n"
                "Type `/cancel` to abort.",
                parse_mode='Markdown'
            )
        else:
            error_code = result.get("error_code", ErrorCode.UNKNOWN_ERROR)
            if error_code == ErrorCode.CODE_EXPIRED:
                await send_error_message(update, error_code)
                await update.message.reply_text(
                    "💡 **Tip:** Use `/resend` to get a new code.",
                    parse_mode='Markdown'
                )
            else:
                await send_error_message(update, error_code)
    
    elif session.step == "password":
        await update.message.reply_text("🔄 **Verifying password...**")
        
        result = await forwarder.verify_password(user_id, text)
        
        if result["status"] == "success":
            await update.message.reply_text(
                f"✅ **Login successful!**\n\n"
                f"Welcome, {result['user']}!\n\n"
                f"Fetching your channels...",
                parse_mode='Markdown'
            )
            
            await forwarder.start_scanner()
            
            channels = await forwarder.fetch_my_channels()
            if channels:
                for ch in channels:
                    add_channel(ch['id'], ch['name'], enabled=False)
                await show_channel_selector(update, channels)
            else:
                await update.message.reply_text("📭 No channels found.")
        else:
            await send_error_message(update, ErrorCode.PASSWORD_INVALID)

async def show_channel_selector(update: Update, channels: List[dict]):
    enabled_ids = {ch["id"] for ch in load_channels() if ch.get("enabled", False)}
    
    keyboard = []
    for ch in channels:
        status = "✅" if ch['id'] in enabled_ids else "⏸️"
        keyboard.append([
            InlineKeyboardButton(
                f"{status} {ch['name'][:35]}",
                callback_data=f"ch_{ch['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("✅ Confirm Selection", callback_data="confirm")])
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📋 **Select Channels to Monitor**\n\n"
        f"Found {len(channels)} channels.\n"
        f"Tap to toggle monitoring on/off.\n\n"
        f"✅ = Active | ⏸️ = Disabled\n\n"
        f"Bot will scan every {SCAN_INTERVAL // 60} minutes.",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_main_menu(update: Update):
    channels = load_channels()
    enabled = len([ch for ch in channels if ch.get("enabled", False)])
    me = await forwarder.client.get_me()
    
    msg = (
        f"✅ **Forwarder Active**\n\n"
        f"👤 **Account:** {me.first_name}\n"
        f"📡 **Channels:** {enabled}/{len(channels)} active\n"
        f"📁 **Scan Interval:** {SCAN_INTERVAL // 60} minutes\n"
        f"🎯 **Target Bot:** Active\n\n"
        f"**Commands:**\n"
        f"/channels - Manage channels\n"
        f"/refresh - Fetch channels\n"
        f"/status - View stats\n"
        f"/scan - Manual scan\n"
        f"/logout - Log out"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not forwarder or not forwarder.client:
        await send_error_message(update, ErrorCode.NOT_LOGGED_IN)
        return
    
    channels = load_channels()
    if not channels:
        await update.message.reply_text("📭 No channels. Use /refresh to fetch.")
        return
    
    keyboard = []
    for ch in channels:
        status = "✅" if ch.get("enabled", False) else "⏸️"
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

async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not forwarder or not forwarder.client:
        await send_error_message(update, ErrorCode.NOT_LOGGED_IN)
        return
    
    await update.message.reply_text("🔄 Fetching channels...")
    channels = await forwarder.fetch_my_channels()
    
    if channels:
        existing = {ch["id"]: ch.get("enabled", False) for ch in load_channels()}
        new_channels = []
        for ch in channels:
            new_channels.append({
                "id": ch["id"],
                "name": ch["name"],
                "enabled": existing.get(ch["id"], False),
                "added_at": datetime.now().isoformat()
            })
        save_channels(new_channels)
        await show_channel_selector(update, channels)
    else:
        await update.message.reply_text("❌ No channels found.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not forwarder or not forwarder.client:
        await send_error_message(update, ErrorCode.NOT_LOGGED_IN)
        return
    
    channels = load_channels()
    enabled = len([ch for ch in channels if ch.get("enabled", False)])
    
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM forwarded_files")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM forwarded_files WHERE forwarded_at > datetime('now', '-24 hours')")
    today = c.fetchone()[0]
    conn.close()
    
    me = await forwarder.client.get_me()
    
    msg = (
        f"📊 **Status**\n\n"
        f"👤 **Account:** {me.first_name}\n"
        f"📡 **Active Channels:** {enabled}\n"
        f"📁 **Files Forwarded:** {total} total, {today} today\n"
        f"⏱️ **Scan Interval:** {SCAN_INTERVAL // 60} minutes\n"
        f"🔌 **Scanner:** {'Running' if forwarder.running else 'Stopped'}"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not forwarder or not forwarder.client:
        await send_error_message(update, ErrorCode.NOT_LOGGED_IN)
        return
    
    await update.message.reply_text("🔄 Manual scan triggered...")
    asyncio.create_task(forwarder.manual_scan())

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global forwarder
    if forwarder:
        await forwarder.shutdown()
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    save_channels([])
    forwarder = None
    await update.message.reply_text(
        "✅ **Logged out**\n\n"
        "Use `/login` to log in again.",
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("toggle_"):
        channel_id = int(data.split("_")[1])
        channels = load_channels()
        for ch in channels:
            if ch["id"] == channel_id:
                ch["enabled"] = not ch.get("enabled", False)
                break
        save_channels(channels)
        await channels_cmd(update, context)
    
    elif data.startswith("ch_"):
        channel_id = int(data.split("_")[1])
        channels = load_channels()
        for ch in channels:
            if ch["id"] == channel_id:
                ch["enabled"] = not ch.get("enabled", False)
                break
        save_channels(channels)
        
        all_channels = await forwarder.fetch_my_channels() if forwarder else []
        enabled_ids = {ch["id"] for ch in load_channels() if ch.get("enabled", False)}
        
        keyboard = []
        for ch in all_channels:
            status = "✅" if ch['id'] in enabled_ids else "⏸️"
            keyboard.append([
                InlineKeyboardButton(
                    f"{status} {ch['name'][:35]}",
                    callback_data=f"ch_{ch['id']}"
                )
            ])
        keyboard.append([InlineKeyboardButton("✅ Confirm Selection", callback_data="confirm")])
        keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
        
        await query.edit_message_text(
            f"📋 **Select Channels to Monitor**\n\n"
            f"Tap to toggle on/off.\n\n"
            f"✅ = Active | ⏸️ = Disabled",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    
    elif data == "confirm":
        await query.edit_message_text(
            "✅ **Channels saved!**\n\n"
            "Monitoring will now begin.\n"
            f"Scanner runs every {SCAN_INTERVAL // 60} minutes.\n"
            "Use /status to check progress.",
            parse_mode='Markdown'
        )
    
    elif data == "refresh":
        await query.edit_message_text("🔄 Refreshing channel list...")
        if forwarder:
            channels = await forwarder.fetch_my_channels()
            if channels:
                existing = {ch["id"]: ch.get("enabled", False) for ch in load_channels()}
                new_channels = []
                for ch in channels:
                    new_channels.append({
                        "id": ch["id"],
                        "name": ch["name"],
                        "enabled": existing.get(ch["id"], False),
                        "added_at": datetime.now().isoformat()
                    })
                save_channels(new_channels)
                
                enabled_ids = {ch["id"] for ch in new_channels if ch.get("enabled", False)}
                keyboard = []
                for ch in new_channels:
                    status = "✅" if ch['id'] in enabled_ids else "⏸️"
                    keyboard.append([
                        InlineKeyboardButton(
                            f"{status} {ch['name'][:35]}",
                            callback_data=f"ch_{ch['id']}"
                        )
                    ])
                keyboard.append([InlineKeyboardButton("✅ Confirm Selection", callback_data="confirm")])
                keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh")])
                
                await query.edit_message_text(
                    f"📋 Found {len(new_channels)} channels.\n\nTap to toggle:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
            else:
                await query.edit_message_text("❌ No channels found.")

# ============================================================
# MAIN
# ============================================================

async def main():
    global forwarder
    
    init_db()
    
    # Try to load existing session
    forwarder = TelegramForwarder()
    if await forwarder.load_existing_session():
        await forwarder.start_scanner()
        logger.info("✅ Existing session loaded, scanner started")
    else:
        logger.info("No existing session found, waiting for login")
    
    # Start control bot
    app = Application.builder().token(CONTROL_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("resend", resend_cmd))
    app.add_handler(CommandHandler("cancel", cancel_login_cmd))
    app.add_handler(CommandHandler("channels", channels_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("\n" + "="*60)
    print("🤖 TELEGRAM TXT FILE FORWARDER")
    print("="*60)
    print(f"Control Bot: {CONTROL_BOT_TOKEN[:15]}...")
    print(f"Target Bot: {TARGET_BOT_TOKEN[:15]}...")
    print(f"Scan Interval: {SCAN_INTERVAL // 60} minutes")
    print("="*60)
    print("Bot is running! Send /start to your control bot")
    print("="*60 + "\n")
    
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
