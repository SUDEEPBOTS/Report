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

# --- CONFIGURATION ---
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")

# Gemini Setup
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')

# MongoDB Setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client['tg_bot_db']
users_collection = db['user_sessions']

# Flask App
app = Flask(__name__)

# --- CONVERSATION STATES ---
ASK_LINK, ASK_ID, ASK_CONTENT = range(3)

# --- HELPER FUNCTIONS ---

async def get_image_data(file_id, bot):
    file = await bot.get_file(file_id)
    f = BytesIO()
    await file.download_to_memory(f)
    return f.getvalue()

def update_db(user_id, data):
    """Data ko MongoDB mein save/update karta hai"""
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": data},
        upsert=True
    )

def get_from_db(user_id):
    """MongoDB se user ka data nikalta hai"""
    return users_collection.find_one({"user_id": user_id})

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Welcome!**\n\nTelegram Group ka screenshot bhejo, main analyze karunga.\n"
        "Main **Analysis Report** aur **Legal Email** dono bana sakta hun.",
        parse_mode="Markdown"
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_file_id = update.message.photo[-1].file_id
    
    # 1. Photo ID ko Database mein save karo
    update_db(user_id, {"photo_id": photo_file_id})
    
    keyboard = [
        [InlineKeyboardButton("⚡ Short Report", callback_data="short"),
         InlineKeyboardButton("📊 Long Report", callback_data="long")],
        [InlineKeyboardButton("✉️ Draft Legal Email", callback_data="start_email")]
    ]
    
    await update.message.reply_text(
        "Screenshot Saved! ✅\nAb batao kya karna hai?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

# --- REPORT LOGIC (Short/Long) ---
async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    mode = query.data

    # Agar user ne Email Button dabaya -> Start Email Wizard
    if mode == "start_email":
        await query.answer()
        await query.edit_message_text(
            "📝 **Step 1/3: Group Link**\n\n"
            "Jis group ko report karna hai, uska **Link** bhejo.\n"
            "(Example: https://t.me/scamgroup)",
            parse_mode="Markdown"
        )
        return ASK_LINK

    # Agar Report mangi hai (Short/Long)
    await query.answer()
    await query.edit_message_text(f"⏳ Generating {mode.upper()} Report...")
    
    try:
        user_data = get_from_db(user_id)
        if not user_data or 'photo_id' not in user_data:
            await query.edit_message_text("❌ Photo expire ho gayi. Please dobara bhejo.")
            return

        img_data = await get_image_data(user_data['photo_id'], context.bot)
        
        prompt = "Analyze this screenshot. "
        if mode == "short":
            prompt += "Give a short verdict (Safe/Unsafe) in 3 lines with emojis."
        else:
            prompt += "Give a detailed professional analysis (Members, Vibe, Fake/Real)."

        response = model.generate_content([
            {'mime_type': 'image/jpeg', 'data': img_data},
            prompt
        ])
        
        await query.edit_message_text(f"✅ **Report:**\n\n`{response.text}`", parse_mode="Markdown")

    except Exception as e:
        await query.edit_message_text(f"Error: {str(e)}")
    
    return ConversationHandler.END

# --- EMAIL WIZARD STEPS ---

async def step_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text
    
    # Save Link to DB
    update_db(user_id, {"gc_link": text})
    
    await update.message.reply_text(
        "✅ Link Saved.\n\n"
        "📝 **Step 2/3: Chat ID**\n"
        "Group ya Scammer ka **Chat ID** bhejo (agar hai toh).\n"
        "Agar nahi hai to 'Skip' likh do."
    )
    return ASK_ID

async def step_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text
    
    # Save ID to DB
    update_db(user_id, {"chat_id": text})
    
    await update.message.reply_text(
        "✅ ID Saved.\n\n"
        "📝 **Step 3/3: Content & Reason**\n"
        "Ab batao report kyun karna hai? Koi scam message ya content link hai?\n"
        "Short mein explain karo."
    )
    return ASK_CONTENT

async def step_generate_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    reason = update.message.text
    
    msg = await update.message.reply_text("🤖 **Creating Email Draft...** (Wait karo)")
    
    try:
        # Fetch everything from DB
        data = get_from_db(user_id)
        
        if not data or 'photo_id' not in data:
            await msg.edit_text("❌ Session Error. Photo dobara bhejo.")
            return ConversationHandler.END
            
        img_data = await get_image_data(data['photo_id'], context.bot)
        
        # Prepare Prompt
        prompt = (
            f"Act as a cybersecurity legal expert. Write a formal 'Takedown Request' email to Telegram Abuse Dept.\n"
            f"DETAILS PROVIDED:\n"
            f"- Group Link: {data.get('gc_link')}\n"
            f"- Chat ID: {data.get('chat_id')}\n"
            f"- Reason/Evidence: {reason}\n"
            f"Analyze the attached screenshot for further proof.\n\n"
            f"OUTPUT FORMAT:\n"
            f"Subject: [Write a strong subject]\n\n"
            f"[Write the email body here. Be professional, urgent, and strict.]"
        )

        response = model.generate_content([
            {'mime_type': 'image/jpeg', 'data': img_data},
            prompt
        ])
        
        email_content = response.text
        
        await msg.edit_text(
            f"📧 **Generated Email Draft**\n\n"
            f"`{email_content}`\n\n"
            f"👆 *Upar wale box par click karke copy karo.*",
            parse_mode="Markdown"
        )

    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Process Cancelled.")
    return ConversationHandler.END

# --- APP SETUP ---

ptb_app = Application.builder().token(TOKEN).build()

# Wizard Configuration
conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(report_callback)], # Entry via Button
    states={
        ASK_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_link)],
        ASK_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_id)],
        ASK_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_generate_email)],
    },
    fallbacks=[CommandHandler('cancel', cancel)],
    allow_reentry=True
)

ptb_app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
ptb_app.add_handler(conv_handler)
ptb_app.add_handler(CommandHandler("start", start))

@app.route("/", methods=["POST"])
def webhook():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), ptb_app.bot)
        asyncio.run(ptb_app.process_update(update))
        return "OK"
    return "Bot is Running"

if __name__ == "__main__":
    app.run(port=5000)
