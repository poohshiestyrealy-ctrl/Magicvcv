from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
import asyncio, random, os

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
SOURCE_CHANNEL = int(os.environ.get("SOURCE_CHANNEL"))
TARGET_CHANNEL = int(os.environ.get("TARGET_CHANNEL"))

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def handler(event):
    if event.video:
        try:
            await client.send_file(TARGET_CHANNEL, event.video, caption="")
            print(f"Forwarded video: {event.video.id}")
            await asyncio.sleep(random.randint(10, 15))
        except Exception as e:
            print(f"Error: {e}")

print("Bot started - mirroring videos only")
client.run_until_disconnected()