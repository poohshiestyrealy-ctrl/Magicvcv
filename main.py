import os
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAnimated
from telethon.tl.functions.messages import SearchRequest
from supabase import create_client, Client

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Env vars
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Userbot login with session string
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
client.start()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.reply("Userbot is running. Use /addsource, /scrape, /addgif, /addshort")

@client.on(events.NewMessage(pattern='/addsource'))
async def add_source(event):
    try:
        args = event.message.text.split()
        if len(args)!= 3:
            await event.reply("Usage: /addsource <source_id> <target_id>")
            return
        source_id = int(args[1])
        target_id = int(args[2])

        supabase.table('mappings').upsert({
            'source_id': source_id,
            'target_id': target_id
        }, on_conflict='source_id').execute()

        await event.reply(f"Mapped {source_id} -> {target_id}")
    except Exception as e:
        logger.error(e)
        await event.reply(f"Error: {e}")











async def get_messages_fallback(client, entity, limit, offset_id=0):
    """Fallback for all Telethon versions"""
    result = await client(SearchRequest(
        peer=entity,
        q='',
        filter=None,
        min_date=None,
        max_date=None,
        offset_id=offset_id,
        add_offset=0,
        limit=limit,
        max_id=0,
        min_id=0,
        hash=0
    ))
    return result.messages

@client.on(events.NewMessage(pattern='/scrape'))
async def scrape(event):
    try:
        args = event.message.text.split()
        if len(args)!= 2:
            await event.reply("Usage: /scrape <source_id>")
            return

        source_id = int(args[1])
        mapping = supabase.table('mappings').select('*').eq('source_id', source_id).execute()

        if not mapping.data:
            await event.reply("Source not found")
            return

        target_id = mapping.data[0]['target_id']
        progress = supabase.table('scrape_progress').select('*').eq('source_id', source_id).execute()
        last_id = progress.data[0]['last_message_id'] if progress.data else 0

        await event.reply(f"Starting scrape from message ID {last_id}")

        count = 0
        async for msg in client.iter_messages(source_id, offset_id=last_id, reverse=True, limit=1000):
            if msg.media:
                await client.send_file(target_id, msg.media, caption=msg.text or '')
                count += 1
                if count % 10 == 0:
                    supabase.table('scrape_progress').upsert({
                        'source_id': source_id,
                        'last_message_id': msg.id
                    }, on_conflict='source_id').execute()

        await event.reply(f"Done. Forwarded {count} messages.")
    except Exception as e:
        logger.error(e)
        await event.reply(f"Error: {e}")

@client.on(events.NewMessage(pattern='/addgif'))
async def add_gif(event):
    await event.reply("GIF auto-posting started. Use /stop to stop.")
    # Add your auto-posting logic here

@client.on(events.NewMessage(pattern='/addshort'))
async def add_short(event):
    await event.reply("Shorts auto-posting started. Use /stop to stop.")
    # Add your auto-posting logic here

logger.info("Userbot started")
client.run_until_disconnected()