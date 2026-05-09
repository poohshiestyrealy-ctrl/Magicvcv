from telethon.sync import TelegramClient, events
from telethon.sessions import StringSession
import asyncio, random, os

API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
TARGET_CHANNEL = int(os.environ.get("TARGET_CHANNEL"))
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

async def get_config():
    pinned = await client.get_messages(LOG_CHANNEL, ids=0)
    if not pinned or not pinned[0]:
        raise ValueError("Pin a message in LOG_CHANNEL with source channel IDs, one per line")

    sources = []
    for line in pinned[0].message.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and line.startswith("-100"):
            try:
                sources.append(int(line.split()[0]))
            except:
                print(f"Bad line in config: {line}")
    if not sources:
        raise ValueError("No valid source channels found in pinned message")
    return sources

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

async def main():
    await client.start()
    SOURCE_CHANNELS = await get_config()
    print(f"Loaded {len(SOURCE_CHANNELS)} sources: {SOURCE_CHANNELS}")

    @client.on(events.NewMessage(chats=SOURCE_CHANNELS))
    async def handler(event):
        if event.video:
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

    print("Done with old videos. Mirroring new videos from all sources")
    await client.run_until_disconnected()

client.loop.run_until_complete(main())