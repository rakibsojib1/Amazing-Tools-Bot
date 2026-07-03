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
    InputSticker,
    LabeledPrice,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = 666053962
DB_PATH = "data.db"
WHITELIST_PATH = "whitelist.json"

DOWNLOADS_DIR = Path("/tmp/amazing_downloads")
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

DAILY_FREE_DEFAULT_LIMIT = 5

# Pricing in Telegram Stars (XTR)
PRICE_DOWNLOAD = 5    # Stars per video download
PRICE_STICKER = 10    # Stars per sticker pack creation

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
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
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
        "<b>How it works:</b>\n"
        "• Send me a <b>video link</b> → I'll download it (requires payment)\n"
        "• Send me a <b>photo</b> → I'll turn it into stickers\n"
        "• Use /tools to see everything I can do\n\n"
        "👇 <i>Try sending a link or photo right now!</i>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "👤 Created by <b>Rakib Sojib</b>\n"
        "📞 Contact: <b>@roki1277</b>\n"
        "🤖 Made with AI\n"
        "━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


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
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


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
        "<b>💳 Payment:</b> Pay with Telegram Stars (XTR)\n"
        "• Just click the payment button when prompted\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "👤 Created by <b>Rakib Sojib</b>\n"
        "📞 Contact: <b>@roki1277</b>\n"
        "🤖 Made with AI\n"
        "━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


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
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


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
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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


# ── Message / auto-detect handler ────────────────────────────────────────

VIDEO_LINK_PATTERN = re.compile(
    r"(https?://)?(www\.)?(tiktok|youtube|youtu\.be|instagram|facebook|fb|x|twitter|"
    r"reddit|vimeo|dailymotion|pinterest|snapchat|likee|vine|streamable|t\.co)\S+",
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

    # ── Photo → sticker flow ──────────────────────────────────────────
    if msg.photo:
        await handle_photo_for_sticker(update, context, msg)
        return

    # ── Video link → download flow ────────────────────────────────────
    if msg.text and VIDEO_LINK_PATTERN.search(msg.text):
        url = VIDEO_LINK_PATTERN.search(msg.text).group(0)
        await handle_video_link(update, context, url)
        return


# ── Video download logic ─────────────────────────────────────────────────

async def handle_video_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    user = update.effective_user
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


async def perform_download(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, user_id: int
) -> None:
    msg = await update.message.reply_text("⏳ Downloading your video...")
    loop = asyncio.get_event_loop()

    def _download() -> str | None:
        """Run yt-dlp synchronously in executor."""
        import yt_dlp

        out_tmpl = str(DOWNLOADS_DIR / "%(id)s.%(ext)s")
        ydl_opts = {
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": 50 * 1024 * 1024,  # 50 MB
            "format": "best[filesize<50M]/best",
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                # Locate the downloaded file
                fname = str(DOWNLOADS_DIR / f"{info['id']}.{info.get('ext', 'mp4')}")
                if Path(fname).exists():
                    return fname
                # yt-dlp may use a different extension; glob for the id
                files = list(DOWNLOADS_DIR.glob(f"{info['id']}.*"))
                return str(files[0]) if files else None
        except Exception as exc:
            logger.error(f"yt-dlp error: {exc}")
            return None

    filepath = await loop.run_in_executor(None, _download)
    if not filepath:
        await msg.edit_text("❌ Failed to download video. The link may be invalid or the video is too large.")
        return

    # Send video to user
    try:
        with open(filepath, "rb") as f:
            await update.message.reply_video(
                video=f,
                caption="📥 Downloaded via Amazing Tools Bot",
                write_timeout=60,
                read_timeout=60,
            )
        await msg.delete()
        # Deduct usage
        deduct_whitelist_use(user_id)
        increment_usage(user_id, "downloads")
    except Exception as exc:
        logger.error(f"Send video error: {exc}")
        await msg.edit_text("✅ Downloaded but couldn't send the file. It may be too large for Telegram.")
    finally:
        # Cleanup
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
    sticker_set_name = f"amazing_{user_id}_{int(time.time())}_by_{context.bot.username}"
    pack_title = f"{user.first_name}'s Amazing Pack"
    try:
        with open(sticker_path, "rb") as f:
            sticker_data = f.read()
        sticker = InputSticker(
            sticker=sticker_data,
            emoji_list=["😊"],
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


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("tools", tools_cmd))
    app.add_handler(CommandHandler("contact", contact_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
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

    logger.info("Amazing Tools Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
