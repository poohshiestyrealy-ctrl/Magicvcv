import asyncio
import random
import time
import json
import os
from collections import deque
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, PeerFloodError, ChatWriteForbiddenError, MessageIdInvalidError, ChatForwardsRestrictedError

# --- CONFIG: Reads from Railway Variables ---
API_ID = int(os.environ['API_ID'])
API_HASH = os.environ['API_HASH']
SESSION_STRING = os.environ['SESSION_STRING']

TARGET_CHANNEL = int(os.environ['TARGET_CHANNEL'])
LOG_CHANNEL = int(os.environ['LOG_CHANNEL'])

# --- Client setup ---
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# --- Global state ---
SOURCE_CHANNELS = set()
UPLOAD_TIMESTAMPS = deque(maxlen=20)
LAST_ERROR_TIME = 0
ERROR_COUNT = 0
CONFIG_MSG_ID = None

# --- Config helpers ---
async def find_or_create_config():
    global CONFIG_MSG_ID
    try:
        async for msg in client.iter_messages(LOG_CHANNEL, limit=50):
            if msg and msg.text and msg.text.startswith('{"sources":') and msg.pinned:
                CONFIG_MSG_ID = msg.id
                print(f"Found existing config at message ID {CONFIG_MSG_ID}")
                return
    except Exception as e:
        print(f"Error searching for config: {e}")

    print("CRITICAL: No pinned config found. Pin a message with {\"sources\": []} first.")
    CONFIG_MSG_ID = None

async def load_config():
    global CONFIG_MSG_ID
    if not CONFIG_MSG_ID:
        await find_or_create_config()
    if not CONFIG_MSG_ID:
        return set()
    try:
        msg = await client.get_messages(LOG_CHANNEL, ids=CONFIG_MSG_ID)
        if not msg or not msg.text:
            return set()
        data = json.loads(msg.text)
        return set(data.get('sources', []))
    except Exception as e:
        print(f"Config load failed: {e}")
        return set()

async def save_config(sources):
    global CONFIG_MSG_ID
    if not CONFIG_MSG_ID:
        await find_or_create_config()
    if not CONFIG_MSG_ID:
        print("Cannot save config - no pinned message found")
        return
    data = json.dumps({'sources': list(sources)})
    try:
        await client.edit_message(LOG_CHANNEL, CONFIG_MSG_ID, data)
    except MessageIdInvalidError:
        print("Pinned config was deleted. Please pin a new one.")
        CONFIG_MSG_ID = None
    except Exception as e:
        print(f"Failed to save config: {e}")

async def reload_sources():
    global SOURCE_CHANNELS
    SOURCE_CHANNELS = await load_config()
    print(f"Config reloaded. Sources: {SOURCE_CHANNELS}")

# --- Dupe tracking via Botlogs ---
async def get_last_ids():
    last_ids = {}
    async for msg in client.iter_messages(LOG_CHANNEL, limit=1000):
        if msg.text and ':' in msg.text and not msg.text.startswith('/') and not msg.text.startswith('{'):
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
        # ALWAYS RE-UPLOAD - forwarding is blocked on most protected channels
        await global_rate_limit()
        delay = random.randint(180, 300)
        print(f"Safe delay: {delay}s before re-upload of {message.id}")
        await asyncio.sleep(delay)

        if message.video:
            await client.send_file(TARGET_CHANNEL, message.video, caption="")
        elif message.document:
            await client.send_file(TARGET_CHANNEL, message.document, caption="")
        else:
            print(f"Skipping {message.id} - not a video")
            return

        UPLOAD_TIMESTAMPS.append(time.time())
        print(f"RE-UPLOADED {message.id} from {source_id} - clean")
        await save_last_id(source_id, message.id)
        ERROR_COUNT = 0

    except ChatForwardsRestrictedError:
        print(f"Cannot forward or download {message.id} - channel blocks saving completely")
        await save_last_id(source_id, message.id) # Skip it so we don't retry forever
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

# --- Bot commands ---
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
    await reload_sources()
    await event.reply(f"Reloaded {len(SOURCE_CHANNELS)} sources: `{list(SOURCE_CHANNELS)}`")
    last_ids = await get_last_ids()
    for source in SOURCE_CHANNELS:
        last_id = last_ids.get(source, 0)
        print(f"Checking {source} from ID {last_id}")
        messages = []
        async for message in client.iter_messages(source, min_id=last_id):
            if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
                messages.append(message)
        for message in reversed(messages):
            await forward_video(message)

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/scan'))
async def scan_handler(event):
    await event.reply("Starting full rescan. This re-uploads everything with delays.")
    sources = await load_config()
    for source in sources:
        await event.reply(f"Scanning {source}...")
        count = 0
        async for message in client.iter_messages(source, reverse=True):
            if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
                await forward_video(message)
                count += 1
        await event.reply(f"Finished {source}. Processed {count} videos.")
    await event.reply("Full rescan complete.")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/help'))
async def help_handler(event):
    await event.reply(
        "**Commands:**\n"
        "`/add -100...` - Add source channel\n"
        "`/remove -100...` - Remove source\n"
        "`/list` - Show active sources\n"
        "`/reload` - Apply changes + scan new videos\n"
        "`/scan` - Force rescan ALL videos from start\n"
        "`/help` - This message"
    )

# --- Live mirroring ---
@client.on(events.NewMessage())
async def video_handler(event):
    if event.chat_id in SOURCE_CHANNELS and (event.video or (event.document and event.document.mime_type and event.document.mime_type.startswith('video'))):
        await forward_video(event)

# --- Startup ---
async def main():
    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {me.username or me.first_name}")
    await find_or_create_config()
    await reload_sources()
    print(f"Bot started. Listening to: {SOURCE_CHANNELS}")
    last_ids = await get_last_ids()
    for source in SOURCE_CHANNELS:
        last_id = last_ids.get(source, 0)
        print(f"Checking {source} from ID {last_id}")
        messages = []
        async for message in client.iter_messages(source, min_id=last_id):
            if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
                messages.append(message)
        for message in reversed(messages):
            await forward_video(message)
    print("Scan complete. Watching for new videos...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    with client:
        client.loop.run_until_complete(main())