import os
import json
import asyncio
import logging
import time
import psutil
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ChatAdminRequiredError, ChannelPrivateError
from supabase import create_client, Client

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ENV VARS
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
LOG_CHANNEL = int(os.environ["LOG_CHANNEL"])
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

# SETTINGS
MAX_FILE_SIZE = 200 * 1024 * 1024 # 200MB cap

# Init
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Config storage
CONFIG = {"sources": {}}
TABLE_NAME = "mappings"
start_time = time.time()
copied_count = 0
scraped_count = 0
skipped_count = 0

async def load_config():
    global CONFIG
    try:
        data = supabase.table(TABLE_NAME).select("*").execute()
        if data.data:
            CONFIG["sources"] = {str(row["source_id"]): str(row["target_id"]) for row in data.data}
            logger.info(f"Loaded {len(CONFIG['sources'])} mappings from Supabase")
        else:
            CONFIG = {"sources": {}}
    except Exception as e:
        logger.error(f"Failed to load from Supabase: {e}")
        CONFIG = {"sources": {}}

async def save_config():
    try:
        supabase.table(TABLE_NAME).delete().neq("source_id", 0).execute()
        rows = [{"source_id": int(k), "target_id": int(v)} for k, v in CONFIG["sources"].items()]
        if rows:
            supabase.table(TABLE_NAME).insert(rows).execute()
        logger.info("Saved to Supabase")
    except Exception as e:
        logger.error(f"Failed to save to Supabase: {e}")

async def check_access(chat_id):
    try:
        await client.get_entity(chat_id)
        return True, None
    except ChannelPrivateError:
        return False, "Channel is private or you left it. Join and add your account."
    except ValueError:
        return False, "Invalid channel ID. Check if ID is correct."
    except Exception as e:
        return False, f"{e}"

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    if event.sender_id!= ADMIN_ID:
        return
    await event.reply(
        "Video-only bot online.\n"
        f"Max file size: `{MAX_FILE_SIZE/1024/1024:.0f}MB`\n\n"
        "**Commands:**\n"
        "/addsource `source_id` `target_id`\n"
        "/removesource `source_id`\n"
        "/listmappings\n"
        "/scrape `source_id`\n"
        "/stats\n"
        "/help"
    )

@client.on(events.NewMessage(pattern='/help'))
async def help_cmd(event):
    if event.sender_id!= ADMIN_ID:
        return
    await event.reply(
        f"**Video-Only Bot**\n\n"
        f"**Max video size:** `{MAX_FILE_SIZE/1024/1024:.0f}MB`\n"
        f"Larger files are skipped.\n\n"
        "**1. Add a channel pair:**\n"
        "`/addsource -100123456789 -100987654321`\n"
        "You must be in both channels with posting rights.\n\n"
        "**2. Remove a pair:**\n"
        "`/removesource -100123456789`\n\n"
        "**3. List all pairs:**\n"
        "`/listmappings`\n\n"
        "**4. Scrape old videos:**\n"
        "`/scrape -100123456789`\n"
        "Copies last 100 videos ≤200MB with 3.5s delay.\n\n"
        "**5. Check stats:**\n"
        "`/stats`\n\n"
        "**Notes:**\n"
        "- Clean reupload: no captions, no forward tags\n"
        "- Only MP4/MKV videos sent as 'video'\n"
        "- Uses 2x bandwidth due to reupload"
    )

@client.on(events.NewMessage(pattern='/addsource'))
async def add_source(event):
    if event.sender_id!= ADMIN_ID:
        return
    try:
        _, source, target = event.text.split()
        source_id, target_id = int(source), int(target)

        # Check access to both channels before saving
        ok_s, err_s = await check_access(source_id)
        if not ok_s:
            await event.reply(f"Cannot access source `{source_id}`: {err_s}")
            return

        ok_t, err_t = await check_access(target_id)
        if not ok_t:
            await event.reply(f"Cannot access target `{target_id}`: {err_t}")
            return

        CONFIG["sources"][str(source_id)] = str(target_id)
        await save_config()
        await event.reply(f"Added: `{source_id}` → `{target_id}`\nVideo-only, 200MB cap. Access verified.")
    except Exception as e:
        await event.reply(f"Error: {e}\nUsage: /addsource -100source -100target")

@client.on(events.NewMessage(pattern='/removesource'))
async def remove_source(event):
    if event.sender_id!= ADMIN_ID:
        return
    try:
        _, source = event.text.split()
        source_id = str(int(source))
        if source_id in CONFIG["sources"]:
            del CONFIG["sources"][source_id]
            await save_config()
            await event.reply(f"Removed `{source_id}`.")
        else:
            await event.reply("Source not found.")
    except Exception as e:
        await event.reply(f"Error: {e}\nUsage: /removesource -100source")

@client.on(events.NewMessage(pattern='/listmappings'))
async def list_mappings(event):
    if event.sender_id!= ADMIN_ID:
        return
    if not CONFIG["sources"]:
        await event.reply("No mappings yet.")
        return
    text = "**Video mappings:**\n"
    for s, t in CONFIG["sources"].items():
        text += f"`{s}` → `{t}`\n"
    await event.reply(text)

@client.on(events.NewMessage(pattern='/stats'))
async def stats(event):
    if event.sender_id!= ADMIN_ID:
        return
    uptime = time.time() - start_time
    h = int(uptime // 3600)
    m = int(uptime%3600//60)
    mem = psutil.virtual_memory()
    await event.reply(
        f"**Stats**\n"
        f"Mode: Video-only, clean reupload\n"
        f"Max size: `{MAX_FILE_SIZE/1024/1024:.0f}MB`\n"
        f"Uptime: `{h}h {m}m`\n"
        f"RAM: `{mem.used/1024/1024:.1f}MB`\n"
        f"Videos copied: `{copied_count}`\n"
        f"Videos scraped: `{scraped_count}`\n"
        f"Skipped >200MB: `{skipped_count}`\n"
        f"Mappings: `{len(CONFIG['sources'])}`\n"
        f"Scrape delay: `3.5s`"
    )

@client.on(events.NewMessage(pattern='/scrape'))
async def scrape_history(event):
    global scraped_count, skipped_count
    if event.sender_id!= ADMIN_ID:
        return
    args = event.text.split()
    if len(args)!= 2:
        await event.reply("Usage: `/scrape -100source_id`")
        return

    try:
        source_id = int(args[1])
    except ValueError:
        await event.reply("Invalid source ID.")
        return

    if str(source_id) not in CONFIG["sources"]:
        await event.reply("Source not mapped. Use `/addsource` first.")
        return

    target_id = int(CONFIG["sources"][str(source_id)])

    # Verify target access before starting
    ok, err = await check_access(target_id)
    if not ok:
        await event.reply(f"Cannot access target `{target_id}`: {err}")
        return

    await event.reply(f"Scraping videos ≤{MAX_FILE_SIZE/1024/1024:.0f}MB from `{source_id}`...\n3.5s delay, clean upload.")

    count = 0
    errors = 0
    limit = 100

    try:
        async for message in client.iter_messages(source_id, limit=limit):
            try:
                if message.video:
                    if message.file.size > MAX_FILE_SIZE:
                        skipped_count += 1
                        continue

                    await client.send_file(target_id, message.file, caption="")
                    count += 1
                    scraped_count += 1
                    await asyncio.sleep(3.5)
            except FloodWaitError as e:
                await event.reply(f"Flood wait: sleeping {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except ChatAdminRequiredError:
                await event.reply(f"Error: No posting rights in target `{target_id}`. Make your account admin.")
                return
            except Exception as e:
                logger.error(f"Scrape failed: {e}")
                errors += 1

        await event.reply(f"Done.\nVideos copied: `{count}`\nSkipped >200MB: `{skipped_count}`\nErrors: `{errors}`")
    except Exception as e:
        await event.reply(f"Scrape failed: {e}")

async def handler(event):
    global copied_count, skipped_count
    source_id = event.chat_id
    if str(source_id) in CONFIG["sources"]:
        target_id = int(CONFIG["sources"][str(source_id)])
        try:
            if event.message.video:
                if event.message.file.size > MAX_FILE_SIZE:
                    skipped_count += 1
                    await client.send_message(LOG_CHANNEL, f"Skipped {event.message.file.size/1024/1024:.1f}MB video from `{source_id}` - exceeds 200MB")
                    return

                await client.send_file(target_id, event.message.file, caption="")
                copied_count += 1
        except ChatAdminRequiredError:
            error_msg = f"Copy failed: No posting rights in target `{target_id}`. Make your account admin."
            logger.error(error_msg)
            await client.send_message(LOG_CHANNEL, error_msg)
        except ChannelPrivateError:
            error_msg = f"Copy failed: Target `{target_id}` is private or you left it. Rejoin the channel."
            logger.error(error_msg)
            await client.send_message(LOG_CHANNEL, error_msg)
        except ValueError as e:
            error_msg = f"Copy failed: Invalid target ID `{target_id}`. {e}"
            logger.error(error_msg)
            await client.send_message(LOG_CHANNEL, error_msg)
        except Exception as e:
            error_msg = f"Copy failed from `{source_id}` to `{target_id}`: {e}"
            logger.error(error_msg)
            await client.send_message(LOG_CHANNEL, error_msg)

async def main():
    await load_config()
    if CONFIG["sources"]:
        client.add_event_handler(handler, events.NewMessage(chats=list(map(int, CONFIG["sources"].keys()))))
    await client.start()
    me = await client.get_me()
    logger.info(f"Video bot started as {me.first_name}")
    await client.send_message(LOG_CHANNEL, f"Video-only bot started. 200MB cap. Clean reupload.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())