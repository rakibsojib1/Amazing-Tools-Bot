#!/usr/bin/env python3
"""Amazing Tools Bot — Video downloader, sticker creator & more."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path

import io
import urllib.request

from PIL import Image
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputSticker,
    LabeledPrice,
    Message,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# Mini App web server (no extra deps)
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from functools import partial

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable is missing.\n"
        "Set it in Render dashboard (Environment tab) or export BOT_TOKEN=... locally.\n"
        "Get your token from @BotFather on Telegram."
    )
OWNER_ID = 666053962
DB_PATH = "data.db"
WHITELIST_PATH = "whitelist.json"

DOWNLOADS_DIR = Path("/tmp/amazing_downloads")
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

DAILY_FREE_DEFAULT_LIMIT = 5

# Pricing in Telegram Stars (XTR)
PRICE_DOWNLOAD = 5    # Stars per video download
PRICE_STICKER = 10    # Stars per sticker pack creation

# Mini App public URL (set this in Render env for the "Open Mini App" button to work)
# Example: https://amazing-tools-bot.onrender.com
MINIAPP_URL = os.environ.get("MINIAPP_URL", "").rstrip("/")
if not MINIAPP_URL:
    MINIAPP_URL = None  # Will show helpful message in /start

# ── Mini App + Main Menu Keyboard ───────────────────────────────

def get_main_menu() -> InlineKeyboardMarkup:
    """Returns a nice inline keyboard with the Mini App + quick actions."""
    buttons = []
    if MINIAPP_URL:
        buttons.append([
            InlineKeyboardButton("✨ Open Beautiful Mini App", web_app=WebAppInfo(url=MINIAPP_URL))
        ])
    buttons.extend([
        [
            InlineKeyboardButton("📥 Download Video", callback_data="cmd_download"),
            InlineKeyboardButton("🎨 Make Stickers", callback_data="cmd_sticker"),
        ],
        [
            InlineKeyboardButton("🔗 Shorten URL", callback_data="cmd_short"),
            InlineKeyboardButton("📊 My Balance", callback_data="cmd_balance"),
        ],
        [
            InlineKeyboardButton("🛠️ All Tools", callback_data="cmd_tools"),
        ],
        [InlineKeyboardButton("💬 Send a video link or photo directly", callback_data="cmd_help")]
    ])
    return InlineKeyboardMarkup(buttons)


# ── Logging ─────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Helpers ─────────────────────────────────────────

def get_usage_db() -> sqlite3.Connection:
    """Return a thread-safe(ish) SQLite connection (one per coroutine)."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS usage (
            user_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            downloads INTEGER DEFAULT 0,
            stickers INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS short_urls (
            short_code TEXT PRIMARY KEY,
            original_url TEXT NOT NULL,
            user_id INTEGER,
            created_at TEXT,
            clicks INTEGER DEFAULT 0
        )"""
    )
    return db


def load_whitelist() -> dict:
    """Return {user_id: remaining_free_uses} (0=only paid, -1=unlimited)."""
    try:
        return json.loads(Path(WHITELIST_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_whitelist(data: dict) -> None:
    Path(WHITELIST_PATH).write_text(json.dumps(data, indent=2))


def is_whitelisted(user_id: int) -> bool:
    wl = load_whitelist()
    return str(user_id) in wl


def get_whitelist_remaining(user_id: int) -> int:
    """-1 unlimited, 0 exhausted-or-not-whitelisted, >0 remaining uses."""
    wl = load_whitelist()
    return wl.get(str(user_id), 0)


def daily_free_enabled() -> bool:
    db = get_usage_db()
    row = db.execute("SELECT value FROM config WHERE key='daily_free_enabled'").fetchone()
    return row is not None and row["value"] == "1"


def daily_free_limit() -> int:
    db = get_usage_db()
    row = db.execute("SELECT value FROM config WHERE key='daily_free_limit'").fetchone()
    return int(row["value"]) if row else DAILY_FREE_DEFAULT_LIMIT


def get_today_usage(user_id: int) -> dict:
    db = get_usage_db()
    today = date.today().isoformat()
    row = db.execute(
        "SELECT downloads, stickers FROM usage WHERE user_id=? AND date=?",
        (user_id, today),
    ).fetchone()
    if row:
        return {"downloads": row["downloads"], "stickers": row["stickers"]}
    return {"downloads": 0, "stickers": 0}


def increment_usage(user_id: int, kind: str = "downloads") -> None:
    db = get_usage_db()
    today = date.today().isoformat()
    db.execute(
        f"""INSERT INTO usage (user_id, date, downloads, stickers)
            VALUES (?, ?, 0, 0)
            ON CONFLICT(user_id,date) DO UPDATE SET
                {kind} = {kind} + 1""",
        (user_id, today),
    )
    db.commit()


def deduct_whitelist_use(user_id: int) -> None:
    wl = load_whitelist()
    key = str(user_id)
    if key in wl:
        if wl[key] > 0:
            wl[key] -= 1
            save_whitelist(wl)


# ── Check if user can use a tool for free ──────────────────────────────────

async def can_use_free(user_id: int, kind: str = "downloads") -> tuple[bool, str]:
    """Returns (allowed, reason_if_denied)."""
    if user_id == OWNER_ID:
        return True, ""
    remaining = get_whitelist_remaining(user_id)
    if remaining == -1:
        return True, ""  # unlimited whitelist
    if remaining > 0:
        return True, ""  # has pre-paid whitelist uses
    # Check daily free tier
    if daily_free_enabled():
        usage = get_today_usage(user_id)
        limit = daily_free_limit()
        used = usage.get(kind, 0)
        if used < limit:
            return True, ""
        return False, f"Daily free limit ({limit}) reached. Use /balance or pay via in-chat payment."
    return False, "Free usage not available. Pay via in-chat payment."


# ── URL Shortener (free self-hosted) ────────────────────────────────────

def generate_short_code(length: int = 6) -> str:
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


async def shorten_url(user_id: int, original_url: str, custom_code: str | None = None) -> str:
    """Returns the short_code. Uses custom_code if provided and available."""
    db = get_usage_db()

    if custom_code:
        existing = db.execute(
            "SELECT 1 FROM short_urls WHERE short_code = ?", (custom_code,)
        ).fetchone()
        if existing:
            raise ValueError("Custom short code already taken")
        code = custom_code
    else:
        # Generate unique code
        for _ in range(5):
            code = generate_short_code()
            existing = db.execute(
                "SELECT 1 FROM short_urls WHERE short_code = ?", (code,)
            ).fetchone()
            if not existing:
                break
        else:
            raise ValueError("Could not generate unique short code")

    db.execute(
        """INSERT INTO short_urls (short_code, original_url, user_id, created_at, clicks)
           VALUES (?, ?, ?, ?, 0)""",
        (code, original_url, user_id, datetime.now().isoformat()),
    )
    db.commit()
    return code


def get_original_url(short_code: str) -> str | None:
    db = get_usage_db()
    row = db.execute(
        "SELECT original_url FROM short_urls WHERE short_code = ?",
        (short_code,),
    ).fetchone()
    return row["original_url"] if row else None


def increment_short_click(short_code: str) -> None:
    db = get_usage_db()
    db.execute(
        "UPDATE short_urls SET clicks = clicks + 1 WHERE short_code = ?",
        (short_code,),
    )
    db.commit()


def get_short_stats(short_code: str) -> dict | None:
    db = get_usage_db()
    row = db.execute(
        "SELECT original_url, clicks, created_at FROM short_urls WHERE short_code = ?",
        (short_code,),
    ).fetchone()
    if row:
        return {
            "original_url": row["original_url"],
            "clicks": row["clicks"],
            "created_at": row["created_at"],
        }
    return None


# ── Payment helpers (Telegram Stars) ───────────────────────────────────

async def send_star_invoice(
    update: Update, context: ContextTypes.DEFAULT_TYPE, title: str, description: str, price: int, payload: str
) -> None:
    """Send an XTR invoice."""
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=description,
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=price)],
        provider_token="",  # XTR uses empty provider_token
    )


async def pre_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    # Always accept valid XTR payments
    await query.answer(ok=True)


# ── Commands ─────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = (
        f"👋 <b>Welcome to Amazing Tools, {user.first_name}!</b>\n\n"
        "I can help you download videos from TikTok, YouTube, Instagram, and more — "
        "or create cool sticker packs from your photos!\n\n"
        "<b>How it works:</b>\n"
        "• Send me a <b>video link</b> → I'll download it (requires payment)\n"
        "• Send me a <b>photo</b> → I'll turn it into stickers\n"
        "• Use /tools to see everything I can do\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "👤 Created by <b>Rakib Sojib</b>\n"
        "📞 Contact: <b>@roki1277</b>\n"
        "🤖 Made with AI\n"
        "━━━━━━━━━━━━━━━━"
    )

    # Show nice buttons including the beautiful Mini App
    reply_markup = get_main_menu()

    if not MINIAPP_URL:
        await update.message.reply_text(
            "💡 <b>Mini App available!</b>\n"
            "Set <code>MINIAPP_URL</code> in Render to your public URL "
            "(https://your-service.onrender.com) to enable the big ✨ button.",
            parse_mode=ParseMode.HTML,
        )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "<b>📖 Help & Commands</b>\n\n"
        "/start — Welcome & intro\n"
        "/tools — List all available tools\n"
        "/download — Download a video (send a link!)\n"
        "/sticker — Create stickers from photos\n"
        "/balance — Check your usage & balance\n"
        "/contact — Contact the creator\n"
        "/help — Show this message\n\n"
        "<b>💡 Tips:</b>\n"
        "• Just send any video link from TikTok/YouTube/Instagram\n"
        "• Just send any photo to turn it into stickers\n"
        "• The bot auto-detects what you send!\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "👤 Created by <b>Rakib Sojib</b>\n"
        "📞 Contact: <b>@roki1277</b>\n"
        "🤖 Made with AI\n"
        "━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu())


async def tools_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "<b>🔧 Available Tools</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📥 <b>Video Downloader</b>\n"
        "Download videos from:\n"
        "• TikTok (no watermark)\n"
        "• YouTube / YouTube Shorts\n"
        "• Instagram Reels / Posts\n"
        "• Facebook\n"
        "• Twitter / X\n"
        "• And many more!\n"
        "💰 <b>Cost:</b> ⭐ 5 Stars per download\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎨 <b>Sticker Creator</b>\n"
        "Turn your photos into Telegram sticker packs!\n"
        "• Auto-resize & optimize\n"
        "• Create custom sticker sets\n"
        "💰 <b>Cost:</b> ⭐ 10 Stars per pack\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>💳 Payment:</b> Pay with Telegram Stars (XTR)\n"
        "• Just click the payment button when prompted\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 Created by <b>Rakib Sojib</b>\n"
        "📞 Contact: <b>@roki1277</b>\n"
        "🤖 Made with AI\n"
        "━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu())


async def contact_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "<b>📞 Contact Information</b>\n\n"
        "👤 <b>Creator:</b> Rakib Sojib\n"
        "📱 <b>Telegram:</b> @roki1277\n\n"
        "For support, feature requests, or inquiries, "
        "feel free to reach out via Telegram!\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "🤖 Made with AI\n"
        "━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu())


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main button menu."""
    await update.message.reply_text(
        "👇 Use the buttons below or just send a link/photo:",
        reply_markup=get_main_menu()
    )


async def menu_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle clicks on the nice inline menu buttons."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    user = update.effective_user

    if data == "cmd_download":
        await query.message.reply_text(
            "📥 Send me any video link (TikTok, YouTube, Instagram, etc.) and I'll download it!",
            reply_markup=get_main_menu()
        )
    elif data == "cmd_sticker":
        await query.message.reply_text(
            "🎨 Send me a photo (or multiple) and I'll turn it into a sticker pack!",
            reply_markup=get_main_menu()
        )
    elif data == "cmd_short":
        await query.message.reply_text(
            "🔗 Use <code>/short https://long-url.com</code> or open the Mini App to shorten URLs.\n\n"
            "You can also use custom code: /short url mycode",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu()
        )
    elif data == "cmd_balance":
        await balance_cmd(update, context)
    elif data == "cmd_tools":
        await tools_cmd(update, context)
    else:
        await query.message.reply_text(
            "💡 Just send a video link or a photo directly in this chat — I auto-detect it!\n"
            "Or tap ✨ Open Mini App for the beautiful UI.",
            reply_markup=get_main_menu()
        )


async def balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    usage = get_today_usage(user.id)
    remaining = get_whitelist_remaining(user.id)
    daily_lim = daily_free_limit()
    free_on = daily_free_enabled()

    lines = [f"<b>📊 Your Usage Today</b>\n"]
    lines.append(f"📥 Downloads used: {usage['downloads']}")
    lines.append(f"🎨 Stickers used:   {usage['stickers']}")

    if user.id == OWNER_ID:
        lines.append("\n✅ <b>Owner</b> — unlimited access")
    elif remaining == -1:
        lines.append("\n✅ <b>Whitelisted</b> — unlimited free access")
    elif remaining > 0:
        lines.append(f"\n🎫 <b>Remaining prepaid uses:</b> {remaining}")
    elif free_on:
        dl_left = max(0, daily_lim - usage["downloads"])
        st_left = max(0, daily_lim - usage["stickers"])
        lines.append(f"\n🎁 <b>Daily free limit:</b> {daily_lim} uses/tool")
        lines.append(f"📥 Downloads left: {dl_left}")
        lines.append(f"🎨 Stickers left:   {st_left}")
    else:
        lines.append("\n💳 Paid-only mode — use in-chat payments.")

    lines.append("\n━━━━━━━━━━━━━━━━\n👤 Created by <b>Rakib Sojib</b>\n📞 Contact: <b>@roki1277</b>\n🤖 Made with AI\n━━━━━━━━━━━━━━━━")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=get_main_menu())


async def download_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt user to send a video link, or process a link from args."""
    args = context.args
    if args:
        url = args[0]
        await handle_video_link(update, context, url)
    else:
        await update.message.reply_text(
            "📥 <b>Send me a video link</b> (TikTok, YouTube, Instagram, Facebook, etc.) "
            "and I'll download it for you!",
            parse_mode=ParseMode.HTML,
        )


async def sticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎨 <b>Send me a photo</b> (or multiple photos) and I'll turn them into "
        "a Telegram sticker pack for you!",
        parse_mode=ParseMode.HTML,
    )


# ── URL Shortener command ───────────────────────────────────────

async def short_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /short <url> [custom_code]  or /shorten """
    args = context.args
    if not args:
        await update.message.reply_text(
            "🔗 <b>URL Shortener</b>\n\n"
            "Usage:\n"
            "/short https://very-long-url.com/example\n"
            "/short https://example.com myalias   (custom short code)\n\n"
            "Short links will be: your-bot-url.onrender.com/s/xxxx",
            parse_mode=ParseMode.HTML,
        )
        return

    long_url = args[0]
    custom_code = args[1] if len(args) > 1 else None

    if not long_url.startswith(("http://", "https://")):
        long_url = "https://" + long_url

    try:
        code = await shorten_url(update.effective_user.id, long_url, custom_code)
        base = MINIAPP_URL or "https://your-bot.onrender.com"
        short_link = f"{base}/s/{code}"
        msg = (
            f"✅ <b>URL Shortened!</b>\n\n"
            f"Original: <code>{long_url}</code>\n\n"
            f"Short: {short_link}\n\n"
            f"Share this link anywhere. Clicks are tracked."
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except ValueError as ve:
        await update.message.reply_text(f"❌ {ve}")
    except Exception as e:
        logger.error(f"Shorten error: {e}")
        await update.message.reply_text("❌ Failed to shorten URL. Try again.")


# ── Admin commands ───────────────────────────────────────

async def admin_dailyfree(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Owner-only command.")
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/dailyfree on — Enable daily free tier\n"
            "/dailyfree off — Disable daily free tier\n"
            "/dailyfree limit N — Set daily usage limit\n"
            "/dailyfree tools all — All tools get free tier\n"
            "/dailyfree tools specific — Only specified tools\n"
            "/dailyfree status — Current config"
        )
        return
    db = get_usage_db()
    cmd = args[0].lower()
    if cmd == "on":
        db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('daily_free_enabled', '1')")
        db.commit()
        await update.message.reply_text("✅ Daily free tier enabled.")
    elif cmd == "off":
        db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('daily_free_enabled', '0')")
        db.commit()
        await update.message.reply_text("✅ Daily free tier disabled.")
    elif cmd == "limit" and len(args) >= 2:
        try:
            n = int(args[1])
            db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('daily_free_limit', ?)", (str(n),))
            db.commit()
            await update.message.reply_text(f"✅ Daily free limit set to {n}.")
        except ValueError:
            await update.message.reply_text("❌ Provide a valid number.")
    elif cmd == "tools":
        if len(args) >= 2 and args[1] in ("all", "specific"):
            db.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('daily_free_tools', ?)", (args[1],))
            db.commit()
            await update.message.reply_text(f"✅ Daily free tools set to: {args[1]}")
        else:
            await update.message.reply_text("Usage: /dailyfree tools all|specific")
    elif cmd == "status":
        enabled = daily_free_enabled()
        limit = daily_free_limit()
        tools = db.execute("SELECT value FROM config WHERE key='daily_free_tools'").fetchone()
        tools_val = tools["value"] if tools else "all"
        await update.message.reply_text(
            f"📊 <b>Daily Free Status</b>\n"
            f"Enabled: {'✅' if enabled else '❌'}\n"
            f"Limit: {limit} uses/tool/day\n"
            f"Tools: {tools_val}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("❌ Unknown subcommand.")


async def admin_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Owner-only command.")
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/whitelist add ID count — Add user (-1 for unlimited)\n"
            "/whitelist remove ID — Remove user\n"
            "/whitelist list — List all whitelisted users"
        )
        return
    sub = args[0].lower()
    wl = load_whitelist()
    if sub == "add" and len(args) >= 3:
        try:
            uid = str(int(args[1]))
            count = int(args[2])
            wl[uid] = count
            save_whitelist(wl)
            label = "unlimited" if count == -1 else f"{count} uses"
            await update.message.reply_text(f"✅ User {uid} whitelisted ({label}).")
        except ValueError:
            await update.message.reply_text("❌ Invalid ID or count.")
    elif sub == "remove" and len(args) >= 2:
        uid = str(int(args[1]))
        if uid in wl:
            del wl[uid]
            save_whitelist(wl)
            await update.message.reply_text(f"✅ User {uid} removed from whitelist.")
        else:
            await update.message.reply_text(f"❌ User {uid} not in whitelist.")
    elif sub == "list":
        if not wl:
            await update.message.reply_text("📋 Whitelist is empty.")
            return
        lines = ["<b>📋 Whitelisted Users</b>\n"]
        for uid, cnt in wl.items():
            label = "♾️ Unlimited" if cnt == -1 else f"🎫 {cnt} remaining"
            lines.append(f"• <code>{uid}</code> — {label}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Unknown subcommand.")


async def admin_addfree(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shortcut: /addfree ID count"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Owner-only command.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /addfree ID count")
        return
    try:
        uid = str(int(args[0]))
        count = int(args[1])
        wl = load_whitelist()
        wl[uid] = count
        save_whitelist(wl)
        label = "unlimited" if count == -1 else f"{count} free uses"
        await update.message.reply_text(f"✅ User {uid} granted {label}.")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID or count.")


# ── Message / auto-detect handler ─────────────────────────────────────

VIDEO_LINK_PATTERN = re.compile(
    r"https?://\S+",
    re.IGNORECASE,
)


async def auto_detect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages with video links or photos."""
    user = update.effective_user
    if not user:
        return
    msg: Message = update.message
    if not msg:
        return

    # ── Photo → sticker flow ───────────────────────────
    if msg.photo:
        await handle_photo_for_sticker(update, context, msg)
        return

    # ── Video link → download flow ───────────────────────────
    if msg.text and VIDEO_LINK_PATTERN.search(msg.text):
        url = VIDEO_LINK_PATTERN.search(msg.text).group(0)
        await handle_video_link(update, context, url)
        return


# ── Video download logic ───────────────────────────────────────

async def handle_video_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Acknowledge the request (especially important for Mini App)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📥 Got download request for:\n<code>{url}</code>",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

    allowed, reason = await can_use_free(user.id, "downloads")
    if not allowed:
        # Ask for payment before proceeding
        await send_star_invoice(
            update, context,
            title="Video Download",
            description=f"Download video from the link you sent.\n\n{reason}",
            price=PRICE_DOWNLOAD,
            payload=f"download:{user.id}:{url}",
        )
        return

    # User has free usage — proceed
    await perform_download(update, context, url, user.id)


async def get_best_direct_url(source_url: str) -> str | None:
    """Try to get a direct video URL so Telegram can fetch it directly (fastest method)."""
    def _extract():
        import yt_dlp
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "format": "bv*+ba/b",
            "noplaylist": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
            "nocheckcertificate": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(source_url, download=False)
                # Some extractors give a direct 'url'
                if info and info.get("url"):
                    return info["url"]
                # Pick the best format with a direct url
                formats = info.get("formats", []) if info else []
                for fmt in sorted(formats, key=lambda x: (x.get("height") or 0, x.get("filesize") or 0), reverse=True):
                    if fmt.get("url") and fmt.get("vcodec") != "none":
                        return fmt["url"]
                return None
        except Exception:
            return None

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract)


async def perform_download(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, user_id: int
) -> None:
    chat_id = update.effective_chat.id
    # Use effective_message or send directly (safer when triggered from Mini App web_app_data)
    reply_target = update.message or update.effective_message
    if reply_target:
        status_msg = await reply_target.reply_text("🔍 Finding best quality format...")
    else:
        status_msg = await context.bot.send_message(chat_id=chat_id, text="🔍 Finding best quality format...")

    # === Method 1: Direct URL (Best & fastest - Telegram downloads for us) ===
    direct_url = await get_best_direct_url(url)
    if direct_url:
        try:
            await status_msg.edit_text("🚀 Sending best quality (Telegram is fetching it)...")
            await context.bot.send_video(
                chat_id=chat_id,
                video=direct_url,
                caption="📥 Best quality via Amazing Tools Bot",
                write_timeout=180,
            )
            await status_msg.delete()
            deduct_whitelist_use(user_id)
            increment_usage(user_id, "downloads")
            return
        except Exception as e:
            logger.info(f"Direct URL method failed for {url}, falling back to server download: {e}")

    # === Method 2: Full download on server with live progress (fallback) ===
    await status_msg.edit_text("⏳ Downloading best quality... 0%")

    loop = asyncio.get_running_loop()
    progress = {"text": "⏳ Downloading best quality... 0%"}

    def progress_hook(d):
        if d["status"] == "downloading":
            percent = d.get("_percent_str", "0%").strip()
            speed = d.get("_speed_str", "")
            total = d.get("_total_bytes_str", d.get("_total_bytes_estimate_str", ""))
            new_text = f"⏳ Downloading best quality... {percent}"
            if speed:
                new_text += f" ({speed})"
            if total:
                new_text += f" / {total}"

            # Only update if changed significantly (throttle)
            if new_text != progress["text"]:
                progress["text"] = new_text
                try:
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(new_text), loop
                    )
                except Exception:
                    pass
        elif d.get("status") in ("finished", "processing"):
            # This covers the merging/post-processing phase after 100% download
            try:
                asyncio.run_coroutine_threadsafe(
                    status_msg.edit_text("✅ Download finished. Processing (merging if needed)..."),
                    loop
                )
            except Exception:
                pass

    def _download() -> str | None:
        """Server-side download with progress hook."""
        import yt_dlp

        out_tmpl = str(DOWNLOADS_DIR / "%(id)s.%(ext)s")
        ydl_opts = {
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "retries": 5,
            "fragment_retries": 5,
            "progress_hooks": [progress_hook],
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
            "nocheckcertificate": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return None

                # Try to get the final filename from yt-dlp (important after merge)
                try:
                    filename = ydl.prepare_filename(info)
                    if filename and Path(filename).exists():
                        return filename
                except Exception:
                    pass

                # Find the downloaded file
                if "requested_downloads" in info and info["requested_downloads"]:
                    path = info["requested_downloads"][0].get("filepath")
                    if path and Path(path).exists():
                        return path

                video_id = info.get("id") or info.get("webpage_url_basename", "video")
                for f in sorted(DOWNLOADS_DIR.glob(f"{video_id}.*"), key=lambda p: p.stat().st_size, reverse=True):
                    return str(f)

                # fallback to largest recent file
                candidates = sorted(
                    [f for f in DOWNLOADS_DIR.glob("*") if f.is_file()],
                    key=lambda p: p.stat().st_size, reverse=True
                )
                return str(candidates[0]) if candidates else None
        except Exception as exc:
            logger.error(f"yt-dlp download error for {url}: {exc}")
            return None

    filepath = await loop.run_in_executor(None, _download)

    if not filepath or not Path(filepath).exists():
        await status_msg.edit_text(
            "❌ Failed to download video.\n\n"
            "• Site blocks downloaders / needs login\n"
            "• Private, geo-restricted or live video\n"
            "• No downloadable format found\n\n"
            "Try another link (YouTube / TikTok / Instagram usually work great)."
        )
        return

    file_size = Path(filepath).stat().st_size
    size_mb = file_size / (1024 * 1024)

    # After 100% download, show clear next step (merging can happen after 100%)
    await status_msg.edit_text(
        f"✅ Download complete ({size_mb:.1f} MB).\n"
        f"📤 Uploading to you now... (large videos can take 1-5+ minutes)"
    )

    try:
        with open(filepath, "rb") as f:
            # For large files always use document (more reliable)
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                caption=f"📥 Best quality ({size_mb:.1f} MB)\nvia Amazing Tools Bot",
                write_timeout=300,   # longer for big uploads,
                read_timeout=300,
            )
        await status_msg.delete()
        deduct_whitelist_use(user_id)
        increment_usage(user_id, "downloads")
    except Exception as exc:
        logger.error(f"Send error for {size_mb:.1f}MB file: {exc}")
        await status_msg.edit_text(
            f"✅ File downloaded successfully ({size_mb:.1f} MB) on server.\n"
            "❌ But sending to Telegram failed or timed out.\n\n"
            "This often happens with very large videos (>100MB) on free Render.\n"
            "Try a smaller quality or different link."
        )
    finally:
        Path(filepath).unlink(missing_ok=True)


# ── Sticker creation logic ───────────────────────────────────────

async def handle_photo_for_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE, msg: Message) -> None:
    user = update.effective_user
    allowed, reason = await can_use_free(user.id, "stickers")
    if not allowed:
        await send_star_invoice(
            update, context,
            title="Sticker Pack Creation",
            description=f"Turn your photo into a sticker pack.\n\n{reason}",
            price=PRICE_STICKER,
            payload=f"sticker:{user.id}:{msg.message_id}",
        )
        return

    await perform_sticker_creation(update, context, msg, user.id)


async def perform_sticker_creation(
    update: Update, context: ContextTypes.DEFAULT_TYPE, msg: Message, user_id: int
) -> None:
    msg_reply = await msg.reply_text("🎨 Creating your sticker pack...")
    user = msg.from_user

    # Ensure we have the bot's username (required for sticker set naming)
    bot_username = getattr(context.bot, "username", None)
    if not bot_username:
        try:
            me = await context.bot.get_me()
            bot_username = me.username or "amazingtoolsbot"
        except Exception:
            bot_username = "amazingtoolsbot"

    # Download the largest photo
    photo_file = await msg.photo[-1].get_file()
    loop = asyncio.get_event_loop()

    def _create_sticker() -> str | None:
        """Convert photo to PNG sticker format."""
        try:
            resp = urllib.request.urlopen(photo_file.file_path, timeout=30)
            img_data = resp.read()
            img = Image.open(io.BytesIO(img_data))
            # Convert to RGBA if needed
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            # Resize to fit 512x512 (Telegram sticker size)
            img.thumbnail((512, 512), Image.LANCZOS)
            # Create a square canvas
            canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            offset = ((512 - img.width) // 2, (512 - img.height) // 2)
            canvas.paste(img, offset)
            # Save as PNG (Telegram accepts PNG for static stickers)
            out_path = str(DOWNLOADS_DIR / f"sticker_{user_id}_{int(time.time())}.png")
            canvas.save(out_path, "PNG")
            return out_path
        except Exception as exc:
            logger.error(f"Sticker creation error: {exc}")
            return None

    sticker_path = await loop.run_in_executor(None, _create_sticker)
    if not sticker_path:
        await msg_reply.edit_text("❌ Failed to create sticker.")
        return

    # Create a sticker set for the user
    sticker_set_name = f"amazing_{user_id}_{int(time.time())}_by_{bot_username}"
    pack_title = f"{user.first_name}'s Amazing Pack"
    try:
        with open(sticker_path, "rb") as f:
            sticker_data = f.read()
        sticker = InputSticker(
            sticker=sticker_data,
            emoji_list=["😊"],
            format="static",
        )
        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=sticker_set_name,
            title=pack_title,
            stickers=[sticker],
            sticker_format="static",
        )
        await msg_reply.edit_text(
            f"✅ Sticker pack created!\n\n"
            f"📦 <b>{pack_title}</b>\n"
            f"Tap <a href='https://t.me/addstickers/{sticker_set_name}'>here</a> to add it to Telegram.\n\n"
            f"Use /sticker to create more packs!",
            parse_mode=ParseMode.HTML,
        )
        deduct_whitelist_use(user_id)
        increment_usage(user_id, "stickers")
    except Exception as exc:
        logger.error(f"Sticker set creation error: {exc}")
        await msg_reply.edit_text(
            f"❌ Couldn't create sticker pack. Telegram may have limits. "
            f"Error: {exc}"
        )
    finally:
        Path(sticker_path).unlink(missing_ok=True)


# ── Successful payment handler ───────────────────────────────────────

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a successful Telegram Stars payment (Message with successful_payment)."""
    sp = update.message.successful_payment
    if not sp:
        return
    payload = sp.invoice_payload
    parts = payload.split(":", 2)
    if len(parts) < 3:
        logger.warning(f"Invalid payment payload: {payload}")
        return
    kind = parts[0]
    user_id = int(parts[1])
    data = parts[2]

    if kind == "download":
        await perform_download(update, context, data, user_id)
    elif kind == "sticker":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Payment received! Now send me the photo you want to turn into stickers.",
        )


# ── Telegram Mini App (WebApp) data handler ────────────────────────────────────

async def web_app_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle data sent from the beautiful Mini App."""
    try:
        raw = update.effective_message.web_app_data.data
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {}
        logger.warning("Invalid web_app_data received")

    action = (payload.get("action") or "").lower()
    chat_id = update.effective_chat.id if update.effective_chat else None
    user = update.effective_user

    # Always use direct send for reliability when data comes from WebApp
    async def safe_reply(text: str):
        if chat_id:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        elif update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    if action == "download":
        url = payload.get("url", "").strip()
        if url:
            # Directly perform using safe method + reuse logic
            await safe_reply("📥 Received your request from Mini App...")
            await handle_video_link(update, context, url)
        else:
            await safe_reply("❌ No URL received from Mini App.")
    elif action == "sticker":
        await safe_reply("🎨 Send me a photo in this chat now and I'll make the sticker pack for you!")
    elif action == "shorten":
        long_url = payload.get("url", "").strip()
        if long_url:
            try:
                if not long_url.startswith(("http://", "https://")):
                    long_url = "https://" + long_url
                code = await shorten_url(user.id if user else 0, long_url)
                base = MINIAPP_URL or "https://your-bot.onrender.com"
                short_link = f"{base}/s/{code}"
                await safe_reply(f"🔗 <b>Short URL created:</b>\n{short_link}")
            except ValueError as ve:
                await safe_reply(f"❌ {ve}")
            except Exception:
                await safe_reply("❌ Failed to create short link.")
        else:
            await safe_reply("❌ Please provide a URL to shorten.")
    elif action == "balance":
        await balance_cmd(update, context)
    elif action == "tools":
        await tools_cmd(update, context)
    else:
        await safe_reply("👋 Thanks! Use the buttons or send a link/photo directly.")


# ── Mini App static file server (background thread) ───────────────────────────────────

class MiniAppRequestHandler(SimpleHTTPRequestHandler):
    """Custom handler to ensure proper HTML serving for Telegram Mini App."""
    def __init__(self, *args, **kwargs):
        # directory will be set via partial
        super().__init__(*args, **kwargs)

    def end_headers(self):
        # Force correct content type for HTML
        if self.path.endswith(('.html', '/')) or self.path == '':
            self.send_header('Content-Type', 'text/html; charset=utf-8')
        # Cache control for mini app (helps with Telegram)
        if self.path.endswith(('.html', '.js', '.css')):
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

    def do_GET(self):
        # Handle short URL redirects: /s/abc123  (before webapp)
        if self.path.startswith('/s/'):
            code = self.path[3:].split('?')[0].split('#')[0]
            if code:
                original = get_original_url(code)
                if original:
                    increment_short_click(code)
                    self.send_response(302)
                    self.send_header('Location', original)
                    self.send_header('Cache-Control', 'no-cache')
                    self.end_headers()
                    return
                else:
                    self.send_error(404, "Short link not found")
                    return

        # Redirect root to index.html (Mini App)
        if self.path in ('/', ''):
            self.path = '/index.html'
        return super().do_GET()

    def guess_type(self, path):
        # Ensure .html is always text/html
        if path.endswith('.html'):
            return 'text/html'
        return super().guess_type(path)


def run_miniapp_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Serve the beautiful Mini App UI on a simple HTTP server."""
    webapp_dir = Path(__file__).parent / "webapp"
    if not webapp_dir.exists():
        logger.warning("webapp/ directory not found — Mini App UI will not be served.")
        return

    # Use custom handler with directory
    handler = partial(MiniAppRequestHandler, directory=str(webapp_dir))

    # Use ThreadingHTTPServer so static assets + main bot can coexist better
    httpd = ThreadingHTTPServer((host, port), handler)
    logger.info(f"Mini App UI serving on http://{host}:{port} (from {webapp_dir})")
    try:
        httpd.serve_forever()
    except Exception as e:
        logger.error(f"Mini App server error: {e}")


def start_miniapp_server_background() -> None:
    """Start the UI server in a daemon thread so polling can run in main thread."""
    port = int(os.environ.get("PORT", "8080"))
    thread = threading.Thread(
        target=run_miniapp_server,
        kwargs={"host": "0.0.0.0", "port": port},
        daemon=True,
    )
    thread.start()
    logger.info(f"Started Mini App static server thread on port {port}")


# ── Main ─────────────────────────────────────────

def main() -> None:
    # Start the beautiful Mini App UI server in background (Render exposes $PORT)
    start_miniapp_server_background()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tools", tools_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("short", short_cmd))
    app.add_handler(CommandHandler("shorten", short_cmd))
    app.add_handler(CommandHandler("download", download_cmd))
    app.add_handler(CommandHandler("sticker", sticker_cmd))
    app.add_handler(CommandHandler("dailyfree", admin_dailyfree, filters.User(user_id=OWNER_ID)))
    app.add_handler(CommandHandler("whitelist", admin_whitelist, filters.User(user_id=OWNER_ID)))
    app.add_handler(CommandHandler("addfree", admin_addfree, filters.User(user_id=OWNER_ID)))

    # Pre-checkout (answer payment query) & successful payment (handle after payment)
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Auto-detect handler for video links & photos
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, auto_detect))

    # Handle data coming from the Telegram Mini App
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, web_app_data_handler))

    # Handle clicks on the beautiful menu buttons
    app.add_handler(CallbackQueryHandler(menu_button_callback, pattern="^cmd_"))

    logger.info("Amazing Tools Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
