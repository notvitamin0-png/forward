#!/usr/bin/env python3
"""
TELEGRAM TXT FILE FORWARDER - WORKING LOGIN
With residential proxy support to bypass Telegram cloud blocks
"""

import os
import asyncio
import sqlite3
import logging
import json
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

CONTROL_BOT_TOKEN = "8666320518:AAEIhkSS0XeJ-k40rc3d80Dn0b-q9JLcnyI"
TARGET_BOT_TOKEN = "8657130802:AAE8Ynf791ramxyFktFPHgwuv0b5vNKiKH0"
TARGET_CHAT_ID = "8260250818"
API_ID = 39184727
API_HASH = "a52c4985a38ef98c84cdf11d45e53baf"

# ============================================================
# PROXY CONFIGURATION (OPTIONAL - UNCOMMENT TO USE)
# Get free proxies from: https://free-proxy-list.net/
# ============================================================
# PROXY = {
#     'proxy_type': 'socks5',  # or 'http'
#     'addr': '192.252.215.5',  # proxy IP
#     'port': 4145,             # proxy port
#     'rdns': True
# }
PROXY = None  # Set to None for no proxy

DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

SESSION_FILE = os.path.join(DATA_DIR, "user_session.session")
DB_FILE = os.path.join(DATA_DIR, "forwarded.db")
CONFIG_FILE = os.path.join(DATA_DIR, "channels.json")

SCAN_INTERVAL = 300

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
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

def get_enabled_channels() -> List[dict]:
    return [ch for ch in load_channels() if ch.get("enabled", True)]

def toggle_channel(channel_id: int):
    channels = load_channels()
    for ch in channels:
        if ch["id"] == channel_id:
            ch["enabled"] = not ch.get("enabled", True)
            break
    save_channels(channels)

def clear_channels():
    save_channels([])

# ============================================================
# FORWARDER ENGINE
# ============================================================

class TelegramForwarder:
    def __init__(self):
        self.client = None
        self.scanner_task = None
        self.running = True
        self.login_data = {}  # Store login state per user

    async def load_existing_session(self) -> bool:
        """Load existing session - NO LOGIN ATTEMPT"""
        if not os.path.exists(SESSION_FILE):
            return False
        
        try:
            if PROXY:
                self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH, proxy=PROXY)
            else:
                self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
            await self.client.start()
            if await self.client.is_user_authorized():
                me = await self.client.get_me()
                logger.info(f"✅ Session loaded: {me.first_name}")
                return True
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
        return False

    async def start_login(self, user_id: int, phone: str):
        """Step 1: Request code"""
        self.login_data[user_id] = {"phone": phone, "step": "code"}
        
        try:
            if PROXY:
                client = TelegramClient(None, API_ID, API_HASH, proxy=PROXY)
            else:
                client = TelegramClient(None, API_ID, API_HASH)
            await client.connect()
            
            result = await client.send_code_request(phone)
            self.login_data[user_id]["client"] = client
            self.login_data[user_id]["phone_code_hash"] = result.phone_code_hash
            
            return {"status": "code_sent"}
        except PhoneNumberInvalidError:
            return {"status": "error", "message": "Invalid phone number"}
        except FloodWaitError as e:
            return {"status": "error", "message": f"Wait {e.seconds} seconds"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def verify_code(self, user_id: int, code: str):
        """Step 2: Verify code"""
        data = self.login_data.get(user_id)
        if not data:
            return {"status": "error", "message": "No login session"}
        
        client = data.get("client")
        if not client:
            return {"status": "error", "message": "Client not found"}
        
        try:
            await client.sign_in(
                data["phone"], 
                code, 
                phone_code_hash=data.get("phone_code_hash")
            )
            
            if await client.is_user_authorized():
                me = await client.get_me()
                await client.disconnect()
                
                # Save session permanently
                if PROXY:
                    self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH, proxy=PROXY)
                else:
                    self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
                await self.client.start()
                
                # Cleanup
                await client.disconnect()
                self.login_data.pop(user_id, None)
                
                return {"status": "success", "user": me.first_name}
                
        except SessionPasswordNeededError:
            return {"status": "password_needed"}
        except PhoneCodeExpiredError:
            return {"status": "error", "message": "Code expired. Use /login again"}
        except PhoneCodeInvalidError:
            return {"status": "error", "message": "Invalid code"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        
        return {"status": "error", "message": "Unknown error"}

    async def verify_password(self, user_id: int, password: str):
        """Step 3: Verify 2FA password"""
        data = self.login_data.get(user_id)
        if not data:
            return {"status": "error", "message": "No login session"}
        
        client = data.get("client")
        if not client:
            return {"status": "error", "message": "Client not found"}
        
        try:
            await client.sign_in(password=password)
            
            if await client.is_user_authorized():
                me = await client.get_me()
                
                # Save session permanently
                if PROXY:
                    self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH, proxy=PROXY)
                else:
                    self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
                await self.client.start()
                
                await client.disconnect()
                self.login_data.pop(user_id, None)
                
                return {"status": "success", "user": me.first_name}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        
        return {"status": "error", "message": "Wrong password"}

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
            
            if messages:
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

    async def fetch_channels(self) -> List[dict]:
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
login_states = {}  # Track which users are logging in

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if forwarder and forwarder.client and forwarder.client.is_connected():
        await show_main_menu(update)
    else:
        msg = (
            "🤖 **Telegram TXT File Forwarder**\n\n"
            "**To start, send:**\n"
            "`/login +977XXXXXXXXX`\n\n"
            "Then enter the OTP code.\n"
            "If 2FA, enter password.\n\n"
            "After login, select channels to monitor.\n\n"
            "**Note:** If you get 'code expired', wait 30 seconds and try `/login` again."
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Usage: `/login +977XXXXXXXXX`\n\n"
            "Example: `/login +9779765952106`",
            parse_mode='Markdown'
        )
        return
    
    phone = args[0].strip()
    if not phone.startswith('+'):
        await update.message.reply_text("❌ Phone must start with `+`", parse_mode='Markdown')
        return
    
    user_id = update.effective_user.id
    
    await update.message.reply_text("🔄 **Requesting verification code...**")
    
    global forwarder
    if not forwarder:
        forwarder = TelegramForwarder()
    
    result = await forwarder.start_login(user_id, phone)
    
    if result["status"] == "code_sent":
        login_states[user_id] = {"step": "code"}
        await update.message.reply_text(
            "✅ **Code sent!**\n\n"
            "Enter the OTP code you received in Telegram:\n"
            "Just type the code (e.g., `12345`)\n\n"
            "Type `/cancel` to abort.",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"❌ {result.get('message', 'Unknown error')}")

async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in login_states:
        login_states.pop(user_id)
    await update.message.reply_text("✅ Login cancelled.")

async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in login_states:
        return
    
    text = update.message.text.strip()
    if not text.isdigit():
        return
    
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
        
        channels = await forwarder.fetch_channels()
        if channels:
            clear_channels()
            for ch in channels:
                add_channel(ch['id'], ch['name'], enabled=True)
            await show_channel_selector(update, channels)
        else:
            await update.message.reply_text("📭 No channels found.")
        
        login_states.pop(user_id, None)
        
    elif result["status"] == "password_needed":
        login_states[user_id] = {"step": "password"}
        await update.message.reply_text(
            "🔐 **2FA Enabled**\n\n"
            "Enter your Telegram password:",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"❌ {result.get('message', 'Invalid code')}")

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in login_states or login_states.get(user_id, {}).get("step") != "password":
        return
    
    password = update.message.text.strip()
    
    await update.message.reply_text("🔄 **Verifying password...**")
    
    result = await forwarder.verify_password(user_id, password)
    
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
                add_channel(ch['id'], ch['name'], enabled=True)
            await show_channel_selector(update, channels)
        else:
            await update.message.reply_text("📭 No channels found.")
        
        login_states.pop(user_id, None)
    else:
        await update.message.reply_text(f"❌ {result.get('message', 'Wrong password')}")

async def show_channel_selector(update: Update, channels: List[dict]):
    keyboard = []
    for ch in channels:
        status = "✅" if ch.get("enabled", True) else "⏸️"
        keyboard.append([
            InlineKeyboardButton(
                f"{status} {ch['name'][:35]}",
                callback_data=f"toggle_{ch['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("✅ Confirm", callback_data="confirm")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📋 **Select Channels to Monitor**\n\n"
        f"Found {len(channels)} channels.\n"
        f"Tap to toggle on/off.\n\n"
        f"✅ = Active | ⏸️ = Disabled",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_main_menu(update: Update):
    channels = load_channels()
    enabled = len([ch for ch in channels if ch.get("enabled", False)])
    
    msg = (
        f"✅ **Forwarder Active**\n\n"
        f"📡 **Channels:** {enabled}/{len(channels)} active\n"
        f"📁 **Scan:** every {SCAN_INTERVAL // 60} min\n\n"
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
        await update.message.reply_text("❌ Not logged in. Use /login +977XXXXXXXXX")
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
        "📋 **Your Channels**\n\nTap to toggle:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not forwarder or not forwarder.client:
        await update.message.reply_text("❌ Not logged in. Use /login +977XXXXXXXXX")
        return
    
    await update.message.reply_text("🔄 Fetching channels...")
    channels = await forwarder.fetch_channels()
    
    if channels:
        clear_channels()
        for ch in channels:
            add_channel(ch['id'], ch['name'], enabled=True)
        
        keyboard = []
        for ch in channels:
            keyboard.append([
                InlineKeyboardButton(
                    f"✅ {ch['name'][:35]}",
                    callback_data=f"toggle_{ch['id']}"
                )
            ])
        keyboard.append([InlineKeyboardButton("✅ Confirm", callback_data="confirm")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"📋 Found {len(channels)} channels.\n\nTap to toggle:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text("❌ No channels found.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not forwarder or not forwarder.client:
        await update.message.reply_text("❌ Not logged in. Use /login +977XXXXXXXXX")
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
    
    msg = (
        f"📊 **Status**\n\n"
        f"📡 Active Channels: {enabled}\n"
        f"📁 Files Forwarded: {total} total, {today} today\n"
        f"⏱️ Scan: every {SCAN_INTERVAL // 60} min"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not forwarder or not forwarder.client:
        await update.message.reply_text("❌ Not logged in")
        return
    
    await update.message.reply_text("🔄 Manual scan triggered...")
    asyncio.create_task(forwarder.manual_scan())

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global forwarder
    if forwarder:
        await forwarder.shutdown()
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    clear_channels()
    forwarder = None
    login_states.clear()
    await update.message.reply_text("✅ Logged out")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("toggle_"):
        channel_id = int(data.split("_")[1])
        toggle_channel(channel_id)
        await channels_cmd(update, context)
    
    elif data == "confirm":
        await query.edit_message_text(
            "✅ **Channels saved!**\n\n"
            "Monitoring will now begin.\n"
            f"Scanner runs every {SCAN_INTERVAL // 60} minutes.",
            parse_mode='Markdown'
        )

# ============================================================
# MAIN
# ============================================================

async def main():
    global forwarder
    
    init_db()
    
    forwarder = TelegramForwarder()
    if await forwarder.load_existing_session():
        await forwarder.start_scanner()
        logger.info("✅ Existing session loaded")
    
    app = Application.builder().token(CONTROL_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("cancel", cancel_login))
    app.add_handler(CommandHandler("channels", channels_cmd))
    app.add_handler(CommandHandler("refresh", refresh_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password))
    app.add_handler(CallbackQueryHandler(button_handler))
    
    print("\n" + "="*60)
    print("🤖 TELEGRAM TXT FILE FORWARDER")
    print("="*60)
    print(f"Bot is running!")
    print(f"Proxy: {'Enabled' if PROXY else 'Disabled'}")
    print("Send /login +977XXXXXXXXX to start")
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
