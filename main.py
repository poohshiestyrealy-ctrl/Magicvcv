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

MAX_FILE_SIZE = 200 * 1024 # 200MB
MAX_DURATION = 60 # seconds - videos SHORTER than this get cleaned
MIN_WIDTH = 1280 # px - 720p width - videos smaller than this get cleaned
MIN_HEIGHT = 720 # px - 720p height - videos smaller than this get cleaned
MIN_FILE_SIZE_NO_META = 15 * 1024 * 1024 # 15MB - aggressive: moves most <60s videos with no metadata
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG = {"sources": {}}
scraped_count = 0
skipped_count = 0

def is_admin(user_id):
    return user_id in ADMIN_IDS

def get_video_attr(message):
    """Extract DocumentAttributeVideo from message"""
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
        logger.info(f"Loaded {len(CONFIG['sources'])} mappings from Supabase")
    except Exception as e:
        logger.error(f"Failed to load sources: {e}")
        CONFIG["sources"] = {}

async def save_mapping(source_id, target_id):
    try:
        supabase.table("mappings").upsert({
            "source_id": source_id,
            "target_id": target_id
        }, on_conflict="source_id").execute()
        CONFIG["sources"][str(source_id)] = str(target_id)
        logger.info(f"Saved to Supabase: {source_id} -> {target_id}")
        return True
    except Exception as e:
        logger.error(f"Save failed: {e}")
        return False

async def remove_mapping(source_id):
    try:
        supabase.table("mappings").delete().eq("source_id", source_id).execute()
        CONFIG["sources"].pop(str(source_id), None)
        logger.info(f"Removed from Supabase: {source_id}")
        return True
    except Exception as e:
        logger.error(f"Remove failed: {e}")
        return False

async def save_checkpoint(source_id, msg_id):
    try:
        supabase.table("scrape_progress").upsert({
            "source_id": source_id,
            "last_message_id": msg_id
        }, on_conflict="source_id").execute()
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
    await event.reply(f"MAX_DURATION={MAX_DURATION}\nMIN_WIDTH={MIN_WIDTH}\nMIN_HEIGHT={MIN_HEIGHT}\nMAX_FILE_SIZE={MAX_FILE_SIZE//1024//1024}MB\nMIN_FILE_SIZE_NO_META={MIN_FILE_SIZE_NO_META//1024//1024}MB")

@client.on(events.NewMessage(pattern='/(start|help)'))
async def start(event):
    if not is_admin(event.sender_id):
        return
    max_mb = MAX_FILE_SIZE // 1024
    min_mb_no_meta = MIN_FILE_SIZE_NO_META // 1024
    await event.reply(
        f"**Video-Only Bot**\n\n"
        f"**Max video size:** {max_mb}MB\n"
        f"Larger files are skipped.\n\n"
        f"**1. Add a channel pair:**\n"
        f"`/addsource -100123456789 -100987654321`\n"
        f"You must be in both channels with posting rights.\n\n"
        f"**2. Remove a pair:**\n"
        f"`/removesource -100123456789`\n\n"
        f"**3. List all pairs:**\n"
        f"`/listmappings`\n\n"
        f"**4. Scrape old videos:**\n"
        f"`/scrape -100123456789`\n"
        f"Re-uploads ALL videos ≤{max_mb}MB with 3.5s delay.\n"
        f"`/scrape -100id fresh` - restart scrape from beginning\n\n"
        f"**5. Clean bad videos:**\n"
        f"`/cleanhere -100clean_id -100trash_id`\n"
        f"Moves videos to trash if:\n"
        f"• Duration <{MAX_DURATION}s OR\n"
        f"• Resolution <{MIN_WIDTH}x{MIN_HEIGHT} OR\n"
        f"• NO METADATA + File size <{min_mb_no_meta}MB\n\n"
        f"**6. Purge non-videos:**\n"
        f"`/purgenonvideo -100channel_id`\n"
        f"Deletes photos/text/audio/docs permanently. Videos untouched.\n\n"
        f"**7. Check stats:**\n"
        f"`/stats`\n\n"
        f"**8. Debug vars:**\n"
        f"`/checkvars`\n\n"
        f"**Notes:**\n"
        f"- Clean reupload: no captions, no forward tags\n"
        f"- Preserves metadata on reupload\n"
        f"- Handles videos sent as files\n"
        f"- Uses 2x bandwidth due to reupload"
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
        await event.reply("IDs must be numbers like `-1001234567890`")
        return

    ok, err = await check_access(target_id)
    if not ok:
        await event.reply(f"Cannot access target `{target_id}`: {err}")
        return

    if await save_mapping(source_id, target_id):
        await event.reply(f"Added/Updated mapping:\n`{source_id}` → `{target_id}`")
    else:
        await event.reply("Failed to save mapping to Supabase")

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
        await event.reply(f"Removed mapping for `{source_id}`")
    else:
        await event.reply("Failed to remove mapping from Supabase")

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
    max_mb = MAX_FILE_SIZE // 1024 // 1024

    ok, err = await check_access(target_id)
    if not ok:
        await event.reply(f"Cannot access target `{target_id}`: {err}")
        return

    force_fresh = len(args) >= 3 and args[2].lower() == 'fresh'

    if force_fresh:
        offset_id = 0
        await save_checkpoint(source_id, 0)
        await event.reply(f"Force fresh scrape `{source_id}` → `{target_id}`\nStarting from oldest message.")
    else:
        offset_id = await get_checkpoint(source_id)
        await event.reply(f"Scraping `{source_id}` → `{target_id}`\nResuming from message ID: `{offset_id}`\n3.5s delay per video.")

    count = 0
    checked = 0
    errors = 0

    try:
        async for message in client.iter_messages(source_id, limit=None, offset_id=offset_id, reverse=True):
            checked += 1
            if checked % 50 == 0:
                await event.reply(f"Checked {checked} messages... Reuploaded {count} videos so far")
                await save_checkpoint(source_id, message.id)

            try:
                if is_video_message(message):
                    if message.file.size > MAX_FILE_SIZE:
                        skipped_count += 1
                        continue

                    video_attr = get_video_attr(message)
                    await client.send_file(
                        target_id,
                        message.media,
                        caption="",
                        attributes=[video_attr] if video_attr else None,
                        force_document=False
                    )
                    count += 1
                    scraped_count += 1
                    await save_checkpoint(source_id, message.id)
                    await asyncio.sleep(3.5)
            except FloodWaitError as e:
                await event.reply(f"Flood wait: sleeping {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except ChatAdminRequiredError:
                await event.reply(f"Error: No posting rights in target `{target_id}`. Make your account admin.")
                return
            except Exception as e:
                logger.error(f"Scrape error: {e}")
                errors += 1

        await save_checkpoint(source_id, 0)
        await event.reply(f"Done.\nChecked: `{checked}` messages\nVideos reuploaded: `{count}`\nSkipped >{max_mb}MB: `{skipped_count}`\nErrors: `{errors}`")
    except Exception as e:
        await event.reply(f"Scrape failed: {e}")

@client.on(events.NewMessage(pattern='/cleanhere'))
async def clean_here(event):
    if not is_admin(event.sender_id):
        return

    args = event.text.split()
    if len(args) < 3:
        await event.reply(
            "**Usage:** `/cleanhere -100channel_to_clean -100trash_id`\n\n"
            f"Cleans any channel your userbot has access to.\n"
            f"Moves videos if:\n"
            f"1. Duration <{MAX_DURATION}s OR\n"
            f"2. Resolution <{MIN_WIDTH}x{MIN_HEIGHT} OR\n"
            f"3. NO METADATA + File size <{MIN_FILE_SIZE_NO_META//1024//1024}MB\n\n"
            "Example: `/cleanhere -1001111111111 -1009999999999`"
        )
        return

    try:
        clean_channel_id = int(args[1])
        trash_id = int(args[2])
    except ValueError:
        await event.reply("Invalid channel IDs. Must be like `-1001234567890`")
        return

    ok_clean, err_clean = await check_access(clean_channel_id)
    if not ok_clean:
        await event.reply(f"Cannot access channel `{clean_channel_id}`: {err_clean}")
        return

    ok_trash, err_trash = await check_access(trash_id)
    if not ok_trash:
        await event.reply(f"Cannot access trash `{trash_id}`: {err_trash}")
        return

    await event.reply(f"Cleaning `{clean_channel_id}` → `{trash_id}`\nFilters: <{MAX_DURATION}s or <{MIN_WIDTH}x{MIN_HEIGHT} or NO META + <{MIN_FILE_SIZE_NO_META//1024//1024}MB\nStarting in 3s...")
    await asyncio.sleep(3)

    checked = 0
    found_videos = 0
    moved = 0
    kept = 0
    errors = 0
    sample_kept = []
    sample_moved = []

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
                reason = ""

                if duration > 0 and duration < MAX_DURATION:
                    should_move = True
                    reason = f"{duration}s < {MAX_DURATION}s"
                elif width > 0 and height > 0 and (width < MIN_WIDTH or height < MIN_HEIGHT):
                    should_move = True
                    reason = f"{width}x{height} < {MIN_WIDTH}x{MIN_HEIGHT}"
                elif duration == 0 and (width == 0 or height == 0):
                    if file_size == 0:
                        should_move = True
                        reason = "No metadata and file size 0"
                    elif file_size < MIN_FILE_SIZE_NO_META:
                        should_move = True
                        reason = f"No metadata, size {file_size//1024//1024}MB < {MIN_FILE_SIZE_NO_META//1024//1024}MB"
                    else:
                        reason = f"No metadata but size {file_size//1024//1024}MB >= {MIN_FILE_SIZE_NO_META//1024//1024}MB - keeping"
                else:
                    reason = f"Good: {duration}s {width}x{height}"

                if should_move:
                    if len(sample_moved) < 5:
                        sample_moved.append(f"ID:{message.id} {duration}s {width}x{height} {file_size//1024//1024}MB -> {reason}")
                    try:
                        await client.send_file(
                            trash_id,
                            message.media,
                            caption="",
                            attributes=[video_meta],
                            force_document=False
                        )
                        await message.delete()
                        moved += 1
                        await asyncio.sleep(3.5)
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds)
                    except Exception as e:
                        logger.error(f"Move failed: {e}")
                        errors += 1
                else:
                    if len(sample_kept) < 5:
                        sample_kept.append(f"ID:{message.id} {duration}s {width}x{height} {file_size//1024//1024}MB")
                    kept += 1

            if checked % 100 == 0:
                await event.reply(f"Checked {checked}... Videos: {found_videos}... Moved: {moved}")

        kept_samples = "\n".join(sample_kept) if sample_kept else "None"
        moved_samples = "\n".join(sample_moved) if sample_moved else "None"
        await event.reply(f"**Clean done**\nChecked: `{checked}`\nVideos found: `{found_videos}`\nMoved: `{moved}`\nKept: `{kept}`\nErrors: `{errors}`\n\n**Sample moved:**\n```\n{moved_samples}\n```\n\n**Sample kept:**\n```\n{kept_samples}\n```")
    except Exception as e:
        await event.reply(f"Clean failed: {e}")

@client.on(events.NewMessage(pattern='/purgenonvideo'))
async def purge_non_video(event):
    if not is_admin(event.sender_id):
        return

    args = event.text.split()
    if len(args) < 2:
        await event.reply(
            "**Usage:** `/purgenonvideo -100channel_id`\n\n"
            "Deletes ALL non-video content permanently.\n"
            "Photos, text, audio, docs, stickers, GIFs - all gone.\n"
            "Videos are completely ignored and left untouched.\n\n"
            "Example: `/purgenonvideo -1001111111111`"
        )
        return

    try:
        channel_id = int(args[1])
    except ValueError:
        await event.reply("Invalid channel ID. Must be like `-1001234567890`")
        return

    ok, err = await check_access(channel_id)
    if not ok:
        await event.reply(f"Cannot access channel `{channel_id}`: {err}")
        return

    await event.reply(f"PURGE MODE: `{channel_id}`\nDeleting ALL non-videos permanently.\nVideos will be ignored.\nStarting in 5s...")
    await asyncio.sleep(5)

    checked = 0
    deleted = 0
    skipped_videos = 0
    errors = 0
    sample_deleted = []

    try:
        async for message in client.iter_messages(channel_id, limit=None):
            checked += 1

            if is_video_message(message):
                skipped_videos += 1
                continue
            else:
                if len(sample_deleted) < 5:
                    msg_type = type(message.media).__name__ if message.media else "Text"
                    sample_deleted.append(f"{msg_type} ID:{message.id}")
                try:
                    await message.delete()
                    deleted += 1
                    await asyncio.sleep(1.5)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    logger.error(f"Delete failed: {e}")
                    errors += 1

            if checked % 200 == 0:
                await event.reply(f"Checked {checked}... Deleted: {deleted}... Videos skipped: {skipped_videos}")

        deleted_samples = "\n".join(sample_deleted) if sample_deleted else "None"
        await event.reply(
            f"**PURGE DONE**\n"
            f"Checked: `{checked}` messages\n"
            f"Non-videos deleted: `{deleted}`\n"
            f"Videos skipped: `{skipped_videos}`\n"
            f"Errors: `{errors}`\n\n"
            f"**Sample deleted:**\n```\n{deleted_samples}\n```"
        )
    except Exception as e:
        await event.reply(f"Purge failed: {e}")

@client.on(events.NewMessage(pattern='/stats'))
async def stats(event):
    if not is_admin(event.sender_id):
        return
    max_mb = MAX_FILE_SIZE // 1024 // 1024
    await event.reply(f"**Stats**\nScraped: `{scraped_count}`\nSkipped >{max_mb}MB: `{skipped_count}`\nMappings: `{len(CONFIG['sources'])}`")

@client.on(events.NewMessage)
async def auto_forward(event):
    if str(event.chat_id) in CONFIG["sources"] and is_video_message(event):
        target_id = int(CONFIG["sources"][str(event.chat_id)])
        try:
            if event.file.size > MAX_FILE_SIZE:
                logger.info(f"Skipped {event.file.size/1024/1024:.1f}MB video")
                return

            video_attr = get_video_attr(event.message)
            await client.send_file(
                target_id,
                event.media,
                caption="",
                attributes=[video_attr] if video_attr else None,
                force_document=False
            )
            logger.info(f"Re-uploaded video from {event.chat_id} to {target_id}")
        except FloodWaitError as e:
            logger.warning(f"Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f"Auto forward failed: {e}")

async def main():
    await load_sources()
    await client.start()
    logger.info(f"Bot started. Admins: {ADMIN_IDS}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())