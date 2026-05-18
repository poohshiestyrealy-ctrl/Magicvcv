import os
import asyncio
import logging
import random
import re
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeVideo
from telethon.tl.functions.channels import CreateForumTopicRequest
from telethon.tl.functions.channels import GetForumTopicsRequest
from telethon.errors.rpcerrorlist import FloodWaitError, ChatAdminRequiredError
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
BOT_LOG_CHAT_ID = int(os.getenv("BOT_LOG_CHAT_ID", "0"))

MAX_FILE_SIZE = 200 * 1024 * 1024
MAX_DURATION = 60
MIN_WIDTH = 1280
MIN_HEIGHT = 720
MIN_FILE_SIZE_NO_META = 15 * 1024 * 1024
UPLOAD_DELAY = 30
DELETE_DELAY = 10

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG = {"sources": {}, "auto_gif": {}, "auto_short": {}}
mapped_chats = set()
scraped_count = 0
skipped_count = 0

def rebuild_mapped_chats():
    global mapped_chats
    mapped_chats = set(CONFIG["sources"].keys()) | set(CONFIG["auto_gif"].keys()) | set(CONFIG["auto_short"].keys())

async def send_log(text):
    if BOT_LOG_CHAT_ID!= 0:
        try:
            await client.send_message(BOT_LOG_CHAT_ID, f"**Bot Log**\n{text}")
        except Exception as e:
            logger.error(f"Failed to send to BOT_LOG: {e}")
    logger.info(text)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_video_attr(message):
    if message.video:
        return message.video
    if message.document:
        for attr in message.document.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                return attr
    return None

def is_video_message(message):
    return get_video_attr(message) is not None

def is_gif(message):
    return (message.document and message.document.mime_type == 'video/mp4' and
            any(getattr(a, 'round_message', False) for a in message.document.attributes))

async def load_sources():
    global CONFIG
    try:
        res = supabase.table("mappings").select("*").execute()
        CONFIG["sources"] = {str(row["source_id"]): str(row["target_id"]) for row in res.data}

        res2 = supabase.table("auto_mappings").select("*").execute()
        CONFIG["auto_gif"] = {}
        CONFIG["auto_short"] = {}
        for row in res2.data:
            src = str(row["source_id"])
            if row["mode"] == "gif":
                CONFIG["auto_gif"][src] = str(row["target_id"])
            elif row["mode"] == "short":
                CONFIG["auto_short"][src] = str(row["target_id"])

        rebuild_mapped_chats()
        await send_log(f"Loaded {len(CONFIG['sources'])} scrape, {len(CONFIG['auto_gif'])} GIF, {len(CONFIG['auto_short'])} short mappings")
    except Exception as e:
        await send_log(f"Failed to load sources: {e}")

async def save_mapping(source_id, target_id):
    try:
        supabase.table("mappings").upsert({"source_id": source_id, "target_id": target_id}, on_conflict="source_id").execute()
        CONFIG["sources"][str(source_id)] = str(target_id)
        rebuild_mapped_chats()
        return True
    except Exception as e:
        await send_log(f"Save failed: {e}")
        return False

async def remove_mapping(source_id):
    try:
        supabase.table("mappings").delete().eq("source_id", source_id).execute()
        CONFIG["sources"].pop(str(source_id), None)
        rebuild_mapped_chats()
        return True
    except Exception as e:
        await send_log(f"Remove failed: {e}")
        return False

async def save_checkpoint(source_id, msg_id):
    try:
        supabase.table("scrape_progress").upsert({"source_id": source_id, "last_message_id": msg_id}, on_conflict="source_id").execute()
    except Exception as e:
        logger.error(f"Checkpoint save failed: {e}")

async def get_checkpoint(source_id):
    try:
        res = supabase.table("scrape_progress").select("last_message_id").eq("source_id", source_id).execute()
        return res.data[0]["last_message_id"] if res.data else 0
    except:
        return 0

async def check_access(chat_id):
    try:
        entity = await client.get_entity(chat_id)
        if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup') or hasattr(entity, 'participants_count'):
            return True, None
        return False, "Not a chat/channel/supergroup"
    except ValueError:
        return False, "Invalid object ID for a chat"
    except Exception as e:
        return False, str(e)

async def get_topic_map(source_id, target_id):
    try:
        res = supabase.table("group_topic_map").select("mapping").eq("source_id", source_id).eq("target_id", target_id).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]["mapping"] if res.data[0]["mapping"] else {}
        return {}
    except Exception as e:
        logger.error(f"get_topic_map error: {e}")
        return {}

async def save_topic_map(source_id, target_id, mapping):
    try:
        supabase.table("group_topic_map").upsert({
            "source_id": source_id,
            "target_id": target_id,
            "mapping": mapping
        }, on_conflict="source_id,target_id").execute()
        return True
    except Exception as e:
        logger.error(f"Topic map save failed: {e}")
        return False

async def save_archive_topic_id(source_id, target_id, archive_topic_id):
    try:
        supabase.table("group_topic_map").upsert({
            "source_id": source_id,
            "target_id": target_id,
            "archive_topic_id": archive_topic_id
        }, on_conflict="source_id,target_id").execute()
        return True
    except Exception as e:
        logger.error(f"Archive topic save failed: {e}")
        return False

async def get_archive_topic_id(source_id, target_id):
    try:
        res = supabase.table("group_topic_map").select("archive_topic_id").eq("source_id", source_id).eq("target_id", target_id).execute()
        if res.data and len(res.data) > 0:
            return res.data[0]["archive_topic_id"] if res.data[0].get("archive_topic_id") else None
        return None
    except Exception as e:
        logger.error(f"get_archive_topic_id error: {e}")
        return None










@client.on(events.NewMessage(pattern=r'/resyncgroup (-?[0-9]+) (-?[0-9]+)'))
async def resync_group_topics(event):
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("🔄 Starting topic resync...")

    try:
        src_entity = await client.get_entity(source_id)
    except Exception as e:
        await msg.edit(f"❌ Can't access source group: {e}\nOpen the group once with the userbot account.")
        return

    if not getattr(src_entity, 'forum', False):
        await msg.edit("❌ Source group doesn't have topics enabled.")
        return

    topic_map = await get_topic_map(source_id, target_id)
    all_topics = []
    offset_topic = 0
    offset_id = 0
    max_attempts = 3

    await msg.edit("📡 Fetching topics from source group...")

    for attempt in range(max_attempts):
        try:
            res = await asyncio.wait_for(
                client(GetForumTopicsRequest(
                    channel=src_entity,
                    offset_date=0,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=20,
                )),
                timeout=15
            )

            if not res.topics:
                break

            all_topics.extend(res.topics)
            if len(res.topics) < 20:
                break

            last_topic = res.topics[-1]
            offset_topic = last_topic.id
            offset_id = last_topic.top_message
            await asyncio.sleep(1.5)

        except asyncio.TimeoutError:
            await msg.edit("⏳ Timeout. Userbot isn't in the group or Telegram is blocking it.")
            return
        except Exception as e:
            await msg.edit(f"❌ Failed: {str(e)}")
            return

    src_topics = all_topics
    if not src_topics:
        await msg.edit("❌ No topics found. Open the group once on Telegram Desktop with this account and try again.")
        return

    await msg.edit(f"✅ Found **{len(src_topics)}** topics. Starting sync...")

    created = 0
    updated = 0
    skipped = 0

    archive_topic_id = None
    try:
        tgt_topics = await client(GetForumTopicsRequest(channel=target_id, offset_date=0, offset_id=0, offset_topic=0, limit=100))
        archive_topic = next((tt for tt in tgt_topics.topics if tt.title.lower() == "archive"), None)

        if not archive_topic:
            result = await client(CreateForumTopicRequest(channel=target_id, title="Archive"))
            for update in getattr(result, 'updates', []):
                if hasattr(update, 'id') and isinstance(getattr(update, 'id', None), int):
                    archive_topic_id = update.id
                    break
            created += 1
            await asyncio.sleep(2)
        else:
            archive_topic_id = archive_topic.id

        await save_archive_topic_id(source_id, target_id, archive_topic_id)
    except Exception as e:
        logger.error(f"Failed to create/find Archive topic: {e}")
        await msg.edit(f"Failed to create Archive topic: {e}")
        return

    for t in src_topics:
        if getattr(t, 'deleted', False) or t.id == 1:
            skipped += 1
            continue

        src_id = str(t.id)
        if src_id in topic_map:
            skipped += 1
            continue

        try:
            tgt_topics = await client(GetForumTopicsRequest(channel=target_id, offset_date=0, offset_id=0, offset_topic=0, limit=100))
            existing = next((tt for tt in tgt_topics.topics if tt.title.lower().strip() == t.title.lower().strip()), None)
        except Exception:
            existing = None

        if existing:
            topic_map[src_id] = existing.id
            updated += 1
        else:
            if len(topic_map) >= 100:
                topic_map[src_id] = archive_topic_id
                skipped += 1
            else:
                try:
                    result = await client(CreateForumTopicRequest(channel=target_id, title=t.title[:128], icon_emoji_id=getattr(t, 'icon_emoji_id', None)))
                    new_id = None
                    for update in getattr(result, 'updates', []):
                        if hasattr(update, 'id') and isinstance(getattr(update, 'id', None), int):
                            new_id = update.id
                            break

                    if new_id:
                        topic_map[src_id] = new_id
                        created += 1
                    await asyncio.sleep(3)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 2)
                except Exception as e:
                    logger.error(f"Topic create failed for {t.title}: {e}")
                    skipped += 1

    await save_topic_map(source_id, target_id, topic_map)
    msg_text = f"**Resync Complete**\nCreated: `{created}`\nMapped existing: `{updated}`\nSkipped: `{skipped}`\nTotal mapped: `{len(topic_map)}`"
    if archive_topic_id:
        msg_text += f"\nArchive topic ID: `{archive_topic_id}`"
    await msg.edit(msg_text)

@client.on(events.NewMessage(pattern=r'/testmapping (-?[0-9]+) (-?[0-9]+)'))
async def test_mapping(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))

    msg = await event.reply("Testing...")
    try:
        src_entity = await client.get_entity(source_id)
        res = await client(GetForumTopicsRequest(channel=src_entity, limit=5))
        await msg.edit(f"Found {len(res.topics)} topics. Userbot has access.")
    except Exception as e:
        await msg.edit(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/help'))
async def help_handler(event):
    if not is_admin(event.sender_id):
        return

    help_text = """
**Yaga Bot Commands**

`/addsource <src_id> <dst_id>` - Add scrape mapping
`/removesource <src_id>` - Remove mapping
`/listmappings` - Show all mappings
`/resyncgroup <src_id> <dst_id>` - Sync topics between groups
`/scrapegrouplike <src_id> [fresh]` - Scrape group with topics
`/testmapping <src_id> <dst_id>` - Test if userbot can read topics
`/stats` - Show stats
`/help` - Show this message
"""
    await event.reply(help_text)












@client.on(events.NewMessage(pattern=r'/resyncgroup (-?[0-9]+) (-?[0-9]+)'))
async def resync_group_topics(event):
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("🔄 Starting topic resync...")

    try:
        src_entity = await client.get_entity(source_id)
    except Exception as e:
        await msg.edit(f"❌ Can't access source group: {e}\nOpen the group once with the userbot account.")
        return

    if not getattr(src_entity, 'forum', False):
        await msg.edit("❌ Source group doesn't have topics enabled.")
        return

    topic_map = await get_topic_map(source_id, target_id)
    all_topics = []
    offset_topic = 0
    offset_id = 0
    max_attempts = 3

    await msg.edit("📡 Fetching topics from source group...")

    for attempt in range(max_attempts):
        try:
            res = await asyncio.wait_for(
                client(GetForumTopicsRequest(
                    channel=src_entity,
                    offset_date=0,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=20,
                )),
                timeout=15
            )

            if not res.topics:
                break

            all_topics.extend(res.topics)
            if len(res.topics) < 20:
                break

            last_topic = res.topics[-1]
            offset_topic = last_topic.id
            offset_id = last_topic.top_message
            await asyncio.sleep(1.5)

        except asyncio.TimeoutError:
            await msg.edit("⏳ Timeout. Userbot isn't in the group or Telegram is blocking it.")
            return
        except Exception as e:
            await msg.edit(f"❌ Failed: {str(e)}")
            return

    src_topics = all_topics
    if not src_topics:
        await msg.edit("❌ No topics found. Open the group once on Telegram Desktop with this account and try again.")
        return

    await msg.edit(f"✅ Found **{len(src_topics)}** topics. Starting sync...")

    created = 0
    updated = 0
    skipped = 0

    archive_topic_id = None
    try:
        tgt_topics = await client(GetForumTopicsRequest(channel=target_id, offset_date=0, offset_id=0, offset_topic=0, limit=100))
        archive_topic = next((tt for tt in tgt_topics.topics if tt.title.lower() == "archive"), None)

        if not archive_topic:
            result = await client(CreateForumTopicRequest(channel=target_id, title="Archive"))
            for update in getattr(result, 'updates', []):
                if hasattr(update, 'id') and isinstance(getattr(update, 'id', None), int):
                    archive_topic_id = update.id
                    break
            created += 1
            await asyncio.sleep(2)
        else:
            archive_topic_id = archive_topic.id

        await save_archive_topic_id(source_id, target_id, archive_topic_id)
    except Exception as e:
        logger.error(f"Failed to create/find Archive topic: {e}")
        await msg.edit(f"Failed to create Archive topic: {e}")
        return

    for t in src_topics:
        if getattr(t, 'deleted', False) or t.id == 1:
            skipped += 1
            continue

        src_id = str(t.id)
        if src_id in topic_map:
            skipped += 1
            continue

        try:
            tgt_topics = await client(GetForumTopicsRequest(channel=target_id, offset_date=0, offset_id=0, offset_topic=0, limit=100))
            existing = next((tt for tt in tgt_topics.topics if tt.title.lower().strip() == t.title.lower().strip()), None)
        except Exception:
            existing = None

        if existing:
            topic_map[src_id] = existing.id
            updated += 1
        else:
            if len(topic_map) >= 100:
                topic_map[src_id] = archive_topic_id
                skipped += 1
            else:
                try:
                    result = await client(CreateForumTopicRequest(channel=target_id, title=t.title[:128], icon_emoji_id=getattr(t, 'icon_emoji_id', None)))
                    new_id = None
                    for update in getattr(result, 'updates', []):
                        if hasattr(update, 'id') and isinstance(getattr(update, 'id', None), int):
                            new_id = update.id
                            break

                    if new_id:
                        topic_map[src_id] = new_id
                        created += 1
                    await asyncio.sleep(3)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 2)
                except Exception as e:
                    logger.error(f"Topic create failed for {t.title}: {e}")
                    skipped += 1

    await save_topic_map(source_id, target_id, topic_map)
    msg_text = f"**Resync Complete**\nCreated: `{created}`\nMapped existing: `{updated}`\nSkipped: `{skipped}`\nTotal mapped: `{len(topic_map)}`"
    if archive_topic_id:
        msg_text += f"\nArchive topic ID: `{archive_topic_id}`"
    await msg.edit(msg_text)

@client.on(events.NewMessage(pattern=r'/testmapping (-?[0-9]+) (-?[0-9]+)'))
async def test_mapping(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))

    msg = await event.reply("Testing...")
    try:
        src_entity = await client.get_entity(source_id)
        res = await client(GetForumTopicsRequest(channel=src_entity, limit=5))
        await msg.edit(f"Found {len(res.topics)} topics. Userbot has access.")
    except Exception as e:
        await msg.edit(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/help'))
async def help_handler(event):
    if not is_admin(event.sender_id):
        return

    help_text = """
**Yaga Bot Commands**

`/addsource <src_id> <dst_id>` - Add scrape mapping
`/removesource <src_id>` - Remove mapping
`/listmappings` - Show all mappings
`/resyncgroup <src_id> <dst_id>` - Sync topics between groups
`/scrapegrouplike <src_id> [fresh]` - Scrape group with topics
`/testmapping <src_id> <dst_id>` - Test if userbot can read topics
`/stats` - Show stats
`/help` - Show this message
"""
    await event.reply(help_text)