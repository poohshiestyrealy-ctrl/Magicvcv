from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
import asyncio, random, os

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
TARGET_CHANNEL = int(os.environ.get("TARGET_CHANNEL"))
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
SOURCE_CHANNELS = set()

async def get_pinned_msg():
    from telethon import types
    pinned_msgs = await client.get_messages(LOG_CHANNEL, limit=1, filter=types.InputMessagesFilterPinned)
    return pinned_msgs[0] if pinned_msgs else None

async def get_config():
    pinned = await get_pinned_msg()
    if not pinned or not pinned.message:
        return set()

    sources = set()
    for line in pinned.message.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and line.startswith("-100"):
            try:
                sources.add(int(line.split()[0]))
            except:
                pass
    return sources

async def update_pinned_config(sources):
    content = "# Bot Config - Auto managed\n# Use /add -100... or /remove -100...\n\n"
    content += "\n".join(str(s) for s in sorted(sources))

    pinned = await get_pinned_msg()
    if pinned:
        await client.edit_message(LOG_CHANNEL, pinned.id, content)
    else:
        msg = await client.send_message(LOG_CHANNEL, content)
        await client.pin_message(LOG_CHANNEL, msg.id)

async def get_last_ids():
    try:
        msgs = await client.get_messages(LOG_CHANNEL, limit=1000)
        last_ids = {}
        for msg in msgs:
            if msg.message and ":" in msg.message and msg.message.count(":") == 1:
                try:
                    sid, mid = msg.message.split(":")
                    last_ids[int(sid)] = int(mid)
                except:
                    continue
        return last_ids
    except:
        return {}

async def save_last_id(source_id, msg_id):
    await client.send_message(LOG_CHANNEL, f"{source_id}:{msg_id}")

async def forward_video(message):
    try:
        await client.send_file(TARGET_CHANNEL, message.video, caption="")
        print(f"Forwarded video {message.id} from {message.chat_id}")
        await save_last_id(message.chat_id, message.id)
        await asyncio.sleep(random.randint(120, 180))
    except Exception as e:
        print(f"Error: {e}")
        if "FloodWaitError" in str(e):
            wait_time = int(str(e).split()[3])
            print(f"FloodWait: sleeping {wait_time}s")
            await asyncio.sleep(wait_time)

async def reload_sources():
    global SOURCE_CHANNELS
    SOURCE_CHANNELS = await get_config()
    print(f"Config reloaded. Sources: {SOURCE_CHANNELS}")
    return SOURCE_CHANNELS

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'/add (-100\d+)'))
async def add_handler(event):
    global SOURCE_CHANNELS
    new_id = int(event.pattern_match.group(1))
    if new_id in SOURCE_CHANNELS:
        await event.reply(f"Already added: `{new_id}`")
        return

    SOURCE_CHANNELS.add(new_id)
    await update_pinned_config(SOURCE_CHANNELS)
    await event.reply(f"Added `{new_id}`. Run `/reload` to start mirroring.")
    await reload_sources()

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern=r'/remove (-100\d+)'))
async def remove_handler(event):
    global SOURCE_CHANNELS
    rem_id = int(event.pattern_match.group(1))
    if rem_id not in SOURCE_CHANNELS:
        await event.reply(f"Not in list: `{rem_id}`")
        return

    SOURCE_CHANNELS.remove(rem_id)
    await update_pinned_config(SOURCE_CHANNELS)
    await event.reply(f"Removed `{rem_id}`. Run `/reload` to stop mirroring.")
    await reload_sources()

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/list'))
async def list_handler(event):
    global SOURCE_CHANNELS
    print(f"/list called. Current SOURCE_CHANNELS: {SOURCE_CHANNELS}")
    if not SOURCE_CHANNELS:
        await event.reply("No sources configured. Use `/add -100...`")
        return
    text = "**Active sources:**\n" + "\n".join(f"`{s}`" for s in sorted(SOURCE_CHANNELS))
    await event.reply(text)

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/help'))
async def help_handler(event):
    await event.reply(
        "**Bot Commands:**\n"
        "`/add -100123456789` - Add source channel\n"
        "`/remove -100123456789` - Remove source\n"
        "`/list` - Show all sources\n"
        "`/reload` - Reload config without restart\n"
        "`/help` - This message\n\n"
        "**Note:** After `/add` or `/remove`, run `/reload` to apply changes."
    )

@client.on(events.NewMessage(chats=LOG_CHANNEL, pattern='/reload'))
async def reload_handler(event):
    await reload_sources()
    await event.reply(f"Reloaded {len(SOURCE_CHANNELS)} sources. Now listening to: `{list(SOURCE_CHANNELS)}`")

async def main():
    global SOURCE_CHANNELS # Critical: allows video_handler to see updates
    await client.start()
    await reload_sources()

    @client.on(events.NewMessage)
    async def video_handler(event):
        global SOURCE_CHANNELS # Critical fix: use current global value, not startup value
        if event.chat_id in SOURCE_CHANNELS and event.video:
            await forward_video(event)

    last_ids = await get_last_ids()
    print(f"Resuming from: {last_ids}")

    for source in SOURCE_CHANNELS:
        last_id = last_ids.get(source, 0)
        print(f"Checking {source} from ID {last_id}")
        messages = []
        async for message in client.iter_messages(source, min_id=last_id):
            if message.video:
                messages.append(message)
        for message in reversed(messages):
            await forward_video(message)

    print("Bot ready. Send /help in LOG_CHANNEL for commands")
    await client.run_until_disconnected()

client.loop.run_until_complete(main())