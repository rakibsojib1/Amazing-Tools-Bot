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
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageFilter
import qrcode
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

# ── Mini App + Main Menu Keyboard ────────────────────────────────────────

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
            InlineKeyboardButton("📱 QR Code", callback_data="cmd_qr"),
            InlineKeyboardButton("🖼️ Image Tools (Free)", callback_data="cmd_image"),
        ],
        [
            InlineKeyboardButton("📋 My Shorts", callback_data="cmd_myshorts"),
            InlineKeyboardButton("🛠️ All Tools", callback_data="cmd_tools"),
        ],
        [InlineKeyboardButton("💬 Send a video link or photo directly", callback_data="cmd_help")]
    ])
    return InlineKeyboardMarkup(buttons)


# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────

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
        """)
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        """)
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS short_urls (
            short_code TEXT PRIMARY KEY,
            original_url TEXT NOT NULL,
            user_id INTEGER,
            created_at TEXT,
            clicks INTEGER DEFAULT 0
        """)
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


# ── Check if user can use a tool for free ────────────────────────────────

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


# ── URL Shortener (free self-hosted) ─────────────────────────────────────

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


def prune_old_shorts(days: int = 45) -> int:
    """Delete old short links that have 0 clicks to save space. Returns number deleted."""
    try:
        from datetime import timedelta
        db = get_usage_db()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cur = db.execute(
            "DELETE FROM short_urls WHERE clicks = 0 AND created_at < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        db.commit()
        if deleted > 0:
            logger.info(f"Pruned {deleted} old unused short links.")
        return deleted
    except Exception as e:
        logger.error(f"Prune error: {e}")
        return 0


def delete_short(short_code: str, user_id: int) -> bool:
    """Delete a short link owned by the user. Returns True if deleted."""
    db = get_usage_db()
    cur = db.execute(
        "DELETE FROM short_urls WHERE short_code = ? AND user_id = ?",
        (short_code, user_id)
    )
    db.commit()
    return cur.rowcount > 0


# ── QR Code & Image Tools (Professional helpers) ─────────────────────────

async def generate_qr_image(text: str) -> bytes:
    """Generate a clean, high-quality QR code image."""
    if not text or len(text.strip()) == 0:
        raise ValueError("Please provide some text or a URL for the QR code.")
    if len(text) > 1500:
        raise ValueError("Text is too long (max ~1500 characters).")

    def _generate():
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=12,
            border=2,
        )
        qr.add_data(text.strip())
        qr.make(fit=True)

        img = qr.make_image(
            fill_color="#0f172a",  # Dark professional color
            back_color="#f1f5f9",
        ).convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf.getvalue()

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _generate)


async def download_photo(photo) -> bytes:
    """Download photo to bytes professionally."""
    photo_file = await photo.get_file()
    loop = asyncio.get_running_loop()

    def _dl():
        with urllib.request.urlopen(photo_file.file_path, timeout=30) as resp:
            return resp.read()

    return await loop.run_in_executor(None, _dl)


async def process_image_tool(photo, operation: str, **params) -> tuple[bytes, str, str]:
    """Professional image processing pipeline.
    Returns (bytes, extension, info_message)
    """
    data = await download_photo(photo)
    original_size = len(data)

    def _process():
        img = Image.open(io.BytesIO(data))
        orig_mode = img.mode
        orig_format = img.format or "PNG"

        if operation == "compress":
            quality = max(10, min(95, int(params.get("quality", 80))))
            if orig_mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
            out_bytes = buf.getvalue()
            ext = "jpg"
            info = f"Compressed JPEG (quality {quality})"

        elif operation == "webp":
            quality = max(50, min(95, int(params.get("quality", 85))))
            buf = io.BytesIO()
            save_params = {"quality": quality, "method": 6}
            if orig_mode == "P":
                img = img.convert("RGBA")
            img.save(buf, "WEBP", **save_params)
            out_bytes = buf.getvalue()
            ext = "webp"
            info = f"WebP (quality {quality})"

        elif operation == "resize":
            max_w = int(params.get("width", 1280))
            max_h = int(params.get("height", 1280))
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            buf = io.BytesIO()
            fmt = "JPEG" if orig_format == "JPEG" else "PNG"
            if fmt == "JPEG" and orig_mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buf, fmt, optimize=True)
            out_bytes = buf.getvalue()
            ext = "jpg" if fmt == "JPEG" else "png"
            info = f"Resized (max {max_w}×{max_h})"

        elif operation == "resize_compress":
            max_w = int(params.get("width", 1280))
            max_h = int(params.get("height", 1280))
            quality = max(10, min(95, int(params.get("quality", 80))))
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
            out_bytes = buf.getvalue()
            ext = "jpg"
            info = f"Resized to max {max_w}×{max_h} + compressed (q{quality})"

        elif operation.startswith("filter:"):
            filt = operation.split(":", 1)[1]
            if filt == "grayscale":
                img = img.convert("L").convert("RGB")
                info = "Grayscale filter"
            elif filt == "sepia":
                if img.mode != "RGB":
                    img = img.convert("RGB")
                def _sepia(p):
                    r, g, b = p
                    tr = min(255, int(0.393 * r + 0.769 * g + 0.189 * b))
                    tg = min(255, int(0.349 * r + 0.686 * g + 0.168 * b))
                    tb = min(255, int(0.272 * r + 0.534 * g + 0.131 * b))
                    return (tr, tg, tb)
                img = img.point(_sepia)
                info = "Sepia filter"
            elif filt == "blur":
                img = img.filter(ImageFilter.GaussianBlur(radius=2))
                info = "Blur filter"
            elif filt == "sharpen":
                img = img.filter(ImageFilter.SHARPEN)
                info = "Sharpen filter"
            elif filt == "edge":
                img = img.filter(ImageFilter.FIND_EDGES)
                info = "Edge detect"
            else:
                info = "Unknown filter"
            buf = io.BytesIO()
            fmt = "JPEG" if orig_format == "JPEG" else "PNG"
            if fmt == "JPEG" and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buf, fmt, optimize=True)
            out_bytes = buf.getvalue()
            ext = "jpg" if fmt == "JPEG" else "png"

        else:
            out_bytes = data
            ext = "png"
            info = "Processed"

        new_size = len(out_bytes)
        saved_pct = ((original_size - new_size) / original_size * 100) if original_size > 0 else 0
        size_info = f"{new_size / 1024:.1f} KB (saved {saved_pct:.0f}%)"
        return out_bytes, ext, f"{info} • {size_info}"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _process)


async def images_to_pdf(photos: list) -> bytes:
    """Convert list of photos to a clean multi-page PDF."""
    if not photos:
        raise ValueError("No photos provided for PDF.")

    images = []
    for photo in photos:
        data = await download_photo(photo)
        img = Image.open(io.BytesIO(data))
        if img.mode in ("RGBA", "P"):
            # Convert with white background for PDF
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        images.append(img)

    buf = io.BytesIO()
    images[0].save(
        buf,
        "PDF",
        resolution=150.0,
        save_all=True,
        append_images=images[1:],
        title="Amazing Tools - PDF",
    )
    buf.seek(0)
    return buf.getvalue()


# ── Payment helpers (Telegram Stars) ─────────────────────────────────────

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


# ── Commands ─────────────────────────────────────────────────────────────

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = (
        f"👋 <b>Welcome to Amazing Tools, {user.first_name}!</b>\n\n"
        "I can help you download videos from TikTok, YouTube, Instagram, and more — "
        "or create cool sticker packs from your photos!\n\n"
        "<b>New Free Tools:</b>\n"
        "• 📱 /qr — Generate QR codes\n"
        "• 🖼️ Send photo — Compress, WebP, Resize, Filters, PDF\n"
        "• 📋 /myshorts — Your short links & clicks\n\n"
        "• Send me a <b>video link</b> → download (paid)\n"
        "• Send me a <b>photo</b> → stickers (paid) or free image tools\n"
        "• Use /tools or the menu for everything\n\n"
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
        "/qr — Generate QR code\n"
        "/compress /webp /resize /pdf — Image & PDF tools (reply to photo)\n"
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
        "━━━━━━━━━━━━━━━━━━━━\n\n"
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
        "📱 <b>QR Code Generator</b>\n"
        "Turn any text or link into a beautiful QR code instantly.\n"
        "Free • /qr your-text\n\n"
        "🖼️ <b>Image Tools</b> (Free)\n"
        "• Compress JPEG • Convert to WebP\n"
        "• Resize photos • Images → PDF\n"
        "Just send a photo and tap the buttons!\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>💳 Payment:</b> Pay with Telegram Stars (XTR)\n"
        "• Just click the payment button when prompted\n\n"
        "━━━━━━━━━━━━━━━━\n"
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
            "Custom code: <code>/short url mycode</code>\n\n"
            "📊 Check clicks: <code>/stats mycode</code>\n"
            "Your links: <code>/myshorts</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu()
        )
    elif data == "cmd_balance":
        await balance_cmd(update, context)
    elif data == "cmd_tools":
        await tools_cmd(update, context)
    elif data == "cmd_qr":
        await query.message.reply_text(
            "📱 <b>QR Code Generator</b>\n\n"
            "Use: <code>/qr your-text-or-link</code>\n"
            "Or open the Mini App for easy QR creation.\n\n"
            "Free!",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu()
        )
    elif data == "cmd_image":
        await query.message.reply_text(
            "🖼️ <b>Image Tools (Free)</b>\n\n"
            "• Send any photo → buttons appear for Compress / WebP / Resize / Filters / PDF\n\n"
            "Commands: /compress /webp /resize /pdf\n\n"
            "Photos are only temporarily referenced in memory (cleared after processing). Nothing is stored in database or on disk.\n\n"
            "Mini App has quality slider + presets for easier use.",
            parse_mode=ParseMode.HTML,
            reply_markup=get_main_menu()
        )
    elif data == "cmd_myshorts":
        await myshorts_cmd(update, context)
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


# ── URL Shortener command ────────────────────────────────────────────────

async def short_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /short <url> [custom_code]  or /shorten """
    args = context.args
    if not args:
        await update.message.reply_text(
            "🔗 <b>URL Shortener</b>\n\n"
            "Usage:\n"
            "/short https://very-long-url.com/example\n"
            "/short https://example.com myalias   (custom short code)\n\n"
            "📊 View clicks: <code>/stats abc123</code>\n"
            "Your links: <code>/myshorts</code>\n\n"
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
            f"🔗 <b>Short Link:</b> {short_link}\n\n"
            f"🔑 <b>Your Short Code:</b> <code>{code}</code>\n\n"
            f"📊 Clicks are tracked in real time.\n"
            f"Check clicks later with: <code>/stats {code}</code>\n"
            f"See all your links: <code>/myshorts</code>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except ValueError as ve:
        await update.message.reply_text(f"❌ {ve}")
    except Exception as e:
        logger.error(f"Shorten error: {e}")
        await update.message.reply_text("❌ Failed to shorten URL. Try again.")


# ── QR Code Command ──────────────────────────────────────────────────────

async def qr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate QR code from text or URL."""
    text = " ".join(context.args).strip() if context.args else None
    if not text:
        await update.message.reply_text(
            "📱 <b>QR Code Generator</b>\n\n"
            "Usage:\n"
            "<code>/qr https://example.com</code>\n"
            "<code>/qr Hello from Amazing Tools</code>\n\n"
            "Works great with links, WiFi info, or any text.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        status = await update.message.reply_text("⏳ Generating QR code...")
        qr_bytes = await generate_qr_image(text)
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=qr_bytes,
            caption=f"📱 <b>QR Code</b>\n<code>{text[:120]}</code>\n\nGenerated by Amazing Tools Bot",
            parse_mode=ParseMode.HTML,
        )
        await status.delete()
    except Exception as e:
        logger.error(f"QR error: {e}")
        await update.message.reply_text(f"❌ QR generation failed: {e}")


# ── Image Tools Commands (reply to photo) ────────────────────────────────

async def compress_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_image_command(update, context, "compress")


async def webp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_image_command(update, context, "webp")


async def resize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _handle_image_command(update, context, "resize")


async def pdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Convert recent photos or replied photo(s) to PDF."""
    photos = []
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        photos.append(update.message.reply_to_message.photo[-1])
    else:
        photos = context.user_data.get("photo_list", [])[-5:]  # last few

    if not photos:
        await update.message.reply_text(
            "📄 <b>PDF from Images</b>\n\n"
            "Send 1 or more photos, then reply to one with /pdf\n"
            "or just use /pdf after sending photos.",
            parse_mode=ParseMode.HTML
        )
        return

    try:
        status = await update.message.reply_text("⏳ Creating PDF...")
        pdf_bytes = await images_to_pdf(photos)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=pdf_bytes,
            filename="amazing_tools_images.pdf",
            caption="📄 PDF created from your images • Amazing Tools",
        )
        await status.delete()
        # Clear temporary photo references (no storage in DB or on disk)
        context.user_data.pop("photo_list", None)
        context.user_data.pop("pending_photo", None)
        await update.message.reply_text("✅ Done. Temporary photo data cleared (nothing stored permanently).")
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await update.message.reply_text(f"❌ PDF creation failed: {e}")


async def delete_short_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /dels <code> or /delete_short <code> """
    if not context.args:
        await update.message.reply_text(
            "🗑️ <b>Delete Short Link</b>\n\n"
            "Usage: /dels abc123\n\n"
            "Only you can delete your own links.",
            parse_mode=ParseMode.HTML
        )
        return
    code = context.args[0].strip().split("/")[-1]
    if delete_short(code, update.effective_user.id):
        await update.message.reply_text(f"🗑️ Short code <code>{code}</code> deleted.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("❌ Not found or not your link.")


async def _handle_image_command(update: Update, context: ContextTypes.DEFAULT_TYPE, op: str):
    """Helper for image processing commands."""
    photo = None
    if update.message.reply_to_message and update.message.reply_to_message.photo:
        photo = update.message.reply_to_message.photo[-1]
    else:
        # fallback to last received
        photo = context.user_data.get("pending_photo") or (
            context.user_data.get("photo_list", [])[-1] if context.user_data.get("photo_list") else None
        )

    if not photo:
        await update.message.reply_text(
            f"Send or reply to a photo with /{op}\n"
            f"Example: reply to photo → <code>/{op}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    params = {}
    if op == "compress" and context.args:
        try:
            params["quality"] = int(context.args[0])
        except:
            params["quality"] = 80
    elif op in ("resize", "resize_compress") and len(context.args) >= 2:
        try:
            params["width"] = int(context.args[0])
            params["height"] = int(context.args[1])
            if len(context.args) >= 3:
                params["quality"] = int(context.args[2])
                op = "resize_compress"  # force combined if quality given
        except:
            params["width"] = 1280
            params["height"] = 1280

    try:
        status = await update.message.reply_text(f"⏳ Processing ({op})...")
        img_bytes, ext, info = await process_image_tool(photo, op, **params)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=img_bytes,
            filename=f"amazing_tools.{ext}",
            caption=f"✅ {info}",
        )
        await status.delete()
        # Clear temporary references (photos are never stored in DB or on disk)
        context.user_data.pop("pending_photo", None)
        # Keep photo_list only if user might want PDF next; otherwise clear on single tool use
        if op != "pdf":
            context.user_data.pop("photo_list", None)
        await update.message.reply_text("✅ Done. Temporary photo data cleared.")
    except Exception as e:
        logger.error(f"Image {op} error: {e}")
        await update.message.reply_text(f"❌ Processing failed: {e}")


# ── Short URL Stats ──────────────────────────────────────────────────────

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /stats <short_code>  — View real click count for a short link """
    args = context.args
    if not args:
        await update.message.reply_text(
            "📊 <b>Short Link Stats</b>\n\n"
            "Usage:\n"
            "/stats abc123\n\n"
            "Shows clicks for any short link.\n"
            "See all links you created: /myshorts",
            parse_mode=ParseMode.HTML,
        )
        return

    code = args[0].strip().split("/")[-1]  # tolerate full link or just code
    stats = get_short_stats(code)
    if not stats:
        await update.message.reply_text("❌ Short code not found or never created.")
        return

    created = ""
    if stats.get("created_at"):
        try:
            created = stats["created_at"][:16].replace("T", " ")
        except Exception:
            created = stats["created_at"]

    base = MINIAPP_URL or "https://your-bot.onrender.com"
    short_link = f"{base}/s/{code}"

    msg = (
        f"📊 <b>Stats for</b> <code>{code}</code>\n\n"
        f"🔗 Short: {short_link}\n"
        f"🌐 Original: <code>{stats['original_url']}</code>\n\n"
        f"👆 <b>Clicks: {stats['clicks']}</b>\n"
        f"🗓️ Created: {created or 'unknown'}\n\n"
        "✅ Clicks are tracked in real-time in the database (incremented on every redirect)."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=get_main_menu())


# ── My Short Links ───────────────────────────────────────────────────────

async def myshorts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current user's own shortened links with click counts."""
    user_id = update.effective_user.id
    db = get_usage_db()
    rows = db.execute(
        """SELECT short_code, original_url, clicks, created_at 
           FROM short_urls 
           WHERE user_id = ? 
           ORDER BY created_at DESC 
           LIMIT 15""",
        (user_id,),
    ).fetchall()

    if not rows:
        await update.message.reply_text(
            "You haven't created any short links yet.\nUse /short <url> to create one.",
            parse_mode=ParseMode.HTML
        )
        return

    base = MINIAPP_URL or "https://your-bot.onrender.com"
    lines = ["📋 <b>Your Short Links</b> (most recent first)\n"]

    for row in rows:
        code = row["short_code"]
        short_link = f"{base}/s/{code}"
        orig = row["original_url"]
        clicks = row["clicks"]
        created = str(row["created_at"])[:10] if row["created_at"] else "?"

        lines.append(
            f"🔑 <code>{code}</code> — {clicks} clicks\n"
            f"   {short_link}\n"
            f"   <code>{orig}</code> (created {created})\n"
            f"   Check: <code>/stats {code}</code>\n"
        )

    lines.append("\n🗑️ Delete: <code>/dels CODE</code>\n📊 Stats: <code>/stats CODE</code>")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=get_main_menu())


# ── Admin commands ───────────────────────────────────────────────────────

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


# ── Image Tools Callback Handler (professional buttons) ──────────────────

async def image_tools_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline image tool buttons from photo offers."""
    query = update.callback_query
    await query.answer()

    data = (query.data or "").split(":")
    if len(data) < 2 or data[0] != "img":
        return

    op = data[1]
    photo = context.user_data.get("pending_photo")
    if not photo:
        await query.message.reply_text("⚠️ Photo session expired. Please send the photo again.")
        return

    params = {}
    if len(data) > 2:
        try:
            if op == "compress":
                params["quality"] = int(data[2])
            elif op == "resize":
                params["width"] = int(data[2])
                params["height"] = int(data[2])
        except:
            pass

    if op == "resize" and data[2] == "custom":
        # Set awaiting state for custom size input
        context.user_data["awaiting_resize"] = True
        context.user_data["pending_photo"] = photo
        await query.message.reply_text(
            "📐 Send the size as reply or next message:\n"
            "• `800 600` for width x height\n"
            "• `800` for square\n"
            "• `800 600 75` to also compress (quality 75)\n\n"
            "Example: 1280 720 80",
            reply_markup=get_main_menu()
        )
        await query.message.delete()
        return

    try:
        await query.message.edit_text(f"⏳ Processing {op}...")

        if op == "pdf":
            pdf_bytes = await images_to_pdf([photo])
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_bytes,
                filename="amazing_tools.pdf",
                caption="📄 Your images as PDF • Amazing Tools",
            )
        else:
            img_bytes, ext, info = await process_image_tool(photo, op, **params)
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=img_bytes,
                filename=f"tool.{ext}",
                caption=f"✅ {info}",
            )

        # cleanup - photos are only temporarily referenced, never stored in DB
        context.user_data.pop("pending_photo", None)
        if op != "pdf":
            context.user_data.pop("photo_list", None)
        await query.message.delete()
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "✅ Done. Temporary photo data cleared (not stored permanently)."
            )
        except:
            pass

    except Exception as e:
        logger.error(f"Image callback {op} error: {e}")
        await query.message.reply_text(f"❌ Failed: {str(e)[:120]}")


# ── Message / auto-detect handler ────────────────────────────────────────

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

    # ── Photo handling: offer professional image tools + sticker option ─
    if msg.photo:
        # Store recent photos for PDF / tools
        if "photo_list" not in context.user_data:
            context.user_data["photo_list"] = []
        context.user_data["photo_list"].append(msg.photo[-1])
        context.user_data["photo_list"] = context.user_data["photo_list"][-6:]

        context.user_data["pending_photo"] = msg.photo[-1]

        # Check if Mini App requested a specific tool
        pending_tool = context.user_data.pop("pending_image_tool", None)
        if pending_tool:
            tool = pending_tool.get("tool", "compress")
            params = pending_tool.get("params", {})
            try:
                status = await update.message.reply_text(f"⏳ Applying {tool} from Mini App...")
                if tool == "pdf":
                    pdf_bytes = await images_to_pdf([msg.photo[-1]])
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=pdf_bytes,
                        filename="amazing_tools.pdf",
                        caption="📄 PDF from Mini App request",
                    )
                else:
                    img_bytes, ext, info = await process_image_tool(msg.photo[-1], tool, **params)
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=img_bytes,
                        filename=f"tool.{ext}",
                        caption=f"✅ {info} (via Mini App)",
                    )
                await status.delete()
                # Clear temp refs
                context.user_data.pop("photo_list", None)
                context.user_data.pop("pending_photo", None)
                try:
                    await update.message.reply_text("✅ Done. Temporary photo data cleared.")
                except:
                    pass
            except Exception as e:
                await update.message.reply_text(f"❌ Tool failed: {e}")
            return

        # Nice inline options for direct chat
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🗜️ Compress JPEG", callback_data="img:compress:80"),
                InlineKeyboardButton("🖼️ Convert WebP", callback_data="img:webp"),
            ],
            [
                InlineKeyboardButton("📐 Resize (custom)", callback_data="img:resize:custom"),
                InlineKeyboardButton("📄 Make PDF", callback_data="img:pdf"),
            ],
            [
                InlineKeyboardButton("🎨 Make Stickers (⭐10)", callback_data="cmd_sticker"),
            ],
        ])

        await update.message.reply_text(
            "📸 <b>Photo received!</b>\nChoose what to do (photos are kept only temporarily in memory — cleared after use, nothing saved to DB):",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )
        return

    # Handle awaiting custom resize size (from button)
    if context.user_data.get("awaiting_resize") and msg.text:
        photo = context.user_data.get("pending_photo")
        if photo:
            text = msg.text.strip()
            parts = text.split()
            try:
                w = int(parts[0])
                h = int(parts[1]) if len(parts) > 1 else w
                q = int(parts[2]) if len(parts) > 2 else None
                op = "resize_compress" if q else "resize"
                params = {"width": w, "height": h}
                if q:
                    params["quality"] = q
                status = await update.message.reply_text(f"⏳ Resizing to {w}x{h}...")
                img_bytes, ext, info = await process_image_tool(photo, op, **params)
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=img_bytes,
                    filename=f"resized.{ext}",
                    caption=f"✅ {info}",
                )
                await status.delete()
                context.user_data.pop("awaiting_resize", None)
                context.user_data.pop("pending_photo", None)
                context.user_data.pop("photo_list", None)
                await update.message.reply_text("✅ Done. Temporary photo data cleared.")
            except Exception:
                await update.message.reply_text("❌ Invalid. Use e.g. 800 600 or 800 600 80 for resize+compress")
            return

    # ── Video link → download flow ────────────────────────────────────
    if msg.text and VIDEO_LINK_PATTERN.search(msg.text):
        url = VIDEO_LINK_PATTERN.search(msg.text).group(0)
        await handle_video_link(update, context, url)
        return


# ── Video download logic ─────────────────────────────────────────────────

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


# --- Standalone Mini App video helpers (used by web server) ---
def get_video_info_sync(source_url: str) -> dict:
    """Return title + best formats for the website video tool (no download)."""
    import yt_dlp
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        },
        "nocheckcertificate": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(source_url, download=False)
            if not info:
                return {"error": "Could not fetch info"}

            title = info.get("title", "video")
            formats = []
            best_direct = None

            for f in info.get("formats", []):
                if not f.get("url"):
                    continue
                height = f.get("height") or 0
                vcodec = f.get("vcodec") or ""
                if vcodec == "none":
                    continue  # skip pure audio for main list
                size = f.get("filesize") or f.get("filesize_approx")
                size_str = f"{size / (1024*1024):.1f}MB" if size else ""
                formats.append({
                    "format_id": f.get("format_id"),
                    "height": height,
                    "ext": f.get("ext", "mp4"),
                    "size": size_str,
                    "url": f.get("url"),
                })

            # Best by height
            if formats:
                formats.sort(key=lambda x: x["height"], reverse=True)
                best_direct = formats[0]["url"]

            # Also include a merged best if possible
            return {
                "title": title[:80],
                "formats": formats[:8],   # top 8
                "best_direct_url": best_direct,
            }
    except Exception as e:
        return {"error": str(e)[:120]}


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
                write_timeout=300,   # longer for big uploads
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


# ── Sticker creation logic ───────────────────────────────────────────────

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


# ── Successful payment handler ───────────────────────────────────────────

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


# ── Telegram Mini App (WebApp) data handler ──────────────────────────────

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
    elif action == "qr":
        text = payload.get("text", "").strip()
        if text:
            try:
                await safe_reply("📱 Generating QR code...")
                qr_bytes = await generate_qr_image(text)
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=qr_bytes,
                    caption="📱 <b>QR Code</b> via Amazing Tools Mini App",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                await safe_reply(f"❌ QR failed: {e}")
        else:
            await safe_reply("❌ No text provided for QR code.")
    elif action == "image_tool":
        tool = payload.get("tool", "compress")
        params = payload.get("params", {})
        context.user_data["pending_image_tool"] = {"tool": tool, "params": params}
        tool_name = tool.replace("filter:", "").replace(":", " ").title()
        await safe_reply(f"🖼️ Send a photo now for <b>{tool_name}</b> (quality/presets applied if set).")
    elif action == "my_shorts":
        # Trigger myshorts
        await myshorts_cmd(update, context)
    elif action == "shorten":
        long_url = payload.get("url", "").strip()
        if long_url:
            try:
                if not long_url.startswith(("http://", "https://")):
                    long_url = "https://" + long_url
                code = await shorten_url(user.id if user else 0, long_url)
                base = MINIAPP_URL or "https://your-bot.onrender.com"
                short_link = f"{base}/s/{code}"
                await safe_reply(
                    f"✅ <b>Short URL created!</b>\n\n"
                    f"🔗 Link: {short_link}\n"
                    f"🔑 Code: <code>{code}</code>\n\n"
                    f"📊 Check clicks: <code>/stats {code}</code>\n"
                    f"See all your links: <code>/myshorts</code>"
                )
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


# ── Mini App static file server (background thread) ──────────────────────

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
        # Simple API for direct web use (for standalone Mini App / website)
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == '/shorten':
            url = qs.get('url', [''])[0]
            if url:
                if not url.startswith(('http://', 'https://')):
                    url = 'https://' + url
                try:
                    code = shorten_url(0, url)  # 0 = anonymous web user
                    base = MINIAPP_URL or f"http://{self.headers.get('Host', 'localhost')}"
                    short_link = f"{base}/s/{code}"
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps({"short_link": short_link, "code": code}).encode('utf-8'))
                    return
                except Exception as e:
                    self.send_error(500, str(e))
                    return

        # Video info for standalone site (show best qualities)
        if parsed.path == '/video-info':
            url = qs.get('url', [''])[0]
            if url:
                if not url.startswith(('http://', 'https://')):
                    url = 'https://' + url
                try:
                    data = get_video_info_sync(url)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode('utf-8'))
                    return
                except Exception as e:
                    self.send_error(500, str(e))
                    return

        # Return best direct download URL (client triggers browser download to phone storage)
        if parsed.path == '/video-direct':
            url = qs.get('url', [''])[0]
            if url:
                if not url.startswith(('http://', 'https://')):
                    url = 'https://' + url
                try:
                    data = get_video_info_sync(url)
                    if data.get("best_direct_url"):
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Access-Control-Allow-Origin', '*')
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "direct_url": data["best_direct_url"],
                            "title": data.get("title", "video"),
                            "filename": (data.get("title", "video")[:40] + ".mp4").replace(" ", "_")
                        }).encode('utf-8'))
                        return
                    else:
                        self.send_error(404, "No direct URL found")
                        return
                except Exception as e:
                    self.send_error(500, str(e))
                    return

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


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    # Start the beautiful Mini App UI server in background (Render exposes $PORT)
    start_miniapp_server_background()

    # Clean up old unused short links to reduce DB size (0-click links older than 45 days)
    try:
        prune_old_shorts(45)
    except Exception:
        pass

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
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("myshorts", myshorts_cmd))
    app.add_handler(CommandHandler("qr", qr_cmd))
    app.add_handler(CommandHandler("compress", compress_cmd))
    app.add_handler(CommandHandler("webp", webp_cmd))
    app.add_handler(CommandHandler("resize", resize_cmd))
    app.add_handler(CommandHandler("pdf", pdf_cmd))
    app.add_handler(CommandHandler("dels", delete_short_cmd))
    app.add_handler(CommandHandler("delete_short", delete_short_cmd))
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
    # Image tools (compress, webp, resize, pdf) from photo buttons
    app.add_handler(CallbackQueryHandler(image_tools_callback, pattern="^img:"))

    logger.info("Amazing Tools Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
