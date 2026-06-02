import os
import asyncio
import imaplib
import email
from email.header import decode_header
import re
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from motor.motor_asyncio import AsyncIOMotorClient
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import telethon.errors

# ==========================================
# 1. ENVIRONMENT & SETUP
# ==========================================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
# Syntax error fixed here 👇
OWNER_ID = int(os.getenv("OWNER_ID"))
API_ID = os.getenv("API_ID", "") 
API_HASH = os.getenv("API_HASH", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# MongoDB Setup
db_client = AsyncIOMotorClient(MONGO_URL)
db = db_client["OTP_Vault"]
gmails_collection = db["gmails"]
tg_collection = db["telegram_accounts"]  # Telegram collection

# Temp clients for FSM Login
temp_clients = {}

# Telegram Login States
class TgLogin(StatesGroup):
    phone = State()
    code = State()
    password = State()

# ==========================================
# 🔒 OWNER CHECK & GMAIL FETCHER
# ==========================================
async def is_owner(message: types.Message) -> bool:
    if message.from_user.id != OWNER_ID:
        print(f"Unauthorized access alert! User ID: {message.from_user.id}")
        return False
    return True

def fetch_latest_email_sync(email_address, app_password):
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_address, app_password)
        mail.select("inbox")
        status, messages = mail.search(None, "ALL")
        email_ids = messages[0].split()
        
        if not email_ids: return "📭 Aapka Inbox bilkul khali hai."
            
        latest_email_id = email_ids[-1]
        status, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        
        response_text = ""
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding if encoding else "utf-8")
                
                response_text += f"📌 **Subject:** {subject}\n\n"
                
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode()
                            break
                else:
                    body = msg.get_payload(decode=True).decode()
                
                otps = re.findall(r'\b\d{4,8}\b', body)
                if otps:
                    response_text += f"🔑 **Possible OTP(s):** `{', '.join(otps[:3])}`\n\n"
                else:
                    response_text += "⚠️ Koi direct OTP detect nahi hua. Niche detail dekhein:\n\n"
                response_text += f"📝 **Message:**\n{body[:250]}..."
                
        mail.logout()
        return response_text
    
    except imaplib.IMAP4.error:
        return "❌ Login Failed! App password ya Email galat hai."
    except Exception as e:
        return f"❌ Error: {e}"

# ==========================================
# 🤖 GMAIL COMMANDS
# ==========================================
@dp.message(Command("start"))
async def start_command(message: types.Message):
    if not await is_owner(message): return
    welcome_text = (
        "👋 Welcome Boss!\n\n"
        "Aapka Personal OTP Vault active hai.\n"
        "Comamnds:\n"
        "/addmail <alias> <email> <password> - Naya mail add karein\n"
        "/listmails - Save kiye mails dekhein\n"
        "/getmail <alias> - OTP/Latest mail dekhein\n"
        "/addtg - Naya Telegram account add karein"
    )
    await message.answer(welcome_text)

@dp.message(Command("addmail"))
async def add_mail_command(message: types.Message):
    if not await is_owner(message): return
    args = message.text.split()
    if len(args) != 4:
        await message.answer("⚠️ Sahi format use karein:\n`/addmail <alias> <email> <app_password>`")
        return
    alias, email_id, app_pass = args[1], args[2], args[3]
    try:
        existing = await gmails_collection.find_one({"alias": alias})
        if existing:
            await message.answer(f"❌ '{alias}' naam se pehle hi ek mail save hai.")
            return
        await gmails_collection.insert_one({"alias": alias, "email": email_id, "app_password": app_pass})
        await message.answer(f"✅ Success! **{email_id}** as '{alias}' save ho gaya hai.")
    except Exception as e:
        await message.answer(f"❌ Database Error: {e}")

@dp.message(Command("listmails"))
async def list_mails_command(message: types.Message):
    if not await is_owner(message): return
    accounts = await gmails_collection.find({}).to_list(length=100)
    if not accounts:
        await message.answer("📭 Abhi tak koi mail save nahi hai.")
        return
    response = "📂 **Aapke Saved Gmail Accounts:**\n\n"
    for acc in accounts: response += f"🔹 **{acc['alias']}** - `{acc['email']}`\n"
    await message.answer(response)

@dp.message(Command("getmail"))
async def get_mail_command(message: types.Message):
    if not await is_owner(message): return
    args = message.text.split()
    if len(args) != 2:
        await message.answer("⚠️ Sahi format: `/getmail <alias>`")
        return
    alias = args[1]
    account = await gmails_collection.find_one({"alias": alias})
    if not account:
        await message.answer(f"❌ '{alias}' naam ka koi account nahi mila.")
        return
    wait_msg = await message.answer("⏳ Inbox check kar raha hoon...")
    result = await asyncio.to_thread(fetch_latest_email_sync, account["email"], account["app_password"])
    await wait_msg.edit_text(f"📧 **Alias:** {alias}\n\n{result}")

# ==========================================
# 📱 TELEGRAM LOGIN SYSTEM (FSM)
# ==========================================
@dp.message(Command("addtg"))
async def add_tg_start(message: types.Message, state: FSMContext):
    if not await is_owner(message): return
    await message.answer("📱 Bina recharge wale Telegram account ka Phone Number bhejein\n(Country code ke sath, jaise +919876543210):")
    await state.set_state(TgLogin.phone)

@dp.message(TgLogin.phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip()
    await state.update_data(phone=phone)
    wait_msg = await message.answer("⏳ Telegram server se connect kar raha hoon...")
    
    # Yahan Device Name add kiya gaya hai
    client = TelegramClient(
        StringSession(), API_ID, API_HASH,
        device_model="Titan OTP Vault",
        system_version="Core 1.0",
        app_version="TitanBot v1.0"
    )
    await client.connect()
    try:
        code_request = await client.send_code_request(phone)
        temp_clients[message.from_user.id] = {"client": client, "phone_code_hash": code_request.phone_code_hash}
        await wait_msg.edit_text("✅ OTP bhej diya gaya hai!\n\nKripya us account ke official Telegram app me aaya hua login OTP yahan bhejein (Space de kar bhejein jaise: 1 2 3 4 5):")
        await state.set_state(TgLogin.code)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Error: {e}\nKripya number check karein aur wapas /addtg bhejein.")
        await state.clear()

@dp.message(TgLogin.code)
async def process_code(message: types.Message, state: FSMContext):
    # Agar user space de kar code bhejta hai toh space hata do
    code = message.text.strip().replace(" ", "")
    data = await state.get_data()
    phone = data['phone']
    client_data = temp_clients.get(message.from_user.id)
    
    if not client_data:
        await message.answer("❌ Session timeout. Wapas /addtg start karein.")
        await state.clear()
        return
        
    client, phone_code_hash = client_data["client"], client_data["phone_code_hash"]
    wait_msg = await message.answer("⏳ OTP verify kar raha hoon...")
    
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        session_string = client.session.save()
        await tg_collection.insert_one({"phone": phone, "session_string": session_string})
        await wait_msg.edit_text(f"✅ Success! Aapka Telegram account ({phone}) database me save ho gaya hai.")
        await client.disconnect()
        del temp_clients[message.from_user.id]
        await state.clear()
    except telethon.errors.SessionPasswordNeededError:
        await wait_msg.edit_text("🔒 Is account par 2-Step Verification (Cloud Password) laga hai. Apna password bhejein:")
        await state.set_state(TgLogin.password)
    except Exception as e:
        await wait_msg.edit_text(f"❌ OTP Error: {e}\nWapas /addtg try karein.")
        await client.disconnect()
        await state.clear()

@dp.message(TgLogin.password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    phone = data['phone']
    client = temp_clients.get(message.from_user.id)["client"]
    wait_msg = await message.answer("⏳ Password verify kar raha hoon...")
    
    try:
        await client.sign_in(password=password)
        session_string = client.session.save()
        await tg_collection.insert_one({"phone": phone, "session_string": session_string})
        await wait_msg.edit_text(f"✅ Success! Aapka Telegram account ({phone}) database me save ho gaya hai (With 2FA).")
        await client.disconnect()
        del temp_clients[message.from_user.id]
        await state.clear()
    except Exception as e:
        await wait_msg.edit_text(f"❌ Password Error: {e}\nGalat password. Wapas /addtg try karein.")
        await client.disconnect()
        await state.clear()

# ==========================================
# 🔄 TELEGRAM KEEP-ALIVE (Har 12 ghante ping)
# ==========================================
async def keep_sessions_alive():
    while True:
        try:
            print("🔄 Checking all Telegram Sessions to keep them alive...")
            accounts = await tg_collection.find({}).to_list(length=100)
            for acc in accounts:
                session_str, phone = acc.get("session_string"), acc.get("phone")
                if session_str:
                    client = TelegramClient(StringSession(session_str), API_ID, API_HASH, device_model="Titan OTP Vault", system_version="Core 1.0", app_version="TitanBot v1.0")
                    await client.connect()
                    if await client.is_user_authorized():
                        await client.get_me()
                        print(f"✅ Session kept alive for: {phone}")
                    else:
                        print(f"⚠️ Session expired for: {phone}")
                    await client.disconnect()
        except Exception as e:
            print(f"❌ Keep Alive Error: {e}")
        await asyncio.sleep(43200) # 12 ghante me ek baar

# ==========================================
# 🌐 DUMMY WEB SERVER & RUNNER
# ==========================================
async def handle_ping(request):
    return web.Response(text="Titan OTP Vault is Live and Running!")

async def web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Dummy web server started on port {port}")

async def main():
    print("Bot is starting...")
    asyncio.create_task(web_server())          # 1. Render Port Setup
    asyncio.create_task(keep_sessions_alive()) # 2. Anti-Expire Ping
    await dp.start_polling(bot)                # 3. Main Bot

if __name__ == "__main__":
    asyncio.run(main())
