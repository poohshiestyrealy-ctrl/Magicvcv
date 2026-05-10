import os
import asyncio
import json
import tempfile
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

# --- CONFIG ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))
ADMIN_IDS = list(map(int, os.environ.get("ADMIN_IDS", "").split(",")))

CONFIG_FILE = "config.json"
MAX_FILE_SIZE = 200 * 1024 * 1024 # 200MB - FIXED: was 200KB before
RETRY_DELAYS = [60, 120, 300, 600, 1800] # 1m, 2m, 5m, 10m, 30m

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

MAPPINGS = {} # {source_id: target_id}
LAST_IDS = {} # {source_id: last_message_id}
RUNNING_TASKS = {} # {source_id: asyncio.Task}

# --- CONFIG MANAGEMENT ---
async def load_config():
    global MAPPINGS, LAST_IDS
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
            MAPPINGS = {int(k): int(v) for k, v in data.get('mappings', {}).items()}
            LAST_IDS = {int(k): int(v) for k, v in data.get('last_ids', {}).items()}
        print(f"Loaded config: {len(MAPPINGS)} mappings")
    except FileNotFoundError:
        print("No config file found, starting fresh")
        MAPPINGS = {}
        LAST_IDS = {}

async def save_config():
    data = {
        'mappings': MAPPINGS,
        'last_ids': LAST_IDS
    }
    with tempfile.NamedTemporaryFile('w', delete=False) as tf:
        json.dump(data, tf)
        tempname = tf.name
    os.replace(tempname, CONFIG_FILE)

# --- ADMIN CHECK ---
def admin_only(func):
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            await event.reply("You are not authorized to use this bot.")
            return
        return await func(event)
    return wrapper

# --- CORE FORWARDING ---
async def forward_video(message):
    source_id = message.chat_id
    target_id = MAPPINGS.get(source_id)

    if not target_id:
        return

    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            # Resolve target entity - fixes "Invalid object ID" error
            target_entity = await client.get_entity(target_id)

            await client.send_file(
                target_entity,
                message.media,
                caption=message.text,
                force_document=False
            )
            print(f"Re-uploaded {message.id} from {source_id} to {target_id}")
            return True

        except FloodWaitError as e:
            print(f"FloodWait {e.seconds}s on {message.id}")
            await asyncio.sleep(e.seconds + 5)
        except ValueError as e:
            print(f"Can't access target {target_id}: {e}")
            print(f"Join {target_id} with userbot account first")
            return False
        except Exception as e:
            if delay is None:
                print(f"Failed to upload {message.id} after all retries: {e}")
                return False
            print(f"Error uploading {message.id}, retry in {delay}s: {e}")
            await asyncio.sleep(delay)
    return False

async def process_source(source_id):
    last_id = LAST_IDS.get(source_id, 0)
    print(f"Processing {source_id} from ID {last_id}")
    processed_in_session = set()

    try:
        # Resolve source entity - works for @username or if joined
        entity = await client.get_entity(source_id)
        print(f"Resolved source: {getattr(entity, 'title', entity.id)}")
    except Exception as e:
        print(f"Can't access source {source_id}: {e}")
        return

    try:
        async for message in client.iter_messages(entity, min_id=last_id, reverse=True, limit=None):
            if message.id in processed_in_session or message.id <= LAST_IDS.get(source_id, 0):
                continue

            if message.video or (message.document and message.document.mime_type and message.document.mime_type.startswith('video')):
                # Skip files > 200MB - FIXED calculation
                if message.file and message.file.size > MAX_FILE_SIZE:
                    size_mb = message.file.size / 1024 / 1024
                    print(f"Skipping {message.id} - {size_mb:.1f}MB > 200MB")
                    LAST_IDS[source_id] = message.id
                    await save_config()
                    continue

                processed_in_session.add(message.id)
                success = await forward_video(message)
                if success:
                    LAST_IDS[source_id] = message.id
                    await save_config()

    except asyncio.CancelledError:
        print(f"Stopped processing {source_id}")
    except Exception as e:
        print(f"Error processing {source_id}: {e}")

def start_processing_task(source_id):
    if source_id in RUNNING_TASKS:
        RUNNING_TASKS[source_id].cancel()
    task = asyncio.create_task(process_source(source_id))
    RUNNING_TASKS[source_id] = task
    print(f"Started new task for {source_id}")

# --- COMMAND HANDLERS ---
@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/(start|help)$'))
@admin_only
async def help_handler(event):
    await event.reply(
        "**Commands:**\n"
        "`/start` or `/help` - Show this help\n"
        "`/addsource -100source -100target` - Add source->target mapping\n"
        "`/removesource -100source` - Remove a source mapping\n"
        "`/list` - Show all mappings with Last processed ID\n"
        "`/reload` - Reload config and restart processing\n"
        "`/status` - Show mappings + video stats\n\n"
        "**Notes:**\n"
        "• Use `@username` for public channels\n"
        "• Join private channels first with userbot account\n"
        "• Max file size: 200MB",
        parse_mode='md'
    )

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/addsource'))
@admin_only
async def addsource_handler(event):
    try:
        _, source, target = event.text.split()
        source_id = int(source) if source.startswith('-100') else source
        target_id = int(target)
        MAPPINGS[source_id] = target_id
        if source_id not in LAST_IDS:
            LAST_IDS[source_id] = 0
        await save_config()
        start_processing_task(source_id)
        await event.reply(f"Added mapping: `{source}` → `{target}`\nStarted processing.", parse_mode='md')
    except Exception as e:
        await event.reply(f"Error: {e}\nUsage: `/addsource -100source -100target`", parse_mode='md')

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/removesource'))
@admin_only
async def removesource_handler(event):
    try:
        _, source = event.text.split()
        source_id = int(source) if source.startswith('-100') else source
        if source_id in MAPPINGS:
            MAPPINGS.pop(source_id)
            if source_id in RUNNING_TASKS:
                RUNNING_TASKS[source_id].cancel()
                RUNNING_TASKS.pop(source_id)
            await save_config()
            await event.reply(f"Removed mapping for `{source}`", parse_mode='md')
        else:
            await event.reply(f"Source `{source}` not found in mappings.", parse_mode='md')
    except Exception as e:
        await event.reply(f"Error: {e}\nUsage: `/removesource -100source`", parse_mode='md')

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/list$'))
@admin_only
async def list_handler(event):
    if not MAPPINGS:
        await event.reply("No mappings configured.")
        return
    text = "**Current Mappings:**\n"
    for src, tgt in MAPPINGS.items():
        last = LAST_IDS.get(src, 0)
        text += f"`{src}` → `{tgt}` | Last: `{last}`\n"
    await event.reply(text, parse_mode='md')

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/reload$'))
@admin_only
async def reload_handler(event):
    await load_config()
    for task in RUNNING_TASKS.values():
        task.cancel()
    RUNNING_TASKS.clear()
    for source_id in MAPPINGS:
        start_processing_task(source_id)
    await event.reply(f"Reloaded config. Restarted {len(MAPPINGS)} tasks.")

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'^/status$'))
@admin_only
async def status_handler(event):
    text = "**Status:**\n"
    text += f"Active mappings: `{len(MAPPINGS)}`\n"
    text += f"Running tasks: `{len(RUNNING_TASKS)}`\n\n"
    for src, tgt in MAPPINGS.items():
        last = LAST_IDS.get(src, 0)
        running = "✅" if src in RUNNING_TASKS and not RUNNING_TASKS[src].done() else "❌"
        text += f"{running} `{src}` → `{tgt}` | Last: `{last}`\n"
    await event.reply(text, parse_mode='md')

# --- STARTUP ---
async def startup_task():
    await load_config()
    print(f"Userbot started. Monitoring {len(MAPPINGS)} sources.")
    for source_id in MAPPINGS:
        start_processing_task(source_id)

async def main():
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: SESSION_STRING invalid or expired. Generate a new one.")
        return
    await startup_task()

with client:
    client.loop.run_until_complete(main())
    client.run_until_disconnected()