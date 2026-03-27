#!/usr/bin/env python3
"""
Telegram Channel TXT File Forwarder
Monitors selected channels and forwards .txt files to Saved Messages and target bot
"""

import os
import sys
import json
import asyncio
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set
import traceback

from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename, Message
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.errors import SessionPasswordNeededError, FloodWaitError, RPCError

# ============================================================
# CONFIGURATION
# ============================================================

# Bot tokens (for sending forwarded files)
TARGET_BOT_TOKEN = "8657130802:AAE8Ynf791ramxyFktFPHgwuv0b5vNKiKH0"
TARGET_CHAT_ID = "8260250818"

# Main bot token (for user commands)
COMMAND_BOT_TOKEN = "8666320518:AAEIhkSS0XeJ-k40rc3d80Dn0b-q9JLcnyI"

# Files
SESSION_FILE = "user_session.session"
CONFIG_FILE = "config.json"
DB_FILE = "forwarded.db"

# Scan interval (seconds)
SCAN_INTERVAL = 300  # 5 minutes

# ============================================================
# LOGGING SETUP
# ============================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
# DATABASE SETUP (Deduplication)
# ============================================================

def init_db():
    """Initialize SQLite database for tracking forwarded files"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS forwarded_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT UNIQUE,
            message_id INTEGER,
            channel_id INTEGER,
            file_name TEXT,
            forwarded_at TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS last_scan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER,
            last_message_id INTEGER,
            last_scan_time TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    logger.info("Database initialized")

def is_file_forwarded(file_id: str) -> bool:
    """Check if file has been forwarded before"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM forwarded_files WHERE file_id = ?", (file_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_file_forwarded(file_id: str, message_id: int, channel_id: int, file_name: str):
    """Mark file as forwarded"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO forwarded_files (file_id, message_id, channel_id, file_name, forwarded_at) VALUES (?, ?, ?, ?, ?)",
        (file_id, message_id, channel_id, file_name, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def update_last_scan(channel_id: int, last_message_id: int):
    """Update last scanned message ID for channel"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO last_scan (id, channel_id, last_message_id, last_scan_time) VALUES ((SELECT id FROM last_scan WHERE channel_id = ?), ?, ?, ?)",
        (channel_id, channel_id, last_message_id, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_last_scan(channel_id: int) -> Optional[int]:
    """Get last scanned message ID for channel"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT last_message_id FROM last_scan WHERE channel_id = ?", (channel_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

# ============================================================
# CONFIGURATION MANAGEMENT
# ============================================================

def load_config() -> dict:
    """Load configuration from JSON file"""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return {
        "channels": [],
        "settings": {
            "auto_forward": True,
            "forward_to_saved": True,
            "forward_to_bot": True,
            "only_txt": True,
            "min_file_size": 0,
            "max_file_size": 10485760  # 10MB default
        }
    }

def save_config(config: dict):
    """Save configuration to JSON file"""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def add_channel(channel_id: int, channel_name: str, enabled: bool = True):
    """Add channel to watch list"""
    config = load_config()
    # Check if already exists
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
    """Remove channel from watch list"""
    config = load_config()
    config["channels"] = [ch for ch in config["channels"] if ch["id"] != channel_id]
    save_config(config)

def toggle_channel(channel_id: int, enabled: bool):
    """Enable/disable channel monitoring"""
    config = load_config()
    for ch in config["channels"]:
        if ch["id"] == channel_id:
            ch["enabled"] = enabled
            break
    save_config(config)

def get_enabled_channels() -> List[dict]:
    """Get list of enabled channels"""
    config = load_config()
    return [ch for ch in config["channels"] if ch.get("enabled", True)]

# ============================================================
# FORWARDING ENGINE
# ============================================================

async def forward_to_saved_messages(client: TelegramClient, message: Message, file_path: str):
    """Forward file to Saved Messages"""
    try:
        await client.send_file('me', file_path, caption=f"Forwarded from: {message.chat.title}\nOriginal message: {message.id}")
        logger.info(f"Forwarded to Saved Messages: {message.file.name if message.file else 'file'}")
        return True
    except Exception as e:
        logger.error(f"Failed to forward to Saved Messages: {e}")
        return False

async def forward_to_target_bot(file_path: str, file_name: str, source_chat: str):
    """Forward file to target bot using bot token"""
    from telethon import TelegramClient as BotClient
    
    bot_client = BotClient('target_bot', api_id=0, api_hash='')  # Bot client uses token only
    try:
        await bot_client.start(bot_token=TARGET_BOT_TOKEN)
        await bot_client.send_file(
            int(TARGET_CHAT_ID),
            file_path,
            caption=f"📁 {file_name}\n📡 Source: {source_chat}\n⏰ Forwarded: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        logger.info(f"Forwarded to target bot: {file_name}")
        await bot_client.disconnect()
        return True
    except Exception as e:
        logger.error(f"Failed to forward to target bot: {e}")
        return False
    finally:
        await bot_client.disconnect()

async def process_new_file(client: TelegramClient, message: Message):
    """Process and forward new .txt file"""
    try:
        # Check if it's a document
        if not message.document:
            return
        
        # Check if it's a .txt file
        file_name = None
        if message.document.attributes:
            for attr in message.document.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    file_name = attr.file_name
                    break
        
        if not file_name or not file_name.endswith('.txt'):
            return
        
        # Get unique file ID
        file_id = str(message.document.id)
        
        # Check if already forwarded
        if is_file_forwarded(file_id):
            logger.info(f"Skipping already forwarded file: {file_name}")
            return
        
        logger.info(f"New .txt file detected: {file_name} from {message.chat.title}")
        
        # Download file
        file_path = f"/tmp/{file_name}"
        await client.download_file(message.media, file_path)
        
        # Forward to Saved Messages
        config = load_config()
        if config["settings"].get("forward_to_saved", True):
            await forward_to_saved_messages(client, message, file_path)
        
        # Forward to target bot
        if config["settings"].get("forward_to_bot", True):
            await forward_to_target_bot(file_path, file_name, message.chat.title)
        
        # Mark as forwarded
        mark_file_forwarded(file_id, message.id, message.chat.id, file_name)
        
        # Clean up temp file
        try:
            os.remove(file_path)
        except:
            pass
        
        logger.info(f"Successfully processed: {file_name}")
        
    except Exception as e:
        logger.error(f"Error processing file: {e}")
        traceback.print_exc()

async def scan_channel(client: TelegramClient, channel_info: dict):
    """Scan a channel for new .txt files"""
    channel_id = channel_info["id"]
    channel_name = channel_info["name"]
    
    try:
        # Get the channel entity
        channel = await client.get_entity(channel_id)
        
        # Get last scanned message ID
        last_msg_id = get_last_scan(channel_id)
        
        # Get messages
        messages = await client.get_messages(channel, limit=50)
        
        # Process in reverse order (oldest first)
        new_messages = []
        for msg in messages:
            if last_msg_id is None or msg.id > last_msg_id:
                new_messages.append(msg)
        
        # Update last scan ID
        if messages:
            latest_id = max([m.id for m in messages])
            update_last_scan(channel_id, latest_id)
        
        # Process new messages (from oldest to newest)
        for msg in reversed(new_messages):
            await process_new_file(client, msg)
            
    except Exception as e:
        logger.error(f"Error scanning channel {channel_name}: {e}")

async def continuous_scanner(client: TelegramClient):
    """Continuous scanner that runs every 5 minutes"""
    logger.info("Starting continuous scanner...")
    
    while True:
        try:
            config = load_config()
            enabled_channels = get_enabled_channels()
            
            if not enabled_channels:
                logger.info("No enabled channels to scan")
            else:
                logger.info(f"Scanning {len(enabled_channels)} channels...")
                
                for channel_info in enabled_channels:
                    await scan_channel(client, channel_info)
                
                logger.info("Scan cycle completed")
            
            # Wait for next scan
            await asyncio.sleep(SCAN_INTERVAL)
            
        except Exception as e:
            logger.error(f"Scanner error: {e}")
            await asyncio.sleep(60)

# ============================================================
# TELEGRAM BOT COMMANDS (Using python-telegram-bot)
# ============================================================

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

command_app = None
user_client = None

async def bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    msg = (
        "🤖 **Telegram TXT File Forwarder**\n\n"
        "This bot monitors channels and forwards .txt files to Saved Messages and target bot.\n\n"
        "**Commands:**\n"
        "/start - Show this message\n"
        "/channels - List and manage channels\n"
        "/add - Add current channel (reply to any message in target channel)\n"
        "/settings - Configure forwarding settings\n"
        "/status - Show current status\n"
        "/scan - Manually trigger scan\n\n"
        "⚠️ **Setup Required:**\n"
        "First, log in with your Telegram account using the login command."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def bot_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List and manage channels"""
    config = load_config()
    
    if not config["channels"]:
        msg = "📭 No channels added.\n\nTo add a channel:\n1. Go to the channel\n2. Reply to any message with /add"
        await update.message.reply_text(msg)
        return
    
    keyboard = []
    for ch in config["channels"]:
        status = "✅ Active" if ch.get("enabled", True) else "⏸️ Disabled"
        keyboard.append([
            InlineKeyboardButton(
                f"{ch['name'][:30]} - {status}",
                callback_data=f"channel_{ch['id']}"
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📋 **Your Channels:**\n\nTap a channel to manage it.", reply_markup=reply_markup, parse_mode='Markdown')

async def bot_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add current channel (reply to message in channel)"""
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Please reply to any message in the channel you want to add.")
        return
    
    chat = update.message.reply_to_message.chat
    if chat.type not in ['channel', 'supergroup']:
        await update.message.reply_text("❌ This is not a channel.")
        return
    
    channel_id = chat.id
    channel_name = chat.title
    
    if add_channel(channel_id, channel_name):
        await update.message.reply_text(f"✅ Added channel: {channel_name}\n\nUse /channels to manage it.")
    else:
        await update.message.reply_text(f"⚠️ Channel already in list: {channel_name}")

async def bot_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configure forwarding settings"""
    config = load_config()
    settings = config.get("settings", {})
    
    msg = (
        "⚙️ **Forwarding Settings**\n\n"
        f"📤 Forward to Saved Messages: {'✅ ON' if settings.get('forward_to_saved', True) else '❌ OFF'}\n"
        f"🤖 Forward to Target Bot: {'✅ ON' if settings.get('forward_to_bot', True) else '❌ OFF'}\n"
        f"📄 Only .txt files: {'✅ ON' if settings.get('only_txt', True) else '❌ OFF'}\n\n"
        "Use buttons below to toggle settings:"
    )
    
    keyboard = [
        [
            InlineKeyboardButton("Toggle Saved Messages", callback_data="toggle_saved"),
            InlineKeyboardButton("Toggle Bot Forward", callback_data="toggle_bot")
        ],
        [InlineKeyboardButton("Toggle .txt Only", callback_data="toggle_txt")],
        [InlineKeyboardButton("Close", callback_data="close")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(msg, reply_markup=reply_markup, parse_mode='Markdown')

async def bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current status"""
    config = load_config()
    enabled_channels = get_enabled_channels()
    
    # Count forwarded files
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM forwarded_files")
    total_forwarded = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM forwarded_files WHERE forwarded_at > datetime('now', '-24 hours')")
    today_forwarded = c.fetchone()[0]
    conn.close()
    
    msg = (
        "📊 **System Status**\n\n"
        f"📡 **Channels:** {len(config['channels'])} total, {len(enabled_channels)} active\n"
        f"📁 **Files Forwarded:** {total_forwarded} total, {today_forwarded} today\n"
        f"⏱️ **Scan Interval:** {SCAN_INTERVAL // 60} minutes\n"
        f"🎯 **Target Bot:** {'Connected' if TARGET_BOT_TOKEN else 'Not set'}\n\n"
        "✅ System is running normally."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def bot_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger scan"""
    await update.message.reply_text("🔄 Manual scan triggered...")
    # Trigger scan in background
    asyncio.create_task(manual_scan())

async def manual_scan():
    """Manual scan function"""
    global user_client
    if user_client and user_client.is_connected():
        config = load_config()
        enabled_channels = get_enabled_channels()
        for channel_info in enabled_channels:
            await scan_channel(user_client, channel_info)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("channel_"):
        channel_id = int(data.split("_")[1])
        config = load_config()
        channel = None
        for ch in config["channels"]:
            if ch["id"] == channel_id:
                channel = ch
                break
        
        if channel:
            status = "✅ Active" if channel.get("enabled", True) else "⏸️ Disabled"
            keyboard = [
                [InlineKeyboardButton("✅ Enable" if not channel.get("enabled", True) else "⏸️ Disable", callback_data=f"toggle_{channel_id}")],
                [InlineKeyboardButton("🗑️ Remove", callback_data=f"remove_{channel_id}")],
                [InlineKeyboardButton("◀️ Back", callback_data="back_to_channels")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"📌 **{channel['name']}**\n\nStatus: {status}\nAdded: {channel.get('added_at', 'Unknown')}",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
    
    elif data.startswith("toggle_"):
        channel_id = int(data.split("_")[1])
        config = load_config()
        for ch in config["channels"]:
            if ch["id"] == channel_id:
                ch["enabled"] = not ch.get("enabled", True)
                save_config(config)
                break
        
        # Go back to channel list
        await bot_channels(update, context)
    
    elif data.startswith("remove_"):
        channel_id = int(data.split("_")[1])
        remove_channel(channel_id)
        await bot_channels(update, context)
    
    elif data == "back_to_channels":
        await bot_channels(update, context)
    
    elif data == "toggle_saved":
        config = load_config()
        config["settings"]["forward_to_saved"] = not config["settings"].get("forward_to_saved", True)
        save_config(config)
        await bot_settings(update, context)
    
    elif data == "toggle_bot":
        config = load_config()
        config["settings"]["forward_to_bot"] = not config["settings"].get("forward_to_bot", True)
        save_config(config)
        await bot_settings(update, context)
    
    elif data == "toggle_txt":
        config = load_config()
        config["settings"]["only_txt"] = not config["settings"].get("only_txt", True)
        save_config(config)
        await bot_settings(update, context)
    
    elif data == "close":
        await query.delete_message()

# ============================================================
# USER LOGIN SYSTEM (Telethon)
# ============================================================

async def login_user():
    """Interactive login with Telegram account"""
    global user_client
    
    print("\n" + "="*50)
    print("🔐 TELEGRAM ACCOUNT LOGIN")
    print("="*50)
    
    # Get API credentials
    print("\n⚠️ You need Telegram API credentials:")
    print("1. Go to https://my.telegram.org/apps")
    print("2. Create an app if you haven't")
    print("3. Enter your api_id and api_hash below\n")
    
    api_id = input("Enter api_id: ").strip()
    api_hash = input("Enter api_hash: ").strip()
    
    if not api_id or not api_hash:
        print("❌ API credentials required!")
        return False
    
    # Create client
    user_client = TelegramClient(SESSION_FILE, int(api_id), api_hash)
    
    try:
        await user_client.start()
        
        if not await user_client.is_user_authorized():
            print("❌ Login failed")
            return False
        
        me = await user_client.get_me()
        print(f"\n✅ Logged in as: {me.first_name} (@{me.username})")
        print(f"📱 Phone: {me.phone}")
        
        # Test connection
        dialogs = await user_client.get_dialogs()
        print(f"📊 Found {len(dialogs)} dialogs")
        
        return True
        
    except Exception as e:
        print(f"❌ Login error: {e}")
        return False

# ============================================================
# MAIN APPLICATION
# ============================================================

async def main():
    global command_app, user_client
    
    # Initialize database
    init_db()
    
    # Login to Telegram account
    print("\n🔐 Telegram Account Login Required")
    print("This bot needs your Telegram account to monitor channels.")
    print("Your session will be saved, so you only need to log in once.\n")
    
    if os.path.exists(SESSION_FILE):
        print("✅ Existing session found!")
        response = input("Use existing session? (y/n): ").strip().lower()
        if response != 'y':
            os.remove(SESSION_FILE)
            print("Session removed. Starting fresh login...")
    
    # Login
    login_success = await login_user()
    if not login_success:
        print("❌ Login failed. Exiting...")
        return
    
    # Start scanner in background
    asyncio.create_task(continuous_scanner(user_client))
    
    # Start command bot
    command_app = Application.builder().token(COMMAND_BOT_TOKEN).build()
    
    command_app.add_handler(CommandHandler("start", bot_start))
    command_app.add_handler(CommandHandler("channels", bot_channels))
    command_app.add_handler(CommandHandler("add", bot_add))
    command_app.add_handler(CommandHandler("settings", bot_settings))
    command_app.add_handler(CommandHandler("status", bot_status))
    command_app.add_handler(CommandHandler("scan", bot_scan))
    command_app.add_handler(CallbackQueryHandler(button_callback))
    
    print("\n" + "="*50)
    print("🤖 TELEGRAM TXT FILE FORWARDER")
    print("="*50)
    print(f"✅ Account logged in")
    print(f"📡 Command Bot: @{COMMAND_BOT_TOKEN.split(':')[0]}")
    print(f"🎯 Target Bot ID: {TARGET_BOT_TOKEN.split(':')[0]}")
    print(f"📁 Target Chat: {TARGET_CHAT_ID}")
    print(f"⏱️ Scan Interval: {SCAN_INTERVAL // 60} minutes")
    print("="*50)
    print("Bot is running! Use /start in your command bot to manage channels.")
    print("Press Ctrl+C to stop.\n")
    
    # Run both clients
    await command_app.initialize()
    await command_app.start()
    
    try:
        await asyncio.gather(
            command_app.updater.start_polling(),
            asyncio.Event().wait()  # Keep running
        )
    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
    finally:
        await command_app.stop()
        if user_client:
            await user_client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())