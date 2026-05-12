import os
import asyncio
import logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, ChatAdminRequiredError
from telethon.tl.types import DocumentAttributeVideo
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
BOT_LOG_CHAT_ID = int(os.getenv("BOT_LOG_CHAT_ID", "0")) # Set to 0 to disable

MAX_FILE_SIZE = 200 * 1024 * 1024 # 200MB - FIXED
MAX_DURATION = 60 # seconds - videos SHORTER than this get cleaned
MIN_WIDTH = 1280 # px - 720p width
MIN_HEIGHT = 720 # px - 720p height
MIN_FILE_SIZE_NO_META = 15 * 1024 * 1024 # 15MB
UPLOAD_DELAY = 30 # SAFE MODE: 2 videos/min
DELETE_DELAY = 15 # for /cleanhere deletes

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG = {"sources": {}}
scraped_count = 0
skipped_count = 0

async def send_log(text):
    """Send to BOT_LOG_CHAT_ID if set, else just logger"""
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

async def load_sources():
    global CONFIG
    try:
        res = supabase.table("mappings").select("*").execute()
        CONFIG["sources"] = {str(row["source_id"]): str(row["target_id"]) for row in res.data}
        await send_log(f"Loaded {len(CONFIG['sources'])} mappings")
    except Exception as e:
        await send_log(f"Failed to load sources: {e}")
        CONFIG["sources"] = {}

async def save_mapping(source_id, target_id):
    try:
        supabase.table("mappings").upsert({"source_id": source_id, "target_id": target_id}, on_conflict="source_id").execute()
        CONFIG["sources"][str(source_id)] = str(target_id)
        await send_log(f"Saved: {source_id} → {target_id}")
        return True
    except Exception as e:
        await send_log(f"Save failed: {e}")
        return False

async def remove_mapping(source_id):
    try:
        supabase.table("mappings").delete().eq("source_id", source_id).execute()
        CONFIG["sources"].pop(str(source_id), None)
        await send_log(f"Removed: {source_id}")
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
        if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup'):
            return True, None
        return False, "Not a channel/supergroup"
    except ValueError:
        return False, "Invalid object ID for a chat"
    except Exception as e:
        return False, str(e)

@client.on(events.NewMessage(pattern='/checkvars'))
async def check_vars(event):
    if not is_admin(event.sender_id):
        return
    await event.reply(f"UPLOAD_DELAY={UPLOAD_DELAY}s\nDELETE_DELAY={DELETE_DELAY}s\nMAX_FILE_SIZE={MAX_FILE_SIZE//1024//1024}MB\nBOT_LOG={BOT_LOG_CHAT_ID}")

@client.on(events.NewMessage(pattern='/(start|help)'))
async def start(event):
    if not is_admin(event.sender_id):
        return
    max_mb = MAX_FILE_SIZE // 1024
    min_mb_no_meta = MIN_FILE_SIZE_NO_META // 1024
    await event.reply(
        f"**Video-Only Bot - SAFE MODE**\n\n"
        f"**Delays:** Upload {UPLOAD_DELAY}s | Delete {DELETE_DELAY}s\n"
        f"**Speed:** ~{60//UPLOAD_DELAY} videos/min\n\n"
        f"`/addsource -100src -100dst`\n"
        f"`/removesource -100src`\n"
        f"`/listmappings`\n"
        f"`/scrape -100src` or `/scrape -100src fresh`\n"
        f"`/cleanhere -100clean -100trash`\n"
        f"Filters: <{MAX_DURATION}s OR <{MIN_WIDTH}x{MIN_HEIGHT} OR NO META + <{min_mb_no_meta}MB\n"
        f"`/stats`\n`/checkvars`"
    )

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
        await event.reply(f"Added: `{source_id}` → `{target_id}`")
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
    if not CONFIG["sources"]:
        await event.reply("No sources mapped")
        return
    msg = "**Current mappings:**\n"
    for src, dst in CONFIG["sources"].items():
        msg += f"`{src}` → `{dst}`\n"
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
    max_mb = MAX_FILE_SIZE // 1024 // 1024 # FIXED
    ok, err = await check_access(target_id)
    if not ok:
        await event.reply(f"Cannot access target `{target_id}`: {err}")
        return
    force_fresh = len(args) >= 3 and args[2].lower() == 'fresh'
    if force_fresh:
        offset_id = 0
        await save_checkpoint(source_id, 0)
        status_msg = await event.reply(f"Fresh scrape `{source_id}` → `{target_id}`\nDelay: {UPLOAD_DELAY}s")
    else:
        offset_id = await get_checkpoint(source_id)
        status_msg = await event.reply(f"Scraping `{source_id}` → `{target_id}`\nResume ID: `{offset_id}`\nDelay: {UPLOAD_DELAY}s")
    await send_log(f"Scrape started: {source_id} → {target_id}")
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
            try:
                if is_video_message(message):
                    if message.file.size > MAX_FILE_SIZE:
                        skipped_count += 1
                        continue
                    video_attr = get_video_attr(message)
                    await client.send_file(target_id, message.media, caption="", attributes=[video_attr] if video_attr else None, force_document=False)
                    count += 1
                    scraped_count += 1
                    await save_checkpoint(source_id, message.id)
                    await asyncio.sleep(current_delay)
            except FloodWaitError as e:
                await send_log(f"FloodWait {e.seconds}s on scrape {source_id}")
                await asyncio.sleep(e.seconds)
                current_delay = min(current_delay * 1.5, 60)
            except ChatAdminRequiredError:
                await event.reply(f"Error: No posting rights in `{target_id}`")
                await send_log(f"No posting rights in {target_id}")
                return
            except Exception as e:
                logger.error(f"Scrape error: {e}")
                errors += 1
        await save_checkpoint(source_id, 0)
        final = f"**Done**\nChecked: `{checked}`\nUploaded: `{count}`\nSkipped >{max_mb}MB: `{skipped_count}`\nErrors: `{errors}`"
        await status_msg.edit(final)
        await send_log(f"Scrape done: {count} videos from {source_id}")
    except Exception as e:
        await event.reply(f"Scrape failed: {e}")
        await send_log(f"Scrape crashed: {e}")

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
    ok_clean, err_clean = await check_access(clean_channel_id)
    if not ok_clean:
        await event.reply(f"Cannot access `{clean_channel_id}`: {err_clean}")
        return
    ok_trash, err_trash = await check_access(trash_id)
    if not ok_trash:
        await event.reply(f"Cannot access trash `{trash_id}`: {err_trash}")
        return
    status_msg = await event.reply(f"Cleaning `{clean_channel_id}` → `{trash_id}`\nDelay: {UPLOAD_DELAY}s")
    await send_log(f"Clean started: {clean_channel_id} → {trash_id}")
    checked = found_videos = moved = kept = errors = 0
    sample_kept, sample_moved = [], []
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
                    if len(sample_moved) < 3:
                        sample_moved.append(f"ID:{message.id} {duration}s {width}x{height} {file_size//1024//1024}MB")
                    try:
                        await client.send_file(trash_id, message.media, caption="", attributes=[video_meta], force_document=False)
                        await message.delete()
                        moved += 1
                        await asyncio.sleep(current_delay)
                    except FloodWaitError as e:
                        await send_log(f"FloodWait {e.seconds}s on clean")
                        await asyncio.sleep(e.seconds)
                        current_delay = min(current_delay * 1.5, 60)
                    except Exception as e:
                        logger.error(f"Move failed: {e}")
                        errors += 1
                else:
                    if len(sample_kept) < 3:
                        sample_kept.append(f"ID:{message.id} {duration}s {width}x{height} {file_size//1024//1024}MB")
                    kept += 1
            if checked % 500 == 0:
                try:
                    await status_msg.edit(f"Checked {checked}... Videos: {found_videos}... Moved: {moved}... Errors: {errors}")
                except:
                    pass
        kept_samples = "\n".join(sample_kept) if sample_kept else "None"
        moved_samples = "\n".join(sample_moved) if sample_moved else "None"
        final = f"**Clean done**\nChecked: `{checked}`\nVideos: `{found_videos}`\nMoved: `{moved}`\nKept: `{kept}`\nErrors: `{errors}`\n\n**Moved:**\n```\n{moved_samples}\n```\n\n**Kept:**\n```\n{kept_samples}\n```"
        await status_msg.edit(final)
        await send_log(f"Clean done: {moved} moved from {clean_channel_id}")
    except Exception as e:
        await event.reply(f"Clean failed: {e}")
        await send_log(f"Clean crashed: {e}")

@client.on(events.NewMessage(pattern='/stats'))
async def stats(event):
    if not is_admin(event.sender_id):
        return
    max_mb = MAX_FILE_SIZE // 1024
    await event.reply(f"**Stats**\nScraped: `{scraped_count}`\nSkipped >{max_mb}MB: `{skipped_count}`\nMappings: `{len(CONFIG['sources'])}`\nDelays: `{UPLOAD_DELAY}s/{DELETE_DELAY}s`")

@client.on(events.NewMessage)
async def auto_forward(event):
    if str(event.chat_id) in CONFIG["sources"] and is_video_message(event):
        target_id = int(CONFIG["sources"][str(event.chat_id)])
        try:
            if event.file.size > MAX_FILE_SIZE:
                logger.info(f"Skipped {event.file.size/1024/1024:.1f}MB video")
                return
            video_attr = get_video_attr(event.message)
            await client.send_file(target_id, event.media, caption="", attributes=[video_attr] if video_attr else None, force_document=False)
            logger.info(f"Re-uploaded video from {event.chat_id} to {target_id}")
            await asyncio.sleep(UPLOAD_DELAY)
        except FloodWaitError as e:
            await send_log(f"FloodWait {e.seconds}s on auto-forward {event.chat_id}")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            await send_log(f"Auto forward failed {event.chat_id}: {e}")

async def main():
    await load_sources()
    await client.start()
    me = await client.get_me()
    await send_log(f"Bot started as {me.first_name}. Delays: {UPLOAD_DELAY}s/{DELETE_DELAY}s")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())