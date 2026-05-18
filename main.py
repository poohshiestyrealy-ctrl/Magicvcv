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
UPLOAD_DELAY = 30

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG = {"sources": {}, "auto_gif": {}, "auto_short": {}}
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
    if message.document and message.document.mime_type == "video/mp4":
        return getattr(message.document, 'attributes', []) and any(getattr(a, 'round_message', False) or getattr(a, 'animated', False) for a in message.document.attributes)
    return False








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








# ==================== NEW ARCHIVE REMAP FEATURES ====================

@client.on(events.NewMessage(pattern=r'/maparchive (-?[0-9]+)'))
async def map_archive_topic(event):
    """Step 1: Check if Archive topic exists in source group."""
    if not is_admin(event.sender_id):
        return

    source_id = int(event.pattern_match.group(1))
    msg = await event.reply("Checking Archive topic...")

    try:
        topics = await client(GetForumTopicsRequest(channel=source_id, limit=200))
        archive_topic_id = None
        for t in topics.topics:
            if getattr(t, 'title', '').lower() == 'archive':
                archive_topic_id = t.id
                break

        if not archive_topic_id:
            await msg.edit("❌ No Archive topic found in this group.")
            return

        await msg.edit(f"✅ Found Archive topic ID: `{archive_topic_id}`\n\nNext run:\n`/remaparchive {source_id} <target_id>`")
    except Exception as e:
        await msg.edit(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/remaparchive (-?[0-9]+) (-?[0-9]+)( --reset)?'))
async def remap_archive(event):
    """Step 2: Move messages from Archive to target group. Creates topics if missing. Resumable."""
    if not is_admin(event.sender_id):
        return

    source_group_id = int(event.pattern_match.group(1))
    target_group_id = int(event.pattern_match.group(2))
    reset_flag = event.pattern_match.group(3) is not None
    job_key = f"{source_group_id}:{target_group_id}"

    msg = await event.reply("Starting remap from Archive...")

    try:
        perms = await client.get_permissions(target_group_id, await client.get_me())
        if not perms.create_topics:
            await msg.edit("❌ Userbot needs 'Manage Topics' permission in the target group.")
            return
    except Exception as e:
        await msg.edit(f"Error checking permissions: {e}")
        return

    try:
        topics = await client(GetForumTopicsRequest(channel=source_group_id, limit=200))
        archive_topic_id = None
        for t in topics.topics:
            if getattr(t, 'title', '').lower() == 'archive':
                archive_topic_id = t.id
                break
        if not archive_topic_id:
            await msg.edit("❌ No Archive topic found. Run `/maparchive` first.")
            return
    except Exception as e:
        await msg.edit(f"Error fetching topics: {e}")
        return

    if reset_flag:
        await supabase.table("remap_jobs").delete().eq("job_key", job_key).execute()
        await msg.edit("Checkpoint cleared. Starting from the beginning...")
        last_processed = 0
    else:
        resume_row = await supabase.table("remap_jobs") \
      .select("last_message_id") \
      .eq("job_key", job_key) \
      .single() \
      .execute()
        last_processed = resume_row.data["last_message_id"] if resume_row.data else 0

    try:
        query = supabase.table("archive_messages") \
     .select("*") \
     .eq("source_group_id", source_group_id) \
     .eq("target_group_id", target_group_id) \
     .gt("message_id", last_processed) \
     .order("message_id")

        rows = await query.execute()
    except Exception as e:
        await msg.edit(f"Error reading archive_messages: {e}")
        return

    if not rows.data:
        await msg.edit("✅ Nothing left to remap.")
        return

    topic_map = await get_topic_map(source_group_id, target_group_id) or {}
    success = 0
    failed = 0
    created_topics = 0
    total = len(rows.data)

    await msg.edit(f"Starting from message {last_processed}. {total} messages left.")

    for row in rows.data:
        try:
            source_topic_id = str(row["source_topic_id"])
            source_topic_name = row["source_topic_name"]
            target_topic_id = topic_map.get(source_topic_id)

            if not target_topic_id:
                try:
                    new_topic = await client(CreateForumTopicRequest(
                        peer=target_group_id,
                        title=source_topic_name[:128],
                        icon_color=0x6FB9F0
                    ))
                    target_topic_id = new_topic.updates[1].topic.id
                    topic_map[source_topic_id] = target_topic_id
                    await save_topic_map(source_group_id, target_group_id, topic_map)
                    created_topics += 1
                    await asyncio.sleep(2)
                except Exception as e:
                    failed += 1
                    logger.error(f"Create topic failed: {e}")
                    continue

            await client.forward_messages(
                entity=target_group_id,
                messages=row["message_id"],
                from_peer=source_group_id,
                reply_to=target_topic_id
            )

            success += 1
            last_processed = row["message_id"]

            if success % 10 == 0:
                await supabase.table("remap_jobs").upsert({
                    "job_key": job_key,
                    "last_message_id": last_processed,
                    "updated_at": "now()"
                }).execute()
                await msg.edit(f"Progress: {success}/{total} | Created topics: {created_topics}")

            await asyncio.sleep(1.5)

        except Exception as e:
            failed += 1
            logger.error(f"Forward failed: {e}")
            continue

    await supabase.table("remap_jobs").delete().eq("job_key", job_key).execute()
    await msg.edit(f"✅ Done\n**Remapped**: {success}\n**Topics Created**: {created_topics}\n**Failed**: {failed}")

@client.on(events.NewMessage(pattern=r'/unmaparchive (-?[0-9]+) (-?[0-9]+)'))
async def unmap_archive(event):
    """Delete topic mapping between source and target group."""
    if not is_admin(event.sender_id):
        return

    source_group_id = int(event.pattern_match.group(1))
    target_group_id = int(event.pattern_match.group(2))

    await supabase.table("group_topic_map") \
    .delete() \
    .eq("source_id", source_group_id) \
    .eq("target_id", target_group_id) \
    .execute()

    await event.reply(f"✅ Topic mapping removed for `{source_group_id}` → `{target_group_id}`")

@client.on(events.NewMessage(pattern=r'/clearremapjob (-?[0-9]+) (-?[0-9]+)'))
async def clear_remap_job(event):
    """Delete resume checkpoint so remap starts from beginning."""
    if not is_admin(event.sender_id):
        return

    source_group_id = int(event.pattern_match.group(1))
    target_group_id = int(event.pattern_match.group(2))
    job_key = f"{source_group_id}:{target_group_id}"

    await supabase.table("remap_jobs") \
    .delete() \
    .eq("job_key", job_key) \
    .execute()

    await event.reply(f"✅ Checkpoint cleared for `{job_key}`")














@client.on(events.NewMessage(pattern=r'/settopicmap (-?[0-9]+) (-?[0-9]+) (\d+) (\d+)'))
async def set_topic_map_cmd(event):
    """Manually map one source topic to one target topic."""
    if not is_admin(event.sender_id):
        return

    source_gid, target_gid, source_tid, target_tid = map(int, event.pattern_match.groups())

    topic_map = await get_topic_map(source_gid, target_gid) or {}
    topic_map[str(source_tid)] = target_tid
    await save_topic_map(source_gid, target_gid, topic_map)

    await event.reply(f"✅ Mapped topic `{source_tid}` → `{target_tid}`")

@client.on(events.NewMessage(pattern=r'/help'))
async def help_handler(event):
    if not is_admin(event.sender_id):
        return

    help_text = """
**Yaga Bot Commands**

**Topic Scraping:**
`/addsource <src_id> <dst_id>` - Add scrape mapping
`/removesource <src_id>` - Remove mapping
`/listmappings` - Show all mappings
`/resyncgroupfresh <src_id> <dst_id>` - Fresh sync, creates 1 topic per source topic
`/clearmapping <src_id> <dst_id>` - Delete mapping from Supabase
`/scrapegrouplike <src_id> [fresh]` - Scrape group with topics
`/debugtopics <group_id> [group_id2]` - Show topics bot sees

**GIF/Short Scraping:**
`/addauto gif|short <src> <dst>` - Add auto mapping
`/removeauto gif|short <src>` - Remove auto mapping
`/scrapegif <src_id>` - Scrape GIFs from source
`/scrapeshort <src_id>` - Scrape shorts <60s from source

**Archive Remap:**
`/maparchive <source_id>` - Check if Archive topic exists
`/remaparchive <src_id> <dst_id>` - Move messages from Archive to target
`/remaparchive <src_id> <dst_id> --reset` - Start over, ignore checkpoint
`/unmaparchive <src_id> <dst_id>` - Delete topic mapping
`/clearremapjob <src_id> <dst_id>` - Delete resume checkpoint
`/settopicmap <src_id> <dst_id> <src_tid> <dst_tid>` - Manually map one topic

`/stats` - Show stats
`/help` - Show this message
"""
    await event.reply(help_text)

async def main():
    await load_sources()
    await client.start()
    me = await client.get_me()
    await send_log(f"Bot started as {me.first_name}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())