
import os
import csv
import asyncio
import requests
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

BASE = "https://api.toobit.com"
SYMBOL = "SOL-SWAP-USDT"

HISTORY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "signals.csv"
)

CSV_FIELDS = [
    "time", "price", "direction", "bias",
    "long", "short", "rsi", "volume",
    "atr", "sl", "tp1", "tp2", "tp3"
]
CHAT_IDS = set()
LAST_ALERT_STATUS = {}

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["📊 تحلیل بازار SOL"],
        ["💰 قیمت لحظه‌ای", "📜 پنج تحلیل آخر"],
        ["ℹ️ راهنما"]
    ],
    resize_keyboard=True,
    is_persistent=True
)

def save_history(d):
    file_exists = os.path.exists(HISTORY_FILE)

    row = {
        "time": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "price": d["price"],
        "direction": d["direction"],
        "bias": d["bias"],
        "long": d["long"],
        "short": d["short"],
        "rsi": d["rsi"],
        "volume": d["volume"],
        "atr": d["atr"],
        "sl": d.get("sl", ""),
        "tp1": d.get("tp1", ""),
        "tp2": d.get("tp2", ""),
        "tp3": d.get("tp3", "")
    }

    with open(
        HISTORY_FILE,
        "a",
        newline="",
        encoding="utf-8-sig"
    ) as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)

        if not file_exists or os.path.getsize(HISTORY_FILE) == 0:
            writer.writeheader()

        writer.writerow(row)


def get_candles(interval):
    r = requests.get(
        BASE + "/quote/v1/klines",
        params={"symbol": SYMBOL, "interval": interval, "limit": 150},
        timeout=15
    )
    r.raise_for_status()
    data = r.json()

    if not isinstance(data, list) or len(data) < 60:
        raise ValueError("داده کندل کافی نیست")

    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close",
        "volume", "ignore", "quote_volume", "trades",
        "buy_volume", "buy_quote"
    ])

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    return df.iloc[:-1].copy().reset_index(drop=True)


def indicators(df):
    close = df["close"]
    high = df["high"]
    low = df["low"]

    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(
        alpha=1/14, adjust=False
    ).mean()
    loss = (-delta.clip(upper=0)).ewm(
        alpha=1/14, adjust=False
    ).mean()

    rs = gain / loss.replace(0, float("nan"))
    df["rsi"] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()

    df["macd"] = ema12 - ema26
    df["signal"] = df["macd"].ewm(
        span=9, adjust=False
    ).mean()

    previous = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - previous).abs(),
        (low - previous).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.rolling(14).mean()

    return df


def analyze_market():
    frames = {
        "5m": indicators(get_candles("5m")),
        "15m": indicators(get_candles("15m")),
        "1h": indicators(get_candles("1h"))
    }

    a = frames["5m"].iloc[-1]
    b = frames["15m"].iloc[-1]
    c = frames["1h"].iloc[-1]

    price_response = requests.get(
        BASE + "/quote/v1/contract/ticker/price",
        params={"symbol": SYMBOL},
        timeout=15
    )
    price_response.raise_for_status()
    ticker = price_response.json()

    if not ticker:
        raise ValueError("قیمت بازار دریافت نشد")

    price = float(ticker[0]["p"])

    long_score = 0
    short_score = 0

    for x in [b, c]:
        if x["ema20"] > x["ema50"]:
            long_score += 1
        elif x["ema20"] < x["ema50"]:
            short_score += 1

    if a["ema20"] > a["ema50"]:
        long_score += 1
    elif a["ema20"] < a["ema50"]:
        short_score += 1

    if a["macd"] > a["signal"]:
        long_score += 1
    elif a["macd"] < a["signal"]:
        short_score += 1

    if 50 < a["rsi"] < 70:
        long_score += 1
    elif 30 < a["rsi"] < 50:
        short_score += 1

    avg_volume = frames["5m"]["volume"].iloc[-21:-1].mean()
    volume_ok = a["volume"] > avg_volume * 1.2

    atr = float(a["atr"])

    if atr <= 0 or pd.isna(atr):
        raise ValueError("محاسبه ATR نامعتبر است")

    direction = "NO TRADE"
    bias = "NEUTRAL"

    if long_score >= 4 and long_score > short_score:
        bias = "LONG"
        if volume_ok:
            direction = "LONG"
        else:
            direction = "WAIT_VOLUME"

    elif short_score >= 4 and short_score > long_score:
        bias = "SHORT"
        if volume_ok:
            direction = "SHORT"
        else:
            direction = "WAIT_VOLUME"

    result = {
        "price": price,
        "direction": direction,
        "bias": bias,
        "long": long_score,
        "short": short_score,
        "rsi": float(a["rsi"]),
        "atr": atr,
        "volume": bool(volume_ok)
    }

    if direction == "LONG":
        result["sl"] = price - 1.5 * atr
        result["tp1"] = price + 1.5 * atr
        result["tp2"] = price + 3 * atr
        result["tp3"] = price + 4.5 * atr

    elif direction == "SHORT":
        result["sl"] = price + 1.5 * atr
        result["tp1"] = price - 1.5 * atr
        result["tp2"] = price - 3 * atr
        result["tp3"] = price - 4.5 * atr

    save_history(result)

    return result


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)

    await update.message.reply_text(
        "سلام! ربات تحلیل فیوچرز SOL فعال است.\n\n"
        "از دکمه‌های زیر برای استفاده از ربات انتخاب کن.\n"
        "دستورهای متنی قبلی هم همچنان فعال هستند.",
        reply_markup=MAIN_MENU
    )

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        r = requests.get(
            BASE + "/quote/v1/contract/ticker/price",
            params={"symbol": SYMBOL},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()

        await update.message.reply_text(
            f"قیمت SOL Futures: {data[0]['p']} USDT"
        )

    except Exception as e:
        print("Price error:", repr(e))
        await update.message.reply_text("خطا در دریافت قیمت")


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CHAT_IDS.add(update.effective_chat.id)

    await update.message.reply_text("در حال بررسی بازار SOL ...")

    try:
        d = analyze_market()

        msg = (
            "📊 تحلیل فیوچرز SOL\n"
            "━━━━━━━━━━━━━━\n"
            f"قیمت: {d['price']:.4f} USDT\n"
            "تایم‌فریم: 5m / 15m / 1h\n"
            f"RSI: {d['rsi']:.2f}\n"
            f"امتیاز لانگ: {d['long']}/5\n"
            f"امتیاز شورت: {d['short']}/5\n"
            f"تأیید حجم: {'بله' if d['volume'] else 'خیر'}\n"
            "━━━━━━━━━━━━━━\n"
        )

        if d["direction"] == "WAIT_VOLUME":
            msg += (
                f"👀 تمایل بازار: {d['bias']}\n"
                "⏳ منتظر تأیید حجم هستیم.\n"
                "فعلاً سیگنال ورود صادر نشده."
            )

        elif d["direction"] == "NO TRADE":
            msg += (
                "⏸ وضعیت: عدم ورود\n"
                "شرایط کافی برای سیگنال معتبر وجود ندارد."
            )

        else:
            msg += (
                f"🚦 سیگنال آزمایشی: {d['direction']}\n"
                f"ورود مرجع: {d['price']:.4f}\n"
                f"حد ضرر: {d['sl']:.4f}\n"
                f"هدف اول: {d['tp1']:.4f}\n"
                f"هدف دوم: {d['tp2']:.4f}\n"
                f"هدف سوم: {d['tp3']:.4f}\n"
                "━━━━━━━━━━━━━━\n"
                "⚠️ بدون اجرای سفارش واقعی"
            )

        await update.message.reply_text(msg)

    except Exception as e:
        print("Analysis error:", repr(e))
        await update.message.reply_text(
            "خطا در تحلیل بازار. جزئیات را در CMD بررسی کن."
        )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not os.path.exists(HISTORY_FILE):
            await update.message.reply_text(
                "هنوز تاریخچه‌ای ثبت نشده است. ابتدا /analyze را اجرا کن."
            )
            return

        with open(
            HISTORY_FILE,
            "r",
            newline="",
            encoding="utf-8-sig"
        ) as f:
            rows = list(csv.DictReader(f))

        if not rows:
            await update.message.reply_text("تاریخچه خالی است.")
            return

        msg = "📚 پنج تحلیل آخر SOL\n━━━━━━━━━━━━━━\n"

        for row in rows[-5:][::-1]:
            if row["direction"] == "LONG":
                status = "سیگنال لانگ"
            elif row["direction"] == "SHORT":
                status = "سیگنال شورت"
            elif row["direction"] == "WAIT_VOLUME":
                status = f"انتظار حجم ({row['bias']})"
            else:
                status = "عدم ورود"

            msg += (
                f"\n🕒 {row['time']}\n"
                f"قیمت: {float(row['price']):.4f}\n"
                f"وضعیت: {status}\n"
                f"لانگ: {row['long']}/5 | شورت: {row['short']}/5\n"
                f"RSI: {float(row['rsi']):.2f}\n"
                "──────────────\n"
            )

        await update.message.reply_text(msg)

    except Exception as e:
        print("History error:", repr(e))
        await update.message.reply_text("خطا در خواندن تاریخچه.")


async def automatic_check(context: ContextTypes.DEFAULT_TYPE):
    try:
        d = await asyncio.to_thread(analyze_market)

        if d["direction"] == "WAIT_VOLUME":
            status = "WAIT_" + d["bias"]
        elif d["direction"] in ("LONG", "SHORT"):
            status = d["direction"]
        else:
            status = "NO TRADE"

        for chat_id in list(CHAT_IDS):
            previous = LAST_ALERT_STATUS.get(chat_id)

            if status == previous:
                continue

            LAST_ALERT_STATUS[chat_id] = status

            if status == "WAIT_LONG":
                msg = (
                    "👀 هشدار پایش خودکار SOL\n"
                    "تمایل بازار: لانگ\n"
                    "⏳ منتظر تأیید حجم هستیم.\n"
                    "فعلاً ورود نکن."
                )
            elif status == "WAIT_SHORT":
                msg = (
                    "👀 هشدار پایش خودکار SOL\n"
                    "تمایل بازار: شورت\n"
                    "⏳ منتظر تأیید حجم هستیم.\n"
                    "فعلاً ورود نکن."
                )
            elif status in ("LONG", "SHORT"):
                msg = (
                    f"🚨 سیگنال آزمایشی {status}\n"
                    f"قیمت مرجع: {d['price']:.4f}\n"
                    f"حد ضرر: {d['sl']:.4f}\n"
                    f"هدف اول: {d['tp1']:.4f}\n"
                    f"هدف دوم: {d['tp2']:.4f}\n"
                    f"هدف سوم: {d['tp3']:.4f}\n"
                    "⚠️ بدون اجرای سفارش واقعی"
                )
            else:
                continue

            await context.bot.send_message(
                chat_id=chat_id,
                text=msg
            )

    except Exception as e:
        print("Automatic check error:", repr(e))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "راهنمای ربات تحلیل فیوچرز SOL\n\n"
        "📊 تحلیل بازار SOL: بررسی چندتایم‌فریمی بازار\n"
        "💰 قیمت لحظه‌ای: دریافت قیمت SOL از Toobit\n"
        "📜 پنج تحلیل آخر: نمایش تاریخچه تحلیل‌ها\n\n"
        "دستورهای متنی: /price ، /analyze ، /history ، /help",
        reply_markup=MAIN_MENU
    )


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📊 تحلیل بازار SOL":
        await analyze(update, context)
    elif text == "💰 قیمت لحظه‌ای":
        await price(update, context)
    elif text == "📜 پنج تحلیل آخر":
        await history(update, context)
    elif text == "ℹ️ راهنما":
        await help_command(update, context)


def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN تنظیم نشده است")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    # بررسی خودکار هر ۵ دقیقه
    app.job_queue.run_repeating(
        automatic_check,
        interval=300,
        first=10
    )

    print("SOL Futures Analysis Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()