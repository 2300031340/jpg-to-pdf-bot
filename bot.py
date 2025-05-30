import os
import time
from PIL import Image
from flask import Flask, request
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)
from telegram.ext import Application

user_sessions = {}
ASK_NAME = 1
SESSION_TIMEOUT = 600  # 10 minutes

# Flask app for Render
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Bot is live with webhook!"

def is_image_file(filename):
    return filename.lower().endswith((".jpg", ".jpeg", ".png"))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send JPG or PNG images (as photos or documents). Type 'pdf' when done.")

async def handle_images(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_time = time.time()
    user_sessions.setdefault(user_id, {"images": [], "last_active": current_time})

    if current_time - user_sessions[user_id]["last_active"] > SESSION_TIMEOUT:
        user_sessions[user_id] = {"images": [], "last_active": current_time}

    file_path = None

    if update.message.photo:
        photo = await update.message.photo[-1].get_file()
        file_path = f"{user_id}_{photo.file_id}.jpg"
        await photo.download_to_drive(file_path)

    elif update.message.document:
        document = update.message.document
        if is_image_file(document.file_name):
            file_path = f"{user_id}_{document.file_unique_id}_{document.file_name}"
            doc_file = await document.get_file()
            await doc_file.download_to_drive(file_path)
        else:
            await update.message.reply_text("❌ Unsupported file type. Only JPG and PNG allowed.")
            return

    if file_path:
        user_sessions[user_id]["images"].append(file_path)
        user_sessions[user_id]["last_active"] = current_time
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
        user_sessions[user_id] = {"images": [], "last_active": current_time}
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
        except:
            continue

    if not images:
        await update.message.reply_text("❌ No valid images to convert.")
        return ConversationHandler.END

    pdf_path = f"{name}.pdf"
    images[0].save(pdf_path, save_all=True, append_images=images[1:])

    await update.message.reply_document(InputFile(pdf_path))

    os.remove(pdf_path)
    for img_path in session["images"]:
        try:
            os.remove(img_path)
        except:
            pass

    user_sessions[user_id] = {"images": [], "last_active": time.time()}
    return ConversationHandler.END

# Global application object
telegram_app: Application = None

@app.route('/webhook', methods=['POST'])
async def webhook():
    if request.method == "POST":
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        await telegram_app.process_update(update)
        return "ok"

async def main():
    global telegram_app
    TOKEN = os.getenv("BOT_TOKEN")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g., https://your-app.onrender.com/webhook

    telegram_app = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.TEXT & filters.Regex("(?i)pdf"), handle_trigger)],
        states={ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pdf_name)]},
        fallbacks=[]
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_images))
    telegram_app.add_handler(conv_handler)

    await telegram_app.bot.set_webhook(url=WEBHOOK_URL)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
