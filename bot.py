import os, logging, asyncio, requests, pandas as pd
from datetime import time as dtime
from zoneinfo import ZoneInfo
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ChatMemberHandler, ContextTypes, filters
)

# -------- settings from environment (DO NOT hardcode secrets) --------
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
NEWSAPI_KEY          = os.getenv("NEWSAPI_KEY")
TZ_NAME              = os.getenv("TZ_NAME", "Africa/Lagos")  # change via env var if you like
TZ = ZoneInfo(TZ_NAME)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN env var")

# -------- logging --------
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("wyseforex-bot")

# -------- constants & helpers --------
HOUSE_RULES = (
    "📌 *WyseForex House Rules*\n"
    "1️⃣ Be respectful.\n"
    "2️⃣ No spam/scams.\n"
    "3️⃣ Stay on topic (Forex-related).\n"
    "4️⃣ Help each other grow.\n"
    "5️⃣ DYOR. No financial advice."
)

MAJOR_PAIRS = [("EUR","USD"),("GBP","USD"),("USD","JPY"),("USD","CHF"),("AUD","USD"),("USD","CAD"),("NZD","USD")]

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean().replace(0, 1e-9)
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def fetch_fx_daily(base: str, quote: str) -> pd.DataFrame:
    url = ("https://www.alphavantage.co/query"
           f"?function=FX_DAILY&from_symbol={base}&to_symbol={quote}"
           f"&apikey={ALPHAVANTAGE_API_KEY}&outputsize=compact")
    r = requests.get(url, timeout=30); r.raise_for_status()
    data = r.json()
    if "Note" in data or "Error Message" in data:
        raise RuntimeError(data.get("Note") or data.get("Error Message") or "API issue")
    ts = data.get("Time Series FX (Daily)")
    if not ts:
        raise RuntimeError("No time series returned.")
    df = (pd.DataFrame(ts).T
          .rename(columns={"1. open":"open","2. high":"high","3. low":"low","4. close":"close"})
          .astype(float).sort_index())
    df.index = pd.to_datetime(df.index)
    return df

def analyze_pair(base: str, quote: str) -> dict:
    df = fetch_fx_daily(base, quote)
    close = df["close"].copy()
    df["SMA50"]  = close.rolling(50).mean()
    df["SMA200"] = close.rolling(200).mean()
    df["RSI14"]  = rsi(close, 14)
    df["MOM5"]   = close.pct_change(5) * 100
    df = df.dropna()
    latest, prev = df.iloc[-1], df.iloc[-2]

    signals = []
    if latest.SMA50 > latest.SMA200 and prev.SMA50 <= prev.SMA200: signals.append("Golden cross (bullish)")
    if latest.SMA50 < latest.SMA200 and prev.SMA50 >= prev.SMA200: signals.append("Death cross (bearish)")

    rsi_state = "Overbought" if latest.RSI14 >= 70 else "Oversold" if latest.RSI14 <= 30 else "Neutral"
    tilt = "⬆️ Bullish tilt" if latest.SMA50 > latest.SMA200 else "⬇️ Bearish tilt" if latest.SMA50 < latest.SMA200 else "➡️ Sideways"

    return {
        "pair": f"{base}/{quote}",
        "price": float(latest.close),
        "rsi": float(latest.RSI14),
        "rsi_state": rsi_state,
        "sma50": float(latest.SMA50),
        "sma200": float(latest.SMA200),
        "momentum": f"{latest.MOM5:+.2f}%",
        "tilt": tilt,
        "signals": "; ".join(signals) if signals else "—",
        "asof": df.index[-1].strftime("%Y-%m-%d"),
    }

def format_trend_summary(items: list) -> str:
    lines = ["📈 *Forex Trend Analysis*", "_Source: Alpha Vantage (daily closes)_", ""]
    for a in items:
        lines.append(
            f"*{a['pair']}* @ `{a['price']}` (as of {a['asof']})\n"
            f"• {a['tilt']} | RSI14 `{a['rsi']:.1f}` ({a['rsi_state']})\n"
            f"• 5d momentum: {a['momentum']}\n"
            f"• SMA50 `{a['sma50']:.5f}` • SMA200 `{a['sma200']:.5f}`\n"
            f"• Signals: {a['signals']}\n"
        )
    return "\n".join(lines)

def get_forex_news(limit: int = 6) -> list:
    if not NEWSAPI_KEY:
        return []
    url = ("https://newsapi.org/v2/everything?"
           "q=forex%20OR%20EURUSD%20OR%20GBPUSD%20OR%20USDJPY%20OR%20USDCHF%20OR%20USDCAD%20OR%20AUDUSD%20OR%20NZDUSD&"
           "language=en&sortBy=publishedAt&pageSize=10&apiKey=" + NEWSAPI_KEY)
    r = requests.get(url, timeout=30); r.raise_for_status()
    data = r.json()
    return [f"• {a.get('title','(no title)')}" for a in data.get("articles", [])[:limit]]

# -------- handlers --------
def _is_join_event(update: Update) -> bool:
    if not update.chat_member: return False
    old_status = update.chat_member.old_chat_member.status
    new_status = update.chat_member.new_chat_member.status
    return (old_status in ("left","kicked")) and (new_status in ("member","administrator","creator"))

async def welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_join_event(update): return
    user = update.chat_member.new_chat_member.user
    name = user.first_name or "Trader"
    await context.bot.send_message(update.chat_member.chat.id, f"👋 Welcome, *{name}*!\n\n{HOUSE_RULES}", parse_mode="Markdown")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    t = update.message.text.lower()
    if "hello" in t:
        await update.message.reply_text("👋 Hello! Welcome to WyseForex!")
    elif "help" in t:
        await update.message.reply_text("Try /forexnews, /trends, or /trend EUR USD")
    else:
        await update.message.reply_text("I’m here to help! /forexnews • /trends • /trend EUR USD")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! I’m your WyseForex bot 🤖\n"
        "• /rules – house rules\n"
        "• /forexnews – latest headlines\n"
        "• /trends – snapshot of majors\n"
        "• /trend EUR USD – analyze any pair\n"
        "• /schedule_digest HH:MM – daily news+trends (your chat)\n"
        "• /cancel_digest – stop the daily digest"
    )

async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HOUSE_RULES, parse_mode="Markdown")

async def forex_news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        heads = get_forex_news()
        if not heads:
            await update.message.reply_text("⚠️ No forex news now or NEWSAPI_KEY missing.")
            return
        await update.message.reply_text("📰 *Latest Forex Headlines:*\n\n" + "\n".join(heads), parse_mode="Markdown")
    except Exception as e:
        log.exception("news fail: %s", e)
        await update.message.reply_text("⚠️ Couldn’t fetch Forex news.")

async def trends_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out, errs = [], []
    for base, quote in MAJOR_PAIRS:
        try:
            out.append(analyze_pair(base, quote))
            await asyncio.sleep(12)  # Alpha Vantage free rate limit
        except Exception as e:
            errs.append(f"{base}/{quote}: {e}")
    if out:
        await update.message.reply_text(format_trend_summary(out), parse_mode="Markdown")
    if errs:
        await update.message.reply_text("Some pairs failed:\n" + "\n".join(errs[:5]))

async def trend_one_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) == 1 and len(context.args[0]) == 6:  # e.g., EURUSD
        base, quote = context.args[0][:3].upper(), context.args[0][3:].upper()
    elif len(context.args) >= 2:
        base, quote = context.args[0].upper(), context.args[1].upper()
    else:
        await update.message.reply_text("Usage: /trend EUR USD  or  /trend EURUSD")
        return
    try:
        a = analyze_pair(base, quote)
        await update.message.reply_text(format_trend_summary([a]), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}")

async def digest_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        heads = get_forex_news(limit=5)
        news_text = "📰 *Daily Forex Headlines:*\n\n" + ("\n".join(heads) if heads else "No headlines.")
        await context.bot.send_message(chat_id, news_text, parse_mode="Markdown")

        out = []
        for base, quote in MAJOR_PAIRS[:4]:
            try:
                out.append(analyze_pair(base, quote))
                await asyncio.sleep(12)
            except Exception as e:
                log.warning("digest pair fail %s/%s: %s", base, quote, e)
        trends_text = format_trend_summary(out) if out else "No trend data."
        await context.bot.send_message(chat_id, trends_text, parse_mode="Markdown")
    except Exception as e:
        log.exception("digest fail: %s", e)
        await context.bot.send_message(chat_id, "⚠️ Daily digest failed.")

async def schedule_digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1 or ":" not in context.args[0]:
        await update.message.reply_text("Usage: /schedule_digest HH:MM (24h, your local time)")
        return
    try:
        hh, mm = map(int, context.args[0].split(":"))
        assert 0 <= hh < 24 and 0 <= mm < 60
    except Exception:
        await update.message.reply_text("Invalid time. Example: /schedule_digest 08:30")
        return
    chat_id = update.effective_chat.id
    name = f"digest_{chat_id}"
    for job in context.job_queue.get_jobs_by_name(name): job.schedule_removal()
    context.job_queue.run_daily(digest_job, time=dtime(hour=hh, minute=mm, tzinfo=TZ), name=name, chat_id=chat_id)
    await update.message.reply_text(f"✅ Daily digest set for {hh:02d}:{mm:02d} ({TZ_NAME}).")

async def cancel_digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    name = f"digest_{chat_id}"
    jobs = context.job_queue.get_jobs_by_name(name)
    if not jobs:
        await update.message.reply_text("No digest job set.")
        return
    for j in jobs: j.schedule_removal()
    await update.message.reply_text("🛑 Daily digest canceled.")

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(ChatMemberHandler(welcome, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("forexnews", forex_news_cmd))
    app.add_handler(CommandHandler("trends", trends_cmd))
    app.add_handler(CommandHandler("trend", trend_one_cmd))
    app.add_handler(CommandHandler("schedule_digest", schedule_digest_cmd))
    app.add_handler(CommandHandler("cancel_digest", cancel_digest_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
