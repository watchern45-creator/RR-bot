import os
import logging
import threading
from typing import Optional
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, ConversationHandler
)
from supabase import create_client, Client

# ---------------- CONFIG (from environment variables) ----------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]

# Comma-separated channel usernames (with @) or numeric chat IDs the user must join.
# Example env value: @mychannel1,@mychannel2
FORCE_JOIN_CHANNELS = [c.strip() for c in os.environ.get("FORCE_JOIN_CHANNELS", "").split(",") if c.strip()]

REFER_BONUS = 10  # ₹ per referral
MIN_WITHDRAWAL = 50  # ₹ minimum withdrawal amount

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Conversation states
ASK_UPI, ASK_AMOUNT = range(2)
ADMIN_MSG_USER, ADMIN_ADD_BALANCE_ID, ADMIN_ADD_BALANCE_AMOUNT, ADMIN_BROADCAST = range(2, 6)


# ---------------- DATABASE HELPERS ----------------

def display_name(user) -> str:
    """Safe display string for a Telegram user; falls back gracefully
    when the user has no @username set."""
    if user.username:
        return f"{user.first_name} (@{user.username})"
    return f"{user.first_name} (no username, id: {user.id})"


def get_user(user_id: int):
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None


def create_user(user_id: int, username: str, referred_by: Optional[int]):
    supabase.table("users").insert({
        "user_id": user_id,
        "username": username,
        "balance": 0,
        "referral_count": 0,
        "referred_by": referred_by,
    }).execute()


def add_balance(user_id: int, amount: float):
    user = get_user(user_id)
    if not user:
        return
    new_balance = float(user["balance"]) + amount
    supabase.table("users").update({"balance": new_balance}).eq("user_id", user_id).execute()


def set_balance(user_id: int, amount: float):
    supabase.table("users").update({"balance": amount}).eq("user_id", user_id).execute()


def increment_referral_count(user_id: int):
    user = get_user(user_id)
    if not user:
        return
    new_count = int(user["referral_count"]) + 1
    supabase.table("users").update({"referral_count": new_count}).eq("user_id", user_id).execute()


def total_user_count():
    res = supabase.table("users").select("user_id", count="exact").execute()
    return res.count


def all_user_ids():
    res = supabase.table("users").select("user_id").execute()
    return [r["user_id"] for r in res.data]


def save_withdrawal(user_id: int, username: str, upi_id: str, amount: float):
    supabase.table("withdrawals").insert({
        "user_id": user_id,
        "username": username,
        "upi_id": upi_id,
        "amount": amount,
        "status": "pending",
    }).execute()


# ---------------- FORCE JOIN CHECK ----------------

async def get_unjoined_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Returns list of channels (from FORCE_JOIN_CHANNELS) the user has NOT joined.
    Bot must be an admin in each channel for get_chat_member to work."""
    unjoined = []
    for channel in FORCE_JOIN_CHANNELS:
        try:
            member = await context.bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked"):
                unjoined.append(channel)
        except Exception as e:
            logger.error(f"Could not check membership for {channel}: {e}")
            # If bot can't check (not admin, wrong username, etc.), fail safe
            # by treating it as unjoined so the issue gets noticed.
            unjoined.append(channel)
    return unjoined


def join_channels_keyboard(unjoined: list):
    buttons = []
    for channel in unjoined:
        name = channel.lstrip("@")
        buttons.append([InlineKeyboardButton(f"➕ Join {name}", url=f"https://t.me/{name}")])
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(buttons)


async def send_join_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, unjoined: list, via_query=False):
    text = "🔒 To use this bot, please join our channel(s) first:"
    markup = join_channels_keyboard(unjoined)
    if via_query:
        await update.callback_query.message.reply_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


# ---------------- USER SIDE ----------------

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Refer", callback_data="refer")],
        [InlineKeyboardButton("💰 Withdrawal", callback_data="withdrawal")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    # Save any referral arg for later (used after join check passes)
    if args:
        context.user_data["pending_ref_arg"] = args[0]

    if FORCE_JOIN_CHANNELS:
        unjoined = await get_unjoined_channels(user.id, context)
        if unjoined:
            await send_join_prompt(update, context, unjoined)
            return

    await proceed_after_join_check(update, context, via_query=False)


async def proceed_after_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE, via_query: bool):
    user = update.effective_user
    args = context.args if not via_query else (
        [context.user_data["pending_ref_arg"]] if context.user_data.get("pending_ref_arg") else []
    )

    existing = get_user(user.id)
    if not existing:
        referred_by = None
        if args:
            try:
                ref_id = int(args[0])
                if ref_id != user.id and get_user(ref_id):
                    referred_by = ref_id
            except ValueError:
                pass

        create_user(user.id, user.username or user.first_name, referred_by)

        # Reward the referrer — only ever happens once, at signup time,
        # so one user can never be referred/rewarded twice.
        if referred_by:
            add_balance(referred_by, REFER_BONUS)
            increment_referral_count(referred_by)
            try:
                await context.bot.send_message(
                    referred_by,
                    f"🎉 You got a new referral! +₹{REFER_BONUS} added to your balance."
                )
            except Exception:
                pass

        existing = get_user(user.id)

    text = (
        f"👋 Welcome, {user.first_name}!\n\n"
        f"💰 Balance: ₹{existing['balance']}\n"
        f"👥 Referrals: {existing['referral_count']}\n\n"
        f"Refer friends and earn ₹{REFER_BONUS} per referral!"
    )
    if via_query:
        await update.callback_query.message.reply_text(text, reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "check_join":
        if FORCE_JOIN_CHANNELS:
            unjoined = await get_unjoined_channels(user_id, context)
            if unjoined:
                await query.message.reply_text(
                    "❌ You haven't joined all channels yet.",
                )
                await send_join_prompt(update, context, unjoined, via_query=True)
                return ConversationHandler.END
        await proceed_after_join_check(update, context, via_query=True)
        return ConversationHandler.END

    # Safety net: re-check join status before refer/withdrawal actions too,
    # in case someone left a channel after joining initially.
    if FORCE_JOIN_CHANNELS:
        unjoined = await get_unjoined_channels(user_id, context)
        if unjoined:
            await send_join_prompt(update, context, unjoined, via_query=True)
            return ConversationHandler.END

    user = get_user(user_id)

    if not user:
        await query.message.reply_text("Please send /start first.")
        return ConversationHandler.END

    if query.data == "refer":
        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start={user_id}"
        await query.message.reply_text(
            f"🔗 Your referral link:\n{link}\n\n"
            f"Share this link. When someone joins using it, you earn ₹{REFER_BONUS}."
        )

    elif query.data == "withdrawal":
        if float(user["balance"]) <= 0:
            await query.message.reply_text("❌ Your balance is ₹0. Refer friends to earn.")
            return ConversationHandler.END
        await query.message.reply_text("💳 Send your UPI ID:")
        return ASK_UPI

    return ConversationHandler.END


async def ask_upi_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["upi_id"] = update.message.text.strip()
    await update.message.reply_text("💵 Enter amount to withdraw:")
    return ASK_AMOUNT


async def ask_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return ASK_AMOUNT

    if amount <= 0:
        await update.message.reply_text("Amount must be greater than 0.")
        return ASK_AMOUNT

    if amount < MIN_WITHDRAWAL:
        await update.message.reply_text(
            f"❌ Minimum withdrawal amount is ₹{MIN_WITHDRAWAL}. Please enter a higher amount."
        )
        return ASK_AMOUNT

    if amount > float(user["balance"]):
        await update.message.reply_text(
            f"❌ Insufficient balance. Your balance is ₹{user['balance']}."
        )
        return ConversationHandler.END

    upi_id = context.user_data.get("upi_id")

    # Deduct balance immediately
    new_balance = float(user["balance"]) - amount
    set_balance(user_id, new_balance)

    save_withdrawal(user_id, update.effective_user.username or update.effective_user.first_name, upi_id, amount)

    await update.message.reply_text(
        f"✅ Withdrawal request submitted!\n"
        f"UPI ID: {upi_id}\nAmount: ₹{amount}\n\n"
        f"Remaining balance: ₹{new_balance}"
    )

    # Notify admins
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"💸 New Withdrawal Request\n\n"
                f"User: {display_name(update.effective_user)}\n"
                f"User ID: {user_id}\n"
                f"UPI ID: {upi_id}\n"
                f"Amount: ₹{amount}"
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ---------------- FORWARD ANY USER MESSAGE/MEDIA TO ADMIN ----------------

async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catches any text/photo/video from a normal user (not in a conversation
    step) and forwards it to all admins with sender info."""
    user = update.effective_user
    if is_admin(user.id):
        return
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"📩 Message from {display_name(user)}\nUser ID: {user.id}"
            )
            await update.message.forward(admin_id)
        except Exception as e:
            logger.error(f"Failed to forward to admin {admin_id}: {e}")


# ---------------- ADMIN SIDE ----------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    count = total_user_count()
    text = (
        f"🛠 Admin Panel\n\n"
        f"👥 Total Users: {count}\n\n"
        "Commands:\n"
        "/msguser <user_id> <message> - Send message to a user\n"
        "/addbalance <user_id> <amount> - Add balance to a user\n"
        "/broadcast <message> - Send message to all users\n"
    )
    await update.message.reply_text(text)


async def msg_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        target_id = int(context.args[0])
        message = " ".join(context.args[1:])
        if not message:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /msguser <user_id> <message>")
        return

    try:
        await context.bot.send_message(target_id, f"📩 Message from admin:\n\n{message}")
        await update.message.reply_text("✅ Message sent.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {e}")


async def add_balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        target_id = int(context.args[0])
        amount = float(context.args[1])
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /addbalance <user_id> <amount>")
        return

    user = get_user(target_id)
    if not user:
        await update.message.reply_text("❌ User not found.")
        return

    add_balance(target_id, amount)
    updated = get_user(target_id)
    await update.message.reply_text(f"✅ Balance updated. New balance: ₹{updated['balance']}")

    try:
        await context.bot.send_message(target_id, f"💰 Admin added ₹{amount} to your balance!")
    except Exception:
        pass


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    message = " ".join(context.args)
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    ids = all_user_ids()
    sent, failed = 0, 0
    for uid in ids:
        try:
            await context.bot.send_message(uid, f"📢 Announcement:\n\n{message}")
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"✅ Broadcast done. Sent: {sent}, Failed: {failed}")


# ---------------- KEEP-ALIVE WEB SERVER (for Render free tier) ----------------
# Render's free "Web Service" tier requires listening on a port and stays
# awake only while it receives HTTP traffic. This tiny Flask app gives the
# cron-job pinger something to hit, so Render treats the service as a web
# service and doesn't spin it down. The actual bot logic (polling Telegram)
# runs in a separate background thread untouched by this.
flask_app = Flask(__name__)


@flask_app.route("/")
def health_check():
    return "Bot is running.", 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)


# ---------------- MAIN ----------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    withdrawal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^(refer|withdrawal|check_join)$")],
        states={
            ASK_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_upi_received)],
            ASK_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_amount_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(withdrawal_conv)

    # Admin commands
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("msguser", msg_user))
    app.add_handler(CommandHandler("addbalance", add_balance_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Catch-all: any other text/photo/video from users -> forward to admin
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
        forward_to_admin
    ))

    logger.info("Bot started...")

    # Start the keep-alive web server in a background thread so it doesn't
    # block the bot's polling loop.
    threading.Thread(target=run_flask, daemon=True).start()

    app.run_polling()


if __name__ == "__main__":
    main()
