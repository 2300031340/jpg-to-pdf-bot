import os
import time
import threading
import asyncio
import logging
from PIL import Image
from flask import Flask, request
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)

# --- Globals ---
user_sessions = {}
ASK_NAME = 1
SESSION_TIMEOUT = 600  # 10 minutes

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
telegram_app = None  # will hold our ApplicationBuilder instance


# --- Routes ---
@app.route('/')
def home():
    return "🤖 Bot is live with webhook!"


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Synchronous endpoint: collect JSON, build Update, then schedule it
    on the bot's asyncio loop without awaiting here.
    """
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)

        # Schedule the processing of this update in bot's loop:
        asyncio.get_event_loop().create_task(telegram_app.process_update(update))
        return "ok"
    except Exception as e:
        logging.exception("Error in /webhook handler:")
        return "error", 500


# --- Helpers ---
def is_image_file(filename):
    return filename.lower().endswith((".jpg", ".jpeg", ".png"))


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Send JPG or PNG images (as photos or documents). Type 'pdf' when done."
    )


async def handle_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    session = user_sessions.setdefault(user_id, {"images": [], "last_active": current_time})

    # If session timed out, clear previous images
    if current_time - session["last_active"] > SESSION_TIMEOUT:
        session["images"].clear()

    file_path = None

    # Photo vs Document
    if update.message.photo:
        photo = await update.message.photo[-1].get_file()
        file_path = f"{user_id}_{photo.file_id}.jpg"
        await photo.download_to_drive(file_path)

    elif update.message.document:
        doc = update.message.document
        if is_image_file(doc.file_name):
            file_path = f"{user_id}_{doc.file_unique_id}_{doc.file_name}"
            doc_file = await doc.get_file()
            await doc_file.download_to_drive(file_path)
        else:
            await update.message.reply_text("❌ Unsupported file type. Only JPG and PNG allowed.")
            return

    if file_path:
        session["images"].append(file_path)
        session["last_active"] = current_time
        await update.message.reply_text("📷 Image saved.")
    else:
        await update.message.reply_text("❌ No valid image found.")


async def handle_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    session = user_sessions.get(user_id)

    if not session or not session["images"]:
        await update.message.reply_text("❌ No images found. Please send some first.")
        return ConversationHandler.END

    if current_time - session["last_active"] > SESSION_TIMEOUT:
        session["images"].clear()
        session["last_active"] = current_time
        await update.message.reply_text("⌛ Your previous session expired. Please send images again.")
        return ConversationHandler.END

    await update.message.reply_text("📝 What should the PDF be called?")
    return ASK_NAME


async def receive_pdf_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.message.text.strip()
    session = user_sessions.get(user_id, {})

    images = []
    for path in session.get("images", []):
        try:
            images.append(Image.open(path).convert("RGB"))
        except Exception as e:
            logging.warning(f"Failed to open image {path}: {e}")

    if not images:
        await update.message.reply_text("❌ No valid images to convert.")
        return ConversationHandler.END

    pdf_path = f"{name}.pdf"
    images[0].save(pdf_path, save_all=True, append_images=images[1:])

    await update.message.reply_document(InputFile(pdf_path))

    # Cleanup files
    try:
        os.remove(pdf_path)
        for img_path in session["images"]:
            os.remove(img_path)
    except Exception as e:
        logging.warning(f"Cleanup error: {e}")

    # Reset session
    user_sessions[user_id] = {"images": [], "last_active": time.time()}
    return ConversationHandler.END


# --- Bot Runner (async) ---
async def run_bot():
    global telegram_app
    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")

    if not TOKEN or not WEBHOOK_URL:
        raise RuntimeError("Missing BOT_TOKEN or WEBHOOK_URL environment variable")

    # Build the bot application
    telegram_app = ApplicationBuilder().token(TOKEN).build()

    # Conversation handler for "pdf" → ask filename → create PDF
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("(?i)pdf"), handle_trigger)],
        states={ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pdf_name)]},
        fallbacks=[]
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_images))
    telegram_app.add_handler(conv_handler)

    logging.info(f"Setting webhook to {WEBHOOK_URL}")
    webhook_ok = await telegram_app.bot.set_webhook(url=WEBHOOK_URL)
    logging.info(f"Webhook set: {webhook_ok}")

    await telegram_app.initialize()
    await telegram_app.start()
    logging.info("Bot started with webhook mode.")
    await telegram_app.updater.idle()


# --- Flask Runner (sync) ---
def run_flask():
    port = int(os.environ["PORT"])
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    # 1) Start Flask in its own thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # 2) Run the bot's asyncio loop
    asyncio.run(run_bot())
