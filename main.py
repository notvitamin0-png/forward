l#!/usr/bin/env python3
"""
Telegram TXT File Forwarder - Railway Ready
Login via bot, select channels, auto-forward .txt files
"""

import os
import sys
import json
import asyncio
import sqlite3
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Set
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import SessionPasswordNeededError, FloodWaitError, RPCError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ============================================================
# CONFIGURATION
# ============================================================

COMMAND_BOT_TOKEN = "8666320518:AAEIhkSS0XeJ-k40rc3d80Dn0b-q9JLcnyI"
TARGET_BOT_TOKEN = "8657130802:AAE8Ynf791ramxyFktFPHgwuv0b5vNKiKH0"
TARGET_CHAT_ID = "8260250818"

FILES_DIR = "/app/data" if os.path.exists("/app") else "data"
SESSION_FILE = os.path.join(FILES_DIR, "user_session.session")
CONFIG_FILE = os.path.join(FILES_DIR, "config.json")
DB_FILE = os.path.join(FILES_DIR, "forwarded.db")
SCAN_INTERVAL = 300  # 5 minutes

# Create data directory
os.makedirs(FILES_DIR, exist_ok=True)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
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
# CONFIGURATION
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
# TELEGRAM CLIENT (in separate thread)
# ============================================================

telethon_client = None
scanner_running = False
client_lock = threading.Lock()

async def start_telethon_client():
    global telethon_client
    api_id, api_hash = get_credentials()
    if not api_id or not api_hash:
        logger.error("No API credentials found")
        return False
    
    telethon_client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
    try:
        await telethon_client.start()
        if not await telethon_client.is_user_authorized():
            logger.error("Not authorized")
            return False
        me = await telethon_client.get_me()
        logger.info(f"Telethon client started as: {me.first_name}")
        return True
    except Exception as e:
        logger.error(f"Failed to start Telethon client: {e}")
        return False

async def forward_to_saved_messages(file_path: str, caption: str):
    """Forward to Saved Messages using Telethon"""
    if not telethon_client:
        return False
    try:
        await telethon_client.send_file('me', file_path, caption=caption)
        return True
    except Exception as e:
        logger.error(f"Failed to forward to Saved Messages: {e}")
        return False

async def forward_to_target_bot(file_path: str, file_name: str, source_chat: str):
    """Forward using target bot (via requests)"""
    try:
        import requests
        url = f"https://api.telegram.org/bot{TARGET_BOT_TOKEN}/sendDocument"
        with open(file_path, 'rb') as f:
            files = {'document': (file_name, f)}
            data = {
                'chat_id': TARGET_CHAT_ID,
                'caption': f"📁 {file_name}\n📡 Source: {source_chat}\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            }
            response = requests.post(url, data=data, files=files, timeout=30)
            return response.status_code == 200
    except Exception as e:
        logger.error(f"Failed to forward to target bot: {e}")
        return False

async def scan_and_forward():
    """Main scanning logic - runs every 5 minutes"""
    global scanner_running
    
    if scanner_running:
        return
    scanner_running = True
    
    try:
        enabled_channels = get_enabled_channels()
        if not enabled_channels or not telethon_client:
            return
        
        for channel_info in enabled_channels:
            try:
                channel_id = channel_info["id"]
                channel_name = channel_info["name"]
                
                # Get channel entity
                channel = await telethon_client.get_entity(channel_id)
                
                # Get last scanned message ID
                last_msg_id = get_last_scan(channel_id)
                
                # Get messages
                messages = await telethon_client.get_messages(channel, limit=50)
                
                # Process new messages
                new_files = []
                for msg in messages:
                    if last_msg_id is None or msg.id > last_msg_id:
                        if msg.document:
                            file_name = None
                            if msg.document.attributes:
                                for attr in msg.document.attributes:
                                    if isinstance(attr, DocumentAttributeFilename):
                                        file_name = attr.file_name
                                        break
                            if file_name and file_name.endswith('.txt'):
                                new_files.append(msg)
                
                # Update last scan
                if messages:
                    latest_id = max([m.id for m in messages])
                    update_last_scan(channel_id, latest_id)
                
                # Process files (oldest first)
                for msg in reversed(new_files):
                    file_id = str(msg.document.id)
                    
                    if is_file_forwarded(file_id):
                        continue
                    
                    file_name = None
                    for attr in msg.document.attributes:
                        if isinstance(attr, DocumentAttributeFilename):
                            file_name = attr.file_name
                            break
                    
                    logger.info(f"New .txt file: {file_name} from {channel_name}")
                    
                    # Download file
                    file_path = os.path.join(FILES_DIR, f"temp_{msg.id}_{file_name}")
                    await telethon_client.download_file(msg.media, file_path)
                    
                    # Forward to Saved Messages
                    config = load_config()
                    if config["settings"].get("forward_to_saved", True):
                        caption = f"Forwarded from: {channel_name}\nOriginal message: {msg.id}"
                        await forward_to_saved_messages(file_path, caption)
                    
                    # Forward to target bot
                    if config["settings"].get("forward_to_bot", True):
                        await forward_to_target_bot(file_path, file_name, channel_name)
                    
                    # Mark as forwarded
                    mark_file_forwarded(file_id, msg.id, channel_id, file_name)
                    
                    # Cleanup
                    try:
                        os.remove(file_path)
                    except:
                        pass
                    
            except Exception as e:
                logger.error(f"Error scanning channel {channel_info.get('name')}: {e}")
                
    except Exception as e:
        logger.error(f"Scan error: {e}")
    finally:
        scanner_running = False

async def scanner_loop():
    """Background loop that runs scan every SCAN_INTERVAL seconds"""
    while True:
        await scan_and_forward()
        await asyncio.sleep(SCAN_INTERVAL)

# ============================================================
# TELEGRAM BOT COMMANDS
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
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
    user_id = update.effective_user.id
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
    """Handle multi-step login input"""
    if 'pending_login' not in context.user_data:
        return
    
    step = context.user_data.get('login_step')
    api_id, api_hash = context.user_data['pending_login']
    
    if step == 'phone':
        phone = update.message.text.strip()
        context.user_data['login_phone'] = phone
        context.user_data['login_step'] = 'code'
        
        # Request code via Telethon (in background)
        async def request_code():
            client = TelegramClient(None, int(api_id), api_hash)
            await client.connect()
            await client.send_code_request(phone)
            await client.disconnect()
        
        asyncio.create_task(request_code())
        
        await update.message.reply_text(
            "✅ **Code sent!**\n\n"
            "Please enter the OTP code you received:"
        )
        
    elif step == 'code':
        code = update.message.text.strip()
        phone = context.user_data['login_phone']
        
        await update.message.reply_text("🔄 **Verifying code...**")
        
        # Attempt login
        client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
        
        try:
            await client.connect()
            await client.sign_in(phone, code)
            
            # Check if 2FA needed
            if await client.is_user_authorized():
                me = await client.get_me()
                save_credentials(api_id, api_hash)
                set_logged_in(True)
                
                await update.message.reply_text(
                    f"✅ **Login successful!**\n\n"
                    f"Welcome, {me.first_name}! (@{me.username or 'no username'})\n\n"
                    f"Now fetching your channels..."
                )
                
                # Fetch all channels
                await fetch_and_show_channels(update, client)
                
                # Start background scanner
                global telethon_client
                telethon_client = client
                asyncio.create_task(scanner_loop())
                
            else:
                context.user_data['login_step'] = 'password'
                await update.message.reply_text("🔐 **2FA Enabled**\n\nPlease enter your password:")
                
        except SessionPasswordNeededError:
            context.user_data['login_step'] = 'password'
            await update.message.reply_text("🔐 **2FA Enabled**\n\nPlease enter your password:")
        except Exception as e:
            await update.message.reply_text(f"❌ **Login failed:** `{str(e)}`", parse_mode='Markdown')
            context.user_data.pop('pending_login', None)
            context.user_data.pop('login_step', None)
            context.user_data.pop('login_phone', None)
            await client.disconnect()
            
    elif step == 'password':
        password = update.message.text.strip()
        phone = context.user_data['login_phone']
        api_id, api_hash = context.user_data['pending_login']
        
        client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
        
        try:
            await client.connect()
            await client.sign_in(password=password)
            
            me = await client.get_me()
            save_credentials(api_id, api_hash)
            set_logged_in(True)
            
            await update.message.reply_text(
                f"✅ **Login successful!**\n\n"
                f"Welcome, {me.first_name}! (@{me.username or 'no username'})\n\n"
                f"Now fetching your channels..."
            )
            
            await fetch_and_show_channels(update, client)
            
            global telethon_client
            telethon_client = client
            asyncio.create_task(scanner_loop())
            
        except Exception as e:
            await update.message.reply_text(f"❌ **Login failed:** `{str(e)}`", parse_mode='Markdown')
        finally:
            context.user_data.pop('pending_login', None)
            context.user_data.pop('login_step', None)
            context.user_data.pop('login_phone', None)

async def fetch_and_show_channels(update: Update, client):
    """Fetch all channels and show selection UI"""
    try:
        dialogs = await client.get_dialogs()
        channels = []
        
        for dialog in dialogs:
            if dialog.is_channel:
                channels.append({
                    'id': dialog.id,
                    'name': dialog.name,
                    'username': dialog.entity.username,
                    'participants_count': getattr(dialog.entity, 'participants_count', 0)
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
    """Show interactive channel selection menu"""
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
    """Show main menu after login"""
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
    """Show channel management menu"""
    if not is_logged_in():
        await update.message.reply_text("❌ Please login first using /login")
        return
    
    config = load_config()
    channels = config["channels"]
    enabled_ids = {ch["id"] for ch in channels if ch.get("enabled", True)}
    
    if not channels:
        await update.message.reply_text("📭 No channels configured. Run /refresh to fetch your channels.")
        return
    
    keyboard = []
    for ch in channels:
        status = "✅" if ch['id'] in enabled_ids else "⏸️"
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
    """Show settings menu"""
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
    """Show status statistics"""
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
        f"🤖 **Scanner:** {'Running' if telethon_client else 'Not started'}\n\n"
        f"Use /scan to trigger manual scan."
    )
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log out and clear session"""
    global telethon_client
    
    if telethon_client:
        await telethon_client.disconnect()
        telethon_client = None
    
    # Remove session file
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    
    set_logged_in(False)
    clear_channels()
    
    await update.message.reply_text(
        "✅ **Logged out**\n\nYour session has been cleared.\nUse /login to log in again."
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("toggle_"):
        channel_id = int(data.split("_")[1])
        config = load_config()
        for ch in config["channels"]:
            if ch["id"] == channel_id:
                current = ch.get("enabled", True)
                ch["enabled"] = not current
                break
        save_config(config)
        
        # Refresh channel list
        await channels_command(update, context)
        
    elif data.startswith("sel_"):
        channel_id = int(data.split("_")[1])
        config = load_config()
        for ch in config["channels"]:
            if ch["id"] == channel_id:
                current = ch.get("enabled", True)
                ch["enabled"] = not current
                break
        save_config(config)
        
        # Refresh selection menu
        await refresh_selection_menu(update, query.message)
        
    elif data == "confirm_channels":
        await query.edit_message_text(
            "✅ **Channels saved!**\n\nMonitoring will now begin.\n\nUse /status to check progress."
        )
        
    elif data == "refresh_channels":
        await query.edit_message_text("🔄 Refreshing channel list...")
        if telethon_client:
            await fetch_and_show_channels(update, telethon_client)
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
        asyncio.create_task(scan_and_forward())
        
    elif data == "close":
        await query.delete_message()

async def refresh_selection_menu(update: Update, message):
    """Refresh the channel selection menu"""
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
    global telethon_client
    
    init_db()
    
    # Try to load existing session
    api_id, api_hash = get_credentials()
    if api_id and api_hash and os.path.exists(SESSION_FILE):
        telethon_client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
        try:
            await telethon_client.start()
            if await telethon_client.is_user_authorized():
                set_logged_in(True)
                me = await telethon_client.get_me()
                logger.info(f"Loaded existing session for: {me.first_name}")
                asyncio.create_task(scanner_loop())
            else:
                set_logged_in(False)
        except Exception as e:
            logger.error(f"Failed to load session: {e}")
            set_logged_in(False)
    
    # Start command bot
    app = Application.builder().token(COMMAND_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("channels", channels_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("logout", logout_command))
    app.add_handler(CommandHandler("scan", lambda u,c: asyncio.create_task(scan_and_forward())))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_input))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    logger.info("Bot started! Waiting for commands...")
    logger.info(f"Command Bot: @{COMMAND_BOT_TOKEN.split(':')[0]}")
    logger.info(f"Target Chat: {TARGET_CHAT_ID}")
    logger.info(f"Scan Interval: {SCAN_INTERVAL // 60} minutes")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await app.stop()
        if telethon_client:
            await telethon_client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
