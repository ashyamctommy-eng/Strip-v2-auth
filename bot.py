"""
Telegram Bot Wrapper for Stripe CC Checker
===========================================
Wraps the existing checker logic — zero changes to the core code.
Inline commands, paste proxies/cards directly in chat, reply with /check to run.
"""

import os
import sys
import io
import asyncio
import json
import logging
import tempfile
import random
import importlib.util
import aiohttp
from aiohttp import web
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Import the checker module — NO code changes, clean import
# ---------------------------------------------------------------------------
CHECKER_PATH = os.path.join(os.path.dirname(__file__), "stripe_checker.py")
spec = importlib.util.spec_from_file_location("checker", CHECKER_PATH)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

# ---------------------------------------------------------------------------
# Env config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ BOT_TOKEN not set!")
    sys.exit(1)

PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CREDIT = "Credits By:@Poriot_ke"
APPROVED_FILE = "approved_stripe.txt"

# ---------------------------------------------------------------------------
# In-memory sessions
# ---------------------------------------------------------------------------
user_sessions: dict = {}

def session(uid: int) -> dict:
    if uid not in user_sessions:
        user_sessions[uid] = {"proxies": [], "cards": [], "results": [], "running": False}
    return user_sessions[uid]


def save_approved_card(result: dict) -> None:
    """Append an approved card to approved_stripe.txt with credit line."""
    line = f"{result['cc']} - {result['response']} - {CREDIT}\n"
    with open(APPROVED_FILE, "a") as f:
        f.write(line)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def parse_card_lines(text: str) -> tuple[list[str], int]:
    """Extract valid cc|mm|yy|cvv lines from text. Returns (valid, skipped_count)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    valid = []
    skipped = 0
    for l in lines:
        parts = l.replace(" ", "").split("|")
        if len(parts) == 4:
            valid.append("|".join(parts))
        else:
            skipped += 1
    return valid, skipped

def parse_proxy_lines(text: str) -> list[str]:
    """Extract valid proxy URLs from text. Returns list of proxy URLs."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # If a single line has many space-separated entries
    if len(lines) == 1 and "://" not in lines[0] and lines[0].count(" ") > 2:
        lines = lines[0].split()
    proxies = []
    for l in lines:
        p = checker.parse_proxy_line(l)
        if p:
            proxies.append(p)
    return proxies


# ────────────────────────────────────────────────────────────────────────────
# Card generation helpers (Luhn algorithm)
# ────────────────────────────────────────────────────────────────────────────

def luhn_checksum(num_str: str) -> int:
    """Compute Luhn checksum digit for a partial card number."""
    digits = [int(d) for d in num_str]
    # Double every second digit from the right
    for i in range(len(digits) - 2, -1, -2):
        d = digits[i] * 2
        digits[i] = d if d < 10 else d - 9
    total = sum(digits)
    return (10 - (total % 10)) % 10

def generate_card_number(bin_prefix: str) -> str:
    """Generate a valid Luhn card number from a BIN prefix."""
    remaining = 16 - len(bin_prefix) - 1  # -1 for Luhn check digit
    middle = "".join(random.choices("0123456789", k=remaining))
    partial = bin_prefix + middle
    return partial + str(luhn_checksum(partial + "0"))

def generate_card_line(bin_prefix: str) -> str:
    """Generate one card line: cc|mm|yy|cvv with valid Luhn."""
    cc = generate_card_number(bin_prefix)
    mm = str(random.randint(1, 12)).zfill(2)
    yy = str(random.randint(datetime.now().year + 1, datetime.now().year + 5))
    cvv = str(random.randint(100, 999))
    return f"{cc}|{mm}|{yy}|{cvv}"


# ────────────────────────────────────────────────────────────────────────────
# /start
# ────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 **Stripe Checker Bot**\n\n"
        "**Commands**\n"
        "`/sh cc|mm|yy|cvv` — single check\n"
        "`/gen [BIN] [count]` — generate cards (max 500)\n"
        "`/bin [card or BIN]` — BIN details lookup\n"
        "`/addproxy` (reply) — load proxies\n"
        "`/addcards` (reply) — load cards\n"
        "`/check [N]` (reply) — run check on card list\n"
        "`/results` — list approved\n"
        "`/saved` — download **`approved_stripe.txt`**\n"
        "`/status` — session summary\n"
        "`/stats` — approval rate & performance\n"
        "`/clear` — wipe session\n\n"
        "💡 Paste cards → reply with **`/check`** → instant mass check.\n"
        f"━━━━━━━━━━━━━━━━\n{CREDIT}",
        parse_mode="Markdown",
    )


# ────────────────────────────────────────────────────────────────────────────
# /sh  —  single inline check
# ────────────────────────────────────────────────────────────────────────────
async def cmd_sh(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)

    if not ctx.args:
        await update.message.reply_text(
            "**Usage:** `/sh 4111111111111111|12|2026|123`", parse_mode="Markdown"
        )
        return

    raw = " ".join(ctx.args)
    parts = raw.replace(" ", "|").split("|")
    if len(parts) != 4:
        await update.message.reply_text(
            "**❌** Need exactly **`cc|month|year|cvv`** — 4 parts.", parse_mode="Markdown"
        )
        return

    cc, mm, yy, cvv = parts

    msg = await update.message.reply_text("**⏳** Checking...", parse_mode="Markdown")
    proxy = checker.random.choice(s["proxies"]) if s["proxies"] else None

    result = await checker.check_card(cc, mm, yy, cvv, proxy=proxy)

    if result["is_live"]:
        s["results"].append(result)
        save_approved_card(result)
        text = (
            f"**✅ APPROVED**\n\n"
            f"**💳** `{result['cc']}`\n"
            f"**📌** {result['response']}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{CREDIT}"
        )
    else:
        text = (
            f"**❌ DECLINED**\n\n"
            f"**💳** `{result['cc']}`\n"
            f"**📌** {result['response']}"
        )
    await msg.edit_text(text, parse_mode="Markdown")


# ────────────────────────────────────────────────────────────────────────────
# /gen [BIN] [count]  —  generate cards from a BIN (max 500)
# ────────────────────────────────────────────────────────────────────────────
async def cmd_gen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)

    if len(ctx.args) < 1:
        await update.message.reply_text(
            "**Usage:** `/gen 424242 50` — generate 50 cards with BIN 424242\n"
            "**Max:** 500 cards per run.",
            parse_mode="Markdown",
        )
        return

    bin_prefix = ctx.args[0].strip().replace(" ", "")
    # Allow up to 8 digits BIN
    if not bin_prefix.isdigit() or len(bin_prefix) < 6 or len(bin_prefix) > 8:
        await update.message.reply_text(
            "**❌** BIN must be **6–8 digits** only.", parse_mode="Markdown"
        )
        return

    count = 10  # default
    if len(ctx.args) >= 2:
        try:
            count = int(ctx.args[1])
        except ValueError:
            await update.message.reply_text(
                "**❌** Count must be a number (max 500).", parse_mode="Markdown"
            )
            return

    count = max(1, min(count, 500))

    msg = await update.message.reply_text(
        f"**⚙️ Generating {count}** cards with BIN **{bin_prefix}**...",
        parse_mode="Markdown",
    )

    generated = []
    for _ in range(count):
        generated.append(generate_card_line(bin_prefix))

    # Load into session
    s["cards"].extend(generated)

    # Show first 10 as preview
    preview = "\n".join(f"`{g}`" for g in generated[:10])
    text = (
        f"**✅ {count}** cards generated with BIN **{bin_prefix}**\n"
        f"**📊** Total cards in session: **{len(s['cards'])}**\n"
        f"**➡️** Reply **`/check`** to run.\n\n"
        f"**Preview (first 10):**\n{preview}"
    )
    if count > 10:
        text += f"\n... **+{count - 10}** more."

    await msg.edit_text(text, parse_mode="Markdown")


# ────────────────────────────────────────────────────────────────────────────
# /bin [card or BIN]  —  lookup BIN details
# ────────────────────────────────────────────────────────────────────────────
async def cmd_bin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text(
            "**Usage:** `/bin 424242` or `/bin 4111111111111111`",
            parse_mode="Markdown",
        )
        return

    raw = "".join(ctx.args).strip().replace(" ", "")
    # Extract first 6 digits (BIN)
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) < 6:
        await update.message.reply_text(
            "**❌** Need at least **6 digits** for a BIN lookup.",
            parse_mode="Markdown",
        )
        return

    bin_num = digits[:6]  # BIN is first 6 digits
    full_card = digits[:16] if len(digits) >= 16 else None

    msg = await update.message.reply_text(
        f"**🔍 Looking up BIN** `{bin_num}`...", parse_mode="Markdown"
    )

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            headers = {
                "Accept-Version": "3",
                "User-Agent": "Mozilla/5.0",
            }
            url = f"https://lookup.binlist.net/{bin_num}"
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    await msg.edit_text(
                        f"**❌** BIN lookup failed (HTTP {resp.status}). Try a different BIN.",
                        parse_mode="Markdown",
                    )
                    return
                data = await resp.json()

        # Format the response
        scheme = data.get("scheme", "N/A") or "N/A"
        brand = data.get("brand", "N/A") or "N/A"
        type_ = data.get("type", "N/A") or "N/A"
        prepaid = "**Yes**" if data.get("prepaid") else "No"
        country_name = (data.get("country") or {}).get("name", "N/A") or "N/A"
        country_code = (data.get("country") or {}).get("alpha2", "") or ""
        bank_name = (data.get("bank") or {}).get("name", "N/A") or "N/A"
        bank_url = (data.get("bank") or {}).get("url", "") or ""
        bank_phone = (data.get("bank") or {}).get("phone", "") or ""

        country_line = f"**🌍** Country: **{country_name}**"
        if country_code:
            country_line += f" (`{country_code}`)"

        parts = [
            f"**🏦 BIN Lookup** `{bin_num}`",
            "",
            f"**💳** Scheme: **{scheme}**",
            f"**🏷️** Brand: **{brand}**",
            f"**📂** Type: **{type_}**",
            f"**💵** Prepaid: {prepaid}",
            country_line,
            f"**🏛️** Bank: **{bank_name}**",
        ]
        if bank_url:
            parts.append(f"**🌐** URL: {bank_url}")
        if bank_phone:
            parts.append(f"**📞** Phone: `{bank_phone}`")

        if not data.get("bank"):
            parts.append("")
            parts.append("⚠️ No bank details available for this BIN.")

        parts.append("")
        parts.append(f"━━━━━━━━━━━━━━━━\n{CREDIT}")

        await msg.edit_text("\n".join(parts), parse_mode="Markdown")

    except asyncio.TimeoutError:
        await msg.edit_text(
            "**❌** BIN lookup timed out. Try again.", parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(
            f"**❌** Error: `{e}`", parse_mode="Markdown"
        )
        logger.exception("BIN lookup failed")


# ────────────────────────────────────────────────────────────────────────────
# /addproxy  —  reply to a proxy list to load them
# ────────────────────────────────────────────────────────────────────────────
async def cmd_addproxy(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)

    source_text = None
    if update.message.reply_to_message and update.message.reply_to_message.text:
        source_text = update.message.reply_to_message.text
    elif ctx.args:
        source_text = " ".join(ctx.args)
    else:
        await update.message.reply_text(
            "Paste proxies in a message and **reply** with `/addproxy`.\n"
            "Or: `/addproxy http://user:pass@1.2.3.4:8080`",
            parse_mode="Markdown",
        )
        return

    proxies = parse_proxy_lines(source_text)
    if not proxies:
        await update.message.reply_text(
            "**❌** No valid proxies found in that message.", parse_mode="Markdown"
        )
        return

    s["proxies"].extend(proxies)
    label = "proxies" if len(proxies) != 1 else "proxy"
    await update.message.reply_text(
        f"**🌐 {len(proxies)}** {label} loaded.\n"
        f"**📊** Total: **{len(s['proxies'])}** proxies in session.",
        parse_mode="Markdown",
    )


# ────────────────────────────────────────────────────────────────────────────
# /addcards  —  reply to a card list to load them
# ────────────────────────────────────────────────────────────────────────────
async def cmd_addcards(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)

    source_text = None
    if update.message.reply_to_message and update.message.reply_to_message.text:
        source_text = update.message.reply_to_message.text
    elif ctx.args:
        source_text = " ".join(ctx.args)
    else:
        await update.message.reply_text(
            "Paste cards in a message and **reply** with `/addcards`.\n"
            "Format: **`cc|month|year|cvv`** — one per line.",
            parse_mode="Markdown",
        )
        return

    valid, skipped = parse_card_lines(source_text)
    if not valid:
        await update.message.reply_text(
            "**❌** No valid cards found. Use format: **`cc|month|year|cvv`**",
            parse_mode="Markdown",
        )
        return

    s["cards"].extend(valid)
    label = "cards" if len(valid) != 1 else "card"
    parts = [f"**💳 {len(valid)}** {label} loaded."]
    if skipped:
        parts.append(f"**⚠️** Skipped **{skipped}** invalid line(s).")
    parts.append(f"**📊** Total: **{len(s['cards'])}** cards in session.")
    await update.message.reply_text("\n".join(parts), parse_mode="Markdown")


# ────────────────────────────────────────────────────────────────────────────
# File upload  —  auto-detect proxies or cards
# ────────────────────────────────────────────────────────────────────────────
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    doc = update.message.document

    if not doc.file_name or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("**❌** Upload a **`.txt`** file please.", parse_mode="Markdown")
        return

    file_obj = await doc.get_file()
    content = (await file_obj.download_as_bytearray()).decode("utf-8", errors="ignore")
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if not lines:
        await update.message.reply_text("**⚠️** Empty file.", parse_mode="Markdown")
        return

    # Heuristic: >50% lines matching "x|x|x|x" = card file
    card_pattern = sum(1 for l in lines if len(l.split("|")) == 4)
    is_cards = card_pattern > len(lines) * 0.5

    if is_cards:
        valid, skipped = parse_card_lines(content)
        if valid:
            s["cards"].extend(valid)
            label = "cards" if len(valid) != 1 else "card"
            parts = [f"**💳 {len(valid)}** {label} loaded from `{doc.file_name}`"]
            if skipped:
                parts.append(f"**⚠️** Skipped **{skipped}** line(s).")
            parts.append(f"**📊** Total: **{len(s['cards'])}** cards.\n**➡️** Reply **`/check`** to run.")
            await update.message.reply_text("\n".join(parts), parse_mode="Markdown")
        else:
            await update.message.reply_text("**❌** No valid cards found in file.", parse_mode="Markdown")
    else:
        proxies = parse_proxy_lines(content)
        if proxies:
            s["proxies"].extend(proxies)
            label = "proxies" if len(proxies) != 1 else "proxy"
            await update.message.reply_text(
                f"**🌐 {len(proxies)}** {label} loaded from `{doc.file_name}`\n"
                f"**📊** Total: **{len(s['proxies'])}** proxies.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("**❌** No valid proxies found in file.", parse_mode="Markdown")


# ────────────────────────────────────────────────────────────────────────────
# /check [concurrency]  —  run mass check
#
#   Reply mode:  reply to a card paste → parse & run immediately
#   Normal mode: run on session cards
# ────────────────────────────────────────────────────────────────────────────
async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)

    if s["running"]:
        await update.message.reply_text(
            "**⏳** Already running! Wait or use **`/clear`**.", parse_mode="Markdown"
        )
        return

    concurrency = 10
    if ctx.args:
        try:
            concurrency = max(1, min(int(ctx.args[0]), 100))
        except ValueError:
            pass

    # ── Try reply-mode first ────────────────────────────────────────────
    cards_to_check = []
    reply_msg = update.message.reply_to_message

    if reply_msg and reply_msg.text:
        cards_to_check, skipped = parse_card_lines(reply_msg.text)

    # ── Fall back to session cards ──────────────────────────────────────
    if not cards_to_check:
        cards_to_check = list(s["cards"])

    if not cards_to_check:
        await update.message.reply_text(
            "**❌** No cards to check.\n"
            "Paste cards → **reply** with `/check`.\n"
            "Or load cards with **`/addcards`** first.",
            parse_mode="Markdown",
        )
        return

    s["running"] = True
    s["results"] = []

    # Also add inline cards to session for later reference
    if reply_msg and reply_msg.text:
        s["cards"].extend(cards_to_check)

    msg = await update.message.reply_text(
        f"**⚡ Checking** {len(cards_to_check)} card{'s' if len(cards_to_check) != 1 else ''}\n"
        f"**🌐** {len(s['proxies'])} proxy{'ies' if len(s['proxies']) != 1 else ''} available\n"
        f"**⚙️** Concurrency: **{concurrency}**",
        parse_mode="Markdown",
    )

    # Write to temp file for mass_check
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
        tmp_path = tmp.name
        for c in cards_to_check:
            tmp.write(c + "\n")

    try:
        proxies = s["proxies"] if s["proxies"] else []
        results = await checker.mass_check(tmp_path, proxies=proxies, concurrency=concurrency)
        s["results"] = results

        approved = [r for r in results if r.get("is_live")]
        declined = sum(1 for r in results if not r.get("is_live"))

        # Save all approved to file
        for r in approved:
            save_approved_card(r)

        parts = [f"**✅ Done!**\n"]
        parts.append(f"**📋** Checked: **{len(results)}**")
        parts.append(f"**✅** Approved: **{len(approved)}**")
        parts.append(f"**❌** Declined: **{declined}**")

        if approved:
            parts.append("")
            parts.append("**Top hits:**")
            top = approved[:5]
            for r in top:
                parts.append(f"`{r['cc']}` — {r['response']}")
            if len(approved) > 5:
                parts.append(f"... **+{len(approved) - 5}** more. Use **`/results`** to see all.")
            parts.append("")
            parts.append(f"━━━━━━━━━━━━━━━━\n{CREDIT}")

        await msg.edit_text("\n".join(parts), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"**❌** Error: `{e}`", parse_mode="Markdown")
        logger.exception("Check failed")
    finally:
        os.unlink(tmp_path)
        s["running"] = False


# ────────────────────────────────────────────────────────────────────────────
# /results  —  show approved cards
# ────────────────────────────────────────────────────────────────────────────
async def cmd_results(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    approved = [r for r in s["results"] if r.get("is_live")]

    if not approved:
        await update.message.reply_text(
            "**❌** No approved cards yet. Run **`/check`**.", parse_mode="Markdown"
        )
        return

    lines = [f"{i}. `{r['cc']}` — {r['response']}" for i, r in enumerate(approved, 1)]
    full = "**✅ Approved Cards**\n\n" + "\n".join(lines) + f"\n\n━━━━━━━━━━━━━━━━\n{CREDIT}"

    for i in range(0, len(full), 3900):
        await update.message.reply_text(full[i:i+3900], parse_mode="Markdown")


# ────────────────────────────────────────────────────────────────────────────
# /status
# ────────────────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    approved = sum(1 for r in s["results"] if r.get("is_live"))
    checked = len(s["results"])

    await update.message.reply_text(
        f"**📊 Status**\n\n"
        f"**💳** Cards: **{len(s['cards'])}**\n"
        f"**🌐** Proxies: **{len(s['proxies'])}**\n"
        f"**⏳** Running: **{'Yes' if s['running'] else 'No'}**\n"
        f"**✅** Approved: **{approved}**\n"
        f"**❌** Checked: **{checked}**",
        parse_mode="Markdown",
    )


# ────────────────────────────────────────────────────────────────────────────
# /stats  —  approval rate & overall performance
# ────────────────────────────────────────────────────────────────────────────
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    s = session(uid)
    approved = sum(1 for r in s["results"] if r.get("is_live"))
    checked = len(s["results"])

    if checked == 0:
        await update.message.reply_text(
            "**📈** No checks yet. Run **`/check`** first.", parse_mode="Markdown"
        )
        return

    declined = checked - approved
    rate = (approved / checked) * 100

    # Build a simple progress bar
    bar_len = 12
    filled = round(rate / 100 * bar_len)
    bar = "🟩" * filled + "⬜" * (bar_len - filled)

    await update.message.reply_text(
        f"**📈 Stats**\n\n"
        f"**📋** Total checked: **{checked}**\n"
        f"**✅** Approved: **{approved}**\n"
        f"**❌** Declined: **{declined}**\n"
        f"**📊** Approval rate: **{rate:.1f}%**\n"
        f"{bar}  `{rate:.1f}%`\n\n"
        f"━━━━━━━━━━━━━━━━\n{CREDIT}",
        parse_mode="Markdown",
    )


# ────────────────────────────────────────────────────────────────────────────
# /clear
# ────────────────────────────────────────────────────────────────────────────
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    user_sessions[uid] = {"proxies": [], "cards": [], "results": [], "running": False}
    await update.message.reply_text("**🗑️** Session wiped.", parse_mode="Markdown")


# ────────────────────────────────────────────────────────────────────────────
# /help
# ────────────────────────────────────────────────────────────────────────────
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "**📖 Help**\n\n"
        "**Single Check**\n"
        "`/sh 4111111111111111|12|2026|123`\n\n"
        "**Generate Cards**\n"
        "`/gen 424242 50` — generate 50 Luhn-valid cards from BIN 424242\n"
        "Max **500** per run. Cards auto-load into session.\n\n"
        "**BIN Lookup**\n"
        "`/bin 424242` or `/bin 4111111111111111` — bank, country, type, etc.\n\n"
        "**Add Proxies**\n"
        "Paste proxies → **reply** with `/addproxy`\n"
        "Formats: `http://user:pass@host:port`, `host:port:user:pass`, ...\n\n"
        "**Add Cards**\n"
        "Paste cards → **reply** with `/addcards`\n"
        "Format: **`cc|month|year|cvv`** — one per line\n\n"
        "**Mass Check**\n"
        "Paste cards → **reply** with **`/check [N]`** — runs immediately\n"
        "Or: load cards first, then `/check` without reply\n\n"
        "**File Upload**\n"
        "Drop `.txt` — auto-detected as cards or proxies\n\n"
        "**Other**\n"
        "`/status` `/stats` `/results` `/saved` `/clear`",
        parse_mode="Markdown",
    )


# ────────────────────────────────────────────────────────────────────────────
# /saved  —  download the approved_stripe.txt file
# ────────────────────────────────────────────────────────────────────────────
async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload the approved_stripe.txt file to the user."""
    if not os.path.exists(APPROVED_FILE):
        await update.message.reply_text(
            "**❌** No approved cards saved yet. Run **`/check`** first.",
            parse_mode="Markdown",
        )
        return

    with open(APPROVED_FILE, "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=APPROVED_FILE,
            caption=f"✅ **Saved approved cards** — {CREDIT}",
            parse_mode="Markdown",
        )


# ────────────────────────────────────────────────────────────────────────────
# Error handler
# ────────────────────────────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception: %s", ctx.error)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def start_health_server():
    """Run a minimal HTTP server so Railway health checks always pass."""
    from aiohttp import web

    async def health(request):
        return web.Response(text="ok")

    health_app = web.Application()
    health_app.router.add_get("/{tail:.*}", health)
    return health_app
async def async_main() -> None:
    """Async entrypoint — starts health server first, then bot."""
    bot_app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()

    bot_app.add_handler(CommandHandler("start", cmd_start))
    bot_app.add_handler(CommandHandler("help", cmd_help))
    bot_app.add_handler(CommandHandler("sh", cmd_sh))
    bot_app.add_handler(CommandHandler("gen", cmd_gen))
    bot_app.add_handler(CommandHandler("bin", cmd_bin))
    bot_app.add_handler(CommandHandler("addproxy", cmd_addproxy))
    bot_app.add_handler(CommandHandler("addcards", cmd_addcards))
    bot_app.add_handler(CommandHandler("check", cmd_check))
    bot_app.add_handler(CommandHandler("results", cmd_results))
    bot_app.add_handler(CommandHandler("status", cmd_status))
    bot_app.add_handler(CommandHandler("stats", cmd_stats))
    bot_app.add_handler(CommandHandler("saved", cmd_saved))
    bot_app.add_handler(CommandHandler("clear", cmd_clear))
    bot_app.add_handler(MessageHandler(filters.Document.TEXT, handle_document))
    bot_app.add_error_handler(error_handler)

    # ── Start HTTP server IMMEDIATELY so Railway health check passes ───
    if WEBHOOK_URL:
        logger.info(f"✅ Webhook mode → {WEBHOOK_URL}")
        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
        )
        await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")
    else:
        # Start health server BEFORE any blocking calls
        health_app = start_health_server()
        runner = web.AppRunner(health_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"✅ Health check server listening on 0.0.0.0:{PORT}")

        await bot_app.initialize()
        await bot_app.start()
        await bot_app.updater.start_polling()

    # ── Now safe to make Telegram API calls ────────────────────────────
    await bot_app.bot.delete_webhook(drop_pending_updates=True)
    logger.info("🧹 Cleared any stale webhook from previous deployments")

    # Keep alive
    try:
        await asyncio.Event().wait()
    finally:
        await bot_app.stop()
        if not WEBHOOK_URL:
            await runner.cleanup()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
