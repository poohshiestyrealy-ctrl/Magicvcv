import asyncio
import os
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# Railway env vars
API_ID = int(os.environ['API_ID'])
API_HASH = os.environ['API_HASH'] 
SESSION_STRING = os.environ['SESSION_STRING']
SOURCE = os.environ['SOURCE_CHANNEL']  # @channel or -1001234567890
TARGET = os.environ['TARGET_CHANNEL']  # @your_channel or -1009876543210

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger(__name__)

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

@client.on(events.NewMessage(chats=SOURCE))
async def mirror_video(event):
    if not event.video:
        return
    
    try:
        log.info(f"New video in source: {event.id}")
        
        # Download even if "Forwarding not allowed" is ON
        file = await event.download_media(file=bytes)
        size_mb = len(file) / 1024
        log.info(f"Downloaded {size_mb:.1f}MB")
        
        # Re-upload to target as new video
        await client.send_file(
            TARGET,
            file=file,
            caption=event.text or "",
            supports_streaming=True,
            progress_callback=lambda c, t: log.info(f"Upload: {c/t*100:.0f}%") if c and t else None
        )
        
        log.info(f"Mirrored to target successfully")
        await asyncio.sleep(5)  # Avoid flood ban
        
    except Exception as e:
        log.error(f"Mirror failed: {e}")

async def main():
    await client.start()
    me = await client.get_me()
    log.info(f"Mirror bot started as {me.first_name}")
    log.info(f"Watching {SOURCE} -> {TARGET}")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
