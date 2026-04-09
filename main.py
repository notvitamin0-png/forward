#!/usr/bin/env python3
"""
TELEGRAM TXT FILE FORWARDER - COMPLETE WORKING VERSION
Monitors channels and forwards .txt files to your target bot
Login once via bot commands - session saved forever
"""

import os
import asyncio
import sqlite3
import logging
import time
from datetime import datetime
from typing import List, Dict, Optional

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename, MessageMediaDocument
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, FloodWaitError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ============================================================
# CONFIGURATION
# ============================================================

# Your main bot token (for controlling the forwarder)
CONTROL_BOT_TOKEN = "8666320518:AAEIhkSS0XeJ-k40rc3d80Dn0b-q9JLcnyI"

# Target bot where files will be forwarded
TARGET_BOT_TOKEN = "8657130802:AAE8Ynf791ramxyFktFPHgwuv0b5vNKiKH0"
TARGET_CHAT_ID = "8260250818"

# Your API credentials (from https://my.telegram.org/apps)
API_ID = 39184727
API_HASH = "a52c4985a38ef98c84cdf11d45e53baf"

DATA_DIR = "/app/data" if os.path.exists("/app") else "data"
os.makedirs(DATA_DIR, exist_ok=True)

SESSION_FILE = os.path.join(DATA_DIR, "forwarder_session.session")
DB_FILE = os.path.join(DATA_DIR, "forwarded.db")
CONFIG_FILE = os.path.join(DATA_DIR, "channels.json")

SCAN_INTERVAL = 300  # 5 minutes

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# DATABASE (Track forwarded files)
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
    import json
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return []

def save_channels(channels: List[dict]):
    import json
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
# FORWARDER ENGINE
# ============================================================

class TelegramForwarder:
    def __init__(self):
        self.client = None
        self.scanner_task = None
        self.running = True

    async def login(self, phone: str = None, code: str = None, password: str = None, phone_code_hash: str = None):
        """Handle login flow"""
        if not self.client:
            self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
            await self.client.connect()
        
        if code is None and password is None and phone is not None:
            # Step 1: Send code
            result = await self.client.send_code_request(phone)
            return {"status": "code_sent", "phone_code_hash": result.phone_code_hash}
        elif password is not None:
            # Step 3: 2FA password
            await self.client.sign_in(password=password)
            me = await self.client.get_me()
            return {"status": "success", "user": me.first_name}
        else:
            # Step 2: Verify code
            try:
                await self.client.sign_in(phone, code, phone_code_hash=phone_code_hash)
                me = await self.client.get_me()
                return {"status": "success", "user": me.first_name}
            except SessionPasswordNeededError:
                return {"status": "password_needed"}
            except PhoneCodeInvalidError:
                return {"status": "invalid_code"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

    async def is_authenticated(self) -> bool:
        """Check if already logged in"""
        if os.path.exists(SESSION_FILE):
            try:
                self.client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
                await self.client.start()
                if await self.client.is_user_authorized():
                    me = await self.client.get_me()
                    logger.info(f"✅ Already logged in as: {me.first_name}")
                    return True
            except:
                pass
        return False

    async def start_scanner(self):
        """Start background scanner"""
        if self.scanner_task:
            return
        self.scanner_task = asyncio.create_task(self._scanner_loop())
        logger.info("Scanner started")

    async def _scanner_loop(self):
        """Scan all enabled channels every SCAN_INTERVAL seconds"""
        while self.running:
            try:
                await self._scan_all_channels()
            except Exception as e:
                logger.error(f"Scanner error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

    async def _scan_all_channels(self):
        """Scan all enabled channels for new .txt files"""
        if not self.client or not self.client.is_connected():
            return
        
        channels = get_enabled_channels()
        if not channels:
            return
        
        for channel_info in channels:
            try:
                await self._scan_channel(channel_info)
            except Exception as e:
                logger.error(f"Error scanning {channel_info.get('name')}: {e}")

    async def _scan_channel(self, channel_info: dict):
        """Scan a single channel for new files"""
        channel_id = channel_info["id"]
        channel_name = channel_info["name"]
        
        try:
            channel = await self.client.get_entity(channel_id)
            last_msg_id = get_last_scan(channel_id)
            
            # Get recent messages
            messages = await self.client.get_messages(channel, limit=50)
            if not messages:
                return
            
            # Find new .txt files
            new_files = []
            for msg in messages:
                if last_msg_id is None or msg.id > last_msg_id:
                    if msg.document:
                        file_name = self._get_filename(msg)
                        if file_name and file_name.endswith('.txt'):
                            new_files.append(msg)
            
            # Update last scan ID
            latest_id = max(m.id for m in messages)
            update_last_scan(channel_id, latest_id)
            
            # Process new files (oldest first)
            for msg in reversed(new_files):
                await self._forward_file(msg, channel_name)
                
        except Exception as e:
            logger.error(f"Error in channel {channel_name}: {e}")

    def _get_filename(self, msg) -> Optional[str]:
        """Extract filename from message"""
        if msg.document and msg.document.attributes:
            for attr in msg.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
        return None

    async def _forward_file(self, msg, channel_name: str):
        """Forward a single file to target bot"""
        file_id = str(msg.document.id)
        
        # Check if already forwarded
        if is_forwarded(file_id):
            logger.info(f"Skipping already forwarded: {self._get_filename(msg)}")
            return
        
        file_name = self._get_filename(msg)
        if not file_name:
            return
        
        logger.info(f"📁 New file detected: {file_name} from {channel_name}")
        
        # Download file
        temp_path = os.path.join(DATA_DIR, f"temp_{msg.id}_{file_name}")
        try:
            await self.client.download_file(msg.media, temp_path)
            
            # Forward to target bot
            url = f"https://api.telegram.org/bot{TARGET_BOT_TOKEN}/sendDocument"
            with open(temp_path, 'rb') as f:
                files = {'document': (file_name, f)}
                data = {
                    'chat_id': TARGET_CHAT_ID,
                    'caption': f"📁 {file_name}\n📡 Source: {channel_name}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                }
                import requests
                response = requests.post(url, data=data, files=files, timeout=30)
                
                if response.status_code == 200:
                    logger.info(f"✅ Forwarded: {file_name}")
                    mark_forwarded(file_id, msg.id, msg.chat_id, file_name)
                else:
                    logger.warning(f"Failed to forward: {file_name} - {response.status_code}")
                    
        except Exception as e:
            logger.error(f"Error forwarding {file_name}: {e}")
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass

    async def fetch_my_channels(self) -> List[dict]:
        """Fetch all channels the user is a member of"""
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

    async def shutdown(self):
        """Shutdown the forwarder"""
        self.running = False
        if self.scanner_task:
            self.scanner_task.cancel()
        if self.client:
            await self.client.disconnect()

# ============================================================
# TELEGRAM BOT COMMANDS
# ============================================================

forwarder = None
login_states = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    if forwarder and forwarder.client and forwarder.client.is_connected():
        await show_main_menu(update)
    else:
        msg = (
            "🤖 **Telegram TXT File Forwarder**\n\n"
            "This bot monitors channels and forwards .txt files.\n\n"
            "**First, log in with your Telegram account:**\n"
            "1. Send `/login`\n"
            "2. Enter your phone number\n"
            "3. Enter the OTP code\n"
            "4. If 2FA enabled, enter password\n\n"
            "After login, select channels to monitor."
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start login process"""
    user_id = update.effective_user.id
    login_states[user_id] = {"step": "phone"}
    await update.message.reply_text(
        "📱 **Enter your phone number** (with country code)\n"
        "Example: `+977XXXXXXXXX`",
        parse_mode='Markdown'
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle login text input"""
    user_id = update.effective_user.id
    if user_id not in login_states:
        return
    
    state = login_states[user_id]
    text = update.message.text.strip()
    
    if state["step"] == "phone":
        state["phone"] = text
        state["step"] = "waiting_code"
        
        await update.message.reply_text("🔄 **Sending verification code...**")
        
        global forwarder
        if not forwarder:
            forwarder = TelegramForwarder()
        
        result = await forwarder.login(phone=text)
        
        if result["status"] == "code_sent":
            state["phone_code_hash"] = result["phone_code_hash"]
            state["step"] = "code"
            await update.message.reply_text(
                "✅ **Code sent!**\n\nEnter the OTP code you received:",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(f"❌ Error: {result.get('message', 'Unknown error')}")
            login_states.pop(user_id, None)
    
    elif state["step"] == "code":
        await update.message.reply_text("🔄 **Verifying code...**")
        
        result = await forwarder.login(
            phone=state["phone"],
            code=text,
            phone_code_hash=state.get("phone_code_hash")
        )
        
        if result["status"] == "success":
            await update.message.reply_text(
                f"✅ **Login successful!**\n\nWelcome, {result['user']}!\n\nFetching your channels...",
                parse_mode='Markdown'
            )
            
            await forwarder.start_scanner()
            
            # Fetch and show channels
            channels = await forwarder.fetch_my_channels()
            if channels:
                for ch in channels:
                    add_channel(ch['id'], ch['name'], enabled=False)
                await show_channel_selector(update, channels)
            else:
                await update.message.reply_text("📭 No channels found.")
            
            login_states.pop(user_id, None)
        
        elif result["status"] == "password_needed":
            state["step"] = "password"
            await update.message.reply_text(
                "🔐 **2FA Enabled**\n\nEnter your Telegram password:",
                parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(f"❌ {result.get('message', 'Invalid code')}")
    
    elif state["step"] == "password":
        await update.message.reply_text("🔄 **Verifying password...**")
        
        result = await forwarder.login(password=text)
        
        if result["status"] == "success":
            await update.message.reply_text(
                f"✅ **Login successful!**\n\nWelcome, {result['user']}!\n\nFetching your channels...",
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
            
            login_states.pop(user_id, None)
        else:
            await update.message.reply_text("❌ Wrong password. Try again:")

async def show_channel_selector(update: Update, channels: List[dict]):
    """Show interactive channel selection menu"""
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
    """Show main menu after login"""
    channels = load_channels()
    enabled = len([ch for ch in channels if ch.get("enabled", False)])
    
    msg = (
        f"✅ **Forwarder Active**\n\n"
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
    """Show channel management menu"""
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
    """Refresh channel list"""
    if not forwarder:
        await update.message.reply_text("❌ Not logged in. Use /login first.")
        return
    
    await update.message.reply_text("🔄 Fetching channels...")
    channels = await forwarder.fetch_my_channels()
    
    if channels:
        # Update channels list (preserve enabled status)
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
    """Show status"""
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
        f"📡 **Active Channels:** {enabled}\n"
        f"📁 **Files Forwarded:** {total} total, {today} today\n"
        f"⏱️ **Scan Interval:** {SCAN_INTERVAL // 60} minutes\n"
        f"🔌 **Scanner:** {'Running' if forwarder and forwarder.running else 'Stopped'}"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual scan trigger"""
    if forwarder:
        await update.message.reply_text("🔄 Manual scan triggered...")
        asyncio.create_task(forwarder._scan_all_channels())
    else:
        await update.message.reply_text("❌ Forwarder not ready.")

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log out and clear session"""
    global forwarder
    if forwarder:
        await forwarder.shutdown()
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    save_channels([])
    forwarder = None
    await update.message.reply_text("✅ **Logged out**\n\nUse /login to log in again.", parse_mode='Markdown')

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks"""
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
        
        # Refresh the selector
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
    if await forwarder.is_authenticated():
        await forwarder.start_scanner()
        logger.info("✅ Existing session loaded, scanner started")
    
    # Start control bot
    app = Application.builder().token(CONTROL_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_cmd))
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
