import os
import asyncio
import logging
import time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ChatAdminRequiredError
from telethon.tl.types import DocumentAttributeVideo
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

MAX_FILE_SIZE = 200 * 1024 * 1024
UPLOAD_DELAY = 30
TOPIC_CREATE_DELAY = 60
SHORT_MAX_DURATION = 60
MIN_RESOLUTION = 720

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
    if message.document and message.document.mime_type == "video/mp4":
        return any(getattr(a, 'round_message', False) or getattr(a, 'animated', False)
                   for a in getattr(message.document, 'attributes', []))
    return False

def is_short(message):
    if is_gif(message):
        return False
    video_attr = get_video_attr(message)
    if not video_attr:
        return False
    duration = getattr(video_attr, 'duration', 0)
    return 0 < duration <= SHORT_MAX_DURATION

@client.on(events.NewMessage(pattern=r'/setdelay ([0-9]+)'))
async def set_delay_cmd(event):
    global UPLOAD_DELAY
    if not is_admin(event.sender_id):
        return
    new_delay = int(event.pattern_match.group(1))
    if new_delay < 5:
        await event.reply("**Delay too low. Minimum 5s to avoid bans.**")
        return
    if new_delay > 300:
        await event.reply("**Delay too high. Maximum 300s.**")
        return
    UPLOAD_DELAY = new_delay
    await event.reply(f"**Upload delay set to {UPLOAD_DELAY}s**\nApplies to all scrapers.")

@client.on(events.NewMessage(pattern=r'/getdelay'))
async def get_delay_cmd(event):
    if not is_admin(event.sender_id):
        return
    await event.reply(f"**Current upload delay: {UPLOAD_DELAY}s**")

async def load_sources():
    global CONFIG
    try:
        res = supabase.table("mappings").select("*").execute()
        CONFIG["sources"] = {str(row["source_id"]): str(row["target_id"]) for row in res.data}
        rebuild_mapped_chats()
        await send_log(f"Loaded {len(CONFIG['sources'])} source mappings")
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














# ==================== UNIFIED SHORT COMMAND ====================
async def scrape_shorts_history(source_id, target_id, status_msg):
    global scraped_count, KILL_SWITCH, UPLOAD_DELAY
    count = checked = errors = 0
    skipped_not_short = skipped_size = 0
    current_delay = UPLOAD_DELAY

    try:
        await status_msg.edit(f"**Phase 1/2: Scraping short history**\nSource: `{source_id}` → `{target_id}`")
        async for message in client.iter_messages(source_id, limit=None, reverse=True):
            if KILL_SWITCH:
                await status_msg.edit("**Short scrape aborted by kill switch**")
                return

            checked += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(
                        f"**Phase 1/2: History**\n"
                        f"Checked: {checked}\n"
                        f"Forwarded: {count}\n"
                        f"Skip NotShort: {skipped_not_short}\n"
                        f"Skip >200MB: {skipped_size}\n"
                        f"Errors: {errors}"
                    )
                except:
                    pass

            if not is_short(message):
                skipped_not_short += 1
                continue

            if message.file and message.file.size > MAX_FILE_SIZE:
                skipped_size += 1
                continue

            try:
                await client.forward_messages(target_id, message, from_peer=source_id)
                count += 1
                scraped_count += 1
                await asyncio.sleep(current_delay)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                current_delay = min(current_delay * 1.5, 60)
            except Exception as e:
                errors += 1
                logger.error(f"Forward failed: {e}")

        await status_msg.edit(
            f"**History done. Now auto-forwarding**\n"
            f"Checked: `{checked}`\n"
            f"Forwarded: `{count}`\n"
            f"Skipped Not Short: `{skipped_not_short}`\n"
            f"Skipped >200MB: `{skipped_size}`\n"
            f"Errors: `{errors}`\n\n"
            f"New shorts will auto-forward. Use `/removesource {source_id}` or `/killall` to stop."
        )

    except Exception as e:
        await status_msg.edit(f"Scrape failed: {e}")

@client.on(events.NewMessage(pattern=r'/short (-?[0-9]+)'))
async def short_cmd(event):
    global KILL_SWITCH
    if not is_admin(event.sender_id):
        return
    if KILL_SWITCH:
        await event.reply("Kill switch is active. Run `/resetkill` first.")
        return
    source_id = int(event.pattern_match.group(1))
    target_id = CONFIG["sources"].get(str(source_id))
    if not target_id:
        await event.reply(f"No mapping for `{source_id}`. Use `/addsource {source_id} <target_id>` first")
        return

    msg = await event.reply(f"Starting `/short` for `{source_id}` → `{target_id}`\nScraping history then auto-forwarding new shorts.")
    await scrape_shorts_history(source_id, int(target_id))

@client.on(events.NewMessage)
async def auto_short_handler(event):
    src_id = str(event.chat_id)
    if src_id in CONFIG["sources"] and is_short(event.message):
        if KILL_SWITCH:
            return
        target = int(CONFIG["sources"][src_id])
        try:
            await client.forward_messages(target, event.message, from_peer=event.chat_id)
            logger.info(f"Auto-forwarded Short from {src_id} to {target}")
        except Exception as e:
            logger.error(f"Auto Short failed: {e}")

# ==================== CHANNEL SCRAPER ====================
async def scrape_channel_to_channel(source_id, target_id, status_msg, force_fresh=False):
    global scraped_count, skipped_count, KILL_SWITCH, UPLOAD_DELAY

    offset_id = 0 if force_fresh else await get_checkpoint(source_id)
    if force_fresh:
        await save_checkpoint(source_id, 0)

    count = checked = errors = 0
    skipped_not_video = skipped_size = skipped_resolution = skipped_gif = 0
    current_delay = UPLOAD_DELAY

    try:
        async for message in client.iter_messages(source_id, limit=None, offset_id=offset_id, reverse=True):
            if KILL_SWITCH:
                await status_msg.edit("**Scrape aborted by kill switch**")
                await save_checkpoint(source_id, message.id)
                return

            checked += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(
                        f"**Channel→Channel Scrape**\n"
                        f"Checked: {checked}\n"
                        f"Uploaded: {count}\n"
                        f"Skip NotVideo: {skipped_not_video}\n"
                        f"Skip GIF: {skipped_gif}\n"
                        f"Skip >200MB: {skipped_size}\n"
                        f"Skip <720p: {skipped_resolution}\n"
                        f"Errors: {errors}"
                    )
                except:
                    pass
                await save_checkpoint(source_id, message.id)

            if not is_video_message(message):
                skipped_not_video += 1
                continue

            if is_gif(message):
                skipped_gif += 1
                continue

            if message.file and message.file.size > MAX_FILE_SIZE:
                skipped_size += 1
                continue

            video_attr = get_video_attr(message)
            if not video_attr:
                skipped_not_video += 1
                continue

            width = getattr(video_attr, 'w', 0)
            height = getattr(video_attr, 'h', 0)
            if width < MIN_RESOLUTION and height < MIN_RESOLUTION:
                skipped_resolution += 1
                continue

            try:
                await client.send_file(
                    target_id,
                    message.media,
                    caption="",
                    attributes=[video_attr],
                    force_document=False,
                    allow_cache=False
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
        await status_msg.edit(
            f"**Channel scrape done**\n"
            f"Checked: `{checked}`\n"
            f"Uploaded: `{count}`\n"
            f"Skipped Not Video: `{skipped_not_video}`\n"
            f"Skipped GIF: `{skipped_gif}`\n"
            f"Skipped >200MB: `{skipped_size}`\n"
            f"Skipped <720p: `{skipped_resolution}`\n"
            f"Errors: `{errors}`"
        )

    except Exception as e:
        await status_msg.edit(f"Scrape failed: {e}")

@client.on(events.NewMessage(pattern=r'/scrapechannel (-?[0-9]+) (-?[0-9]+)(?:\s+(fresh))?'))
async def scrape_channel_cmd(event):
    global KILL_SWITCH
    if not is_admin(event.sender_id):
        return
    if KILL_SWITCH:
        await event.reply("Kill switch is active. Run `/resetkill` first.")
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    force_fresh = event.pattern_match.group(3) == 'fresh'
    msg = await event.reply("Starting channel scrape...")
    await scrape_channel_to_channel(source_id, target_id, msg, force_fresh)















# ==================== GROUP SCRAPER ====================
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
    sent_to_general = skipped_no_video = skipped_too_large = skipped_no_map = 0
    current_delay = UPLOAD_DELAY

    try:
        async for message in client.iter_messages(source_id, limit=None, offset_id=offset_id, reverse=True):
            if KILL_SWITCH:
                await status_msg.edit("**Scrape aborted by kill switch**")
                await save_checkpoint(source_id, message.id)
                return

            checked += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(
                        f"Checked: {checked}\n"
                        f"Uploaded: {count}\n"
                        f"Sent to General: {sent_to_general}\n"
                        f"Skip NoVideo: {skipped_no_video}\n"
                        f"Skip >200MB: {skipped_too_large}\n"
                        f"Skip NoMap: {skipped_no_map}\n"
                        f"Errors: {errors}"
                    )
                except:
                    pass
                await save_checkpoint(source_id, message.id)

            if message.file and message.file.size > MAX_FILE_SIZE:
                skipped_too_large += 1
                continue

            if not is_video_message(message):
                skipped_no_video += 1
                continue

            video_attr = get_video_attr(message)
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
            f"**Topic scrape done**\n"
            f"Checked: `{checked}`\n"
            f"Uploaded: `{count}`\n"
            f"Sent to General: `{sent_to_general}`\n"
            f"Skipped No Video: `{skipped_no_video}`\n"
            f"Skipped >200MB: `{skipped_too_large}`\n"
            f"Skipped No Map: `{skipped_no_map}`\n"
            f"Errors: `{errors}`"
        )
        if archive_topic_id:
            final += f"\nArchive ID: `{archive_topic_id}`"
        await status_msg.edit(final)

    except Exception as e:
        await status_msg.edit(f"Scrape failed: {e}")

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

# ==================== KILL SWITCH ====================
@client.on(events.NewMessage(pattern=r'/killall'))
async def kill_all(event):
    global KILL_SWITCH
    if not is_admin(event.sender_id):
        return
    KILL_SWITCH = True
    await event.reply("**KILL SWITCH ACTIVATED**\nStopping all running scrapers and auto-forwards...")
    await send_log(f"KILL SWITCH triggered by {event.sender_id}")

@client.on(events.NewMessage(pattern=r'/resetkill'))
async def reset_kill(event):
    global KILL_SWITCH
    if not is_admin(event.sender_id):
        return
    KILL_SWITCH = False
    await event.reply("Kill switch reset. Scrapers can run again.")

# ==================== TOPIC MANAGEMENT ====================
@client.on(events.NewMessage(pattern=r'/resyncgroupfresh (-?[0-9]+) (-?[0-9]+)'))
async def resync_group_fresh(event):
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("🔄 Starting FRESH topic resync...")

    try:
        src_entity = await asyncio.wait_for(client.get_entity(source_id), timeout=15)
        tgt_entity = await asyncio.wait_for(client.get_entity(target_id), timeout=15)
    except Exception as e:
        await msg.edit(f"❌ Error: {e}")
        return

    if not getattr(src_entity, 'forum', False) or not getattr(tgt_entity, 'forum', False):
        await msg.edit("❌ Both groups need topics enabled. Group →... → Manage Group → Turn on Topics")
        return

    await msg.edit("📡 Fetching source topics...")

    all_topics = []
    offset_date = 0
    offset_id = 0
    offset_topic = 0
    retries = 0
    max_retries = 8

    while retries < max_retries:
        try:
            res = await asyncio.wait_for(
                client(GetForumTopicsRequest(
                    channel=src_entity,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=100,
                )),
                timeout=30
            )

            if not res.topics or len(res.topics) == 0:
                break

            all_topics.extend(res.topics)
            await msg.edit(f"📡 Fetched {len(all_topics)} topics so far...")

            if len(res.topics) < 100:
                break

            last = res.topics[-1]
            offset_date = getattr(last, 'date', 0)
            offset_id = getattr(last, 'top_message', 0)
            offset_topic = last.id

            await asyncio.sleep(2)

        except asyncio.TimeoutError:
            retries += 1
            await asyncio.sleep(8)
            continue
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 5)
            retries += 1
        except Exception as e:
            logger.error(f"Fetch error: {e}")
            retries += 1
            await asyncio.sleep(5)

    src_topics = []
    seen = set()
    for t in all_topics:
        if t.id in seen or t.id == 1:
            continue
        if getattr(t, 'deleted', False):
            continue
        if not getattr(t, 'title', '').strip():
            continue
        seen.add(t.id)
        src_topics.append(t)

    await msg.edit(f"✅ Found **{len(src_topics)}** valid topics (raw: {len(all_topics)})")

    if not src_topics:
        await msg.edit("❌ No valid topics found.")
        return

    try:
        tgt_res = await asyncio.wait_for(
            client(GetForumTopicsRequest(channel=tgt_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100)),
            timeout=20
        )
        active_topics = [tt for tt in tgt_res.topics if not getattr(tt, 'deleted', False) and tt.id!= 1]
    except Exception as e:
        await msg.edit(f"❌ Failed to fetch target topics: {e}")
        return

    archive_topic_id = await get_archive_topic_id(source_id, target_id)
    archive_topic = None

    if archive_topic_id:
        archive_topic = next((tt for tt in active_topics if tt.id == archive_topic_id), None)

    if not archive_topic:
        archive_topic = next((tt for tt in active_topics if getattr(tt, 'title', '') == "Archive"), None)

    if not archive_topic:
        try:
            await msg.edit("Creating Archive topic...")
            result = await client(CreateForumTopicRequest(channel=tgt_entity, title="Archive"))

            archive_topic_id = None
            if hasattr(result, 'updates') and result.updates:
                for update in result.updates:
                    if hasattr(update, 'message') and hasattr(update.message, 'id'):
                        archive_topic_id = update.message.id
                        break
                    elif hasattr(update, 'topic') and hasattr(update.topic, 'id'):
                        archive_topic_id = update.topic.id
                        break

            if not archive_topic_id:
                raise Exception("Could not extract Archive topic ID from response")

            logger.info(f"Archive created with ID: {archive_topic_id}")
            await asyncio.sleep(TOPIC_CREATE_DELAY)

        except ChatAdminRequiredError:
            await msg.edit("❌ Bot needs 'Manage Topics' admin right in target group")
            return
        except FloodWaitError as e:
            await msg.edit(f"❌ Rate limited for {e.seconds}s creating Archive. Wait and run again.")
            return
        except Exception as e:
            await msg.edit(f"❌ Failed to create Archive: {e}\n\nCheck: 1) Group is a Forum 2) Bot is admin with Manage Topics")
            return
    else:
        archive_topic_id = archive_topic.id
        await msg.edit(f"Found existing Archive topic: {archive_topic_id}")

    await save_archive_topic_id(source_id, target_id, archive_topic_id)

    target_name_map = {tt.title: tt.id for tt in active_topics if tt.id!= archive_topic_id}

    new_mapping = {}
    created = 0
    skipped = 0
    available_slots = 100 - len(active_topics) - (0 if archive_topic else 1)

    for idx, t in enumerate(src_topics):
        if created >= available_slots:
            new_mapping[str(t.id)] = archive_topic_id
            skipped += 1
            continue

        title = (t.title or f"Topic {t.id}")[:128]

        if title in target_name_map:
            new_mapping[str(t.id)] = target_name_map[title]
            skipped += 1
            continue

        for attempt in range(3):
            try:
                await msg.edit(f"Creating {idx+1}/{len(src_topics)}: {title}\nCreated: {created} | Archive: {skipped}")
                result = await client(CreateForumTopicRequest(
                    channel=tgt_entity,
                    title=title,
                    icon_emoji_id=getattr(t, 'icon_emoji_id', None)
                ))

                new_id = None
                if hasattr(result, 'updates'):
                    for update in result.updates:
                        if hasattr(update, 'message'):
                            new_id = update.message.id
                            break
                        elif hasattr(update, 'topic'):
                            new_id = update.topic.id
                            break

                if new_id:
                    new_mapping[str(t.id)] = new_id
                    created += 1
                    await asyncio.sleep(TOPIC_CREATE_DELAY)
                    break
                else:
                    raise Exception("No topic ID in response")

            except FloodWaitError as e:
                if attempt == 2:
                    await msg.edit(f"FloodWait on {title}. Skipping to Archive.")
                    new_mapping[str(t.id)] = archive_topic_id
                    skipped += 1
                else:
                    await asyncio.sleep(e.seconds + 10)
            except Exception as e:
                if attempt == 2:
                    logger.error(f"Failed to create {title}: {e}")
                    new_mapping[str(t.id)] = archive_topic_id
                    skipped += 1
                else:
                    await asyncio.sleep(5)

    await save_topic_map(source_id, target_id, new_mapping)
    await msg.edit(f"**Fresh Resync Complete**\nValid topics: `{len(src_topics)}`\nCreated: `{created}`\nSkipped to Archive: `{skipped}`\nArchive ID: `{archive_topic_id}`\n\nRun `/scrapegrouplike {source_id} fresh` to start scraping.")

@client.on(events.NewMessage(pattern=r'/syncmissing (-?[0-9]+) (-?[0-9]+)'))
async def sync_missing(event):
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("🔍 Checking existing mappings...")

    try:
        src_entity = await asyncio.wait_for(client.get_entity(source_id), timeout=15)
        tgt_entity = await asyncio.wait_for(client.get_entity(target_id), timeout=15)
    except Exception as e:
        await msg.edit(f"❌ Error: {e}")
        return

    if not getattr(src_entity, 'forum', False) or not getattr(tgt_entity, 'forum', False):
        await msg.edit("❌ Both groups need topics enabled")
        return

    existing_mapping = await get_topic_map(source_id, target_id)
    archive_topic_id = await get_archive_topic_id(source_id, target_id)

    if not existing_mapping:
        await msg.edit("❌ No existing mapping found. Run `/resyncgroupfresh` first to create the initial map.")
        return

    await msg.edit("📡 Fetching source topics...")
    all_topics = []
    offset_date = 0
    offset_id = 0
    offset_topic = 0
    retries = 0

    while retries < 8:
        try:
            res = await asyncio.wait_for(
                client(GetForumTopicsRequest(channel=src_entity, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100)),
                timeout=30
            )
            if not res.topics:
                break
            all_topics.extend(res.topics)
            if len(res.topics) < 100:
                break
            last = res.topics[-1]
            offset_date = getattr(last, 'date', 0)
            offset_id = getattr(last, 'top_message', 0)
            offset_topic = last.id
            await asyncio.sleep(2)
        except:
            retries += 1
            await asyncio.sleep(5)

    src_topics = []
    seen = set()
    for t in all_topics:
        if t.id in seen or t.id == 1 or getattr(t, 'deleted', False) or not getattr(t, 'title', '').strip():
            continue
        seen.add(t.id)
        src_topics.append(t)

    await msg.edit(f"📡 Fetching current target topics...")
    try:
        tgt_res = await asyncio.wait_for(client(GetForumTopicsRequest(
            channel=tgt_entity,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100
        )), timeout=20)
        tgt_topics = [tt for tt in tgt_res.topics if not getattr(tt, 'deleted', False) and tt.id!= 1]
    except Exception as e:
        await msg.edit(f"❌ Failed to fetch target topics: {e}")
        return

    if archive_topic_id and not any(tt.id == archive_topic_id for tt in tgt_topics):
        await msg.edit(f"❌ Archive topic ID {archive_topic_id} not found in target. Run `/resyncgroupfresh` to rebuild.")
        return

    valid_target_ids = {tt.id for tt in tgt_topics}

    new_mapping = {}
    preserved = 0
    remapped_from_archive = 0
    still_archive = 0
    invalid_remapped = 0

    for src_t in src_topics:
        src_id_str = str(src_t.id)

        if src_id_str in existing_mapping:
            old_target_id = existing_mapping[src_id_str]

            if old_target_id == archive_topic_id:
                new_mapping[src_id_str] = archive_topic_id
                still_archive += 1
                continue

            if old_target_id in valid_target_ids:
                new_mapping[src_id_str] = old_target_id
                preserved += 1
                continue

            invalid_remapped += 1

        matching_topic = next((tt for tt in tgt_topics if tt.title == src_t.title[:128] and tt.id!= archive_topic_id), None)

        if matching_topic:
            new_mapping[src_id_str] = matching_topic.id
            remapped_from_archive += 1
        else:
            new_mapping[src_id_str] = archive_topic_id
            still_archive += 1

    await save_topic_map(source_id, target_id, new_mapping)

    await msg.edit(f"**Sync Complete**\n"
                   f"Source topics: `{len(src_topics)}`\n"
                   f"Preserved existing mappings: `{preserved}`\n"
                   f"Moved from Archive to real topic: `{remapped_from_archive}`\n"
                   f"In Archive: `{still_archive}`\n"
                   f"Fixed invalid mappings: `{invalid_remapped}`\n\n"
                   f"Run `/scrapegrouplike {source_id} fresh` to start scraping.")












@client.on(events.NewMessage(pattern=r'/clearmapping (-?[0-9]+) (-?[0-9]+)'))
async def clear_mapping(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply(f"Clearing mapping for `{source_id}` -> `{target_id}`...")
    try:
        supabase.table("group_topic_map").delete().eq("source_id", source_id).eq("target_id", target_id).execute()
        await msg.edit("**Mapping cleared**\nUse `/resyncgroupfresh` to rebuild.")
    except Exception as e:
        await msg.edit(f"Failed: {e}")

@client.on(events.NewMessage(pattern=r'/debugtopics (-?[0-9]+)(?:\s+(-?[0-9]+))?'))
async def debug_topics(event):
    if not is_admin(event.sender_id):
        return
    args = event.pattern_match.groups()
    gid1 = int(args[0])
    gid2 = int(args[1]) if args[1] else None
    msg = await event.reply("Fetching topics...")
    try:
        entity1 = await asyncio.wait_for(client.get_entity(gid1), timeout=15)
        res = await asyncio.wait_for(client(GetForumTopicsRequest(channel=entity1, offset_date=0, offset_id=0, offset_topic=0, limit=200)), timeout=20)
        text = f"**Group {gid1}**\nTotal: {len(res.topics)}\n"
        for t in res.topics[:50]:
            text += f"ID:`{t.id}` Title:`{t.title}`\n"
        if gid2:
            entity2 = await asyncio.wait_for(client.get_entity(gid2), timeout=15)
            res2 = await asyncio.wait_for(client(GetForumTopicsRequest(channel=entity2, offset_date=0, offset_id=0, offset_topic=0, limit=200)), timeout=20)
            text += f"\n**Group {gid2}**\nTotal: {len(res2.topics)}\n"
            for t in res2.topics[:50]:
                text += f"ID:`{t.id}` Title:`{t.title}`\n"
        await msg.edit(text[:4000])
    except Exception as e:
        await msg.edit(f"Error: {e}")

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
        await msg.edit(f"**Diagnostics Complete**\nForum: `{getattr(entity, 'forum', False)}`\nTopics found: `{len(res.topics)}`")
    except Exception as e:
        await msg.edit(f"**Diagnostics Failed**\n`{type(e).__name__}: {e}`")

@client.on(events.NewMessage(pattern=r'/addsource (-?[0-9]+) (-?[0-9]+)'))
async def add_source(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply(f"Adding mapping `{source_id}` → `{target_id}`...")
    if await save_mapping(source_id, target_id):
        await msg.edit(f"**Mapping added**\nSource: `{source_id}`\nTarget: `{target_id}`")
    else:
        await msg.edit("**Failed to add mapping**")

@client.on(events.NewMessage(pattern=r'/removesource (-?[0-9]+)'))
async def remove_source(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    msg = await event.reply(f"Removing mapping for `{source_id}`...")
    if await remove_mapping(source_id):
        await msg.edit(f"**Mapping removed** for `{source_id}`")
    else:
        await msg.edit("**Failed to remove mapping**")

@client.on(events.NewMessage(pattern=r'/listsources'))
async def list_sources(event):
    if not is_admin(event.sender_id):
        return
    if not CONFIG["sources"]:
        await event.reply("**No sources mapped**\nUse `/addsource <source_id> <target_id>`")
        return
    text = "**Active Mappings:**\n"
    for src, tgt in CONFIG["sources"].items():
        text += f"`{src}` → `{tgt}`\n"
    await event.reply(text)

@client.on(events.NewMessage(pattern=r'/stats'))
async def stats_cmd(event):
    if not is_admin(event.sender_id):
        return
    await event.reply(
        f"**Bot Stats**\n"
        f"Scraped: `{scraped_count}`\n"
        f"Skipped: `{skipped_count}`\n"
        f"Upload Delay: `{UPLOAD_DELAY}s`\n"
        f"Kill Switch: `{'ON' if KILL_SWITCH else 'OFF'}`\n"
        f"Sources: `{len(CONFIG['sources'])}`"
    )

# ==================== MAIN ====================
async def main():
    await load_sources()
    logger.info("Bot starting...")
    await client.start()
    me = await client.get_me()
    await send_log(f"Bot started as @{me.username} | ID: {me.id}")
    logger.info(f"Logged in as {me.first_name}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise