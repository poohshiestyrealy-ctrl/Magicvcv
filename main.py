import os
import asyncio
import random
from telethon import TelegramClient, events, errors
from telethon.tl.types import PeerChannel
from supabase import create_client, Client

# --- ENV VARS ---
API_ID = int(os.environ['API_ID'])
API_HASH = os.environ['API_HASH']
BOT_TOKEN = os.environ['BOT_TOKEN']
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']
LOG_CHANNEL = int(os.environ['LOG_CHANNEL'])
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_IDS', '').split(',') if x]

# --- INIT ---
client = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

SOURCE_TARGET_MAP = {}
VIDEOS_PROCESSED = 0

# --- DB HELPERS ---
async def get_last_ids():
    try:
        res = supabase.table('last_ids').select('*').execute()
        return {int(row['source_id']): int(row['last_id']) for row in res.data}
    except Exception as e:
        print(f"Error fetching last_ids: {e}")
        return {}

async def save_last_id(source_id, last_id, target_id):
    try:
        supabase.table('last_ids').upsert({
            'source_id': str(source_id),
            'last_id': int(last_id),
            'target_id': str(target_id)
        }).execute()
    except Exception as e:
        print(f"Error saving last_id: {e}")

async def reload_sources():
    global SOURCE_TARGET_MAP
    try:
        res = supabase.table('sources').select('*').execute()
        SOURCE_TARGET_MAP = {int(row['source_id']): int(row['target_id']) for row in res.data}
        print(f"Loaded {len(SOURCE_TARGET_MAP)} source->target mappings")
    except Exception as e:
        print(f"Error loading sources: {e}")
        SOURCE_TARGET_MAP = {}

# --- DECORATORS ---
def admin_only(func):
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            await event.reply("Unauthorized")
            return
        return await func(event)
    return wrapper

# --- CORE LOGIC: RE-UPLOAD FOR CLEAN POSTS ---
async def forward_video(message):
    source_id = message.chat_id
    if source_id not in SOURCE_TARGET_MAP:
        return
    target_id = SOURCE_TARGET_MAP[source_id]

    # 45-90s delay to avoid PEER_FLOOD. Bump to 90-150 if you get banned.
    delay = random.randint(45, 90)
    print(f"Safe delay: {delay}s before re-upload of {message.id} to {target_id}")
    await asyncio.sleep(delay)

    try:
        if message.video:
            await client.send_file(target_id, message.video, caption="")
        elif message.document and message.document.mime_type and message.document.mime_type.startswith('video'):
            await client.send_file(target_id, message.document, caption="")
        else:
            return

        await save_last_id(source_id, message.id, target_id)
        global VIDEOS_PROCESSED
        VIDEOS_PROCESSED += 1
        print(f"RE-UPLOADED {message.id} from {source_id} -> {target_id}")

    except errors.FloodWaitError as e:
        print(f"FloodWait {e.seconds}s - sleeping")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        print(f"Error re-uploading {message.id}: {e}")

# --- PARALLEL PROCESSING ---
async def process_source(source_id):
    last_id = (await get_last_ids()).get(source_id, 0)
    print(f"Processing {source_id} from ID {last_id}")
    async for message in client.iter_messages(source_id, min_id=last_id, reverse=True):
        if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
            await forward_video(message)

async def startup_task():
    await reload_sources()
    tasks = [process_source(source) for source in SOURCE_TARGET_MAP.keys()]
    if tasks:
        await asyncio.gather(*tasks)
    await client.send_message(LOG_CHANNEL, f"Bot started. {len(SOURCE_TARGET_MAP)} sources loaded. Clean post mode.")

# --- COMMANDS ---
@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/start'))
@admin_only
async def start_handler(event):
    await event.reply("Bot is running. Clean post mode active.\n/start, /add, /remove, /reload, /status")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/add'))
@admin_only
async def add_handler(event):
    try:
        _, source, target = event.text.split()
        supabase.table('sources').upsert({
            'source_id': source,
            'target_id': target
        }).execute()
        await reload_sources()
        await event.reply(f"Added {source} -> {target}")
    except Exception as e:
        await event.reply(f"Usage: /add source_id target_id\nError: {e}")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/remove'))
@admin_only
async def remove_handler(event):
    try:
        _, source = event.text.split()
        supabase.table('sources').delete().eq('source_id', source).execute()
        await reload_sources()
        await event.reply(f"Removed {source}")
    except Exception as e:
        await event.reply(f"Usage: /remove source_id\nError: {e}")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/reload'))
@admin_only
async def reload_handler(event):
    await reload_sources()
    await event.reply(f"Reloaded {len(SOURCE_TARGET_MAP)} mappings. Starting parallel processing...")
    tasks = [process_source(source) for source in SOURCE_TARGET_MAP.keys()]
    if tasks:
        await asyncio.gather(*tasks)

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/status'))
@admin_only
async def status_handler(event):
    last_ids = await get_last_ids()
    status = f"**Status**\nVideos processed: {VIDEOS_PROCESSED}\n\n"
    for source, target in SOURCE_TARGET_MAP.items():
        last = last_ids.get(source, 0)
        status += f"`{source}` -> `{target}` | Last: {last}\n"
    bandwidth = VIDEOS_PROCESSED * 0.4  # 200MB*2 = 0.4GB per video
    status += f"\nEst. bandwidth used: {bandwidth:.1f} GB"
    await event.reply(status)

# --- RUN ---
async def main():
    await startup_task()

with client:
    client.loop.run_until_complete(main())
    client.run_until_disconnected()