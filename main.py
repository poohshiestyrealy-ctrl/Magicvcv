import os
import asyncio
import logging
import hashlib # ADDED
from collections import defaultdict # ADDED
from datetime import datetime # ADDED
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ChatAdminRequiredError
from telethon.tl.types import DocumentAttributeVideo, InputMessagesFilterVideo
from telethon.tl.functions.channels import CreateForumTopicRequest, GetForumTopicsRequest
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== CONFIG ====================
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
BOT_LOG_CHAT_ID = int(os.getenv("BOT_LOG_CHAT_ID", "0"))

MAX_FILE_SIZE = 200 * 1024 *1024
MIN_RESOLUTION = 720 # Minimum height in pixels
UPLOAD_DELAY = int(os.getenv("UPLOAD_DELAY", "30")) # For /scrape and /scrapegrouplike
SHORTS_DELAY = int(os.getenv("SHORTS_DELAY", "20")) # For /shorts only
TOPIC_CREATE_DELAY = 60

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG = {"sources": {}}
scraped_count = 0
skipped_count = 0
KILL_SWITCH = False

def rebuild_mapped_chats():
    global mapped_chats
    mapped_chats = set(CONFIG["sources"].keys())

async def send_log(text):
    if BOT_LOG_CHAT_ID!= 0:
        try:
            await client.send_message(BOT_LOG_CHAT_ID, f"**📡 Bot Log**\n{text}")
        except Exception as e:
            logger.error(f"Failed to send to BOT_LOG: {e}")
    logger.info(text)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_video_attr(message):
    if not message.media:
        return None
    if hasattr(message, 'video') and message.video:
        if hasattr(message.video, 'attributes'):
            for attr in message.video.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    return attr
        if hasattr(message.video, 'duration'):
            return message.video
    if hasattr(message, 'document') and message.document:
        if hasattr(message.document, 'attributes'):
            for attr in message.document.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    return attr
    if hasattr(message.media, 'document') and message.media.document:
        if hasattr(message.media.document, 'attributes'):
            for attr in message.media.document.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    return attr
    return None

def is_video_message(message):
    if not message.media:
        return False
    if hasattr(message, 'video') and message.video:
        return True
    if hasattr(message, 'document') and message.document:
        mime = getattr(message.document, 'mime_type', '')
        if mime and mime.startswith('video/'):
            return True
        if hasattr(message.document, 'attributes'):
            for attr in message.document.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    return True
    return False

def meets_resolution(video_attr):
    if not video_attr:
        return False
    height = getattr(video_attr, 'h', 0)
    return height >= MIN_RESOLUTION

async def load_sources():
    global CONFIG
    try:
        res = supabase.table("mappings").select("*").execute()
        CONFIG["sources"] = {str(row["source_id"]): str(row["target_id"]) for row in res.data}
        rebuild_mapped_chats()
        await send_log(f"Loaded {len(CONFIG['sources'])} scrape mappings")
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
            return res.data[0].get("archive_topic_id")
        return None
    except Exception as e:
        logger.error(f"get_archive_topic_id error: {e}")
        return None











async def scrape_group_with_topics(source_id, target_id, status_msg, force_fresh=False):
    global scraped_count, skipped_count, KILL_SWITCH
    topic_map = await get_topic_map(source_id, target_id)
    archive_topic_id = await get_archive_topic_id(source_id, target_id)

    if not topic_map:
        await status_msg.edit("No topic map found. Run `/resyncgroupfresh source_id target_id` first")
        return

    source_topic_names = {}
    try:
        src_entity = await client.get_entity(source_id)
        src_topics_res = await client(GetForumTopicsRequest(channel=src_entity, offset_date=0, offset_id=0, offset_topic=0, limit=200))
        for t in src_topics_res.topics:
            source_topic_names[str(t.id)] = t.title
    except Exception as e:
        logger.error(f"Failed to fetch source topic names: {e}")

    offset_id = 0 if force_fresh else await get_checkpoint(source_id)
    if force_fresh:
        await save_checkpoint(source_id, 0)

    count = checked = errors = 0
    sent_to_general = skipped_too_large = skipped_low_res = skipped_no_map = 0
    current_delay = UPLOAD_DELAY

    try:
        async for message in client.iter_messages(source_id, limit=None, offset_id=offset_id, reverse=True, filter=InputMessagesFilterVideo):
            if KILL_SWITCH:
                await status_msg.edit("**🛑 Scrape aborted by kill switch**")
                await save_checkpoint(source_id, message.id)
                return

            checked += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(
                        f"**🔄 Scraping Group Videos...**\n"
                        f"├ Videos Checked: `{checked}`\n"
                        f"├ Uploaded: `{count}`\n"
                        f"├ To General: `{sent_to_general}`\n"
                        f"├ Skip <720p: `{skipped_low_res}`\n"
                        f"├ Skip >200MB: `{skipped_too_large}`\n"
                        f"├ Skip NoMap: `{skipped_no_map}`\n"
                        f"└ Errors: `{errors}`"
                    )
                except:
                    pass
                await save_checkpoint(source_id, message.id)

            if message.file and message.file.size > MAX_FILE_SIZE:
                skipped_too_large += 1
                continue

            video_attr = get_video_attr(message)
            if not meets_resolution(video_attr):
                skipped_low_res += 1
                continue

            reply_to = None
            src_topic_id = getattr(message, 'reply_to_topic_id', None)
            caption = ""

            if src_topic_id:
                if src_topic_id == 1:
                    reply_to = 1
                    sent_to_general += 1
                else:
                    reply_to = topic_map.get(str(src_topic_id))
                    if reply_to == archive_topic_id:
                        original_name = source_topic_names.get(str(src_topic_id), f"Topic {src_topic_id}")
                        caption = f"[ARCHIVED FROM: {original_name}]"
                    elif reply_to is None and archive_topic_id:
                        reply_to = archive_topic_id
                        original_name = source_topic_names.get(str(src_topic_id), f"Topic {src_topic_id}")
                        caption = f"[ARCHIVED FROM: {original_name}]"
            else:
                reply_to = 1
                sent_to_general += 1

            if not reply_to:
                skipped_no_map += 1
                continue

            try:
                await client.send_file(
                    target_id,
                    message.media,
                    caption=caption,
                    attributes=[video_attr] if video_attr else None,
                    force_document=False,
                    reply_to=reply_to
                )
                count += 1
                scraped_count += 1
                await save_checkpoint(source_id, message.id)
                await asyncio.sleep(current_delay)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                current_delay = min(current_delay * 1.5, 60)
            except Exception as e:
                errors += 1
                logger.error(f"Send failed: {e}")

        await save_checkpoint(source_id, 0)
        final = (
            f"**✅ Topic Scrape Complete**\n"
            f"├ Videos Checked: `{checked}`\n"
            f"├ Uploaded: `{count}`\n"
            f"├ To General: `{sent_to_general}`\n"
            f"├ Skipped <720p: `{skipped_low_res}`\n"
            f"├ Skipped >200MB: `{skipped_too_large}`\n"
            f"├ Skipped NoMap: `{skipped_no_map}`\n"
            f"└ Errors: `{errors}`"
        )
        if archive_topic_id:
            final += f"\n**Archive ID:** `{archive_topic_id}`"
        await status_msg.edit(final)

    except Exception as e:
        await status_msg.edit(f"❌ Scrape failed: {e}")

@client.on(events.NewMessage(pattern=r'/scrape (-?[0-9]+)'))
async def scrape_channel_handler(event):
    global KILL_SWITCH, scraped_count, skipped_count
    if not is_admin(event.sender_id):
        return
    if KILL_SWITCH:
        await event.reply("Kill switch is active. Run `/resetkill` first.")
        return

    source_id = int(event.pattern_match.group(1))
    target_id = CONFIG["sources"].get(str(source_id))

    if not target_id:
        await event.reply("Source not mapped. Use `/addsource source_id target_id` first.")
        return

    KILL_SWITCH = False
    scraped_count = 0
    skipped_count = 0

    await send_log(f"Starting /scrape for {source_id} -> {target_id}")
    msg = await event.reply(f"**🔍 Starting Channel Scrape**\nSource: `{source_id}`\nTarget: `{target_id}`")

    last_id = await get_checkpoint(source_id)
    batch = 0
    skipped_res = skipped_size = errors = 0

    try:
        async for message in client.iter_messages(
            int(source_id),
            offset_id=last_id,
            reverse=True,
            filter=InputMessagesFilterVideo
        ):
            if KILL_SWITCH:
                await msg.edit("**🛑 Scrape stopped by kill switch**")
                await send_log("Scrape killed")
                return

            video_attr = get_video_attr(message)
            if not meets_resolution(video_attr):
                skipped_res += 1
                continue

            if message.file and message.file.size > MAX_FILE_SIZE:
                skipped_size += 1
                continue

            try:
                await client.send_file(int(target_id), message.media)
                scraped_count += 1
                await save_checkpoint(source_id, message.id)

                batch += 1
                if batch % 10 == 0:
                    await msg.edit(
                        f"**📥 Scraping Channel...**\n"
                        f"├ Scraped: `{scraped_count}`\n"
                        f"├ Skip <720p: `{skipped_res}`\n"
                        f"├ Skip >200MB: `{skipped_size}`\n"
                        f"└ Errors: `{errors}`"
                    )

                await asyncio.sleep(UPLOAD_DELAY)

            except FloodWaitError as e:
                await msg.edit(f"⏳ FloodWait {e.seconds}s...")
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                logger.error(f"Error forwarding {message.id}: {e}")
                errors += 1

        await msg.edit(
            f"**✅ Channel Scrape Complete**\n"
            f"├ Scraped: `{scraped_count}`\n"
            f"├ Skipped <720p: `{skipped_res}`\n"
            f"├ Skipped >200MB: `{skipped_size}`\n"
            f"└ Errors: `{errors}`"
        )
        await send_log(f"Scrape done: {scraped_count} scraped")

    except Exception as e:
        await msg.edit(f"❌ Scrape failed: {e}")
        await send_log(f"Scrape error: {e}")

@client.on(events.NewMessage(pattern=r'/shorts (-?[0-9]+) (-?[0-9]+)'))
async def shorts_handler(event):
    global KILL_SWITCH
    if not is_admin(event.sender_id):
        return
    if KILL_SWITCH:
        await event.reply("Kill switch is active. Run `/resetkill` first.")
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("**🎬 Starting /shorts**\nForwarding videos ≤60s...")

    count = checked = errors = skipped_duration = skipped_size = skipped_no_attr = 0
    current_delay = SHORTS_DELAY
    MAX_SHORTS_SIZE_NO_ATTR = 10 * 1024 * 1024

    try:
        async for message in client.iter_messages(source_id, limit=None, filter=InputMessagesFilterVideo):
            if KILL_SWITCH:
                await msg.edit("**🛑 Shorts aborted by kill switch**")
                return

            checked += 1
            if checked % 200 == 0:
                await msg.edit(
                    f"**🎬 Processing Shorts...**\n"
                    f"├ Videos Checked: `{checked}`\n"
                    f"├ Forwarded: `{count}`\n"
                    f"├ Skip >60s: `{skipped_duration}`\n"
                    f"├ Skip Size: `{skipped_size}`\n"
                    f"├ Skip NoAttr: `{skipped_no_attr}`\n"
                    f"└ Errors: `{errors}`"
                )

            video_attr = get_video_attr(message)
            file_size = getattr(message.file, 'size', 0)

            if not video_attr:
                if file_size > MAX_SHORTS_SIZE_NO_ATTR:
                    skipped_no_attr += 1
                    continue
            else:
                duration = getattr(video_attr, 'duration', 0)
                if duration > 60:
                    skipped_duration += 1
                    continue
                if duration == 0 and file_size > MAX_SHORTS_SIZE_NO_ATTR:
                    skipped_size += 1
                    continue

            try:
                await client.forward_messages(target_id, message)
                await client.delete_messages(source_id, message.id)
                count += 1
                await asyncio.sleep(current_delay)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                current_delay = min(current_delay * 1.5, 60)
            except ChatAdminRequiredError:
                await msg.edit("❌ Bot needs 'Delete Messages' admin right in source to use /shorts")
                return
            except Exception as e:
                errors += 1
                logger.error(f"Forward/delete failed: {e}")

        await msg.edit(
            f"**✅ Shorts Complete**\n"
            f"├ Videos Checked: `{checked}`\n"
            f"├ Forwarded & Deleted: `{count}`\n"
            f"├ Skipped >60s: `{skipped_duration}`\n"
            f"├ Skipped Size: `{skipped_size}`\n"
            f"├ Skipped NoAttr: `{skipped_no_attr}`\n"
            f"└ Errors: `{errors}`"
        )
    except Exception as e:
        await msg.edit(f"❌ Shorts failed: {e}")















@client.on(events.NewMessage(pattern=r'/help'))
async def help_handler(event):
    if not is_admin(event.sender_id):
        return
    help_text = """
**🤖 Yaga Bot Commands**

**📋 1. Setup & Topics**
`/addsource <src_id> <dst_id>` - Link source to target
`/removesource <src_id>` - Remove link
`/listmappings` - Show all links
`/resyncgroupfresh <src_id> <dst_id>` - Clone topics 1:1
`/clearmapping <src_id> <dst_id>` - Delete topic map
`/debugtopics <group_id>` - List all topics
`/diag <group_id>` - Run diagnostics

**📥 2. Scraping**
`/scrape <src_id>` - Channel/Group → Channel, videos only, 720p+, no captions
`/scrapegrouplike <src_id> [fresh]` - Group with topics, maps to topics. Add 'fresh' to restart
`/testmapping <src_id> <dst_id>` - Send test videos to verify mapping
`/killall` - Emergency stop all scrapers
`/resetkill` - Re-enable scrapers

**🎬 3. Shorts**
`/shorts <src_id> <dst_id>` - Forward videos ≤60s + delete from source

**🧹 4. Dedupe**
`/dedupe <target_id> [dryrun]` - Delete duplicate videos, keeps oldest. Sends sample videos. Add 'dryrun' to preview

**📊 5. Other**
`/stats` - Show stats
`/debugvideos <group_id>` - Sample 2 videos with metadata

**⚙️ Env Vars**
`UPLOAD_DELAY=30` - Delay for /scrape & /scrapegrouplike
`SHORTS_DELAY=20` - Delay for /shorts only
"""
    await event.reply(help_text)

@client.on(events.NewMessage(pattern=r'/listmappings'))
async def list_mappings(event):
    if not is_admin(event.sender_id):
        return
    if not CONFIG["sources"]:
        await event.reply("No mappings found")
        return
    text = "**🔗 Current Mappings:**\n"
    for src, dst in CONFIG["sources"].items():
        text += f"`{src}` → `{dst}`\n"
    await event.reply(text)

@client.on(events.NewMessage(pattern=r'/addsource (-?[0-9]+) (-?[0-9]+)'))
async def add_source(event):
    if not is_admin(event.sender_id):
        return
    try:
        source_id = int(event.pattern_match.group(1))
        target_id = int(event.pattern_match.group(2))
        if await save_mapping(source_id, target_id):
            await event.reply(f"✅ Added mapping: `{source_id}` → `{target_id}`")
        else:
            await event.reply("❌ Failed to save mapping")
    except Exception as e:
        await event.reply(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/removesource (-?[0-9]+)'))
async def remove_source(event):
    if not is_admin(event.sender_id):
        return
    try:
        source_id = int(event.pattern_match.group(1))
        if await remove_mapping(source_id):
            await event.reply(f"✅ Removed mapping for `{source_id}`")
        else:
            await event.reply("❌ Failed to remove mapping")
    except Exception as e:
        await event.reply(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/clearmapping (-?[0-9]+) (-?[0-9]+)'))
async def clear_mapping(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply(f"Clearing mapping for `{source_id}` → `{target_id}`...")
    try:
        supabase.table("group_topic_map").delete().eq("source_id", source_id).eq("target_id", target_id).execute()
        await msg.edit("**✅ Mapping cleared**\nUse `/resyncgroupfresh` to rebuild.")
    except Exception as e:
        await msg.edit(f"Failed: {e}")

@client.on(events.NewMessage(pattern=r'/diag (-?[0-9]+)'))
async def diag_group(event):
    if not is_admin(event.sender_id):
        return
    gid = int(event.pattern_match.group(1))
    msg = await event.reply(f"Running diagnostics on `{gid}`...")
    try:
        entity = await asyncio.wait_for(client.get_entity(gid), timeout=10)
        await msg.edit(f"**Step 1/2**: get_entity\n✅ OK\n**Step 2/2**: get_topics\nRunning...")
        res = await asyncio.wait_for(client(GetForumTopicsRequest(channel=entity, offset_date=0, offset_id=0, offset_topic=0, limit=5)), timeout=15)
        await msg.edit(f"**Step 1/2**: get_entity\n✅ OK\n**Step 2/2**: get_topics\n✅ OK\nTopics found: `{len(res.topics)}`")
    except Exception as e:
        await msg.edit(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/scrapegrouplike (-?[0-9]+)(?:\s+(fresh))?'))
async def scrape_group_like(event):
    global KILL_SWITCH
    if not is_admin(event.sender_id):
        return
    if KILL_SWITCH:
        await event.reply("Kill switch is active. Run `/resetkill` first.")
        return
    source_id = int(event.pattern_match.group(1))
    force_fresh = event.pattern_match.group(2) == 'fresh'
    target_id = CONFIG["sources"].get(str(source_id))
    if not target_id:
        await event.reply(f"No mapping for `{source_id}`. Use `/addsource` first")
        return
    msg = await event.reply("Starting group scrape...")
    await scrape_group_with_topics(source_id, int(target_id), msg, force_fresh)

@client.on(events.NewMessage(pattern=r'/stats'))
async def stats_handler(event):
    if not is_admin(event.sender_id):
        return
    await event.reply(f"**📊 Bot Stats**\n├ Scraped: `{scraped_count}`\n├ Skipped: `{skipped_count}`\n└ Mappings: `{len(CONFIG['sources'])}`")

@client.on(events.NewMessage(pattern=r'/dedupe (-?[0-9]+)(?:\s+(dryrun))?'))
async def dedupe_target(event):
    if not is_admin(event.sender_id):
        return

    target_id = int(event.pattern_match.group(1))
    dry_run = event.pattern_match.group(2) == 'dryrun'

    msg = await event.reply("**🧹 Starting dedupe scan oldest→newest...**\nCollecting samples...")

    seen_hashes = {} # hash -> first_message_id
    duplicate_groups = defaultdict(list) # hash -> [message objects]
    deleted_count = 0
    checked_count = 0

    # Check if target is forum group and build topic map
    topic_name_map = {}
    is_forum = False
    try:
        entity = await client.get_entity(target_id)
        if getattr(entity, 'forum', False):
            is_forum = True
            topics_res = await client(GetForumTopicsRequest(channel=entity, offset_date=0, offset_id=0, offset_topic=0, limit=200))
            for t in topics_res.topics:
                topic_name_map[t.id] = t.title
            topic_name_map[1] = "General"
    except:
        pass

    try:
        # reverse=True = oldest first, keeps oldest copy
        async for message in client.iter_messages(target_id, limit=None, reverse=True):
            if not is_video_message(message):
                continue

            checked_count += 1

            # Quick hash: size + duration + first 5MB
            h = hashlib.md5()
            h.update(str(message.file.size).encode())
            h.update(str(getattr(get_video_attr(message), 'duration', 0)).encode())

            bytes_read = 0
            async for chunk in client.iter_download(message.media, chunk_size=1024*1024):
                h.update(chunk)
                bytes_read += len(chunk)
                if bytes_read >= 5 * 1024 * 1024:
                    break

            file_hash = h.hexdigest()
            duplicate_groups[file_hash].append(message)

            if file_hash in seen_hashes:
                deleted_count += 1
                if not dry_run:
                    try:
                        await message.delete()
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.error(f"Delete failed for {message.id}: {e}")
            else:
                seen_hashes[file_hash] = message.id

            if checked_count % 100 == 0:
                try:
                    await msg.edit(
                        f"**🧹 Deduping...**\n"
                        f"├ Checked: `{checked_count}`\n"
                        f"├ Unique: `{len(seen_hashes)}`\n"
                        f"└ Deleted: `{deleted_count}`"
                    )
                except:
                    pass

    except Exception as e:
        await event.reply(f"❌ Dedupe failed: {e}")
        return

    # Build sample output with actual videos
    dup_groups = [msgs for msgs in duplicate_groups.values() if len(msgs) > 1]

    result = (
        f"**✅ Dedupe complete**\n"
        f"├ Checked: `{checked_count}`\n"
        f"├ Unique: `{len(seen_hashes)}`\n"
        f"├ Deleted: `{deleted_count}`\n"
        f"└ Mode: Kept oldest copies\n"
    )
    if dry_run:
        result += "\n**DRY RUN** - No files deleted. Run without `dryrun` to actually delete.\n"

    await msg.edit(result)

    if dup_groups:
        await event.reply("**📹 Sample duplicates found:**")
        shown = 0
        for group in dup_groups:
            if shown >= 2: # Send max 2 groups = 4 videos
                break
            shown += 1
            await event.reply(f"**Duplicate Group {shown} - {len(group)} copies:**")

            for i, m in enumerate(group[:2], 1): # Send 2 videos per group
                date_str = m.date.strftime("%Y-%m-%d %H:%M UTC")
                caption = f"Copy {i} | Msg `{m.id}` | {date_str}"

                if is_forum:
                    topic_id = getattr(m, 'reply_to_topic_id', None)
                    topic_name = topic_name_map.get(topic_id, f"Topic {topic_id}") if topic_id else "General"
                    caption += f" | Topic: `{topic_name}`"

                if i == 1:
                    caption += " ← **KEPT**"
                else:
                    caption += " ← **DUPLICATE**"

                try:
                    await client.send_file(event.chat_id, m.media, caption=caption)
                    await asyncio.sleep(2)
                except Exception as e:
                    await event.reply(f"Failed to send video {m.id}: {e}")

            if len(group) > 2:
                await event.reply(f"+{len(group)-2} more identical copies not shown")
            await asyncio.sleep(1)
    else:
        await event.reply("**No duplicates found in channel/group.**")













# ==================== MAIN ====================
async def main():
    await client.start()
    await load_sources()
    await send_log("✅ Bot started successfully")
    print("✅ Bot is running...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

