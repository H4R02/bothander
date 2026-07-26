import os
import asyncio
import imaplib
import email
from email.header import decode_header
import re
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
import telethon.errors
from aiohttp import web
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from motor.motor_asyncio import AsyncIOMotorClient
import logging
logging.basicConfig(level=logging.INFO) # <-- Yeh line add karein

# 1. Environment variables load karein
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")
OWNER_ID = int(os.getenv("OWNER_ID"))
API_ID = os.getenv("API_ID", "") 
API_HASH = os.getenv("API_HASH", "")

# 2. Bot, Dispatcher aur Database setup
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# MongoDB se connect karein
db_client = AsyncIOMotorClient(MONGO_URL)
db = db_client["OTP_Vault"]
gmails_collection = db["gmails"]
# Telegram accounts ke liye nayi collection
tg_collection = db["telegram_accounts"]

# Temp clients save karne ke liye dictionary (taaki login beech me kate na)
temp_clients = {}

# FSM States (Bot ko yaad dilane ke liye ki wo kya mang raha hai)
class TgLogin(StatesGroup):
    phone = State()
    code = State()
    password = State()

# ==========================================
# 🔒 OWNER CHECK (Security Layer)
# ==========================================
# Yeh function check karega ki message aapne bheja hai ya kisi aur ne
async def is_owner(message: types.Message) -> bool:
    if message.from_user.id != OWNER_ID:
        # Agar koi aur user start karega, bot usko block/ignore kar dega
        print(f"Unauthorized access alert! User ID: {message.from_user.id}")
        return False
    return True

# ==========================================
# 📧 GMAIL IMAP FETCHER (Background Worker)
# ==========================================
def fetch_latest_email_sync(email_address, app_password):
    try:
        # Gmail se connect karein
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_address, app_password)
        mail.select("inbox")
        
        # Sabse latest mail (ALL me se aakhiri) search karein
        status, messages = mail.search(None, "ALL")
        email_ids = messages[0].split()
        
        if not email_ids:
            return "📭 Aapka Inbox bilkul khali hai."
            
        latest_email_id = email_ids[-1]
        status, msg_data = mail.fetch(latest_email_id, "(RFC822)")
        
        response_text = ""
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                
                # 1. Subject Decode Karein
                subject, encoding = decode_header(msg["Subject"])[0]
                if isinstance(subject, bytes):
                    # errors='ignore' add kiya hai taaki koi ajeeb character hone par error na aaye
                    subject = subject.decode(encoding if encoding else "utf-8", errors='ignore')
                
                response_text += f"📌 **Subject:** {subject}\n\n"
                
                # 2. Body Extract Karein (Plain Text)
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body = payload.decode(errors='ignore')
                            break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body = payload.decode(errors='ignore')
                
                # 3. OTP / Highlighted Code Extract Karein (Optional)
                otps = re.findall(r'\b\d{4,8}\b', body)
                if otps:
                    response_text += f"🔑 **Possible Code(s):** `{', '.join(otps[:3])}`\n\n"
                
                # 4. Pura Message Dikhayein
                # Telegram limit 4096 chars ki hoti hai, isliye hum max 3800 chars bhejenge
                safe_body = body[:4096]
                
                if len(body) > 3800:
                    safe_body += "\n\n⚠️ [Email lamba hone ki wajah se aage ka hissa cut gaya hai...]"
                    
                response_text += f"📝 **Complete Message:**\n\n{safe_body}"
                
        mail.logout()
        return response_text
    
    except imaplib.IMAP4.error:
        return "❌ Login Failed! App password ya Email galat hai."
    except Exception as e:
        return f"❌ Error: {e}"

# ==========================================
# 🤖 BOT COMMANDS
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
        "/getmail <alias> - OTP/Latest mail dekhein"
    )
    await message.answer(welcome_text)

@dp.message(Command("addmail"))
async def add_mail_command(message: types.Message):
    if not await is_owner(message): return
    
    # User message ko split karke data nikalna
    # Example command format: /addmail gaming pro-gamer@gmail.com app_password
    args = message.text.split()
    
    if len(args) != 4:
        await message.answer("⚠️ Sahi format use karein:\n`/addmail <alias> <email> <app_password>`")
        return
        
    alias, email_id, app_pass = args[1], args[2], args[3]
    
    # MongoDB me data insert karna
    try:
        # Check karna ki alias pehle se exist toh nahi karta
        existing = await gmails_collection.find_one({"alias": alias})
        if existing:
            await message.answer(f"❌ '{alias}' naam se pehle hi ek mail save hai. Dusra naam use karein.")
            return

        new_account = {
            "alias": alias,
            "email": email_id,
            "app_password": app_pass
        }
        await gmails_collection.insert_one(new_account)
        await message.answer(f"✅ Success! **{email_id}** as '{alias}' save ho gaya hai.")
    except Exception as e:
        await message.answer(f"❌ Database Error: {e}")

@dp.message(Command("listmails"))
async def list_mails_command(message: types.Message):
    if not await is_owner(message): return
    
    cursor = gmails_collection.find({})
    accounts = await cursor.to_list(length=100)
    
    if not accounts:
        await message.answer("📭 Abhi tak koi mail save nahi hai.")
        return
        
    response = "📂 **Aapke Saved Gmail Accounts:**\n\n"
    for acc in accounts:
        response += f"🔹 **{acc['alias']}** - `{acc['email']}`\n"
        
    await message.answer(response)

@dp.message(Command("getmail"))
async def get_mail_command(message: types.Message):
    if not await is_owner(message): return
    
    args = message.text.split()
    if len(args) != 2:
        await message.answer("⚠️ Sahi format: `/getmail <alias>`\nExample: `/getmail main`")
        return
        
    alias = args[1]
    
    # 1. Database se account dhundhein
    account = await gmails_collection.find_one({"alias": alias})
    
    if not account:
        await message.answer(f"❌ '{alias}' naam ka koi account nahi mila. Pehle `/listmails` check karein.")
        return
        
    # 2. Loading message bhejein
    wait_msg = await message.answer("⏳ Inbox check kar raha hoon... kripya wait karein.")
    
    # 3. Background thread me IMAP function chalayein
    email_address = account["email"]
    app_password = account["app_password"]
    
    result = await asyncio.to_thread(fetch_latest_email_sync, email_address, app_password)
    
    # 4. Result Edit karke dikhayein
    await wait_msg.edit_text(f"📧 **Alias:** {alias}\n\n{result}")

# ==========================================
# 📱 TELEGRAM ACCOUNT LOGIN COMMANDS (FSM)
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
    
    wait_msg = await message.answer("⏳ Telegram server se connect kar raha hoon aur OTP bhej raha hoon...")
    
    # Telethon Client initialize karein
    client = TelegramClient(
        StringSession(), 
        API_ID, 
        API_HASH,
        device_model="Titan OTP Vault",
        system_version="Core 1.0",
        app_version="TitanBot v1.0"
    )
    await client.connect()
    
    try:
        # OTP bhejne ki request
        code_request = await client.send_code_request(phone)
        phone_code_hash = code_request.phone_code_hash
        
        # Is connection ko temporary save karein
        temp_clients[message.from_user.id] = {
            "client": client,
            "phone_code_hash": phone_code_hash
        }
        
        await wait_msg.edit_text("✅ OTP bhej diya gaya hai!\n\nKripya us account ke official Telegram app (777000 chat) me aaya hua login OTP yahan bhejein:")
        await state.set_state(TgLogin.code)
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ Error: {e}\nKripya number check karein aur wapas /addtg bhejein.")
        await state.clear()

@dp.message(TgLogin.code)
async def process_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    data = await state.get_data()
    phone = data['phone']
    
    client_data = temp_clients.get(message.from_user.id)
    if not client_data:
        await message.answer("❌ Session timeout ho gaya. Kripya wapas /addtg start karein.")
        await state.clear()
        return
        
    client = client_data["client"]
    phone_code_hash = client_data["phone_code_hash"]
    
    wait_msg = await message.answer("⏳ OTP verify kar raha hoon...")
    
    try:
        # OTP ke sath Sign In karein
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        
        # Agar successful hua toh string banakar MongoDB me save karein
        session_string = client.session.save()
        await tg_collection.insert_one({"phone": phone, "session_string": session_string})
        
        await wait_msg.edit_text(f"✅ Success! Aapka Telegram account ({phone}) database me save ho gaya hai.")
        await client.disconnect()
        del temp_clients[message.from_user.id]
        await state.clear()
        
    except telethon.errors.SessionPasswordNeededError:
        # Agar 2-Step Verification laga ho
        await wait_msg.edit_text("🔒 Is account par 2-Step Verification (Cloud Password) laga hai. Kripya apna password bhejein:")
        await state.set_state(TgLogin.password)
        
    except Exception as e:
        await wait_msg.edit_text(f"❌ OTP Error: {e}\nAgar OTP galat hai toh wapas /addtg bhejein.")
        await client.disconnect()
        await state.clear()

@dp.message(TgLogin.password)
async def process_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    phone = data['phone']
    
    client_data = temp_clients.get(message.from_user.id)
    client = client_data["client"]
    
    wait_msg = await message.answer("⏳ Password verify kar raha hoon...")
    
    try:
        # Password ke sath Sign In karein
        await client.sign_in(password=password)
        
        # Save to DB
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
# 🌐 DUMMY WEB SERVER (For Render Port Binding)
# ==========================================
async def handle_ping(request):
    return web.Response(text="Bot is Live and Running!")

async def web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    # Render automatic $PORT environment variable deta hai
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Dummy web server started on port {port}")

# ==========================================
# 🔄 TELEGRAM SESSION KEEP-ALIVE (Auto-Ping)
# ==========================================
async def keep_sessions_alive():
    while True:
        try:
            print("🔄 Checking all Telegram Sessions to keep them alive...")
            cursor = tg_collection.find({})
            accounts = await cursor.to_list(length=100)
            
            for acc in accounts:
                session_str = acc.get("session_string")
                phone = acc.get("phone")
                
                if session_str:
                    # Custom Device Name yahan set kiya gaya hai
                    client = TelegramClient(
                        StringSession(session_str), 
                        API_ID, 
                        API_HASH,
                        device_model="Titan OTP Vault",
                        system_version="Core 1.0",
                        app_version="TitanBot v1.0"
                    )
                    await client.connect()
                    
                    if await client.is_user_authorized():
                        await client.get_me() # Ping server
                        print(f"✅ Session kept alive for: {phone}")
                    else:
                        print(f"⚠️ Session expired or invalid for: {phone}")
                        
                    await client.disconnect()
        except Exception as e:
            print(f"❌ Keep Alive Error: {e}")
            
        # Har 12 ghante (43200 seconds) baad dobara check karega
        await asyncio.sleep(43200)

# ==========================================
# 🚀 BOT RUNNER
# ==========================================
async def main():
    print("Bot is starting...")
    # 1. Background me Dummy Web Server start karein Render ke liye
    asyncio.create_task(web_server())
    
    # 2. Telegram Sessions ko zinda rakhne wala task start karein
    asyncio.create_task(keep_sessions_alive())
    
    # 3. Telegram Bot start karein
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
