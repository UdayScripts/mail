import logging
import smtplib
import ssl
from email.message import EmailMessage
import asyncio
import os

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text, Command
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (
    ParseMode, ReplyKeyboardRemove, User,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils import executor
from aiogram.utils.exceptions import MessageToDeleteNotFound, BotBlocked, MessageCantBeDeleted, MessageNotModified

# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# --- Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
log = logging.getLogger(__name__)

if not BOT_TOKEN:
    log.warning("BOT_TOKEN not found in Replit Secrets. Using hardcoded token (less secure).")
    BOT_TOKEN = "7083366022:AAFZdkHM4mEPyVzGXAt72corhzqmyl07ujU" # Fallback if not in secrets

if not BOT_TOKEN:
    print("FATAL ERROR: Bot token is not configured. Set it in Replit Secrets or hardcode it.")
    exit()

OWNER_ID = 6775748231 # !!! REPLACE WITH YOUR ID !!!

# --- Premium User Management ---
PREMIUM_USERS_FILE = "premium_users.txt"
premium_users = set()

# --- Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
log = logging.getLogger(__name__)

# --- Bot Setup ---
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot, storage=storage)

# --- Persistence Functions ---
def load_premium_users():
    global premium_users
    premium_users = set()
    try:
        if os.path.exists(PREMIUM_USERS_FILE):
            with open(PREMIUM_USERS_FILE, 'r') as f:
                premium_users = {int(line.strip()) for line in f if line.strip().isdigit()}
            log.info(f"Loaded {len(premium_users)} premium users from {PREMIUM_USERS_FILE}.")
        else:
            log.info(f"{PREMIUM_USERS_FILE} not found. Starting empty.")
    except Exception as e:
        log.error(f"Error loading premium users from {PREMIUM_USERS_FILE}: {e}")

def save_premium_users():
    global premium_users
    try:
        with open(PREMIUM_USERS_FILE, 'w') as f:
            for user_id in sorted(list(premium_users)):
                f.write(f"{user_id}\n")
        log.info(f"Saved {len(premium_users)} premium users to {PREMIUM_USERS_FILE}.")
    except Exception as e:
        log.error(f"Error saving premium users to {PREMIUM_USERS_FILE}: {e}")

# --- Define States for FSM ---
class ReportForm(StatesGroup):
    waiting_for_email = State()         # Step 1
    waiting_for_password = State()      # Step 2
    waiting_for_smtp_server = State()   # Step 3
    waiting_for_smtp_port = State()     # Step 4
    waiting_for_target_email = State()  # Step 5
    waiting_for_subject = State()       # Step 6
    waiting_for_body = State()          # Step 7
    waiting_for_count = State()         # Step 8
    waiting_for_confirmation = State()  # Waiting for button click

# --- Helper Functions ---
def is_allowed_user(user: User) -> bool:
    return user.id == OWNER_ID or user.id in premium_users

async def delete_message_safely(message: types.Message):
    try:
        await message.delete()
    except Exception:
        pass # Ignore deletion errors silently for sensitive info

# --- Email Sending Function (Improved Error Handling) ---
async def send_emails_async(user_data: dict, user_id: int):
    email = user_data.get('email')
    password = user_data.get('password')
    smtp_server = user_data.get('smtp_server')
    smtp_port = user_data.get('smtp_port')
    target_email = user_data.get('target_email')
    subject = user_data.get('subject')
    body = user_data.get('body')
    count = user_data.get('count')

    required_fields = [email, password, smtp_server, smtp_port, target_email, subject, body, count]
    if not all(required_fields):
        log.error(f"User {user_id}: Missing data for sending email: {user_data}")
        return False, "Internal error: Missing required data. Please start over using /report."

    try:
        port = int(smtp_port)
        count_int = int(count)
        if not (1 <= port <= 65535): raise ValueError("Invalid port range")
        if count_int <= 0 : raise ValueError("Count must be positive")
    except ValueError as e:
        log.error(f"User {user_id}: Invalid port or count. Port='{smtp_port}', Count='{count}'. Error: {e}")
        return False, "Invalid port or count number provided. Please use positive numbers and valid port range."

    context = ssl.create_default_context()
    log.info(f"User {user_id}: Attempting connection to {smtp_server}:{port} for {count_int} emails to {target_email}")
    server = None
    sent_count = 0
    send_errors = []

    try:
        # Establish connection
        if port == 465:
            server = smtplib.SMTP_SSL(smtp_server, port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(smtp_server, port, timeout=30)
            server.ehlo() # Check connection
            server.starttls(context=context)
            server.ehlo() # Re-check after TLS

        # Login
        server.login(email, password)
        log.info(f"User {user_id}: Login successful for {email}.")

        # Send loop
        for i in range(count_int):
            current_email_num = i + 1
            try:
                msg = EmailMessage()
                msg['Subject'] = subject
                msg['From'] = email
                msg['To'] = target_email
                msg.set_content(body)
                server.send_message(msg)
                sent_count += 1
                log.info(f"User {user_id}: Email {current_email_num}/{count_int} sent.")
                # Optional delay for rate limiting
                if count_int > 15 and sent_count % 10 == 0: # Small delay every 10 emails if sending many
                    await asyncio.sleep(0.5)

            except smtplib.SMTPSenderRefused as e_loop:
                log.error(f"User {user_id}: Sender refused for email {current_email_num}. Stopping. Error: {e_loop}")
                send_errors.append(f"Sender address <code>{email}</code> refused after {sent_count} sent.")
                break # Stop sending
            except Exception as e_loop:
                log.error(f"User {user_id}: Error sending email {current_email_num}: {e_loop}")
                send_errors.append(f"Failed sending email #{current_email_num}")
                # Optional: break here too, or continue trying others
                if len(send_errors) > 5: # Stop if too many errors occur
                     send_errors.append("Too many errors, stopping.")
                     break

        log.info(f"User {user_id}: Finished sending loop. Sent: {sent_count}/{count_int}.")

        # --- Result Formatting ---
        if sent_count == count_int:
            return True, f"✅ Successfully sent all <b>{sent_count}</b> emails to <code>{target_email}</code>!"
        elif sent_count > 0:
             error_summary = "\n".join(send_errors)
             return False, (f"⚠️ Sent <b>{sent_count}/{count_int}</b> emails to <code>{target_email}</code>.\n"
                           f"Encountered errors:\n{error_summary}")
        else: # sent_count == 0
             error_summary = "\n".join(send_errors)
             return False, (f"❌ Failed to send any emails to <code>{target_email}</code>.\n"
                           f"Errors:\n{error_summary}")

    # --- Connection/Authentication Error Handling ---
    except smtplib.SMTPAuthenticationError:
        log.error(f"User {user_id}: Authentication failed for {email} on {smtp_server}:{port}.")
        return False, ("🔑 Authentication failed. Please check your email/password. "
                       "<i>(Did you use an App Password if required by Gmail/Outlook etc?)</i>")
    except smtplib.SMTPConnectError as e:
        log.error(f"User {user_id}: Could not connect to {smtp_server}:{port}. Error: {e}")
        return False, f"🔌 Could not connect to <code>{smtp_server}:{port}</code>. Check server/port and firewall settings."
    except smtplib.SMTPServerDisconnected:
         log.error(f"User {user_id}: Server disconnected unexpectedly {smtp_server}:{port}.")
         return False, "🔌 Server disconnected unexpectedly. Please try again later."
    except ConnectionRefusedError:
        log.error(f"User {user_id}: Connection refused by {smtp_server}:{port}.")
        return False, f"🔌 Connection refused by <code>{smtp_server}:{port}</code>. Check server/port details."
    except TimeoutError:
        log.error(f"User {user_id}: Connection/operation timed out to {smtp_server}:{port}.")
        return False, f"⏳ Connection timed out connecting to <code>{smtp_server}:{port}</code>."
    except ssl.SSLError as e:
        log.error(f"User {user_id}: SSL Error connecting to {smtp_server}:{port}. Error: {e}")
        return False, f"🔒 SSL Error: {e}. (Common if port 465 is used without SSL or port 587 without STARTTLS)."
    except smtplib.SMTPException as e: # Catch other specific SMTP errors
         log.error(f"User {user_id}: SMTP Error during communication with {smtp_server}:{port}. Error: {e}")
         return False, f"✉️ SMTP Error: <code>{e}</code>"
    except Exception as e:
        log.exception(f"User {user_id}: An unexpected error occurred during email sending process: {e}")
        return False, f"⚙️ An unexpected error occurred: <code>{e}</code>"
    finally:
        if server:
            try:
                server.quit()
                log.info(f"User {user_id}: SMTP connection closed.")
            except Exception:
                 pass # Ignore errors during quit

# --- Bot Handlers ---

# /start command
@dp.message_handler(commands=['start'], state='*')
async def cmd_start(message: types.Message, state: FSMContext):
    # Finish the current state
    await state.finish()
    
    user_name = message.from_user.first_name
    log.info(f"User {message.from_user.id} ({message.from_user.username or 'no_username'}) started the bot.")

    # Creating the custom keyboard
    start_keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    start_keyboard.add(KeyboardButton("📊 Start Report"))
    start_keyboard.add(KeyboardButton("❓ Help"))

    # Start message content
    start_msg = f"""⚡️ Welcome to 𝕸𝖆𝖎𝖑 𝕱𝖚𝖈𝖐*𝖗 ⚡️
ᴛʜᴇ ᴜʟᴛɪᴍᴀᴛᴇ ꜱᴘᴀᴍ ᴘʟᴀʏɢʀᴏᴜɴᴅ ꜰᴏʀ ꜱᴀᴠᴀɢᴇ ꜱᴇɴᴅᴇʀꜱ.
𝗙𝗢𝗥𝗚𝗘𝗧 𝗥𝗨𝗟𝗘𝗦. 𝗙𝗘𝗔𝗥 𝗡𝗢 𝗙𝗜𝗟𝗧𝗘𝗥𝗦.

━━━━━━━━━━━━━
🔥 𝘽𝙤𝙩 𝘼𝙧𝙨𝙚𝙣𝙖𝙡:
• 𝙎𝙈𝘼𝙎𝙃 𝙄𝙉𝘽𝙊𝙓𝙀𝙎 𝙬𝙞𝙩𝙝 𝙃𝙞𝙜𝙝-𝙑𝙤𝙡𝙪𝙢𝙚 𝘽𝙡𝙖𝙨𝙩𝙨
• 𝘽𝙮𝙥𝙖𝙨𝙨 𝘿𝙚𝙩𝙚𝙘𝙩𝙞𝙤𝙣 𝙡𝙞𝙠𝙚 𝙖 𝙂𝙝𝙤𝙨𝙩
• 𝙍𝙖𝙣𝙙𝙤𝙢𝙞𝙯𝙚 & 𝙎𝙥𝙡𝙞𝙩 𝘼𝙩𝙩𝙖𝙘𝙠 𝙋𝙖𝙩𝙩𝙚𝙧𝙣𝙨
• 𝙁𝙖𝙨𝙩. 𝙁𝙚𝙖𝙧𝙡𝙚𝙨𝙨. 𝙁𝙞𝙡𝙩𝙚𝙧-𝙋𝙧𝙤𝙤𝙛.

━━━━━━━━━━━━━
⚙️ 𝙃𝙤𝙬 𝙩𝙤 𝙐𝙨𝙚 𝙏𝙝𝙚 𝘽𝙀𝘼𝙎𝙏:
📌 Press '📊 Start Report' to launch your attack.
📌 Tap '❓ Help' to learn all commands.

━━━━━━━━━━━━━
Stay Ruthless. Stay Untouchable.

<b> Bot by:</b> <b>@unknownxinfo</b>
"""

    # Send the welcome message with the custom keyboard
    await message.reply(start_msg, reply_markup=start_keyboard)

# /help command (also handles Help button)
@dp.message_handler(Text(equals="❓ Help", ignore_case=True), state='*')
@dp.message_handler(commands=['help'], state='*')
async def cmd_help(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await state.finish()
        await message.reply("⚠️ Operation cancelled by requesting help.", reply_markup=ReplyKeyboardRemove())
        log.info(f"User {message.from_user.id} cancelled state {current_state} by using help.")

    user_id = message.from_user.id
    help_text = (
        "╭━━━━━━━━━━━━━━━━━━━╮\n"
        "    <b>⚙️ 𝙃𝙀𝙇𝙋 𝘾𝙊𝙈𝙈𝘼𝙉𝘿𝙎</b>\n"
        "╰━━━━━━━━━━━━━━━━━━━╯\n\n"

        "<b>📌 USER COMMANDS:</b>\n"
        "📊 <code>/report</code> or <i>'Start Report'</i>\n"
        "   ┗ Starts the email spamming engine. (⚠️ Premium only)\n\n"
        "❓ <code>/help</code> or <i>'Help'</i>\n"
        "   ┗ Displays this command list & cancels any current task.\n\n"
        "🚫 <code>/cancel</code>\n"
        "   ┗ Abort current action or session immediately.\n"
    )

    if user_id == OWNER_ID:
        help_text += (
            "\n<b>👑 OWNER COMMANDS:</b>\n"
            "🔑 <code>/addpremium [user_id]</code>\n"
            "   ┗ Give user premium access.\n\n"
            "🔒 <code>/removepremium [user_id]</code>\n"
            "   ┗ Revoke premium access from a user.\n\n"
            "👥 <code>/listpremium</code>\n"
            "   ┗ Show list of all premium users.\n"
        )

    help_text += "\n<b>🧠 Bot by:</b> <a href='https://t.me/unknownxinfo'>@unknownxinfo</a>"

    reply_markup = ReplyKeyboardRemove() if message.text.startswith('/') else None
    await message.reply(help_text, parse_mode="HTML", reply_markup=reply_markup, disable_web_page_preview=True)


# /cancel command
@dp.message_handler(commands=['cancel'], state='*')
@dp.message_handler(Text(equals='cancel', ignore_case=True), state='*')
async def cmd_cancel(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    current_state = await state.get_state()

    if current_state is None:
        log.info(f"User {user_id} tried /cancel, but no active state.")
        await message.reply(
            "⚠️ <b>No active operation found.</b>\n"
            "You're all clear, nothing to stop.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="HTML"
        )
        return

    log.info(f"Cancelling state {current_state} for user {user_id} via /cancel.")
    await state.finish()
    await message.reply(
        "🚫 <b>Operation Terminated.</b>\n"
        "All processes have been stopped. You're back to the main menu.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="HTML"
    )

# --- Owner Commands ---
@dp.message_handler(Command("addpremium"), user_id=OWNER_ID, state="*")
async def cmd_add_premium(message: types.Message):
    args = message.get_args()
    if not args or not args.isdigit():
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/addpremium &lt;user_id&gt;</code>\n"
            "User ID must be a numeric Telegram ID.",
            parse_mode="HTML"
        )
        return

    user_id_to_add = int(args)
    if user_id_to_add == OWNER_ID:
        await message.reply("👑 Owner already has full access.", parse_mode="HTML")
        return

    if user_id_to_add in premium_users:
        await message.reply(f"ℹ️ User <code>{user_id_to_add}</code> already has premium access.", parse_mode="HTML")
    else:
        premium_users.add(user_id_to_add)
        save_premium_users()
        log.info(f"Owner {message.from_user.id} added premium for {user_id_to_add}")
        await message.reply(
            f"✅ <b>Success!</b>\nUser <code>{user_id_to_add}</code> was granted premium access.",
            parse_mode="HTML"
        )
        try:
            await bot.send_message(user_id_to_add, "🎉 You’ve been granted <b>Premium Access</b> by the bot owner!", parse_mode="HTML")
        except Exception as e:
            log.warning(f"Could not notify user {user_id_to_add} about premium grant: {e}")


@dp.message_handler(Command("removepremium"), user_id=OWNER_ID, state="*")
async def cmd_remove_premium(message: types.Message):
    args = message.get_args()
    if not args or not args.isdigit():
        await message.reply(
            "⚠️ <b>Usage:</b> <code>/removepremium &lt;user_id&gt;</code>\n"
            "Provide a valid numeric user ID.",
            parse_mode="HTML"
        )
        return

    user_id_to_remove = int(args)
    if user_id_to_remove == OWNER_ID:
        await message.reply("⛔️ Cannot remove owner's implicit access.", parse_mode="HTML")
        return

    if user_id_to_remove in premium_users:
        premium_users.discard(user_id_to_remove)
        save_premium_users()
        log.info(f"Owner {message.from_user.id} removed premium for {user_id_to_remove}")
        await message.reply(
            f"❌ <b>Premium access revoked</b> for user <code>{user_id_to_remove}</code>.",
            parse_mode="HTML"
        )
        try:
            await bot.send_message(user_id_to_remove, "ℹ️ Your <b>Premium Access</b> has been revoked by the owner.", parse_mode="HTML")
        except Exception as e:
            log.warning(f"Could not notify user {user_id_to_remove} about premium removal: {e}")
    else:
        await message.reply(f"⚠️ User <code>{user_id_to_remove}</code> does not have premium access.", parse_mode="HTML")


@dp.message_handler(Command("listpremium"), user_id=OWNER_ID, state="*")
async def cmd_list_premium(message: types.Message):
    if not premium_users:
        await message.reply(
            "📭 <b>No users currently have premium access</b> (besides you, the owner).",
            parse_mode="HTML"
        )
        return

    user_list = "\n".join([f"• <code>{uid}</code>" for uid in sorted(premium_users)])
    await message.reply(
        f"👥 <b>Premium Users ({len(premium_users)}):</b>\n{user_list}",
        parse_mode="HTML"
    )

# --- Report Command and FSM Handlers ---

# Handles /report command OR the "📊 Start Report" button text
@dp.message_handler(Text(equals="📊 Start Report", ignore_case=True), state=None)
@dp.message_handler(commands=['report'], state=None)
async def cmd_report(message: types.Message, state: FSMContext):
    user = message.from_user
    if not is_allowed_user(user):
        log.warning(f"Unauthorized /report attempt by {user.id} ({user.username or 'no_username'})")
        await message.reply("🚫 Access Denied: This feature requires premium access. Contact the owner.",
                            reply_markup=ReplyKeyboardRemove())
        return

    log.info(f"User {user.id} starting /report process.")
    await ReportForm.waiting_for_email.set()
    await message.reply("Okay, let's configure the mass email report.\n\n"
                        "<b>Step 1/8:</b> 📧 Enter your sender email address (e.g., <code>you@gmail.com</code>):\n"
                        "<i>(Type /cancel anytime to stop)</i>\n\n"
                        "⚠️ <b>Security Note:</b> Using an <b>App Password</b> is strongly recommended if your provider supports it (Gmail, Outlook etc.).",
                        reply_markup=ReplyKeyboardRemove()) # Remove main keyboard

# Step 1: Get Email
@dp.message_handler(state=ReportForm.waiting_for_email)
async def process_email(message: types.Message, state: FSMContext):
    email_text = message.text.strip()
    # Simple validation
    if '@' not in email_text or '.' not in email_text.split('@')[-1] or ' ' in email_text or len(email_text) < 6 :
         await message.reply("Hmm, that doesn't look quite right. Please enter a valid email address.")
         return
    await state.update_data(email=email_text)
    await ReportForm.next()
    await message.reply("<b>Step 2/8:</b> 🔑 Enter your email password or App Password.\n"
                        "<i>(Your message here will be deleted for security)</i>")
    await delete_message_safely(message)

# Step 2: Get Password
@dp.message_handler(state=ReportForm.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    log.info(f"Received password from user {message.from_user.id}.") # Avoid logging password itself
    if not message.text: # Basic check if password is empty
        await message.reply("Password cannot be empty. Please try again.")
        await delete_message_safely(message) # Delete empty attempt too
        return
    await state.update_data(password=message.text)
    await ReportForm.next()
    await message.reply("<b>Step 3/8:</b> 🖥️ Enter the SMTP server address (e.g., <code>smtp.gmail.com</code>, <code>smtp.office365.com</code>):")
    await delete_message_safely(message)

# Step 3: Get SMTP Server
@dp.message_handler(state=ReportForm.waiting_for_smtp_server)
async def process_smtp_server(message: types.Message, state: FSMContext):
    smtp_server_text = message.text.strip().lower() # Store lowercase
    if not smtp_server_text or ' ' in smtp_server_text or '.' not in smtp_server_text:
        await message.reply("Please enter a valid SMTP server address (e.g., <code>smtp.example.com</code>).")
        return
    await state.update_data(smtp_server=smtp_server_text)
    await ReportForm.next()
    await message.reply("<b>Step 4/8:</b> 🔌 Enter the SMTP port (e.g., <code>587</code> for TLS, <code>465</code> for SSL):")

# Step 4: Get SMTP Port
@dp.message_handler(state=ReportForm.waiting_for_smtp_port)
async def process_smtp_port(message: types.Message, state: FSMContext):
    port_text = message.text.strip()
    if not port_text.isdigit():
        await message.reply("Port must be a number (e.g., <code>587</code> or <code>465</code>).")
        return
    port_int = int(port_text)
    if not 1 <= port_int <= 65535:
        await message.reply("Port number must be between 1 and 65535.")
        return
    await state.update_data(smtp_port=port_int)
    await ReportForm.waiting_for_target_email.set()
    await message.reply("<b>Step 5/8:</b> 🎯 Enter the target recipient email address:")

# Step 5: Get Target Email
@dp.message_handler(state=ReportForm.waiting_for_target_email)
async def process_target_email(message: types.Message, state: FSMContext):
    target_email_text = message.text.strip()
    if '@' not in target_email_text or '.' not in target_email_text.split('@')[-1] or ' ' in target_email_text:
        await message.reply("Please enter a valid email address.")
        return
    await state.update_data(target_email=target_email_text)
    await ReportForm.next()
    await message.reply("<b>Step 6/8:</b> 📝 Enter the subject line for the emails:")
# Step 6: Get Subject
@dp.message_handler(state=ReportForm.waiting_for_subject)
async def process_subject(message: types.Message, state: FSMContext):
    subject_text = message.text.strip()
    if not subject_text:
        await message.reply("Subject cannot be empty. Please enter a subject line.")
        return
    await state.update_data(subject=subject_text)
    await ReportForm.next()
    await message.reply("<b>Step 7/8:</b> 📋 Enter the email body text:")

# Step 7: Get Body
@dp.message_handler(state=ReportForm.waiting_for_body)
async def process_body(message: types.Message, state: FSMContext):
    body_text = message.text.strip()
    if not body_text:
        await message.reply("Body cannot be empty. Please enter the email content.")
        return
    await state.update_data(body=body_text)
    await ReportForm.next()
    await message.reply("<b>Step 8/8:</b> 🔢 Enter how many emails to send (1-100):")

# Step 8: Get Count
@dp.message_handler(state=ReportForm.waiting_for_count)
async def process_count(message: types.Message, state: FSMContext):
    count_text = message.text.strip()
    if not count_text.isdigit():
        await message.reply("Please enter a valid number.")
        return
    count = int(count_text)
    if not 1 <= count <= 100:
        await message.reply("Please enter a number between 1 and 100.")
        return
    
    await state.update_data(count=count)
    user_data = await state.get_data()
    
    confirm_keyboard = InlineKeyboardMarkup()
    confirm_keyboard.add(
        InlineKeyboardButton("✅ Send", callback_data="confirm_send"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_send")
    )
    
    await ReportForm.waiting_for_confirmation.set()
    await message.reply(
        f"<b>📋 Review your settings:</b>\n\n"
        f"From: <code>{user_data['email']}</code>\n"
        f"To: <code>{user_data['target_email']}</code>\n"
        f"Subject: <code>{user_data['subject']}</code>\n"
        f"Count: <code>{count}</code>\n\n"
        f"Ready to send?",
        reply_markup=confirm_keyboard
    )

# Handle confirmation buttons
@dp.callback_query_handler(state=ReportForm.waiting_for_confirmation)
async def process_confirmation(callback_query: types.CallbackQuery, state: FSMContext):
    if callback_query.data == "cancel_send":
        await state.finish()
        await callback_query.message.edit_text("❌ Operation cancelled.")
        return
        
    if callback_query.data == "confirm_send":
        await callback_query.message.edit_text("📤 Sending emails...")
        user_data = await state.get_data()
        
        success, message = await send_emails_async(user_data, callback_query.from_user.id)
        await state.finish()
        await callback_query.message.edit_text(message)

if __name__ == '__main__':
    # Load premium users on startup
    load_premium_users()
    # Start the bot
    executor.start_polling(dp, skip_updates=True)
