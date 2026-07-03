# 🤖 Amazing Tools Bot

A feature-rich **Telegram bot** for downloading videos (TikTok, YouTube, Instagram, Facebook, and more) and creating custom sticker packs from photos. Supports **Telegram Stars (XTR)** payments and a daily free usage tier.

---

## ✨ Features

| Feature | Description | Cost |
|---|---|---|
| 📥 **Video Downloader** | Download from TikTok, YouTube, Instagram, Facebook, Twitter/X & more | ⭐ 5 Stars |
| 🎨 **Sticker Creator** | Turn your photos into Telegram sticker packs | ⭐ 10 Stars |
| 🎁 **Daily Free Tier** | Configurable free daily usage for all users | Free |
| 👑 **Whitelist System** | Grant unlimited or limited free access to specific users | Admin |
| 💳 **Telegram Stars** | Pay securely with Telegram's native currency (XTR) | — |

---

## 🚀 Deploy on Render

### 1. Fork / Clone

```bash
git clone https://github.com/rakibsojib1/Amazing-Tools.git
cd Amazing-Tools
```

### 2. Create a Bot on Telegram

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** you receive
4. **Enable Stars payments**: In BotFather, send `/mybots`, select your bot → **Bot Settings** → **Payments** → enable Telegram Stars
5. (Optional) Set a profile photo and description

### 3. Deploy on Render

1. Go to [dashboard.render.com](https://dashboard.render.com) → **New** → **Web Service**
2. Connect your GitHub repo (`rakibsojib1/Amazing-Tools`)
3. Configure:

   | Setting | Value |
   |---|---|
   | **Name** | `amazing-tools-bot` (or any name) |
   | **Runtime** | `Python 3` |
   | **Build Command** | `pip install -r requirements.txt` |
   | **Start Command** | `python bot.py` |
   | **Plan** | Free (or paid for better uptime) |

4. Add **Environment Variable**:

   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | Your bot token from BotFather |

5. Click **Deploy**

> ⚠️ **Important**: Render's free tier spins down after 15 min of inactivity. To keep the bot always-on, upgrade to a paid Starter plan.

### Alternative: Deploy on a VPS

```bash
# Install Python + dependencies
apt update && apt install -y python3 python3-pip ffmpeg
pip install -r requirements.txt

# Create .env file
echo "BOT_TOKEN=your_token_here" > .env

# Run (use tmux or screen to keep alive)
screen -S bot
python bot.py
# Ctrl+A, D to detach
```

---

## 📋 Commands

### User Commands

| Command | Description |
|---|---|
| `/start` | Welcome message with credits |
| `/help` | Show help & tips |
| `/tools` | List all available tools |
| `/download` | Download a video (or just send a link!) |
| `/sticker` | Create stickers from photos (or just send a photo!) |
| `/balance` | Check your usage & remaining free uses |
| `/contact` | Contact the creator |

### Admin Commands (Owner only — ID: `666053962`)

| Command | Description |
|---|---|
| `/dailyfree on` | Enable daily free usage tier |
| `/dailyfree off` | Disable daily free usage tier |
| `/dailyfree limit N` | Set daily free limit per tool (default: 5) |
| `/dailyfree tools all|specific` | Choose which tools get free tier |
| `/dailyfree status` | Show current daily free config |
| `/whitelist add ID count` | Add user to whitelist (-1 = unlimited) |
| `/whitelist remove ID` | Remove user from whitelist |
| `/whitelist list` | List whitelisted users |
| `/addfree ID count` | Shortcut — grant free uses |

---

## 🧠 Auto-Detect

Just send any message and the bot figures it out:

- 📤 **Send a video link** (TikTok, YouTube, etc.) → starts download flow
- 🖼️ **Send a photo** → starts sticker creation flow

---

## 🗄️ Data

- **SQLite** (`data.db`) — tracks daily usage per user
- **whitelist.json** — stores whitelisted user IDs and their remaining free uses

Both files are created automatically on first run.

---

## 👤 Credits

- **Created by:** [Rakib Sojib](https://t.me/roki1277)
- **Contact:** [@roki1277](https://t.me/roki1277)
- **Made with:** Python, `python-telegram-bot`, `yt-dlp`, `Pillow`

---

## 📄 License

This project is open source. Feel free to modify and improve!
