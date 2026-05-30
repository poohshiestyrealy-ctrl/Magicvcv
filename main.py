import os
import asyncio
import logging
import hashlib
import random
from collections import defaultdict
from datetime import datetime
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
NORMAL_BOT_USERNAME = os.getenv("NORMAL_BOT_USERNAME", "")

# ============ DYNAMIC FILTERS ============
FILTERS = {
    "max_size_mb": 200, # 200-500 MB
    "min_resolution": 720, # 480-2160
    "max_duration": 0 # 0 = no limit, else seconds
}

TOPIC_CREATE_DELAY = 60

# ============ DYNAMIC DELAY ENGINE ============
SAFE_DELAYS = {
    "scrape_upload": 30,
    "scrape_copy": 12,
    "scrape_forward": 5,
    "shorts_forward": 20,
    "shorts_delete": 2
}
DELAYS = SAFE_DELAYS.copy()

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG = {"sources": {}}
mapped_chats = set()
scraped_count = 0
skipped_count = 0
KILL_SWITCH = False
ME_ID = 0
DEBUG_AUDIT_LOG = []

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
    return get_video_attr(message) is not None

def meets_filters(message, video_attr):
    if message.file and message.file.size > FILTERS["max_size_mb"] * 1024 * 1024:
        return False, "size"
    if video_attr:
        height = getattr(video_attr, 'h', 0)
        if height < FILTERS["min_resolution"]:
            return False, "resolution"
        if FILTERS["max_duration"] > 0:
            duration = getattr(video_attr, 'duration', 0)
            if duration > FILTERS["max_duration"]:
                return False, "duration"
    return True, ""

def verify_topic_integrity(src_topic_id, topic_map, archive_topic_id):
    if not src_topic_id:
        return 1, "GENERAL"
    if src_topic_id == 1:
        return 1, "GENERAL_EXPLICIT"
    reply_to = topic_map.get(str(src_topic_id))
    if reply_to:
        return reply_to, "MAPPED"
    elif archive_topic_id:
        return archive_topic_id, "ORPHAN_TOPIC"
    else:
        return None, "NO_MAP"

async def send_debug_audit(checked_count):
    if not DEBUG_AUDIT_LOG or BOT_LOG_CHAT_ID == 0:
        return
    sample = random.sample(DEBUG_AUDIT_LOG, min(3, len(DEBUG_AUDIT_LOG)))
    text = f"**🔍 Telemetry Audit @ {checked_count} videos**\n"
    for i, log in enumerate(sample, 1):
        text += f"\n**Sample {i}:**\n├ Msg: `{log['msg_id']}`\n├ Src Topic: `{log['src_topic']}`\n├ Routed To: `{log['dst_topic']}`\n└ Reason: `{log['reason']}`\n"
    try:
        await client.send_message(BOT_LOG_CHAT_ID, text)
    except:
        pass
    DEBUG_AUDIT_LOG.clear()















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
        if res.data and len(res.data) > 0:
            return res.data[0].get("last_message_id", 0)
        return 0
    except Exception as e:
        logger.error(f"Get checkpoint failed: {e}")
        return 0

async def get_topic_map(source_id, target_id):
    try:
        res = supabase.table("group_topic_map").select("mapping").eq("source_id", source_id).eq("target_id", target_id).execute()
        if res.data and len(res.data) > 0:
            return res.data[0].get("mapping") if res.data[0].get("mapping") else {}
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

async def ensure_archive_topic(source_id, target_id, source_name=""):
    existing_id = await get_archive_topic_id(source_id, target_id)
    if existing_id:
        return existing_id
    try:
        tgt_entity = await client.get_entity(target_id)
        if not getattr(tgt_entity, 'forum', False):
            return None
        result = await client(CreateForumTopicRequest(
            channel=tgt_entity,
            title=f"📦 Archive - {source_name or source_id}",
            icon_color=0x6FB9F0
        ))
        archive_id = result.updates[1].message.id
        await save_archive_topic_id(source_id, target_id, archive_id)
        await asyncio.sleep(5)
        return archive_id
    except Exception as e:
        logger.error(f"Archive topic create failed: {e}")
        return None

async def copy_video_to_target(target_id, message, caption="", reply_to=None):
    try:
        await client.send_message(
            target_id,
            file=message,
            message=caption,
            reply_to=reply_to
        )
        return "copy", True
    except Exception as e:
        logger.warning(f"Server copy failed for {message.id}, fallback to upload: {e}")
        try:
            await client.send_message(
                target_id,
                file=message.media,
                message=caption,
                reply_to=reply_to
            )
            return "upload", True
        except Exception as e2:
            logger.error(f"Both copy+upload failed for {message.id}: {e2}")
            return "failed", False











@client.on(events.NewMessage(pattern=r'/filters'))
async def filters_handler(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    text = "**🎛️ Current Filters**\n"
    text += f"├ Max Size: `{FILTERS['max_size_mb']} MB`\n"
    text += f"├ Min Resolution: `{FILTERS['min_resolution']}p`\n"
    text += f"└ Max Duration: `{'No limit' if FILTERS['max_duration']==0 else str(FILTERS['max_duration'])+'s'}`\n\n"
    text += "Use `/setfilter <type> <value>`\n"
    text += "Types: `size_mb`, `resolution`, `duration`"
    await event.reply(text)

@client.on(events.NewMessage(pattern=r'/setfilter (\w+) (\d+)'))
async def setfilter_handler(event):
    global ME_ID, FILTERS
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    ftype = event.pattern_match.group(1)
    val = int(event.pattern_match.group(2))
    if ftype == "size_mb":
        if val < 200 or val > 500:
            await event.reply("❌ Size must be 200-500 MB")
            return
        FILTERS["max_size_mb"] = val
    elif ftype == "resolution":
        if val < 480 or val > 2160:
            await event.reply("❌ Resolution must be 480-2160")
            return
        FILTERS["min_resolution"] = val
    elif ftype == "duration":
        if val < 0 or val > 3600:
            await event.reply("❌ Duration must be 0-3600 seconds. 0 = no limit")
            return
        FILTERS["max_duration"] = val
    else:
        await event.reply("Invalid type. Use: size_mb, resolution, duration")
        return
    await event.reply(f"✅ Set `{ftype}` to `{val}`")

@client.on(events.NewMessage(pattern=r'/listmappings'))
async def list_mappings(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    if not CONFIG["sources"]:
        await event.reply("No mappings found")
        return
    text = "**🔗 Current Mappings:**\n"
    for src, dst in CONFIG["sources"].items():
        text += f"`{src}` → `{dst}`\n"
    await event.reply(text)

@client.on(events.NewMessage(pattern=r'/mapshow (-?[0-9]+)'))
async def mapshow_handler(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    source_id = int(event.pattern_match.group(1))
    target_id = CONFIG["sources"].get(str(source_id))
    if not target_id:
        await event.reply(f"No mapping for `{source_id}`. Use `/addsource` first")
        return

    topic_map = await get_topic_map(source_id, int(target_id))
    archive_id = await get_archive_topic_id(source_id, int(target_id))

    text = f"**🗺️ Map `{source_id}` → `{target_id}`**\n"
    text += f"├ Archive Topic: `{archive_id}`\n"
    text += f"└ Mapped Topics: `{len(topic_map)}`\n\n"

    if topic_map:
        text += "**Mappings:**\n"
        for src_tid, dst_tid in list(topic_map.items())[:20]:
            text += f"`{src_tid}` → `{dst_tid}`\n"
        if len(topic_map) > 20:
            text += f"... and {len(topic_map) - 20} more"
    else:
        text += "❌ **EMPTY MAP** - run `/resyncgroupfresh`"

    await event.reply(text)

@client.on(events.NewMessage(pattern=r'/addsource (-?[0-9]+) (-?[0-9]+)'))
async def add_source(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    try:
        source_id = int(event.pattern_match.group(1))
        target_id = int(event.pattern_match.group(2))
        await send_log(f"Attempting to add mapping: {source_id} -> {target_id}")
        if await save_mapping(source_id, target_id):
            await event.reply(f"✅ Added mapping: `{source_id}` → `{target_id}`")
            await send_log(f"Successfully added mapping: {source_id} -> {target_id}")
        else:
            await event.reply("❌ Failed to save mapping - check Supabase connection")
    except Exception as e:
        logger.error(f"add_source error: {e}")
        await event.reply(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/removesource (-?[0-9]+)'))
async def remove_source(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    try:
        source_id = int(event.pattern_match.group(1))
        if await remove_mapping(source_id):
            await event.reply(f"✅ Removed mapping for `{source_id}`")
        else:
            await event.reply("❌ Failed to remove mapping")
    except Exception as e:
        await event.reply(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/delays'))
async def delays_handler(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    text = "**⚙️ Dynamic Delays**\n"
    for k, v in DELAYS.items():
        text += f"├ `{k}`: `{v}s`\n"
    text += "\nUse `/setdelay <type> <seconds>` to change live"
    await event.reply(text)

@client.on(events.NewMessage(pattern=r'/setdelay (\w+) (\d+)'))
async def setdelay_handler(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    dtype = event.pattern_match.group(1)
    val = int(event.pattern_match.group(2))
    if dtype not in DELAYS:
        await event.reply(f"Invalid type. Options: {', '.join(DELAYS.keys())}")
        return
    if val < 1 or val > 300:
        await event.reply("Delay must be 1-300 seconds")
        return
    DELAYS[dtype] = val
    await event.reply(f"✅ Set `{dtype}` to `{val}s`")

@client.on(events.NewMessage(pattern=r'/killall'))
async def kill_all_handler(event):
    global KILL_SWITCH, ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    KILL_SWITCH = True
    await event.reply("**🛑 Emergency Kill Switch Activated.** All active scrapers are stopping safely at their next message checkpoint.")

@client.on(events.NewMessage(pattern=r'/resetkill'))
async def reset_kill_handler(event):
    global KILL_SWITCH, ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    KILL_SWITCH = False
    await event.reply("**✅ Kill Switch Deactivated.** Scrapers are re-enabled and ready to run.")

@client.on(events.NewMessage(pattern=r'/stats'))
async def stats_handler(event):
    global ME_ID, scraped_count, skipped_count
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    await event.reply(f"**📊 Bot Stats**\n├ Scraped: `{scraped_count}`\n├ Skipped (Filters/NoMap): `{skipped_count}`\n└ Mappings: `{len(CONFIG['sources'])}`")











@client.on(events.NewMessage(pattern=r'/resyncgroupfresh (-?[0-9]+) (-?[0-9]+)'))
async def resyncgroupfresh_handler(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("**🔄 Starting topic resync...**\nFetching source topics...")
    try:
        src_entity = await client.get_entity(source_id)
        tgt_entity = await client.get_entity(target_id)
        if not getattr(tgt_entity, 'forum', False):
            await msg.edit("❌ Target is not a forum group")
            return
        src_topics_res = await client(GetForumTopicsRequest(channel=src_entity, offset_date=0, offset_id=0, offset_topic=0, limit=200))
        tgt_topics_res = await client(GetForumTopicsRequest(channel=tgt_entity, offset_date=0, offset_id=0, offset_topic=0, limit=200))
        existing_titles = {t.title: t.id for t in tgt_topics_res.topics}
        new_map = {}
        created = 0
        for src_topic in src_topics_res.topics:
            if src_topic.title in existing_titles:
                new_map[str(src_topic.id)] = existing_titles[src_topic.title]
            else:
                try:
                    result = await client(CreateForumTopicRequest(
                        channel=tgt_entity,
                        title=src_topic.title,
                        icon_color=src_topic.icon_color,
                        icon_emoji_id=getattr(src_topic, 'icon_emoji_id', None)
                    ))
                    new_map[str(src_topic.id)] = result.updates[1].message.id
                    created += 1
                    await asyncio.sleep(TOPIC_CREATE_DELAY)
                except Exception as e:
                    logger.error(f"Failed to create topic {src_topic.title}: {e}")
        await save_topic_map(source_id, target_id, new_map)
        archive_id = await ensure_archive_topic(source_id, target_id, getattr(src_entity, 'title', ''))
        await msg.edit(f"**✅ Resync complete**\n├ Mapped: `{len(new_map)}`\n├ Created: `{created}`\n├ Archive ID: `{archive_id}`\n└ Use `/scrapegrouplike` to start")
    except Exception as e:
        await msg.edit(f"❌ Resync failed: {e}")

@client.on(events.NewMessage(pattern=r'/testmapping (-?[0-9]+) (-?[0-9]+)'))
async def test_mapping_handler(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    topic_map = await get_topic_map(source_id, target_id)
    archive_id = await get_archive_topic_id(source_id, target_id)
    if not topic_map:
        await event.reply("No topic map found. Run `/resyncgroupfresh` first")
        return

    await event.reply(f"**🧪 Testing mapping with REAL videos**\nSending 1 video per mapped topic...")
    test_count = 0

    for src_tid_str, dst_tid in topic_map.items():
        if test_count >= 3:
            break
        try:
            async for msg in client.iter_messages(source_id, limit=50, reply_to=int(src_tid_str), filter=InputMessagesFilterVideo):
                if is_video_message(msg):
                    caption = f"🧪 TEST: Source Topic `{src_tid_str}` → Dest Topic `{dst_tid}`"
                    await client.send_file(target_id, msg.media, caption=caption, reply_to=dst_tid)
                    test_count += 1
                    await asyncio.sleep(3)
                    break
        except Exception as e:
            await event.reply(f"Failed test topic `{src_tid_str}`: {e}")

    if archive_id:
        try:
            await client.send_message(target_id, f"🧪 Test ARCHIVE topic", reply_to=archive_id)
        except Exception as e:
            await event.reply(f"Failed test to ARCHIVE: {e}")

    await event.reply(f"**✅ Sent {test_count} test videos**\nCheck if they landed in the right topics")

@client.on(events.NewMessage(pattern=r'/debugtopics (-?[0-9]+)'))
async def debug_topics(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    gid = int(event.pattern_match.group(1))
    try:
        entity = await client.get_entity(gid)
        res = await client(GetForumTopicsRequest(channel=entity, offset_date=0, offset_id=0, offset_topic=0, limit=200))
        text = f"**📋 Topics in `{gid}`**\n"
        for t in res.topics:
            text += f"• `{t.id}`: {t.title}\n"
        if len(text) > 4000:
            text = text[:4000] + "\n...truncated"
        await event.reply(text)
    except Exception as e:
        await event.reply(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/debugvideos (-?[0-9]+)'))
async def debug_videos(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    gid = int(event.pattern_match.group(1))
    msg = await event.reply(f"**🔍 Sampling 2 videos from `{gid}`**...")
    count = 0
    try:
        async for message in client.iter_messages(gid, limit=50, filter=InputMessagesFilterVideo):
            if count >= 2:
                break
            if is_video_message(message):
                video_attr = get_video_attr(message)
                duration = getattr(video_attr, 'duration', 'N/A')
                height = getattr(video_attr, 'h', 'N/A')
                width = getattr(video_attr, 'w', 'N/A')
                size_mb = (message.file.size / (1024 * 1024)) if message.file else 0
                topic_id = getattr(message, 'reply_to_topic_id', 'None')
                caption = (
                    f"**Video {count+1}**\n"
                    f"├ ID: `{message.id}`\n"
                    f"├ Size: `{size_mb:.2f} MB`\n"
                    f"├ Duration: `{duration}s`\n"
                    f"├ Resolution: `{width}x{height}`\n"
                    f"└ Topic: `{topic_id}`"
                )
                await client.send_file(event.chat_id, message.media, caption=caption)
                count += 1
                await asyncio.sleep(2)
        if count == 0:
            await msg.edit("No videos found in last 50 messages")
        else:
            await msg.edit(f"**✅ Sent {count} sample videos**")
    except Exception as e:
        await msg.edit(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/dedupe (-?[0-9]+)(?:\s+(dryrun))?'))
async def dedupe_target(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS:
        return
    target_id = int(event.pattern_match.group(1))
    dry_run = event.pattern_match.group(2) == 'dryrun'
    msg = await event.reply("**🧹 Starting dedupe scan oldest→newest...**\nCollecting samples...")
    seen_hashes = {}
    duplicate_groups = defaultdict(list)
    deleted_count = 0
    checked_count = 0
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
    except Exception as e:
        logger.debug(f"Forum check failed during dedupe initialization: {e}")
    try:
        async for message in client.iter_messages(target_id, limit=None, reverse=True):
            if not is_video_message(message):
                continue
            checked_count += 1
            h = hashlib.md5()
            h.update(str(message.file.size).encode())
            h.update(str(getattr(get_video_attr(message), 'duration', 0)).encode())
            bytes_read = 0
            async for chunk in client.iter_download(message.media, chunk_size=8192):
                h.update(chunk)
                bytes_read += len(chunk)
                if bytes_read >= 5 * 1024:
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
            if shown >= 2:
                break
            shown += 1
            await event.reply(f"**Duplicate Group {shown} - {len(group)} copies:**")
            for i, m in enumerate(group[:2], 1):
                date_str = m.date.strftime("%Y-%m-%d %H:%M UTC")
                caption = f"Copy {i} | Msg `{m.id}` | {date_str}"
                if is_forum:
                    topic_id = getattr(m, 'reply_to_topic_id', None)
                    if topic_id:
                        topic_name = topic_name_map.get(topic_id, f"Topic {topic_id}")
                    else:
                        topic_name = "General"
                    caption += f" | Topic: `{topic_name}`"
                if i == 1:
                    caption += " ← **KEPT**"
                else:
                    caption += " ← **DUPLICATE**" if dry_run else " ← **DELETED**"
                try:
                    await client.send_file(event.chat_id, m.media, caption=caption)
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Failed to send sample {m.id}: {e}")
        await event.reply(f"**Shown {shown} duplicate groups**")

# ==================== MAIN ====================
async def main():
    global ME_ID
    await client.start()
    ME_ID = (await client.get_me()).id
    await load_sources()
    await send_log(f"✅ Bot started successfully. ME_ID: {ME_ID}")
    print(f"✅ Bot is running... ME_ID: {ME_ID}")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())