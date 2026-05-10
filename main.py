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
MAX_VIDEO_SIZE = int(os.environ.get('MAX_VIDEO_SIZE', 300)) * 1024 * 1024

# --- INIT ---
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

SOURCE_TARGET_MAP = {}
LAST_IDS = {}
VIDEOS_PROCESSED = 0
CONFIG_MSG_ID = None
RUNNING_TASKS = {}

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
    text += f"\n\nMax video size: {MAX_VIDEO_SIZE/1024/1024:.0f} MB\nActive mappings: {len(SOURCE_TARGET_MAP)}"
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

    file_size = message.video.size if message.video else message.document.size
    if file_size > MAX_VIDEO_SIZE:
        print(f"Skipping {message.id} - {file_size/1024/1024:.1f}MB > {MAX_VIDEO_SIZE/1024/1024}MB")
        return

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

        global VIDEOS_PROCESSED
        VIDEOS_PROCESSED += 1
        print(f"RE-UPLOADED {message.id} from {source_id} -> {target_id} | Total: {VIDEOS_PROCESSED}")

    except errors.FloodWaitError as e:
        print(f"FloodWait {e.seconds}s - sleeping")
        await asyncio.sleep(e.seconds)
    except errors.PeerFloodError:
        print("PEER_FLOOD hit - stopping this source for 24h")
        if source_id in RUNNING_TASKS:
            RUNNING_TASKS[source_id].cancel()
    except Exception as e:
        print(f"Error re-uploading {message.id}: {e}")

# --- PARALLEL PROCESSING WITH DEDUPE ---
async def process_source(source_id):
    last_id = LAST_IDS.get(source_id, 0)
    print(f"Processing {source_id} from ID {last_id}")
    processed_in_session = set()
    
    try:
        async for message in client.iter_messages(source_id, min_id=last_id, reverse=True, limit=None):
            if message.id in processed_in_session or message.id <= LAST_IDS.get(source_id, 0):
                continue
                
            if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
                processed_in_session.add(message.id)
                await forward_video(message)
                LAST_IDS[source_id] = message.id
                await save_config()
                
    except asyncio.CancelledError:
        print(f"Stopped processing {source_id}")
    except Exception as e:
        print(f"Error processing {source_id}: {e}")

async def start_processing(source_id):
    if source_id in RUNNING_TASKS:
        print(f"Cancelling old task for {source_id}")
        RUNNING_TASKS[source_id].cancel()
        try:
            await RUNNING_TASKS[source_id]
        except asyncio.CancelledError:
            pass
    
    task = asyncio.create_task(process_source(source_id))
    RUNNING_TASKS[source_id] = task
    print(f"Started new task for {source_id}")

async def startup_task():
    await load_config()
    await client.send_message(LOG_CHANNEL, f"Userbot started.\n{len(SOURCE_TARGET_MAP)} sources.\nClean post mode.\nMax: {MAX_VIDEO_SIZE/1024/1024}MB")
    for source in SOURCE_TARGET_MAP.keys():
        await start_processing(source)

# --- COMMANDS ---
@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/start$'))
@admin_only
async def start_handler(event):
    await event.reply("**Commands:**\n"
                      "`/start` - Show help\n"
                      "`/addsource -100src -100dst` - Add mapping\n"
                      "`/removesource -100src` - Remove mapping\n"
                      "`/list` - Show all mappings\n"
                      "`/reload` - Reload + restart processing\n"
                      "`/status` - Show mappings + stats", parse_mode='md')

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/addsource'))
@admin_only
async def addsource_handler(event):
    args = event.text.split()
    if len(args) != 3:
        await event.reply("Usage: `/addsource -100source -100target`", parse_mode='md')
        return
    try:
        source = int(args[1])
        target = int(args[2])
        SOURCE_TARGET_MAP[source] = target
        if source not in LAST_IDS:
            LAST_IDS[source] = 0
        await save_config()
        await event.reply(f"Added: `{source}` -> `{target}`", parse_mode='md')
        await start_processing(source)
    except ValueError:
        await event.reply("IDs must be numbers. Ex: `/addsource -1001111111111 -1002222222222`", parse_mode='md')

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/removesource'))
@admin_only
async def removesource_handler(event):
    args = event.text.split()
    if len(args) != 2:
        await event.reply("Usage: `/removesource -100source`", parse_mode='md')
        return
    try:
        source = int(args[1])
        if source in SOURCE_TARGET_MAP:
            if source in RUNNING_TASKS:
                RUNNING_TASKS[source].cancel()
                del RUNNING_TASKS[source]
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
    for task in RUNNING_TASKS.values():
        task.cancel()
    RUNNING_TASKS.clear()
    await event.reply(f"Reloaded {len(SOURCE_TARGET_MAP)} mappings. Restarting processing...")
    for source in SOURCE_TARGET_MAP.keys():
        await start_processing(source)

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/status$'))
@admin_only
async def status_handler(event):
    bandwidth = VIDEOS_PROCESSED * (MAX_VIDEO_SIZE/1024/1024/1024) * 2
    status = f"**Status**\nVideos processed: `{VIDEOS_PROCESSED}`\nMax size: `{MAX_VIDEO_SIZE/1024/1024}MB`\nEst. bandwidth: `{bandwidth:.1f} GB`\nActive mappings: `{len(SOURCE_TARGET_MAP)}`\n\n"
    if SOURCE_TARGET_MAP:
        status += "**Mappings:**\n"
        for source, target in SOURCE_TARGET_MAP.items():
            last = LAST_IDS.get(source, 0)
            running = "Running" if source in RUNNING_TASKS else "Stopped"
            status += f"`{source}` -> `{target}` | Last: `{last}` | {running}\n"
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