# Stripe CC Checker — Telegram Bot

Your existing Stripe checker logic, wrapped in a Telegram bot with a clean UX.
**Zero changes to the core checker code** — imported as-is.

---

## Quick Start (Railway Free Trial)

### 1. Get a bot token

Open Telegram → [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.

### 2. Deploy

Push this repo to GitHub, then on [Railway](https://railway.app):
- **New Project** → **Deploy from GitHub repo**
- Add environment variable: `BOT_TOKEN` = your token

That's it. Your bot is live.

### Optional: Webhook

If you want webhook mode (faster responses), also set:
```
WEBHOOK_URL = https://your-project.up.railway.app
```
Otherwise the bot uses polling — works fine.

---

## Commands

### ⚡ Fastest Path: Paste → Reply → Done

```
You:       4111111111111111|12|2026|123
           5500000000000004|01|2027|456
You:       /check          (replying to that message)
Bot:       ⚡ Checking 2 cards...
           ✅ Done! 1 approved.
```

No `/addcards` needed — reply with `/check` parses & runs immediately.

### Single Check — `/sh`

```
/sh 4111111111111111|12|2026|123
```
Instant result, no menus.

### Add Proxies — `/addproxy`

**Reply** to a message full of proxies:
```
http://user:pass@1.2.3.4:8080
1.2.3.4:3128:user:pass
socks5://1.2.3.4:1080
```
→ reply with `/addproxy` — loads all of them.

### Add Cards — `/addcards`

Same pattern — paste a list and reply with `/addcards` to load silently:
```
4111111111111111|12|2026|123
5500000000000004|01|2027|456
```

### Mass Check — `/check [N]`

Two modes:
1. **Reply mode** — reply to a card paste → parses cards and runs immediately
2. **Normal mode** — runs on cards you've loaded with `/addcards` or file upload

Concurrency: `/check` = 10, `/check 20` = 20, `/check 50` = 50.

### Results & Status

| Command | What it does |
|---|---|
| `/results` | Show all approved cards |
| `/status` | Cards / proxies / approved / running |
| `/clear` | Wipe your session |
| `/help` | Full usage reference |

### File Upload (Alternative)

Drop a `.txt` file in chat — the bot auto-detects if it's proxies or cards.

---

## Proxy Formats Accepted

```
http://user:pass@1.2.3.4:8080
socks5://1.2.3.4:1080
1.2.3.4:8080:user:pass
1.2.3.4:3128
```

---

## Project Structure

```
├── stripe_checker.py   ← Your exact code (imported, untouched)
├── stripe@multi.py     ← Original filename for reference
├── bot.py              ← Telegram bot wrapper
├── requirements.txt    ← Dependencies
├── railway.toml        ← Railway deploy config
├── .gitignore
└── README.md
```

## Notes

- **All data is in-memory.** Railway's free tier has an ephemeral filesystem — data resets on restart. Fine for session-based checking.
- The original `check_card()`, `mass_check()`, `load_proxies()`, `parse_proxy_line()` — all called **directly**, zero modifications.
- `/sh` results are also stored in your session if approved.
