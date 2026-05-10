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
LOG_CHANNEL = int(os.environ['LOG_CHANNEL'])
# Strip spaces to fix "8799097823 " bug
ADMIN_IDS = [int(x.strip()) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip()]
MAX_VIDEO_SIZE = int(os.environ.get('MAX_VIDEO_SIZE', '300')) * 1024 # 300MB default

# --- Client setup ---
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# --- Global state ---
SOURCE_TARGET_MAP = {} # {source_id: target_id}
UPLOAD_TIMESTAMPS = deque(maxlen=20)
LAST_ERROR_TIME = 0
ERROR_COUNT = 0
CONFIG_MSG_ID = None
SCAN_COMPLETE = False
TOTAL_BANDWIDTH_USED = 0 # bytes
VIDEOS_PROCESSED = 0
VIDEOS_SKIPPED_SIZE = 0

# --- Admin check decorator ---
def admin_only(func):
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            await event.reply(f"You are not authorized. Your ID: {event.sender_id}")
            return
        await func(event)
    return wrapper

# --- Config helpers ---
async def find_or_create_config():
    global CONFIG_MSG_ID
    try:
        async for msg in client.iter_messages(LOG_CHANNEL, limit=50):
            if msg and msg.text and msg.text.startswith('{"mappings":') and msg.pinned:
                CONFIG_MSG_ID = msg.id
                print(f"Found existing config at message ID {CONFIG_MSG_ID}")
                return
    except Exception as e:
        print(f"Error searching for config: {e}")
    print("CRITICAL: No pinned config found. Pin a message with {\"mappings\": []} first.")
    CONFIG_MSG_ID = None

async def load_config():
    global SOURCE_TARGET_MAP
    if not CONFIG_MSG_ID: await find_or_create_config()
    if not CONFIG_MSG_ID: return {}
    try:
        msg = await client.get_messages(LOG_CHANNEL, ids=CONFIG_MSG_ID)
        data = json.loads(msg.text)
        mappings = data.get('mappings', [])
        SOURCE_TARGET_MAP = {m['source']: m['target'] for m in mappings}
        return SOURCE_TARGET_MAP
    except Exception as e:
        print(f"Failed to load config: {e}")
        return {}

async def save_config(mappings_list):
    global CONFIG_MSG_ID
    if not CONFIG_MSG_ID: await find_or_create_config()
    if not CONFIG_MSG_ID: return False
    try:
        await client.edit_message(LOG_CHANNEL, CONFIG_MSG_ID, json.dumps({'mappings': mappings_list}))
        return True
    except MessageIdInvalidError:
        CONFIG_MSG_ID = None
        return False

async def reload_sources():
    global SOURCE_TARGET_MAP
    await load_config()
    print(f"Config reloaded. Mappings: {SOURCE_TARGET_MAP}")

# --- Dupe tracking via Botlogs ---
async def get_last_ids():
    last_ids = {}
    count = 0
    # Format: source_id:msg_id:target_id
    async for msg in client.iter_messages(LOG_CHANNEL, limit=5000):
        if msg.text and ':' in msg.text and not msg.text.startswith('/') and not msg.text.startswith('{') and not msg.pinned and not msg.action:
            try:
                parts = msg.text.split(':')
                sid, mid = int(parts[0]), int(parts[1])
                if sid not in last_ids or mid > last_ids[sid]:
                    last_ids[sid] = mid
                    count += 1
            except:
                continue
    print(f"Loaded {count} last_id entries from logs: {last_ids}")
    return last_ids

async def save_last_id(source_id, msg_id, target_id):
    try:
        await client.send_message(LOG_CHANNEL, f"{source_id}:{msg_id}:{target_id}")
        print(f"Saved checkpoint: {source_id}:{msg_id}:{target_id}")
    except Exception as e:
        print(f"Failed to save last_id {source_id}:{msg_id}: {e}")

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
    global LAST_ERROR_TIME, ERROR_COUNT, TOTAL_BANDWIDTH_USED, VIDEOS_PROCESSED, VIDEOS_SKIPPED_SIZE
    if await check_circuit_breaker():
        return

    source_id = message.chat_id
    target_id = SOURCE_TARGET_MAP.get(source_id)
    if not target_id:
        print(f"No target mapped for source {source_id}, skipping")
        return

    file_ref = message.video or message.document
    if not file_ref:
        return

    # Size check - 300MB limit
    if file_ref.size > MAX_VIDEO_SIZE:
        print(f"Skipping {message.id} - too large: {file_ref.size / 1024 / 1024:.2f} MB > {MAX_VIDEO_SIZE / 1024 / 1024:.0f} MB")
        await save_last_id(source_id, message.id, target_id)
        VIDEOS_SKIPPED_SIZE += 1
        return

    try:
        # Save checkpoint BEFORE doing anything slow. Prevents dupes on crash/restart
        await save_last_id(source_id, message.id, target_id)

        await global_rate_limit()
        delay = random.randint(180, 300)
        print(f"Safe delay: {delay}s before re-upload of {message.id} to {target_id}")
        await asyncio.sleep(delay)

        if message.video:
            await client.send_file(target_id, message.video, caption="")
        elif message.document:
            await client.send_file(target_id, message.document, caption="")
        else:
            print(f"Skipping {message.id} - not a video")
            return

        UPLOAD_TIMESTAMPS.append(time.time())
        TOTAL_BANDWIDTH_USED += file_ref.size # Only upload, no download
        VIDEOS_PROCESSED += 1
        print(f"RE-UPLOADED {message.id} from {source_id} -> {target_id}")
        ERROR_COUNT = 0

    except ChatForwardsRestrictedError:
        print(f"Cannot forward {message.id} - channel blocks forwarding")
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
        print(f"BANNED from {target_id}. Skipping.")
        return
    except Exception as e:
        LAST_ERROR_TIME = time.time()
        ERROR_COUNT += 1
        backoff = min(1800, 60 * (2 ** ERROR_COUNT))
        print(f"Error {message.id}: {e}. Backoff {backoff}s")
        await asyncio.sleep(backoff)

# --- Bot commands ---
@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/debug'))
@admin_only
async def debug_handler(event):
    await event.reply(f"**Debug Info**\nSender ID: `{event.sender_id}`\nAdmins loaded: `{ADMIN_IDS}`\nMatch: `{event.sender_id in ADMIN_IDS}`\nLog Channel: `{LOG_CHANNEL}`")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/map'))
@admin_only
async def map_handler(event):
    args = event.text.split()
    if len(args) < 2:
        await event.reply("Usage:\n`/map add <source> <target>`\n`/map remove <source>`\n`/map list`")
        return

    cmd = args[1].lower()
    await load_config()
    msg = await client.get_messages(LOG_CHANNEL, ids=CONFIG_MSG_ID)
    data = json.loads(msg.text)
    mappings = data.get('mappings', [])

    if cmd == "add" and len(args) == 4:
        try:
            source, target = int(args[2]), int(args[3])
            mappings = [m for m in mappings if m['source']!= source]
            mappings.append({"source": source, "target": target})
            if await save_config(mappings):
                await event.reply(f"Mapped `{source}` -> `{target}`. Send `/reload` to apply.")
            else:
                await event.reply("Failed to save config.")
        except:
            await event.reply("Invalid IDs. Usage: `/map add -1001111111111 -1002222222222`")

    elif cmd == "remove" and len(args) == 3:
        try:
            source = int(args[2])
            mappings = [m for m in mappings if m['source']!= source]
            if await save_config(mappings):
                await event.reply(f"Removed mapping for `{source}`. Send `/reload` to apply.")
            else:
                await event.reply("Failed to save config.")
        except:
            await event.reply("Invalid ID. Usage: `/map remove -1001111111111`")

    elif cmd == "list":
        if mappings:
            text = "**Current Mappings:**\n" + "\n".join([f"`{m['source']}` -> `{m['target']}`" for m in mappings])
            await event.reply(text)
        else:
            await event.reply("No mappings configured. Use `/map add <source> <target>`")
    else:
        await event.reply("Usage:\n`/map add <source> <target>`\n`/map remove <source>`\n`/map list`")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/reload'))
@admin_only
async def reload_handler(event):
    await reload_sources()
    await event.reply(f"Reloaded {len(SOURCE_TARGET_MAP)} mappings")
    last_ids = await get_last_ids()
    for source in SOURCE_TARGET_MAP.keys():
        last_id = last_ids.get(source, 0)
        print(f"Checking {source} from ID {last_id}")
        messages = []
        async for message in client.iter_messages(source, min_id=last_id):
            if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
                messages.append(message)
        for message in reversed(messages):
            await forward_video(message)

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/scan'))
@admin_only
async def scan_handler(event):
    await event.reply("Starting full rescan.")
    await load_config()
    for source in SOURCE_TARGET_MAP.keys():
        await event.reply(f"Scanning {source}...")
        count = 0
        async for message in client.iter_messages(source, reverse=True):
            if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
                await forward_video(message)
                count += 1
        await event.reply(f"Finished {source}. Processed {count} videos.")
    await event.reply("Full rescan complete.")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/usage'))
@admin_only
async def usage_handler(event):
    bandwidth_gb = TOTAL_BANDWIDTH_USED / 1024 / 1024
    limit_gb = 100
    limit_mb = MAX_VIDEO_SIZE / 1024 / 1024
    await event.reply(
        f"**Railway Usage Estimate**\n"
        f"Videos processed: `{VIDEOS_PROCESSED}`\n"
        f"Videos skipped > {limit_mb:.0f}MB: `{VIDEOS_SKIPPED_SIZE}`\n"
        f"Bandwidth used: `{bandwidth_gb:.2f} GB / {limit_gb} GB`\n"
        f"Max video size: `{limit_mb:.0f} MB`\n"
        f"Active mappings: `{len(SOURCE_TARGET_MAP)}`\n\n"
        f"Note: No download = 1x bandwidth only. Check Railway dashboard for exact usage."
    )

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/whoami'))
@admin_only
async def whoami_handler(event):
    await event.reply(f"Your user ID: `{event.sender_id}`")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/help'))
@admin_only
async def help_handler(event):
    await event.reply(
        "**Commands:**\n"
        "`/debug` - Show your ID + admin list\n"
        "`/map add <source> <target>` - Map source to target\n"
        "`/map remove <source>` - Remove mapping\n"
        "`/map list` - Show all mappings\n"
        "`/reload` - Apply changes + scan new videos\n"
        "`/scan` - Force rescan ALL videos from start\n"
        "`/usage` - Show bandwidth + stats\n"
        "`/whoami` - Show your user ID"
    )

# --- Live mirroring ---
@client.on(events.NewMessage())
async def video_handler(event):
    if not SCAN_COMPLETE:
        return
    if event.chat_id in SOURCE_TARGET_MAP and (event.video or (event.document and event.document.mime_type and event.document.mime_type.startswith('video'))):
        await forward_video(event)

# --- Startup ---
async def main():
    global SCAN_COMPLETE
    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {me.username or me.first_name}")
    print(f"Admins: {ADMIN_IDS}")
    print(f"Max video size: {MAX_VIDEO_SIZE / 1024 / 1024:.0f} MB") # Fixed log
    await find_or_create_config()
    await reload_sources()
    print(f"Bot started. Mappings: {SOURCE_TARGET_MAP}")
    last_ids = await get_last_ids()
    for source in SOURCE_TARGET_MAP.keys():
        last_id = last_ids.get(source, 0)
        print(f"Checking {source} from ID {last_id}")
        messages = []
        async for message in client.iter_messages(source, min_id=last_id):
            if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
                messages.append(message)
        for message in reversed(messages):
            await forward_video(message)

    SCAN_COMPLETE = True
    print("Scan complete. Watching for new videos...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    with client:
        client.loop.run_until_complete(main())