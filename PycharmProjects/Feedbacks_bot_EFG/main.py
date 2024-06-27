import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from pymongo import MongoClient
from datetime import datetime
from telegram.error import BadRequest
#from dotenv import load_dotenv
import os

#load_dotenv()

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB connection
DB_URI = os.getenv('DB_URI')
client = MongoClient(DB_URI)
db = client['EcoFlow']
feedbacks_collection = db['feedBacks']

# Telegram bot token
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

# Authorized users
AUTHORIZED_USERS = os.getenv('AUTHORIZED_USERS').split(',')

PAGE_SIZE = 5

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    username = update.message.from_user.username
    if username not in AUTHORIZED_USERS:
        await update.message.reply_text("You are not allowed to use this bot, it's private.")
        return

    feedbacks = list(feedbacks_collection.find())
    context.user_data['feedbacks'] = feedbacks
    context.user_data['page'] = 0
    await show_feedbacks(update.message, context)

async def show_feedbacks(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    feedbacks = context.user_data['feedbacks']
    page = context.user_data['page']
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE

    if start_idx >= len(feedbacks):
        await message.reply_text("No more feedbacks to display.")
        return

    feedback_texts = []
    for i, feedback in enumerate(feedbacks[start_idx:end_idx]):
        user_name = feedback.get('userName', 'Unknown')
        date = feedback.get('date')
        if isinstance(date, datetime):
            formatted_date = date.strftime('%Y-%m-%d %H:%M:%S')
        else:
            formatted_date = datetime.fromtimestamp(date).strftime('%Y-%m-%d %H:%M:%S')
        feedback_text = feedback.get('feedback', 'No feedback text')
        feedback_texts.append(f"Feedback {i + 1} from {user_name} on {formatted_date}:\n{feedback_text}")

    feedback_message = "\n\n".join(feedback_texts)

    buttons = []
    if start_idx > 0:
        buttons.append(InlineKeyboardButton("Previous", callback_data='prev'))
    if end_idx < len(feedbacks):
        buttons.append(InlineKeyboardButton("Next", callback_data='next'))
    buttons.append(InlineKeyboardButton("Update", callback_data='update'))

    reply_markup = InlineKeyboardMarkup([buttons])

    try:
        await message.edit_text(text=feedback_message, reply_markup=reply_markup)
    except BadRequest as e:
        if e.message == "Message can't be edited":
            await message.reply_text(feedback_message, reply_markup=reply_markup)
        else:
            raise  # Re-raise the exception if it's not related to message editing

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    username = query.from_user.username

    if username not in AUTHORIZED_USERS:
        await query.edit_message_text(text="You are not allowed to use this bot, it's private.")
        return

    if query.data == 'next':
        context.user_data['page'] += 1
    elif query.data == 'prev':
        context.user_data['page'] -= 1
    elif query.data == 'update':
        context.user_data['feedbacks'] = list(feedbacks_collection.find())
        context.user_data['page'] = 0

    try:
        await show_feedbacks(query.message, context)
    except BadRequest as e:
        if e.message == "Message can't be edited":
            await query.message.reply_text("The message cannot be edited due to Telegram's limitations. "
                                           "Sending the updated message instead.")
            await show_feedbacks(query.message, context)
        else:
            raise  # Re-raise the exception if it's not related to message editing

def main() -> None:
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button))

    # Start the Bot
    application.run_polling()


if __name__ == '__main__':
    main()
