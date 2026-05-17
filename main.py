import os
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeVideo
from telethon.tl.functions.channels import CreateForumTopicRequest
from telethon.tl.functions.channels import GetForumTopicsRequest
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
















@client.on(events.NewMessage(pattern='/getid'))
async def getid(event):
    chat = None
    message = event.message

    # 1. Replied to a forwarded message
    if message.reply_to:
        try:
            replied = await event.get_reply_message()
            if replied and replied.forward:
                fwd = replied.forward
                chat = getattr(fwd, 'chat', None) or getattr(getattr(fwd, 'origin', None), 'chat', None)
        except:
            pass

    # 2. Message itself is forwarded
    if not chat and message.forward:
        fwd = message.forward
        chat = getattr(fwd, 'chat', None) or getattr(getattr(fwd, 'origin', None), 'chat', None)

    # 3. Used directly in group/channel
    if not chat and (event.is_group or event.is_channel):
        chat = event.chat

    if not chat:
        await event.reply("Forward a message from the group/channel to me, then reply to it with /getid")
        return

    title = getattr(chat, 'title', None) or getattr(chat, 'first_name', None) or getattr(chat, 'username', 'Unknown')
    await event.reply(f"**ID:** `{chat.id}`\n**Name:** `{title}`")


@client.on(events.NewMessage(pattern=r'/resyncgroup (-?\d+) (-?\d+)'))
async def resync_group_topics(event):
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply("Checking for new topics...")

    topic_map = await get_topic_map(source_id, target_id)
    if not topic_map:
        await msg.edit("No existing mapping found. Run `/resyncgroup` after first setup")
        return

    existing_src_ids = set(int(k) for k in topic_map.keys())

    src_topics = await client(GetForumTopicsRequest(channel=source_id, limit=100))
    new_topics = [t for t in src_topics.topics if t.id not in existing_src_ids and not getattr(t, 'deleted', False)]

    if not new_topics:
        await msg.edit("No new topics found. Everything is synced.")
        return

    await msg.edit(f"Found {len(new_topics)} new topic(s). Creating...")

    created = 0
    for t in new_topics:
        try:
            result = await client(CreateForumTopicRequest(
                channel=target_id,
                title=t.title,
                icon_emoji_id=getattr(t, 'icon_emoji_id', None)
            ))
            new_id = None
            for update in getattr(result, 'updates', []):
                if hasattr(update, 'id') and isinstance(getattr(update, 'id', None), int):
                    new_id = update.id
                    break

            if new_id:
                topic_map[str(t.id)] = new_id
                created += 1
            await asyncio.sleep(3)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            logger.error(f"Topic create failed: {e}")

    await save_topic_map(source_id, target_id, topic_map)
    await msg.edit(f"**Resync Complete**\nAdded {created} new topic(s).\nTotal mapped: {len(topic_map)}")


@client.on(events.NewMessage(pattern=r'/scrapegrouplike (-?\d+)'))
async def scrape_group_like(event):
    if not is_admin(event.sender_id):
        return
    args = event.text.split()
    fresh = len(args) > 2 and args[2] == 'fresh'
    source_id = int(event.pattern_match.group(1))

    target_id = CONFIG["sources"].get(str(source_id))
    if not target_id:
        await event.reply(f"No mapping for `{source_id}`. Use `/addsource` first")
        return

    msg = await event.reply("Starting group scrape...")
    await scrape_group_to_forum(source_id, int(target_id), msg, fresh)


@client.on(events.NewMessage(pattern='/(start|help)'))
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
        f"`/scrapegrouplike -100src`\n"
        f"`/resyncgroup -100src -100dst`\n"
        f"`/cleanhere -100clean -100trash`\n\n"
        f"**Note:** Restart bot after adding any mapping"
    )

    msg2 = (
        f"**Auto-Forward Your Own Channels:**\n"
        f"`/addgif -100src -100dst`\n"
        f"`/removegif -100src`\n"
        f"`/scrapegif -100src`\n"
        f"`/addshort -100src -100dst`\n"
        f"`/removeshort -100src`\n"
        f"`/scrapeshort -100src`\n"
        f"`/listmappings`\n`/stats`\n`/getid`\n`/checkvars`"
    )

    await event.reply(msg1)
    await asyncio.sleep(0.5)
    await event.reply(msg2)


@client.on(events.NewMessage(pattern='/addsource'))
async def add_source(event):
    if not is_admin(event.sender_id):
        return
    args = event.text.split()
    if len(args) != 3:
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
    if len(args) != 2:
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

@client.on(events.NewMessage(pattern='/scrapegrouplike (-?\d+)'))
async def scrape_group_with_topics(event):
    global scraped_count, skipped_count
    if not is_admin(event.sender_id):
        return

    args = event.text.split()
    if len(args) < 2:
        await event.reply("Usage: `/scrapegrouplike -100source_id`")
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
    topic_map = await get_topic_map(source_id, target_id)

    if not topic_map:
        await event.reply("No topic map found. Run `/resyncgroup source_id target_id` first")
        return

    status_msg = await event.reply(f"Scraping `{source_id}` with topics → `{target_id}`\nDelay: {UPLOAD_DELAY}s")
    count = checked = errors = 0
    current_delay = UPLOAD_DELAY

    try:
        async for message in client.iter_messages(source_id, limit=None, reverse=True):
            checked += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(f"Checked {checked}... Uploaded {count}... Errors {errors}")
                except:
                    pass

            if is_video_message(message):
                if message.file.size > MAX_FILE_SIZE:
                    skipped_count += 1
                    continue

                video_attr = get_video_attr(message)
                reply_to = None

                if getattr(message, 'reply_to_topic_id', None):
                    reply_to = topic_map.get(str(message.reply_to_topic_id))

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
                    await asyncio.sleep(current_delay)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                    current_delay = min(current_delay * 1.5, 60)
                except Exception as e:
                    errors += 1

        final = f"**Topic scrape done**\nChecked: `{checked}`\nUploaded: `{count}`\nSkipped >{MAX_FILE_SIZE//1024//1024}MB: `{skipped_count}`\nErrors: `{errors}`"
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