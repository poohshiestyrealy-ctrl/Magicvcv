from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
import asyncio, random, os

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
SOURCE_CHANNEL = int(os.environ.get("SOURCE_CHANNEL"))
TARGET_CHANNEL = int(os.environ.get("TARGET_CHANNEL"))
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

async def get_last_id():
    try:
        # Get last message from log channel
        msg = await client.get_messages(LOG_CHANNEL, limit=1)
        return int(msg[0].message) if msg else 0
    except:
        return 0

async def save_last_id(msg_id):
    await client.send_message(LOG_CHANNEL, str(msg_id))

async def forward_video(message):
    try:
        await client.send_file(TARGET_CHANNEL, message.video, caption="")
        print(f"Forwarded video: {message.id}")
        await save_last_id(message.id) # bulletproof save
        await asyncio.sleep(random.randint(120, 180)) # 2-3 min delay
    except Exception as e:
        print(f"Error: {e}")
        if "FloodWaitError" in str(e):
            wait_time = int(str(e).split()[3])
            print(f"FloodWait: sleeping {wait_time}s")
            await asyncio.sleep(wait_time)

@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def handler(event):
    if event.video:
        await forward_video(event)

async def main():
    await client.start()
    last_id = await get_last_id()
    print(f"Bot started - resuming after video ID {last_id}")
    
    # Get all unsent videos, oldest first
    messages = []
    async for message in client.iter_messages(SOURCE_CHANNEL, min_id=last_id):
        if message.video:
            messages.append(message)
    
    for message in reversed(messages):
        print(f"Processing old video {message.id}")
        await forward_video(message)
    
    print("Done with old videos. Mirroring new videos only")
    await client.run_until_disconnected()

client.loop.run_until_complete(main())