import os
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Railway Variables
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ.get("SESSION_STRING")
LOG_CHANNEL = int(os.environ["LOG_CHANNEL"])
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# Supabase setup
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Globals loaded from DB
MAPPINGS = {}
LAST_IDS = {}

async def load_config():
    global MAPPINGS, LAST_IDS
    try:
        data = supabase.table("config").select("*").eq("id", 1).execute()
        if data.data:
            MAPPINGS = data.data[0]["mappings"] or {}
            LAST_IDS = {int(k): v for k, v in (data.data[0]["last_ids"] or {}).items()}
            logger.info(f"Loaded {len(MAPPINGS)} mappings from Supabase")
        else:
            logger.info("No config found. Starting empty.")
    except Exception as e:
        logger.error(f"Failed to load config: {e}")

async def save_config():
    try:
        supabase.table("config").update({
            "mappings": MAPPINGS,
            "last_ids": {str(k): v for k, v in LAST_IDS.items()}
        }).eq("id", 1).execute()
        logger.info("Config saved to Supabase")
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

# Telethon client
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

@client.on(events.NewMessage(pattern="/start"))
async def start(event):
    await event.reply("Bot online. Using Supabase for storage.\n\n**Commands:**\n`/addsource source_id target_id`\n`/removesource source_id`\n`/listmappings`")

@client.on(events.NewMessage(pattern="/addsource"))
async def add_source(event):
    try:
        _, source, target = event.text.split()
        MAPPINGS[source] = target
        await save_config()
        await event.reply(f"Added: `{source}` → `{target}`\nSaved to Supabase.")
    except:
        await event.reply("Usage: `/addsource -100111 -100222`")

@client.on(events.NewMessage(pattern="/removesource"))
async def remove_source(event):
    try:
        _, source = event.text.split()
        if source in MAPPINGS:
            del MAPPINGS[source]
            if int(source) in LAST_IDS:
                del LAST_IDS[int(source)]
            await save_config()
            await event.reply(f"Removed: `{source}`\nSaved to Supabase.")
        else:
            await event.reply("Source not found.")
    except:
        await event.reply("Usage: `/removesource -100111`")

@client.on(events.NewMessage(pattern="/listmappings"))
async def list_mappings(event):
    if not MAPPINGS:
        await event.reply("No mappings set.")
        return
    text = "**Current mappings:**\n"
    for s, t in MAPPINGS.items():
        text += f"`{s}` → `{t}`\n"
    await event.reply(text)

@client.on(events.NewMessage())
async def copy_handler(event):
    if str(event.chat_id) not in MAPPINGS:
        return

    # Skip commands
    if event.text and event.text.startswith('/'):
        return

    target = int(MAPPINGS[str(event.chat_id)])

    # Skip duplicates on restart
    if LAST_IDS.get(event.chat_id) == event.id:
        return

    try:
        await client.send_message(target, event.message)
        LAST_IDS[event.chat_id] = event.id
        await save_config()
    except Exception as e:
        logger.error(f"Copy failed: {e}")
        await client.send_message(LOG_CHANNEL, f"Copy failed from `{event.chat_id}`: {e}")

async def main():
    await load_config()
    await client.start()
    me = await client.get_me()
    await client.send_message(LOG_CHANNEL, f"Bot started as {me.first_name}. Supabase ready.")
    logger.info("Bot running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())