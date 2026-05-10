import asyncio
import random
import time
import json
from collections import deque
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PeerFloodError, ChatWriteForbiddenError

API_ID = 12345 # Your api_id
API_HASH = 'your_api_hash'
SESSION = 'mirrorbot'
BOT_TOKEN = 'your_bot_token_here'

TARGET_CHANNEL = -100123456789 # Where videos go
LOG_CHANNEL = -100987654321 # Botlogs channel for /add /remove /list
CONFIG_MSG_ID = 2 # Pinned message ID in Botlogs holding JSON config

client = TelegramClient(SESSION, API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# --- Global state ---
SOURCE_CHANNELS = set()
UPLOAD_TIMESTAMPS = deque(maxlen=20) # Track last 20 uploads for rate limit
LAST_ERROR_TIME = 0
ERROR_COUNT = 0

# --- Config helpers ---
async def load_config():
    try:
        msg = await client.get_messages(LOG_CHANNEL, ids=CONFIG_MSG_ID)
        data = json.loads(msg.text)
        return set(data.get('sources', []))
    except:
        return set()

async def save_config(sources):
    data = json.dumps({'sources': list(sources)})
    await client.edit_message(LOG_CHANNEL, CONFIG_MSG_ID, data)

async def reload_sources():
    global SOURCE_CHANNELS
    SOURCE_CHANNELS = await load_config()
    print(f"Config reloaded. Sources: {SOURCE_CHANNELS}")

# --- Dupe tracking via Botlogs ---
async def get_last_ids():
    last_ids = {}
    async for msg in client.iter_messages(LOG_CHANNEL, search=':'):
        if msg.text and ':' in msg.text:
            try:
                sid, mid = msg.text.split(':')
                sid, mid = int(sid), int(mid)
                if sid not in last_ids or mid > last_ids[sid]:
                    last_ids[sid] = mid
            except: continue
    return last_ids

async def save_last_id(source_id, msg_id):
    await client.send_message(LOG_CHANNEL, f"{source_id}:{msg_id}")

# --- Spam protection ---
async def is_protected(chat_id):
    try:
        entity = await client.get_entity(chat_id)
        return getattr(entity, 'noforwards', False)
    except:
        return False

async def check_circuit_breaker():
    global ERROR_COUNT
    if ERROR_COUNT >= 3 and time.time() - LAST_ERROR_TIME < 86400:
        print("CIRCUIT BREAKER: Too many errors. Sleeping 6 hours.")
        await asyncio.sleep(21600)
        ERROR_COUNT = 0
        return True
    return False

async def global_rate_limit():
    now = time.time()
    if len(UPLOAD_TIMESTAMPS) == 20 and now - UPLOAD_TIMESTAMPS[0] < 3600:
        sleep_for = 3600 - (now - UPLOAD_TIMESTAMPS[0]) + 10
        print(f"Global rate limit: sleeping {int(sleep_for)}s")
        await asyncio.sleep(sleep_for)

# --- Core forwarding logic ---
async def forward_video(message):
    global LAST_ERROR_TIME, ERROR_COUNT

    if await check_circuit_breaker():
        return

    try:
        source_id = message.chat_id
        protected = await is_protected(source_id)

        if protected:
            # Protected = native forward, no upload = safest
            await client.forward_messages(TARGET_CHANNEL, message)
            print(f"FORWARDED {message.id} from {source_id} - protected")
        else:
            # Unprotected = re-upload with delays
            await global_rate_limit()
            delay = random.randint(180, 300) # 3-5 min. Higher = safer
            print(f"Safe delay: {delay}s before re-upload")
            await asyncio.sleep(delay)

            await client.send_file(TARGET_CHANNEL, message.video, caption="")
            UPLOAD_TIMESTAMPS.append(time.time())
            print(f"RE-UPLOADED {message.id} from {source_id} - clean")

        await save_last_id(source_id, message.id)
        ERROR_COUNT = 0 # Reset on success

    except FloodWaitError as e:
        LAST_ERROR_TIME = time.time()
        ERROR_COUNT += 1
        wait_time = e.seconds + random.randint(30, 60)
        print(f"FloodWait: sleeping {wait_time}s")
        await asyncio.sleep(wait_time)
    except PeerFloodError:
        LAST_ERROR_TIME = time.time()
        ERROR_COUNT += 1
        print("PEER_FLOOD: Account limited. Sleeping 3 hours.")
        await asyncio.sleep(10800)
    except ChatWriteForbiddenError:
        print(f"BANNED from {TARGET_CHANNEL}. Stopping.")
        return
    except Exception as e:
        LAST_ERROR_TIME = time.time()
        ERROR_COUNT += 1
        backoff = min(1800, 60 * (2 ** ERROR_COUNT))
        print(f"Error {message.id}: {e}. Backoff {backoff}s")
        await asyncio.sleep(backoff)

# --- Bot commands in Botlogs ---
@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/add'))
async def add_handler(event):
    try:
        source_id = int(event.text.split()[1])
        sources = await load_config()
        sources.add(source_id)
        await save_config(sources)
        await event.reply(f"Added `{source_id}`. Send /reload to apply.")
    except:
        await event.reply("Usage: /add -100123456789")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/remove'))
async def remove_handler(event):
    try:
        source_id = int(event.text.split()[1])
        sources = await load_config()
        sources.discard(source_id)
        await save_config(sources)
        await event.reply(f"Removed `{source_id}`. Send /reload to apply.")
    except:
        await event.reply("Usage: /remove -100123456789")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/list'))
async def list_handler(event):
    sources = await load_config()
    if sources:
        await event.reply(f"Active sources: `{list(sources)}`")
    else:
        await event.reply("No sources configured. Use /add -100...")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/reload'))
async def reload_handler(event):
    global SOURCE_CHANNELS
    await reload_sources()
    await event.reply(f"Reloaded {len(SOURCE_CHANNELS)} sources. Now listening to: `{list(SOURCE_CHANNELS)}`")

    # Scan old videos after reload
    last_ids = await get_last_ids()
    print(f"Rescanning after /reload. Resuming from: {last_ids}")

    for source in SOURCE_CHANNELS:
        last_id = last_ids.get(source, 0)
        print(f"Checking {source} from ID {last_id}")
        messages = []
        async for message in client.iter_messages(source, min_id=last_id):
            if message.video or (message.document and message.document.mime_type.startswith('video')):
                messages.append(message)
        for message in reversed(messages): # Oldest first
            await forward_video(message)

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/help'))
async def help_handler(event):
    await event.reply(
        "**Commands:**\n"
        "`/add -100...` - Add source channel\n"
        "`/remove -100...` - Remove source\n"
        "`/list` - Show active sources\n"
        "`/reload` - Apply changes + scan old videos\n"
        "`/help` - This message"
    )

# --- Live mirroring ---
@client.on(events.NewMessage())
async def video_handler(event):
    if event.chat_id in SOURCE_CHANNELS and (event.video or (event.document and event.document.mime_type.startswith('video'))):
        await forward_video(event)

# --- Startup ---
async def main():
    await reload_sources()
    print(f"Bot started. Listening to: {SOURCE_CHANNELS}")

    # Initial scan for old videos
    last_ids = await get_last_ids()
    print(f"Startup scan. Resuming from: {last_ids}")

    for source in SOURCE_CHANNELS:
        last_id = last_ids.get(source, 0)
        print(f"Checking {source} from ID {last_id}")
        messages = []
        async for message in client.iter_messages(source, min_id=last_id):
            if message.video or (message.document and message.document.mime_type.startswith('video')):
                messages.append(message)
        for message in reversed(messages):
            await forward_video(message)

    print("Scan complete. Watching for new videos...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    client.loop.run_until_complete(main())