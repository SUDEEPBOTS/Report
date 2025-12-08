import os
import asyncio
import smtplib
import json
import random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

# Multiple Senders Load Karna
SENDER_LIST_JSON = os.environ.get("SENDER_LIST")
try:
    SENDER_ACCOUNTS = json.loads(SENDER_LIST_JSON) if SENDER_LIST_JSON else []
except:
    SENDER_ACCOUNTS = []
    print("Error loading SENDER_LIST from .env")

# --- SETUP ---
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})

# MongoDB Connection
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client['tg_bot_db']
    users_collection = db['user_sessions']
except:
    users_collection = None

app = Flask(__name__)

# States
ASK_LINK, ASK_ID, ASK_CONTENT = range(3)

# --- FAKE NAMES LIST ---
FAKE_NAMES = [
    "Alex Smith", "John Miller", "Sarah Jenkins", "David Ross", "Michael B.",
    "James Carter", "Robert H.", "Security Analyst", "Legal Officer", "T. Anderson",
    "Chris Evans", "Daniel Craig", "Emma Watson", "Steve Rogers"
]

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
    # User start karega toh uska message delete karke clean welcome denge
    try:
        await context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
    except: pass
    
    await update.message.reply_text(f"👋 **Bot Ready!**\nLoaded {len(SENDER_ACCOUNTS)} Sender Accounts.\nPhoto bhejo shuru karne ke liye.")

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    photo_file_id = update.message.photo[-1].file_id
    update_db(user_id, {"photo_id": photo_file_id})
    
    keyboard = [
        [InlineKeyboardButton("⚡ Short Report", callback_data="short"),
         InlineKeyboardButton("📊 Long Report", callback_data="long")],
        [InlineKeyboardButton("✉️ Mass Report Email", callback_data="start_email")]
    ]
    await update.message.reply_text("Screenshot Saved! Action select karo:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END

# --- REPORT LOGIC ---
async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    mode = query.data

    if mode == "start_email":
        await query.answer()
        await query.edit_message_text("📝 **Step 1:** Group Link bhejo.")
        return ASK_LINK

    await query.answer()
    await query.edit_message_text(f"⏳ Analyzing...")
    try:
        data = get_from_db(user_id)
        img = await get_image_data(data['photo_id'], context.bot)
        
        text_model = genai.GenerativeModel('gemini-1.5-flash') 
        prompt = "Short verdict" if mode == "short" else "Detailed analysis"
        response = text_model.generate_content([{'mime_type': 'image/jpeg', 'data': img}, prompt])
        
        await query.edit_message_text(f"✅ Report:\n\n`{response.text}`", parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"Error: {str(e)}")
    return ConversationHandler.END

# --- EMAIL WIZARD (AUTO-DELETE ADDED) ---

async def step_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Save Data
    update_db(update.message.from_user.id, {"gc_link": update.message.text})
    
    # 2. DELETE USER MESSAGE (Clean Up)
    try:
        await context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
    except: pass

    # 3. Next Question
    # Purna message edit nahi kar sakte kyunki wo text tha, naya bhejenge
    await update.message.reply_text("✅ Link Saved.\n\n📝 **Step 2:** Chat ID bhejo (ya Skip).")
    return ASK_ID

async def step_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Save Data
    update_db(update.message.from_user.id, {"chat_id": update.message.text})
    
    # 2. DELETE USER MESSAGE (Clean Up)
    try:
        await context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
    except: pass

    # 3. Next Question
    await update.message.reply_text("✅ ID Saved.\n\n📝 **Step 3:** Reason/Evidence batao.")
    return ASK_CONTENT

async def step_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    reason = update.message.text
    
    # 1. DELETE USER MESSAGE (Clean Up)
    try:
        await context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
    except: pass

    msg = await update.message.reply_text("🤖 Generating Email Draft...")
    
    try:
        data = get_from_db(user_id)
        img = await get_image_data(data['photo_id'], context.bot)
        
        # Logic: Clean Link & Random Name
        raw_link = data.get('gc_link', '')
        clean_link = raw_link.replace("https://", "").replace("http://", "")
        random_name = random.choice(FAKE_NAMES)
        
        prompt = (
            f"Write a legal takedown email regarding Telegram Group. "
            f"Target Group Link: {clean_link} (Keep format exactly as: t.me/...), "
            f"Chat ID: {data.get('chat_id')}, Reason: {reason}. "
            f"IMPORTANT: Sign off the email with the name: '{random_name}'. "
            f"IMPORTANT: Do NOT use 'https://' in any Telegram links inside the body, use 't.me/...' format only. "
            f"Determine recipient (abuse/dmca). "
            f"Output JSON: {{'to': 'email', 'subject': 'sub', 'body': 'text'}}"
        )

        response = model.generate_content([{'mime_type': 'image/jpeg', 'data': img}, prompt])
        email_data = json.loads(response.text)
        
        update_db(user_id, {"draft": email_data})
        
        count = len(SENDER_ACCOUNTS)
        keyboard = [[InlineKeyboardButton(f"🚀 Mass Send (from {count} IDs)", callback_data="send_mass")]]
        
        await msg.edit_text(
            f"📧 **Draft Ready!**\n"
            f"**To:** `{email_data['to']}`\n"
            f"**Subject:** `{email_data['subject']}`\n\n"
            f"👇 **Body Preview:**\n{email_data['body'][:300]}...\n\n"
            f"Clicking below will send this email from **{count} different accounts** one by one.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")
        return ConversationHandler.END
        
    return ConversationHandler.END

# --- MASS SENDING LOGIC ---
async def send_email_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if query.data != "send_mass": return

    await query.answer()
    
    if not SENDER_ACCOUNTS:
        await query.edit_message_text("❌ No Sender Accounts found in .env (SENDER_LIST)!")
        return

    await query.edit_message_text(f"🚀 **Starting Mass Report...**\nTarget: {len(SENDER_ACCOUNTS)} Emails")
    
    user_data = get_from_db(user_id)
    draft = user_data.get('draft')
    
    status_log = "**📢 Report Status:**\n\n"
    success_count = 0
    
    for idx, account in enumerate(SENDER_ACCOUNTS):
        sender_email = account['email']
        sender_pass = account['pass']
        
        try:
            if idx > 0 and idx % 2 == 0:
                await query.edit_message_text(f"🚀 Sending... ({idx}/{len(SENDER_ACCOUNTS)} done)")

            msg = MIMEMultipart()
            msg['From'] = sender_email
            msg['To'] = draft['to']
            msg['Subject'] = draft['subject']
            msg.attach(MIMEText(draft['body'], 'plain'))

            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(sender_email, sender_pass)
            server.send_message(msg)
            server.quit()
            
            status_log += f"✅ Sent via {sender_email}\n"
            success_count += 1
            
        except Exception as e:
            status_log += f"❌ Failed {sender_email} (Error)\n"

    await query.edit_message_text(
        f"{status_log}\n"
        f"➖➖➖➖➖➖\n"
        f"🎯 **Total Sent:** {success_count}/{len(SENDER_ACCOUNTS)}",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.delete_message(chat_id=update.message.chat_id, message_id=update.message.message_id)
    except: pass
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# --- APP SETUP ---
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
ptb_app.add_handler(CallbackQueryHandler(send_email_callback, pattern="^send_mass$"))
ptb_app.add_handler(conv)
ptb_app.add_handler(CommandHandler("start", start))

@app.route("/", methods=["POST"])
def webhook():
    if request.method == "POST":
        async def handle_update():
            if not ptb_app._initialized: await ptb_app.initialize()
            update = Update.de_json(request.get_json(force=True), ptb_app.bot)
            await ptb_app.process_update(update)
            await ptb_app.shutdown()
        try:
            asyncio.run(handle_update())
            return "OK"
        except: return "Error", 500
    return "Running"

if __name__ == "__main__":
    app.run(port=5000)
