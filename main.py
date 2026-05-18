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
        res = supabase.table("group_topic_map").select("mapping").eq("source_id", source_id).eq("target_id", target_id).single().execute()
        return res.data["mapping"] if res.data else {}
    except:
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
        res = supabase.table("group_topic_map").select("archive_topic_id").eq("source_id", source_id).eq("target_id", target_id).single().execute()
        return res.data["archive_topic_id"] if res.data and res.data.get("archive_topic_id") else None
    except:
        return None










@client.on(events.NewMessage(pattern=r'/resyncgroup (-?[0-9]+) (-?[0-9]+)'))
async def resync_group_topics(event):
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("Fetching topics...")

    topic_map = await get_topic_map(source_id, target_id)

    # Fetch ALL topics with pagination + timeout
    all_topics = []
    offset_topic = 0
    offset_id = 0

    while True:
        try:
            res = await asyncio.wait_for(
                client(GetForumTopicsRequest(
                    channel=source_id,
                    offset_date=0,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=100
                )),
                timeout=30
            )
        except asyncio.TimeoutError:
            await msg.edit("Timed out fetching topics. Bot probably isn't admin in source group.")
            return
        except ChatAdminRequiredError:
            await msg.edit("Error: Bot needs to be admin in source group to read topics.")
            return
        except Exception as e:
            await msg.edit(f"Failed to get source topics: {str(e)}")
            logger.error(f"GetForumTopicsRequest failed: {e}")
            return

        if not res.topics:
            break

        all_topics.extend(res.topics)

        if len(res.topics) < 100:
            break

        last_topic = res.topics[-1]
        offset_topic = last_topic.id
        offset_id = last_topic.top_message
        await asyncio.sleep(1)

    src_topics = all_topics

    if not src_topics:
        await msg.edit("Source has no topics or bot can't access them.")
        return

    await msg.edit(f"Found {len(src_topics)} topic(s). Syncing...")

    created = 0
    updated = 0
    skipped = 0

    # Create or find Archive topic
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

    # Map topics
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

    await save_topic_map(source_id, target_id, topic_map)
    msg_text = f"**Resync Complete**\nCreated: {created}\nMapped existing: {updated}\nSkipped: {skipped}\nTotal mapped: {len(topic_map)}"
    if archive_topic_id:
        msg_text += f"\nArchive topic ID: {archive_topic_id}"
    await msg.edit(msg_text)

@client.on(events.NewMessage(pattern=r'/scrapegrouplike (-?[0-9]+)(?:\s+fresh)?'))
async def scrape_group_like(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    force_fresh = event.pattern_match.group(2) is not None

    target_id = CONFIG["sources"].get(str(source_id))
    if not target_id:
        await event.reply(f"No mapping for `{source_id}`. Use `/addsource` first")
        return

    msg = await event.reply("Starting group scrape...")
    await scrape_group_with_topics(source_id, int(target_id), msg, force_fresh)

async def scrape_group_with_topics(source_id, target_id, status_msg, force_fresh=False):
    global scraped_count, skipped_count
    topic_map = await get_topic_map(source_id, target_id)
    archive_topic_id = await get_archive_topic_id(source_id, target_id)

    if not topic_map:
        await status_msg.edit("No topic map found. Run `/resyncgroup source_id target_id` first")
        return

    offset_id = 0 if force_fresh else await get_checkpoint(f"group_{source_id}")
    if force_fresh:
        await save_checkpoint(f"group_{source_id}", 0)

    count = checked = errors = 0
    current_delay = UPLOAD_DELAY

    try:
        async for message in client.iter_messages(source_id, limit=None, offset_id=offset_id, reverse=True):
            checked += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(f"Checked {checked}... Uploaded {count}... Errors {errors}")
                except:
                    pass
                await save_checkpoint(f"group_{source_id}", message.id)

            if message.file and message.file.size > MAX_FILE_SIZE:
                skipped_count += 1
                continue

            if is_video_message(message):
                video_attr = get_video_attr(message)

                reply_to = None
                src_topic_id = getattr(message, 'reply_to_topic_id', None)

                if src_topic_id:
                    reply_to = topic_map.get(str(src_topic_id))
                    if reply_to is None and archive_topic_id:
                        reply_to = archive_topic_id
                else:
                    reply_to = 1

                if src_topic_id == 1:
                    continue

                try:
                    await client.send_file(
                        target_id,
                        message.media,
                        caption="",
                        attributes=[video_attr] if video_attr else None,
                        force_document=False,
                        reply_to=reply_to
                    )
                    count += 1
                    scraped_count += 1
                    await save_checkpoint(f"group_{source_id}", message.id)
                    await asyncio.sleep(current_delay)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                    current_delay = min(current_delay * 1.5, 60)
                except Exception as e:
                    errors += 1
                    logger.error(f"Send failed: {e}")

        await save_checkpoint(f"group_{source_id}", 0)
        final = f"**Topic scrape done**\nChecked: `{checked}`\nUploaded: `{count}`\nSkipped >{MAX_FILE_SIZE//1024//1024}MB: `{skipped_count}`\nErrors: `{errors}`"
        if archive_topic_id:
            final += f"\nOverflow sent to Archive topic"
        await status_msg.edit(final)

    except Exception as e:
        await status_msg.edit(f"Scrape failed: {e}")

@client.on(events.NewMessage(pattern=r'/testmapping (-?[0-9]+) (-?[0-9]+)'))
async def test_mapping(event):
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))

    msg = await event.reply("Loading topic map...")

    topic_map = await get_topic_map(source_id, target_id)
    if len(topic_map) < 3:
        await msg.edit("Need at least 3 mapped topics. Run `/resyncgroup` first")
        return

    try:
        source_entity = await client.get_entity(source_id)
        target_entity = await client.get_entity(target_id)
    except Exception as e:
        await msg.edit(f"Failed to get entities: {e}")
        return

    test_topics = random.sample(list(topic_map.items()), min(3, len(topic_map)))
    await msg.edit(f"Testing {len(test_topics)} random topics: {[k for k,v in test_topics]}")

    for src_id_str, tgt_id in test_topics:
        src_id = int(src_id_str)
        msgs = await client.get_messages(source_entity, limit=1, reply_to=src_id)

        if not msgs or not msgs[0].media:
            await event.reply(f"Topic {src_id} → {tgt_id}: ⚠️ No media found")
            await asyncio.sleep(1)
            continue

        await client.send_file(
            target_entity,
            msgs[0].media,
            caption=f"TEST: Source {src_id} → Target {tgt_id}",
            reply_to=tgt_id
        )

        await event.reply(f"Topic {src_id} → Topic {tgt_id}: ✅ Sent")
        await asyncio.sleep(2)

    await event.reply("Check those topics in target group. If messages landed right, mapping works.")









@client.on(events.NewMessage(pattern=re.compile(r'^/(start|help)(@\w+)?$', re.IGNORECASE)))
async def start(event):
    if not is_admin(event.sender_id):
        return

    msg1 = (
        f"**Video-Only Bot - SAFE MODE**\n\n"
        f"**Delays:** Upload {UPLOAD_DELAY}s | Delete {DELETE_DELAY}s\n"
        f"**Scraping Channels You Don't Own:**\n"
        f"`/addsource -100src -100dst`\n"
        f"`/removesource -100src`\n"
        f"`/scrape -100src` or `/scrape -100src fresh`\n"
        f"`/scrapegrouplike -100src` or `/scrapegrouplike -100src fresh`\n"
        f"`/resyncgroup -100src -100dst`\n"
        f"`/testmapping -100src -100dst`\n"
        f"`/cleanhere -100clean -100trash`\n"
    )

    msg2 = (
        f"**Auto-Forward Your Own Channels:**\n"
        f"`/addgif -100src -100dst`\n"
        f"`/removegif -100src`\n"
        f"`/scrapegif -100src`\n"
        f"`/addshort -100src -100dst`\n"
        f"`/removeshort -100src`\n"
        f"`/scrapeshort -100src`\n"
        f"`/listmappings`\n`/stats`\n`/checkvars`"
    )

    await event.reply(msg1)
    await asyncio.sleep(0.5)
    await event.reply(msg2)

@client.on(events.NewMessage(pattern='/addsource'))
async def add_source(event):
    if not is_admin(event.sender_id):
        return
    args = event.text.split()
    if len(args)!= 3:
        await event.reply("Usage: `/addsource -100source_id -100target_id`")
        return
    try:
        source_id = int(args[1])
        target_id = int(args[2])
    except ValueError:
        await event.reply("IDs must be numbers")
        return
    ok, err = await check_access(target_id)
    if not ok:
        await event.reply(f"Cannot access target `{target_id}`: {err}")
        return
    if await save_mapping(source_id, target_id):
        await event.reply(f"Added scrape mapping: `{source_id}` → `{target_id}`")
    else:
        await event.reply("Failed to save")

@client.on(events.NewMessage(pattern='/removesource'))
async def remove_source(event):
    if not is_admin(event.sender_id):
        return
    args = event.text.split()
    if len(args)!= 2:
        await event.reply("Usage: `/removesource -100source_id`")
        return
    try:
        source_id = int(args[1])
    except ValueError:
        await event.reply("Invalid source ID")
        return
    if str(source_id) not in CONFIG["sources"]:
        await event.reply("Source not mapped")
        return
    if await remove_mapping(source_id):
        await event.reply(f"Removed `{source_id}`")
    else:
        await event.reply("Failed to remove")

@client.on(events.NewMessage(pattern='/listmappings'))
async def list_mappings(event):
    if not is_admin(event.sender_id):
        return
    msg = "**Scrape mappings:**\n"
    if CONFIG["sources"]:
        for src, dst in CONFIG["sources"].items():
            msg += f"`{src}` → `{dst}`\n"
    else:
        msg += "None\n"

    msg += "\n**Auto-forward mappings:**\n"
    if CONFIG.get("auto_gif"):
        for src, dst in CONFIG["auto_gif"].items():
            msg += f"`{src}` → `{dst}` [GIF]\n"
    if CONFIG.get("auto_short"):
        for src, dst in CONFIG["auto_short"].items():
            msg += f"`{src}` → `{dst}` [60s-120s]\n"
    if not CONFIG.get("auto_gif") and not CONFIG.get("auto_short"):
        msg += "None\n"

    await event.reply(msg)

@client.on(events.NewMessage(pattern='/scrape'))
async def scrape_history(event):
    global scraped_count, skipped_count
    if not is_admin(event.sender_id):
        return
    args = event.text.split()
    if len(args) < 2:
        await event.reply("Usage: `/scrape -100source_id` or `/scrape -100source_id fresh`")
        return
    try:
        source_id = int(args[1])
    except ValueError:
        await event.reply("Invalid source ID")
        return
    if str(source_id) not in CONFIG["sources"]:
        await event.reply("Source not mapped. Use `/addsource` first")
        return
    target_id = int(CONFIG["sources"][str(source_id)])
    max_mb = MAX_FILE_SIZE // 1024 // 1024
    ok, err = await check_access(target_id)
    if not ok:
        await event.reply(f"Cannot access target `{target_id}`: {err}")
        return
    force_fresh = len(args) >= 3 and args[2].lower() == 'fresh'
    offset_id = 0 if force_fresh else await get_checkpoint(source_id)
    status_msg = await event.reply(f"Scraping `{source_id}` → `{target_id}`\nResume ID: `{offset_id}`\nDelay: {UPLOAD_DELAY}s")
    count = checked = errors = 0
    current_delay = UPLOAD_DELAY
    try:
        async for message in client.iter_messages(source_id, limit=None, offset_id=offset_id, reverse=True):
            checked += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(f"Checked {checked}... Uploaded {count}... Errors {errors}")
                except:
                    pass
                await save_checkpoint(source_id, message.id)
            if is_video_message(message):
                if message.file.size > MAX_FILE_SIZE:
                    skipped_count += 1
                    continue
                video_attr = get_video_attr(message)
                try:
                    await client.send_file(target_id, message.media, caption="", attributes=[video_attr] if video_attr else None, force_document=False)
                    count += 1
                    scraped_count += 1
                    await save_checkpoint(source_id, message.id)
                    await asyncio.sleep(current_delay)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                    current_delay = min(current_delay * 1.5, 60)
                except Exception as e:
                    errors += 1
        await save_checkpoint(source_id, 0)
        final = f"**Done**\nChecked: `{checked}`\nUploaded: `{count}`\nSkipped >{max_mb}MB: `{skipped_count}`\nErrors: `{errors}`"
        await status_msg.edit(final)
    except Exception as e:
        await event.reply(f"Scrape failed: {e}")

@client.on(events.NewMessage(pattern='/scrapegif'))
async def scrape_gif_history(event):
    global scraped_count, skipped_count
    if not is_admin(event.sender_id):
        return
    args = event.text.split()
    if len(args) < 2:
        await event.reply("Usage: `/scrapegif -100source_id`")
        return
    try:
        source_id = int(args[1])
    except ValueError:
        await event.reply("Invalid source ID")
        return
    if str(source_id) not in CONFIG["auto_gif"]:
        await event.reply("Source not mapped for GIFs. Use `/addgif` first")
        return
    target_id = int(CONFIG["auto_gif"][str(source_id)])

    status_msg = await event.reply(f"Scraping GIFs from `{source_id}` → `{target_id}`\nDelay: {UPLOAD_DELAY}s")
    count = checked = errors = 0

    try:
        async for message in client.iter_messages(source_id, limit=None, reverse=True):
            checked += 1
            if checked % 500 == 0:
                await status_msg.edit(f"Checked {checked}... Forwarded {count}... Errors {errors}")

            if is_gif(message):
                try:
                    await client.forward_messages(target_id, message, source_id)
                    count += 1
                    scraped_count += 1
                    await asyncio.sleep(UPLOAD_DELAY)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                except Exception:
                    errors += 1

        await status_msg.edit(f"**GIF scrape done**\nChecked: `{checked}`\nForwarded: `{count}`\nErrors: `{errors}`")
    except Exception as e:
        await event.reply(f"Scrape failed: {e}")

@client.on(events.NewMessage(pattern='/scrapeshort'))
async def scrape_short_history(event):
    global scraped_count, skipped_count
    if not is_admin(event.sender_id):
        return
    args = event.text.split()
    if len(args) < 2:
        await event.reply("Usage: `/scrapeshort -100source_id`")
        return
    try:
        source_id = int(args[1])
    except ValueError:
        await event.reply("Invalid source ID")
        return
    if str(source_id) not in CONFIG["auto_short"]:
        await event.reply("Source not mapped for shorts. Use `/addshort` first")
        return
    target_id = int(CONFIG["auto_short"][str(source_id)])

    status_msg = await event.reply(f"Scraping 60-120s videos from `{source_id}` → `{target_id}`\nDelay: {UPLOAD_DELAY}s")
    count = checked = errors = 0

    try:
        async for message in client.iter_messages(source_id, limit=None, reverse=True):
            checked += 1
            if checked % 500 == 0:
                await status_msg.edit(f"Checked {checked}... Forwarded {count}... Errors {errors}")

            if is_video_message(message):
                video_attr = get_video_attr(message)
                duration = getattr(video_attr, 'duration', 0)
                if 60 < duration <= 120:
                    try:
                        await client.forward_messages(target_id, message, source_id)
                        count += 1
                        scraped_count += 1
                        await asyncio.sleep(UPLOAD_DELAY)
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds)
                    except Exception:
                        errors += 1

        await status_msg.edit(f"**Short scrape done**\nChecked: `{checked}`\nForwarded: `{count}`\nErrors: `{errors}`")
    except Exception as e:
        await event.reply(f"Scrape failed: {e}")

@client.on(events.NewMessage(pattern='/cleanhere'))
async def clean_here(event):
    if not is_admin(event.sender_id):
        return
    args = event.text.split()
    if len(args) < 3:
        await event.reply("**Usage:** `/cleanhere -100clean_id -100trash_id`")
        return
    try:
        clean_channel_id = int(args[1])
        trash_id = int(args[2])
    except ValueError:
        await event.reply("Invalid channel IDs")
        return
    status_msg = await event.reply(f"Cleaning `{clean_channel_id}` → `{trash_id}`\nDelay: {UPLOAD_DELAY}s")
    checked = found_videos = moved = kept = errors = 0
    current_delay = UPLOAD_DELAY
    try:
        async for message in client.iter_messages(clean_channel_id, limit=None):
            checked += 1
            video_meta = get_video_attr(message)
            if video_meta:
                found_videos += 1
                duration = getattr(video_meta, 'duration', 0)
                width = getattr(video_meta, 'w', 0)
                height = getattr(video_meta, 'h', 0)
                file_size = message.file.size if message.file else 0
                should_move = False
                if duration > 0 and duration < MAX_DURATION:
                    should_move = True
                elif width > 0 and height > 0 and (width < MIN_WIDTH or height < MIN_HEIGHT):
                    should_move = True
                elif duration == 0 and (width == 0 or height == 0):
                    if file_size == 0 or file_size < MIN_FILE_SIZE_NO_META:
                        should_move = True
                if should_move:
                    try:
                        await client.send_file(trash_id, message.media, caption="", attributes=[video_meta], force_document=False)
                        await message.delete()
                        moved += 1
                        await asyncio.sleep(current_delay)
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds)
                        current_delay = min(current_delay * 1.5, 60)
                    except Exception as e:
                        errors += 1
                else:
                    kept += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(f"Checked {checked}... Videos: {found_videos}... Moved: {moved}... Errors: {errors}")
                except:
                    pass
        final = f"**Clean done**\nChecked: `{checked}`\nVideos: `{found_videos}`\nMoved: `{moved}`\nKept: `{kept}`\nErrors: `{errors}`"
        await status_msg.edit(final)
    except Exception as e:
        await event.reply(f"Clean failed: {e}")

@client.on(events.NewMessage(pattern='/stats'))
async def stats(event):
    if not is_admin(event.sender_id):
        return
    max_mb = MAX_FILE_SIZE // 1024 // 1024
    await event.reply(f"**Stats**\nScraped: `{scraped_count}`\nSkipped >{max_mb}MB: `{skipped_count}`\nScrape mappings: `{len(CONFIG['sources'])}`\nGIF mappings: `{len(CONFIG['auto_gif'])}`\nShort mappings: `{len(CONFIG['auto_short'])}`")

@client.on(events.NewMessage(chats=mapped_chats))
async def auto_forward(event):
    src = str(event.chat_id)

    if src in CONFIG["auto_gif"] and is_gif(event):
        target_id = int(CONFIG["auto_gif"][src])
        await client.forward_messages(target_id, event.message, event.chat_id)
        try:
            await event.delete()
            await asyncio.sleep(DELETE_DELAY)
        except Exception as e:
            await send_log(f"Delete failed for GIF {event.id}: {e}")
        await asyncio.sleep(10)
        return

    if src in CONFIG["auto_short"] and is_video_message(event):
        video_attr = get_video_attr(event.message)
        duration = getattr(video_attr, 'duration', 0)
        if 60 < duration <= 120:
            target_id = int(CONFIG["auto_short"][src])
            await client.forward_messages(target_id, event.message, event.chat_id)
            try:
                await event.delete()
                await asyncio.sleep(DELETE_DELAY)
            except Exception as e:
                await send_log(f"Delete failed for video {event.id}: {e}")
            await asyncio.sleep(10)
        return

    if src in CONFIG["sources"] and is_video_message(event):
        target_id = int(CONFIG["sources"][src])
        try:
            if event.file.size > MAX_FILE_SIZE:
                return
            video_attr = get_video_attr(event.message)
            await client.send_file(target_id, event.media, caption="", attributes=[video_attr] if video_attr else None, force_document=False)
            await asyncio.sleep(UPLOAD_DELAY)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except Exception as e:
            await send_log(f"Auto forward failed {event.chat_id}: {e}")

async def main():
    await load_sources()
    await client.start()
    me = await client.get_me()
    await send_log(f"Bot started as {me.first_name}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())