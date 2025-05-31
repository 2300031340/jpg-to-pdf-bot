import os
import time
import threading
import logging
import asyncio
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

app = Flask(__name__)
telegram_app = None  # Global telegram app reference
loop = None  # Global event loop reference

logging.basicConfig(level=logging.INFO)


# --- Routes ---
@app.route('/')
def home():
    return "🤖 Bot is live with webhook!"


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, telegram_app.bot)
    
    # Run the update processing in the event loop
    asyncio.run_coroutine_threadsafe(
        telegram_app.process_update(update),
        loop
    )
    return "ok"


# --- Helpers ---
def is_image_file(filename):
    return filename.lower().endswith((".jpg", ".jpeg", ".png"))


# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send JPG or PNG images (as photos or documents). Type 'pdf' when done.")


async def handle_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    session = user_sessions.setdefault(user_id, {"images": [], "last_active": current_time})

    if current_time - session["last_active"] > SESSION_TIMEOUT:
        session["images"].clear()

    file_path = None

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
            logging.warning(f"Failed to open image: {path} - {e}")

    if not images:
        await update.message.reply_text("❌ No valid images to convert.")
        return ConversationHandler.END

    pdf_path = f"{name}.pdf"
    images[0].save(pdf_path, save_all=True, append_images=images[1:])

    await update.message.reply_document(InputFile(pdf_path))

    # Cleanup
    try:
        os.remove(pdf_path)
        for img_path in session["images"]:
            os.remove(img_path)
    except Exception as e:
        logging.warning(f"Cleanup error: {e}")

    user_sessions[user_id] = {"images": [], "last_active": time.time()}
    return ConversationHandler.END


# --- Bot Runner ---
async def run_bot():
    global telegram_app, loop
    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")

    if not TOKEN or not WEBHOOK_URL:
        raise RuntimeError("Missing BOT_TOKEN or WEBHOOK_URL environment variable")

    telegram_app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("(?i)pdf"), handle_trigger)],
        states={ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pdf_name)]},
        fallbacks=[]
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_images))
    telegram_app.add_handler(conv_handler)

    logging.info(f"Setting webhook to {WEBHOOK_URL}")
    await telegram_app.bot.set_webhook(url=WEBHOOK_URL)

    await telegram_app.initialize()
    await telegram_app.start()
    logging.info("Bot started with webhook.")
    
    # Keep the event loop running
    while True:
        await asyncio.sleep(1)


# --- Flask Runner ---
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# --- Main Entrypoint ---
if __name__ == "__main__":
    # Create and set the event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Start Flask in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Run the bot in the main thread
    try:
        loop.run_until_complete(run_bot())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    finally:
        loop.close()
