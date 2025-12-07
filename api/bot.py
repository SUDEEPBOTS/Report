import os
import asyncio
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
import google.generativeai as genai
from pymongo import MongoClient
from io import BytesIO

# --- ENVIRONMENT VARIABLES ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")

# --- SETUP ---
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# MongoDB Connection
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client['tg_bot_db']
    users_collection = db['user_sessions']
except:
    users_collection = None
    print("MongoDB Connection Failed - Check URI")

app = Flask(__name__)

# States for Conversation
ASK_LINK, ASK_ID, ASK_CONTENT = range(3)

# --- HELPER FUNCTIONS ---
async def get_image_data(file_id, bot):
    file = await bot.get_file(file_id)
    f = BytesIO()
    await file.download_to_memory(f)
    return f.getvalue()

def update_db(user_id, data):
    if users_collection is not None:
        users_collection.update_one({"user_id": user_id}, {"$set": data}, upsert=True)

def get_from_db(user_id):
    if users_collection is not None:
        return users_collection.find_one({"user_id": user_id})
    return {}

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Bot Ready!**\nSend me a screenshot of a Telegram Group.",
        parse_mode="Markdown"
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_file_id = update.message.photo[-1].file_id
    
    update_db(user_id, {"photo_id": photo_file_id})
    
    keyboard = [
        [InlineKeyboardButton("⚡ Short Report", callback_data="short"),
         InlineKeyboardButton("📊 Long Report", callback_data="long")],
        [InlineKeyboardButton("✉️ Draft Legal Email", callback_data="start_email")]
    ]
    await update.message.reply_text("Screenshot Received! Choose action:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    mode = query.data

    if mode == "start_email":
        await query.answer()
        await query.edit_message_text("📝 **Step 1:** Group Link bhejo.")
        return ASK_LINK

    await query.answer()
    await query.edit_message_text(f"⏳ Analyzing for {mode}...")
    
    try:
        user_data = get_from_db(user_id)
        if not user_data or 'photo_id' not in user_data:
            await query.edit_message_text("❌ Photo Session Expired. Send again.")
            return

        img_data = await get_image_data(user_data['photo_id'], context.bot)
        prompt = "Analyze this image. Give a short safety verdict." if mode == "short" else "Analyze this image. Give a detailed professional report."
        
        response = model.generate_content([{'mime_type': 'image/jpeg', 'data': img_data}, prompt])
        await query.edit_message_text(f"✅ Report:\n\n`{response.text}`", parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"Error: {str(e)}")
    return ConversationHandler.END

# --- EMAIL STEPS ---
async def step_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_db(update.message.from_user.id, {"gc_link": update.message.text})
    await update.message.reply_text("📝 **Step 2:** Chat ID bhejo (ya Skip likho).")
    return ASK_ID

async def step_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_db(update.message.from_user.id, {"chat_id": update.message.text})
    await update.message.reply_text("📝 **Step 3:** Reason/Evidence batao.")
    return ASK_CONTENT

async def step_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    reason = update.message.text
    msg = await update.message.reply_text("✍️ Drafting Email...")
    
    try:
        data = get_from_db(user_id)
        img_data = await get_image_data(data['photo_id'], context.bot)
        
        prompt = f"Write a legal takedown email for Telegram Abuse. Link: {data.get('gc_link')}, ID: {data.get('chat_id')}, Reason: {reason}. Subject & Body."
        response = model.generate_content([{'mime_type': 'image/jpeg', 'data': img_data}, prompt])
        
        await msg.edit_text(f"📧 **Draft:**\n\n`{response.text}`", parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# --- APP BUILDER ---
ptb_app = Application.builder().token(TOKEN).build()

conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(report_callback)],
    states={
        ASK_LINK: [MessageHandler(filters.TEXT, step_link)],
        ASK_ID: [MessageHandler(filters.TEXT, step_id)],
        ASK_CONTENT: [MessageHandler(filters.TEXT, step_generate)]
    },
    fallbacks=[CommandHandler('cancel', cancel)],
    allow_reentry=True
)

ptb_app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
ptb_app.add_handler(conv)
ptb_app.add_handler(CommandHandler("start", start))

# --- WEBHOOK (FIXED LOGIC) ---
@app.route("/", methods=["POST"])
def webhook():
    if request.method == "POST":
        async def handle_update():
            # 1. Initialize Application (Zaroori hai v20+ ke liye)
            if not ptb_app._initialized:
                await ptb_app.initialize()
            
            # 2. Process Update
            update = Update.de_json(request.get_json(force=True), ptb_app.bot)
            await ptb_app.process_update(update)
            
            # 3. Shutdown to prevent loop errors in serverless
            await ptb_app.shutdown()

        try:
            asyncio.run(handle_update())
            return "OK"
        except Exception as e:
            print(f"Error: {e}")
            return "Error", 500
            
    return "Bot is Running"

if __name__ == "__main__":
    app.run(port=5000)
    
