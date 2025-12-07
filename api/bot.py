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
ADMIN_ID = 6356015122  # Sirf ye ID admin access kar sakti hai

# --- SETUP ---
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-flash', generation_config={"response_mime_type": "application/json"})

# MongoDB Connection
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client['tg_bot_db']
    users_collection = db['user_sessions']
    senders_collection = db['sender_accounts'] # Naya collection emails ke liye
except:
    users_collection = None
    senders_collection = None

app = Flask(__name__)

# States
ASK_LINK, ASK_ID, ASK_CONTENT = range(3)
ADMIN_ASK_EMAIL, ADMIN_ASK_PASS = range(3, 5) # Admin States

# Fake Names
FAKE_NAMES = [
    "Alex Smith", "John Miller", "Sarah Jenkins", "David Ross", "Michael B.",
    "James Carter", "Robert H.", "Security Analyst", "Legal Officer"
]

# --- HELPER FUNCTIONS ---

def mask_email(email):
    """Email ko chupane ke liye (e.g., sudeep@gmail.com -> sud***@gmail.com)"""
    try:
        user, domain = email.split('@')
        if len(user) > 3:
            return f"{user[:3]}***@{domain}"
        return f"***@{domain}"
    except:
        return email

def get_senders():
    """DB se saare saved emails layega"""
    if senders_collection is not None:
        return list(senders_collection.find({}))
    return []

async def clean_chat(context, chat_id, message_id):
    """Message delete karne ka safe tarika"""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except: pass

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
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    
    sender_count = len(get_senders())
    await update.message.reply_text(f"👋 **Bot Ready!**\nActive Senders: {sender_count}\nPhoto bhejo report ke liye.")

# --- ADMIN PANEL LOGIC ---

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await clean_chat(context, update.message.chat_id, update.message.message_id)

    if user_id != ADMIN_ID:
        return # Chupchap ignore karo agar admin nahi hai

    senders = get_senders()
    text = "🔐 **Admin Panel**\n\n**Saved Accounts:**\n"
    if not senders:
        text += "No accounts added yet."
    else:
        for acc in senders:
            text += f"• `{mask_email(acc['email'])}`\n"

    keyboard = [[InlineKeyboardButton("➕ Add New Account", callback_data="add_acc")]]
    
    msg = await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    update_db(user_id, {"last_bot_msg": msg.message_id})

async def admin_add_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if user_id != ADMIN_ID: return

    await query.answer()
    
    # Purana message delete
    try:
        user_data = get_from_db(user_id)
        if 'last_bot_msg' in user_data:
            await clean_chat(context, query.message.chat_id, user_data['last_bot_msg'])
    except: pass

    msg = await query.message.reply_text("📧 **New Account Setup**\n\nEnter Gmail Address:")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return ADMIN_ASK_EMAIL

async def admin_step_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID: return ConversationHandler.END
    
    email = update.message.text
    update_db(user_id, {"temp_email": email})

    # Cleanup
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)

    msg = await update.message.reply_text(f"✅ Email: `{email}`\n\n🔑 **Enter App Password:**")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return ADMIN_ASK_PASS

async def admin_step_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id != ADMIN_ID: return ConversationHandler.END
    
    password = update.message.text.replace(" ", "") # Remove spaces
    user_data = get_from_db(user_id)
    email = user_data.get('temp_email')

    # Save to DB
    if senders_collection is not None:
        senders_collection.update_one(
            {"email": email}, 
            {"$set": {"email": email, "pass": password}}, 
            upsert=True
        )

    # Cleanup
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)

    await update.message.reply_text(f"🎉 **Success!**\nAccount Added: `{mask_email(email)}`", parse_mode="Markdown")
    return ConversationHandler.END

# --- NORMAL USER FLOW ---

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

async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    mode = query.data

    if mode == "start_email":
        await query.answer()
        msg = await query.edit_message_text("📝 **Step 1:** Group Link bhejo.")
        update_db(user_id, {"last_bot_msg": msg.message_id})
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

async def step_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    update_db(user_id, {"gc_link": update.message.text})
    
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)

    msg = await update.message.reply_text("✅ Link Saved.\n\n📝 **Step 2:** Chat ID bhejo (ya Skip).")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return ASK_ID

async def step_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    update_db(user_id, {"chat_id": update.message.text})
    
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)

    msg = await update.message.reply_text("✅ ID Saved.\n\n📝 **Step 3:** Reason/Evidence batao.")
    update_db(user_id, {"last_bot_msg": msg.message_id})
    return ASK_CONTENT

async def step_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    reason = update.message.text
    
    user_data = get_from_db(user_id)
    await clean_chat(context, update.message.chat_id, user_data.get('last_bot_msg'))
    await clean_chat(context, update.message.chat_id, update.message.message_id)

    msg = await update.message.reply_text("🤖 Generating Email Draft...")
    
    try:
        data = get_from_db(user_id)
        img = await get_image_data(data['photo_id'], context.bot)
        
        raw_link = data.get('gc_link', '')
        clean_link = raw_link.replace("https://", "").replace("http://", "")
        random_name = random.choice(FAKE_NAMES)
        
        prompt = (
            f"Write a legal takedown email regarding Telegram Group. "
            f"Target Group Link: {clean_link} (Format: t.me/...), "
            f"Chat ID: {data.get('chat_id')}, Reason: {reason}. "
            f"Sign off with: '{random_name}'. "
            f"Do NOT use 'https://' inside body. "
            f"Output JSON: {{'to': 'email', 'subject': 'sub', 'body': 'text'}}"
        )

        response = model.generate_content([{'mime_type': 'image/jpeg', 'data': img}, prompt])
        email_data = json.loads(response.text)
        
        update_db(user_id, {"draft": email_data})
        
        senders = get_senders()
        count = len(senders)
        keyboard = [[InlineKeyboardButton(f"🚀 Mass Send (from {count} IDs)", callback_data="send_mass")]]
        
        await msg.edit_text(
            f"📧 **Draft Ready!**\n"
            f"**To:** `{email_data['to']}`\n"
            f"**Subject:** `{email_data['subject']}`\n\n"
            f"👇 **Body Preview:**\n{email_data['body'][:200]}...\n\n"
            f"Clicking below will send this from **{count} accounts**.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        await msg.edit_text(f"Error: {str(e)}")
        return ConversationHandler.END
        
    return ConversationHandler.END

async def send_email_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if query.data != "send_mass": return

    await query.answer()
    
    senders = get_senders()
    if not senders:
        await query.edit_message_text("❌ No Sender Accounts in Database! Use /admin to add.")
        return

    await query.edit_message_text(f"🚀 **Starting Mass Report...**\nTarget: {len(senders)} Emails")
    
    user_data = get_from_db(user_id)
    draft = user_data.get('draft')
    status_log = "**📢 Report Status:**\n\n"
    success_count = 0
    
    for idx, account in enumerate(senders):
        sender_email = account['email']
        sender_pass = account['pass']
        masked = mask_email(sender_email)
        
        try:
            if idx > 0 and idx % 2 == 0:
                await query.edit_message_text(f"🚀 Sending... ({idx}/{len(senders)} done)")

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
            
            status_log += f"✅ Sent via {masked}\n"
            success_count += 1
        except:
            status_log += f"❌ Failed {masked}\n"

    await query.edit_message_text(
        f"{status_log}\n➖➖➖➖➖➖\n🎯 **Total Sent:** {success_count}/{len(senders)}",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await clean_chat(context, update.message.chat_id, update.message.message_id)
    await update.message.reply_text("❌ Cancelled.")
    return ConversationHandler.END

# --- WEBHOOK & APP ---
ptb_app = Application.builder().token(TOKEN).build()

# Main Conversation (Report)
conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(report_callback)],
    states={
        ASK_LINK: [MessageHandler(filters.TEXT, step_link)],
        ASK_ID: [MessageHandler(filters.TEXT, step_id)],
        ASK_CONTENT: [MessageHandler(filters.TEXT, step_generate)]
    },
    fallbacks=[CommandHandler('cancel', cancel)],
    allow_reentry=True
)

# Admin Conversation (Add Account)
admin_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_add_click, pattern="^add_acc$")],
    states={
        ADMIN_ASK_EMAIL: [MessageHandler(filters.TEXT, admin_step_email)],
        ADMIN_ASK_PASS: [MessageHandler(filters.TEXT, admin_step_pass)]
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)

ptb_app.add_handler(CommandHandler("admin", admin_command))
ptb_app.add_handler(admin_conv)
ptb_app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
ptb_app.add_handler(CallbackQueryHandler(send_email_callback, pattern="^send_mass$"))
ptb_app.add_handler(conv_handler)
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
    
