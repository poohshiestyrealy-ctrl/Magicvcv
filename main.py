import os
import asyncio
import random
import json
from telethon import TelegramClient, events, errors
from telethon.sessions import StringSession

# --- ENV VARS ---
API_ID = int(os.environ['API_ID'])
API_HASH = os.environ['API_HASH']
SESSION_STRING = os.environ['SESSION_STRING']
LOG_CHANNEL = int(os.environ['LOG_CHANNEL'])
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_IDS', '').split(',') if x]
MAX_VIDEO_SIZE = int(os.environ.get('MAX_VIDEO_SIZE', 200)) * 1024 * 1024  # MB to bytes

# --- INIT AS USERBOT ---
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

SOURCE_TARGET_MAP = {}
LAST_IDS = {}
VIDEOS_PROCESSED = 0
CONFIG_MSG_ID = None

# --- CONFIG STORED IN PINNED MESSAGE ---
async def load_config():
    global SOURCE_TARGET_MAP, LAST_IDS, CONFIG_MSG_ID
    try:
        async for msg in client.iter_messages(LOG_CHANNEL, limit=50):
            if msg.pinned and msg.text and msg.text.startswith('{'):
                try:
                    data = json.loads(msg.text)
                    SOURCE_TARGET_MAP = {int(k): int(v) for k, v in data.get('sources', {}).items()}
                    LAST_IDS = {int(k): int(v) for k, v in data.get('last_ids', {}).items()}
                    CONFIG_MSG_ID = msg.id
                    print(f"Loaded config: {len(SOURCE_TARGET_MAP)} sources")
                    return
                except:
                    continue
    except Exception as e:
        print(f"Error loading config: {e}")
    await save_config()

async def save_config():
    global CONFIG_MSG_ID
    data = {
        'sources': {str(k): str(v) for k, v in SOURCE_TARGET_MAP.items()},
        'last_ids': {str(k): str(v) for k, v in LAST_IDS.items()}
    }
    text = json.dumps(data, indent=2)
    try:
        if CONFIG_MSG_ID:
            await client.edit_message(LOG_CHANNEL, CONFIG_MSG_ID, text)
        else:
            msg = await client.send_message(LOG_CHANNEL, text)
            await client.pin_message(LOG_CHANNEL, msg.id)
            CONFIG_MSG_ID = msg.id
    except Exception as e:
        print(f"Error saving config: {e}")

# --- DECORATORS ---
def admin_only(func):
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            return
        return await func(event)
    return wrapper

# --- CORE: RE-UPLOAD FOR CLEAN POSTS ---
async def forward_video(message):
    source_id = message.chat_id
    if source_id not in SOURCE_TARGET_MAP:
        return
    target_id = SOURCE_TARGET_MAP[source_id]

    # Skip if video > MAX_VIDEO_SIZE
    file_size = message.video.size if message.video else message.document.size
    if file_size > MAX_VIDEO_SIZE:
        print(f"Skipping {message.id} - {file_size/1024/1024:.1f}MB > {MAX_VIDEO_SIZE/1024/1024}MB")
        LAST_IDS[source_id] = message.id
        await save_config()
        return

    # 45-90s delay to avoid PEER_FLOOD
    delay = random.randint(45, 90)
    print(f"Delay: {delay}s before re-upload of {message.id}")
    await asyncio.sleep(delay)

    try:
        if message.video:
            await client.send_file(target_id, message.video, caption="")
        elif message.document and message.document.mime_type and message.document.mime_type.startswith('video'):
            await client.send_file(target_id, message.document, caption="")
        else:
            return

        LAST_IDS[source_id] = message.id
        await save_config()
        global VIDEOS_PROCESSED
        VIDEOS_PROCESSED += 1
        print(f"RE-UPLOADED {message.id} from {source_id} -> {target_id} | Total: {VIDEOS_PROCESSED}")

    except errors.FloodWaitError as e:
        print(f"FloodWait {e.seconds}s - sleeping")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        print(f"Error re-uploading {message.id}: {e}")

# --- PARALLEL PROCESSING ---
async def process_source(source_id):
    last_id = LAST_IDS.get(source_id, 0)
    print(f"Processing {source_id} from ID {last_id}")
    async for message in client.iter_messages(source_id, min_id=last_id, reverse=True):
        if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
            await forward_video(message)

async def startup_task():
    await load_config()
    await client.send_message(LOG_CHANNEL, f"Userbot started.\n{len(SOURCE_TARGET_MAP)} sources.\nClean post mode.\nMax: {MAX_VIDEO_SIZE/1024/1024}MB")
    tasks = [process_source(source) for source in SOURCE_TARGET_MAP.keys()]
    if tasks:
        await asyncio.gather(*tasks)

# --- COMMANDS - NON-AMBIGUOUS ---
@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/start$'))
@admin_only
async def start_handler(event):
    await event.reply("**Userbot Commands:**\n"
                      "`/start` - Show this help\n"
                      "`/addsource -100src -100dst` - Add mapping\n"
                      "`/removesource -100src` - Remove mapping\n"
                      "`/list` - Show all mappings\n"
                      "`/reload` - Reload + restart processing\n"
                      "`/status` - Show mappings + stats", parse_mode='md')

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/addsource'))
@admin_only
async def add_handler(event):
    args = event.text.split()
    if len(args) != 3:
        await event.reply("Usage: `/addsource -100source -100target`", parse_mode='md')
        return
    try:
        source = int(args[1])
        target = int(args[2])
        SOURCE_TARGET_MAP[source] = target
        await save_config()
        await event.reply(f"Added: `{source}` -> `{target}`", parse_mode='md')
    except ValueError:
        await event.reply("IDs must be numbers. Ex: `/addsource -1001111111111 -1002222222222`", parse_mode='md')
    except Exception as e:
        await event.reply(f"Error: {e}")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/removesource'))
@admin_only
async def remove_handler(event):
    args = event.text.split()
    if len(args) != 2:
        await event.reply("Usage: `/removesource -100source`", parse_mode='md')
        return
    try:
        source = int(args[1])
        if source in SOURCE_TARGET_MAP:
            SOURCE_TARGET_MAP.pop(source)
            LAST_IDS.pop(source, None)
            await save_config()
            await event.reply(f"Removed: `{source}`", parse_mode='md')
        else:
            await event.reply(f"Source `{source}` not found", parse_mode='md')
    except ValueError:
        await event.reply("ID must be a number. Ex: `/removesource -1001111111111`", parse_mode='md')

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/list$'))
@admin_only
async def list_handler(event):
    if not SOURCE_TARGET_MAP:
        await event.reply("No sources configured. Use `/addsource -100xxx -100yyy`")
        return
    text = "**Active Mappings:**\n"
    for source, target in SOURCE_TARGET_MAP.items():
        last = LAST_IDS.get(source, 0)
        text += f"`{source}` -> `{target}` | Last: `{last}`\n"
    await event.reply(text, parse_mode='md')

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/reload$'))
@admin_only
async def reload_handler(event):
    await load_config()
    await event.reply(f"Reloaded {len(SOURCE_TARGET_MAP)} mappings. Starting parallel processing...")
    tasks = [process_source(source) for source in SOURCE_TARGET_MAP.keys()]
    if tasks:
        await asyncio.gather(*tasks)

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/status$'))
@admin_only
async def status_handler(event):
    bandwidth = VIDEOS_PROCESSED * (MAX_VIDEO_SIZE/1024/1024/1024) * 2
    status = f"**Status**\nVideos processed: `{VIDEOS_PROCESSED}`\nMax size: `{MAX_VIDEO_SIZE/1024/1024}MB`\nBandwidth: `{bandwidth:.1f} GB`\n\n"
    if SOURCE_TARGET_MAP:
        status += "**Mappings:**\n"
        for source, target in SOURCE_TARGET_MAP.items():
            last = LAST_IDS.get(source, 0)
            status += f"`{source}` -> `{target}` | Last: `{last}`\n"
    else:
        status += "No sources configured."
    await event.reply(status, parse_mode='md')

# --- RUN ---
async def main():
    await client.start()
    await startup_task()

with client:
    client.loop.run_until_complete(main())
    client.run_until_disconnected()