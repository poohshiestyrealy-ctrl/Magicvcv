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
    "scrape_forward": 5,
    "shorts_forward": 20,
    "shorts_delete": 2
}
DELAYS = SAFE_DELAYS.copy()

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG = {"sources": {}}
mapped_chats = set() # FIX: Initialize globally
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

@client.on(events.NewMessage(pattern=r'/filters'))
async def filters_handler(event):
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS: # FIXED
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
    if not ME_ID or event.sender_id not in ADMIN_IDS: # FIXED
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
    if not ME_ID or event.sender_id not in ADMIN_IDS: # FIXED
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
    global ME_ID
    if not ME_ID or event.sender_id not in ADMIN_IDS: # FIXED
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
    if not ME_ID or event.sender_id not in ADMIN_IDS: # FIXED
        return
    try:
        source_id = int(event.pattern_match.group(1))
        if await remove_mapping(source_id):
            await event.reply(f"✅ Removed mapping for `{source_id}`")
        else:
            await event.reply("❌ Failed to remove mapping")
    except Exception as e:
        await event.reply(f"Error: {e}")











async def scrape_group_with_topics(source_id, target_id, status_msg, force_fresh=False):
    global scraped_count, skipped_count, KILL_SWITCH, ME_ID, DEBUG_AUDIT_LOG
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
    sent_to_general = skipped_too_large = skipped_low_res = skipped_no_map = skipped_non_video = skipped_duration = 0
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
                        f"├ Skip <{FILTERS['min_resolution']}p: `{skipped_low_res}`\n"
                        f"├ Skip >{FILTERS['max_size_mb']}MB: `{skipped_too_large}`\n"
                        f"├ Skip Duration: `{skipped_duration}`\n"
                        f"├ Skip NoMap: `{skipped_no_map}`\n"
                        f"├ Skip NonVideo: `{skipped_non_video}`\n"
                        f"└ Errors: `{errors}`"
                    )
                except:
                    pass
                await save_checkpoint(source_id, message.id)
                await send_debug_audit(checked)
            if not message.video and not message.document:
                skipped_non_video += 1
                continue
            video_attr = get_video_attr(message)
            passes, reason = meets_filters(message, video_attr)
            if not passes:
                if reason == "size":
                    skipped_too_large += 1
                elif reason == "resolution":
                    skipped_low_res += 1
                elif reason == "duration":
                    skipped_duration += 1
                skipped_count += 1
                continue
            src_topic_id = getattr(message, 'reply_to_topic_id', None)
            reply_to, reason = verify_topic_integrity(src_topic_id, topic_map, archive_topic_id)
            if reason == "GENERAL" or reason == "GENERAL_EXPLICIT":
                sent_to_general += 1
            if not reply_to:
                skipped_no_map += 1
                skipped_count += 1
                continue
            caption = ""
            if reason == "ORPHAN_TOPIC":
                original_name = source_topic_names.get(str(src_topic_id), f"Topic {src_topic_id}")
                caption = f"[ARCHIVED FROM: {original_name}]"
            DEBUG_AUDIT_LOG.append({
                "msg_id": message.id,
                "src_topic": src_topic_id if src_topic_id else "None",
                "dst_topic": reply_to,
                "reason": reason
            })
            try:
                reply_to_param = reply_to if reply_to and reply_to!= 1 else None
                if message.text or caption:
                    await client.send_message(target_id, file=message.media, message=caption, reply_to=reply_to_param)
                    await asyncio.sleep(DELAYS["scrape_upload"])
                else:
                    await client.send_message(target_id, file=message.media, message="", reply_to=reply_to_param)
                    await asyncio.sleep(DELAYS["scrape_forward"])
                count += 1
                scraped_count += 1
                await save_checkpoint(source_id, message.id)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except Exception as e:
                errors += 1
                logger.error(f"Send failed: {e}")
        await save_checkpoint(source_id, 0)
        final = (
            f"**✅ Topic Scrape Complete**\n"
            f"├ Videos Checked: `{checked}`\n"
            f"├ Uploaded: `{count}`\n"
            f"├ To General: `{sent_to_general}`\n"
            f"├ Skipped <{FILTERS['min_resolution']}p: `{skipped_low_res}`\n"
            f"├ Skipped >{FILTERS['max_size_mb']}MB: `{skipped_too_large}`\n"
            f"├ Skipped Duration: `{skipped_duration}`\n"
            f"├ Skipped NoMap: `{skipped_no_map}`\n"
            f"├ Skipped NonVideo: `{skipped_non_video}`\n"
            f"└ Errors: `{errors}`"
        )
        if archive_topic_id:
            final += f"\n**Archive ID:** `{archive_topic_id}`"
        await status_msg.edit(final)
    except Exception as e:
        await status_msg.edit(f"❌ Scrape failed: {e}")

@client.on(events.NewMessage(pattern=r'/scrape (-?[0-9]+)'))
async def scrape_channel_handler(event):
    global KILL_SWITCH, scraped_count, skipped_count, ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
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
    await scrape_channel_core(source_id, int(target_id), event, force_fresh=False)

@client.on(events.NewMessage(pattern=r'/scrapefresh (-?[0-9]+)'))
async def scrapefresh_handler(event):
    global KILL_SWITCH, scraped_count, skipped_count, ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
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
    await scrape_channel_core(source_id, int(target_id), event, force_fresh=True)

async def scrape_channel_core(source_id, target_id, event, force_fresh=False):
    global KILL_SWITCH, scraped_count, skipped_count
    KILL_SWITCH = False
    scraped_count = 0
    skipped_count = 0
    if force_fresh:
        await save_checkpoint(source_id, 0)
        await send_log(f"Starting /scrapefresh for {source_id} -> {target_id}")
        msg = await event.reply(f"**🔍 Starting Fresh Channel Scrape**\nSource: `{source_id}`\nTarget: `{target_id}`\nIgnoring checkpoint...")
    else:
        await send_log(f"Starting /scrape for {source_id} -> {target_id}")
        msg = await event.reply(f"**🔍 Starting Channel Scrape**\nSource: `{source_id}`\nTarget: `{target_id}`")
    destination_topics = {}
    try:
        tgt_entity = await client.get_entity(int(target_id))
        if getattr(tgt_entity, 'forum', False):
            topics_res = await client(GetForumTopicsRequest(channel=tgt_entity, offset_date=0, offset_id=0, offset_topic=0, limit=200))
            for t in topics_res.topics:
                normalized_title = t.title.lower().replace(" ", "").replace("_", "").strip()
                destination_topics[normalized_title] = t.id
            destination_topics["general"] = 1
    except Exception as e:
        logger.debug(f"Target parsing bypassed or target channel is flat: {e}")
    last_id = 0 if force_fresh else await get_checkpoint(source_id)
    batch = 0
    skipped_res = skipped_size = skipped_non_video = skipped_duration = errors = 0
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
            if not message.video and not message.document:
                skipped_non_video += 1
                continue
            video_attr = get_video_attr(message)
            passes, reason = meets_filters(message, video_attr)
            if not passes:
                if reason == "size":
                    skipped_size += 1
                elif reason == "resolution":
                    skipped_res += 1
                elif reason == "duration":
                    skipped_duration += 1
                skipped_count += 1
                continue
            reply_to = None
            if message.text and destination_topics:
                hashtags = [word.strip("#.,!?\"'").lower().replace(" ", "").replace("_", "") for word in message.text.split() if word.startswith("#")]
                for tag in hashtags:
                    if tag in destination_topics:
                        reply_to = destination_topics[tag]
                        break
            try:
                reply_to_param = reply_to if reply_to and reply_to!= 1 else None
                if message.text:
                    await client.send_message(int(target_id), file=message.media, message="", reply_to=reply_to_param)
                    await asyncio.sleep(DELAYS["scrape_upload"])
                else:
                    await client.send_message(int(target_id), file=message.media, message="", reply_to=reply_to_param)
                    await asyncio.sleep(DELAYS["scrape_forward"])
                scraped_count += 1
                await save_checkpoint(source_id, message.id)
                batch += 1
                if batch % 10 == 0:
                    await msg.edit(
                        f"**📥 Scraping Channel...**\n"
                        f"├ Scraped: `{scraped_count}`\n"
                        f"├ Skip <{FILTERS['min_resolution']}p: `{skipped_res}`\n"
                        f"├ Skip >{FILTERS['max_size_mb']}MB: `{skipped_size}`\n"
                        f"├ Skip Duration: `{skipped_duration}`\n"
                        f"├ Skip NonVideo: `{skipped_non_video}`\n"
                        f"└ Errors: `{errors}`"
                    )
            except FloodWaitError as e:
                await msg.edit(f"⏳ FloodWait {e.seconds}s...")
                await asyncio.sleep(e.seconds + 5)
            except Exception as e:
                logger.error(f"Error forwarding {message.id}: {e}")
                errors += 1
        await msg.edit(
            f"**✅ Channel Scrape Complete**\n"
            f"├ Scraped: `{scraped_count}`\n"
            f"├ Skipped <{FILTERS['min_resolution']}p: `{skipped_res}`\n"
            f"├ Skipped >{FILTERS['max_size_mb']}MB: `{skipped_size}`\n"
            f"├ Skipped Duration: `{skipped_duration}`\n"
            f"├ Skipped NonVideo: `{skipped_non_video}`\n"
            f"└ Errors: `{errors}`"
        )
        await send_log(f"Scrape done: {scraped_count} scraped")
    except Exception as e:
        await msg.edit(f"❌ Scrape failed: {e}")
        await send_log(f"Scrape error: {e}")

@client.on(events.NewMessage(pattern=r'/shorts (-?[0-9]+) (-?[0-9]+)'))
async def shorts_handler(event):
    global KILL_SWITCH, ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
        return
    if KILL_SWITCH:
        await event.reply("Kill switch is active. Run `/resetkill` first.")
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("**🎬 Starting /shorts**\nCaching destination topics...")
    destination_topics = {}
    try:
        tgt_entity = await client.get_entity(target_id)
        if getattr(tgt_entity, 'forum', False):
            topics_res = await client(GetForumTopicsRequest(channel=tgt_entity, offset_date=0, offset_id=0, offset_topic=0, limit=200))
            for t in topics_res.topics:
                normalized_title = t.title.lower().replace(" ", "").replace("_", "").strip()
                destination_topics[normalized_title] = t.id
            destination_topics["general"] = 1
    except Exception as e:
        logger.debug(f"Shorts topic fetch skipped or target channel flat: {e}")
    await msg.edit("**🎬 Processing Shorts...**\nForwarding videos ≤60s...")
    count = checked = errors = skipped_duration = skipped_size = skipped_no_attr = 0
    MAX_SHORTS_SIZE_NO_ATTR = 10 * 1024
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
            reply_to = None
            if message.text and destination_topics:
                hashtags = [word.strip("#.,!?\"'").lower().replace(" ", "").replace("_", "") for word in message.text.split() if word.startswith("#")]
                for tag in hashtags:
                    if tag in destination_topics:
                        reply_to = destination_topics[tag]
                        break
            try:
                reply_to_param = reply_to if reply_to and reply_to!= 1 else None
                await client.send_message(target_id, file=message.media, message="", reply_to=reply_to_param)
                await asyncio.sleep(DELAYS["shorts_forward"])
                await client.delete_messages(source_id, message.id)
                await asyncio.sleep(DELAYS["shorts_delete"])
                count += 1
                await save_checkpoint(source_id, message.id)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
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
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
        return
    if event.is_private and NORMAL_BOT_USERNAME:
        chat = await event.get_chat()
        if chat.username and chat.username.lower() == NORMAL_BOT_USERNAME.lower():
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
`/scrape <src_id>` - Channel/Group → Channel, resume from checkpoint
`/scrapefresh <src_id>` - Channel → Channel, ignore checkpoint, start from 0
`/scrapegrouplike <src_id> [fresh]` - Group with topics, maps to topics. Add 'fresh' to restart
`/testmapping <src_id> <dst_id>` - Send test videos to verify mapping
`/killall` - Emergency stop all scrapers
`/resetkill` - Re-enable scrapers

**🎬 3. Shorts**
`/shorts <src_id> <dst_id>` - Forward videos ≤60s + delete from source

**🧹 4. Dedupe**
`/dedupe <target_id> [dryrun]` - Delete duplicate videos, keeps oldest. Sends sample videos. Add 'dryrun' to preview

**⚙️ 5. Filters & Delays**
`/filters` - Show current filter settings
`/setfilter <type> <value>` - Change: size_mb, resolution, duration
`/delays` - Show current delay settings
`/setdelay <type> <seconds>` - Live change: scrape_upload, scrape_forward, shorts_forward, shorts_delete

**📊 6. Other**
`/stats` - Show stats
`/debugvideos <group_id>` - Sample 2 videos with metadata
"""
    await event.reply(help_text)

@client.on(events.NewMessage(pattern=r'/clearmapping (-?[0-9]+) (-?[0-9]+)'))
async def clear_mapping(event):
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
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
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
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
    global KILL_SWITCH, ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
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
    global ME_ID, scraped_count, skipped_count
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
        return
    await event.reply(f"**📊 Bot Stats**\n├ Scraped: `{scraped_count}`\n├ Skipped (Filters/NoMap): `{skipped_count}`\n└ Mappings: `{len(CONFIG['sources'])}`")

@client.on(events.NewMessage(pattern=r'/dedupe (-?[0-9]+)(?:\s+(dryrun))?'))
async def dedupe_target(event):
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
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

@client.on(events.NewMessage(pattern=r'/delays'))
async def delays_handler(event):
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
        return
    text = "**⚙️ Dynamic Delays**\n"
    for k, v in DELAYS.items():
        text += f"├ `{k}`: `{v}s`\n"
    text += "\nUse `/setdelay <type> <seconds>` to change live"
    await event.reply(text)

@client.on(events.NewMessage(pattern=r'/setdelay (\w+) (\d+)'))
async def setdelay_handler(event):
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
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

@client.on(events.NewMessage(pattern=r'/resyncgroupfresh (-?[0-9]+) (-?[0-9]+)'))
async def resyncgroupfresh_handler(event):
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
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
        await msg.edit(f"**✅ Resync complete**\n├ Mapped: `{len(new_map)}`\n├ Created: `{created}`\n└ Use `/scrapegrouplike` to start")

    except Exception as e:
        await msg.edit(f"❌ Resync failed: {e}")

@client.on(events.NewMessage(pattern=r'/testmapping (-?[0-9]+) (-?[0-9]+)'))
async def test_mapping_handler(event):
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))

    topic_map = await get_topic_map(source_id, target_id)
    archive_id = await get_archive_topic_id(source_id, target_id)

    if not topic_map:
        await event.reply("No topic map found. Run `/resyncgroupfresh` first")
        return

    await event.reply(f"**🧪 Testing mapping**\nSending test videos to each mapped topic...")

    test_count = 0
    for src_tid_str, dst_tid in topic_map.items():
        if test_count >= 3:
            break
        try:
            await client.send_message(
                target_id,
                f"🧪 Test mapping: Source Topic `{src_tid_str}` → Dest Topic `{dst_tid}`",
                reply_to=dst_tid
            )
            test_count += 1
            await asyncio.sleep(2)
        except Exception as e:
            await event.reply(f"Failed test to topic `{dst_tid}`: {e}")

    if archive_id:
        try:
            await client.send_message(
                target_id,
                f"🧪 Test ARCHIVE topic",
                reply_to=archive_id
            )
        except Exception as e:
            await event.reply(f"Failed test to ARCHIVE: {e}")

    await event.reply(f"**✅ Sent {test_count} test messages**\nCheck target group topics")

@client.on(events.NewMessage(pattern=r'/debugtopics (-?[0-9]+)'))
async def debug_topics(event):
    global ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
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
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
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

@client.on(events.NewMessage(pattern=r'/killall'))
async def kill_all_handler(event):
    global KILL_SWITCH, ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
        return
    KILL_SWITCH = True
    await event.reply("**🛑 Emergency Kill Switch Activated.** All active scrapers are stopping safely at their next message checkpoint.")

@client.on(events.NewMessage(pattern=r'/resetkill'))
async def reset_kill_handler(event):
    global KILL_SWITCH, ME_ID
    if not ME_ID or event.chat_id!= ME_ID:
        return
    if not is_admin(event.sender_id):
        return
    KILL_SWITCH = False
    await event.reply("**✅ Kill Switch Deactivated.** Scrapers are re-enabled and ready to run.")

# ==================== MAIN ====================
async def main():
    global ME_ID
    await client.start()

    # Lock ME_ID instantly at startup to prevent handler race validation crashes
    ME_ID = (await client.get_me()).id

    await load_sources()
    await send_log(f"✅ Bot started successfully. ME_ID: {ME_ID}")
    print(f"✅ Bot is running... ME_ID: {ME_ID}")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())