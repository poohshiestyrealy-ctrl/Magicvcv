import os
import asyncio
import logging
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
TOPIC_CREATE_DELAY = 60  # Telegram requires 60s between topic creates

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CONFIG = {"sources": {}, "auto_gif": {}, "auto_short": {}}
scraped_count = 0
skipped_count = 0

def rebuild_mapped_chats():
    global mapped_chats
    mapped_chats = set(CONFIG["sources"].keys()) | set(CONFIG["auto_gif"].keys()) | set(CONFIG["auto_short"].keys())

async def send_log(text):
    if BOT_LOG_CHAT_ID != 0:
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
            return res.data[0].get("archive_topic_id")
        return None
    except Exception as e:
        logger.error(f"get_archive_topic_id error: {e}")
        return None











async def scrape_group_with_topics(source_id, target_id, status_msg, force_fresh=False):
    global scraped_count, skipped_count
    topic_map = await get_topic_map(source_id, target_id)
    archive_topic_id = await get_archive_topic_id(source_id, target_id)

    if not topic_map:
        await status_msg.edit("No topic map found. Run `/resyncgroupfresh source_id target_id` first")
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

# ==================== FIXED resyncgroupfresh ====================
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

    # ===== CREATE ARCHIVE WITH PROPER ERROR HANDLING =====
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
            await asyncio.sleep(TOPIC_CREATE_DELAY) # 60s wait

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

    # ===== CREATE SOURCE TOPICS WITH RETRY =====
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
                    await asyncio.sleep(TOPIC_CREATE_DELAY) # 60s mandatory
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
    await msg.edit(f"**Fresh Resync Complete**\nValid topics: `{len(src_topics)}`\nCreated: `{created}`\nSkipped to Archive: `{skipped}`\n\nRun `/scrapegrouplike {source_id} fresh` to start scraping.")










@client.on(events.NewMessage(pattern=r'/start'))
async def start_cmd(event):
    if not is_admin(event.sender_id):
        return
    await event.reply("Bot is online. Send `/help` to see all commands.")

@client.on(events.NewMessage(pattern=r'/help'))
async def help_handler(event):
    if not is_admin(event.sender_id):
        return

    help_text = """
**Yaga Bot Commands - Beginner Guide**

**1. Setup Topics:**
`/addsource <src_id> <dst_id>` - Link source group to target group
`/removesource <src_id>` - Remove the link
`/listmappings` - Show all linked groups
`/resyncgroupfresh <src_id> <dst_id>` - Create 1 topic in target for every topic in source
`/clearmapping <src_id> <dst_id>` - Delete the topic mapping
`/debugtopics <group_id> [group_id2]` - Check if bot can see topics
`/diag <group_id>` - Run diagnostics to check why topics aren't loading

**2. Scrape Groups:**
`/scrapegrouplike <src_id> [fresh]` - Start scraping. Add 'fresh' to start from beginning

**3. Auto GIF/Short:**
`/addauto gif|short <src> <dst>` - Auto forward GIFs or shorts
`/removeauto gif|short <src>` - Stop auto forward
`/scrapegif <src_id>` - Scrape existing GIFs
`/scrapeshort <src_id>` - Scrape existing shorts under 60s

**4. Archive Remap:**
`/maparchive <source_id>` - Check if 'Archive' topic exists in source
`/remaparchive <src_id> <dst_id>` - Move messages from Archive to correct topics
`/unmaparchive <src_id> <dst_id>` - Delete topic mapping
`/clearremapjob <src_id> <dst_id>` - Reset remap progress
`/settopicmap <src_id> <dst_id> <src_tid> <dst_tid>` - Manually link one topic

**5. Other:**
`/stats` - Show stats
"""
    await event.reply(help_text)

@client.on(events.NewMessage(pattern=r'/listmappings'))
async def list_mappings(event):
    if not is_admin(event.sender_id):
        return
    if not CONFIG["sources"]:
        await event.reply("No mappings found")
        return
    text = "**Current Mappings:**\n"
    for src, dst in CONFIG["sources"].items():
        text += f"`{src}` -> `{dst}`\n"
    await event.reply(text)

@client.on(events.NewMessage(pattern=r'/addsource (-?[0-9]+) (-?[0-9]+)'))
async def add_source(event):
    if not is_admin(event.sender_id):
        return
    try:
        source_id = int(event.pattern_match.group(1))
        target_id = int(event.pattern_match.group(2))
        if await save_mapping(source_id, target_id):
            await event.reply(f"Added mapping: `{source_id}` -> `{target_id}`")
        else:
            await event.reply("Failed to save mapping")
    except Exception as e:
        await event.reply(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/removesource (-?[0-9]+)'))
async def remove_source(event):
    if not is_admin(event.sender_id):
        return
    try:
        source_id = int(event.pattern_match.group(1))
        if await remove_mapping(source_id):
            await event.reply(f"Removed mapping for `{source_id}`")
        else:
            await event.reply("Failed to remove mapping")
    except Exception as e:
        await event.reply(f"Error: {e}")

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
            res2 = await asyncio.wait_for(client(GetForumTopicsRequest(channel=entity2, limit=200)), timeout=20)
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
        res = await asyncio.wait_for(client(GetForumTopicsRequest(channel=entity, limit=5)), timeout=15)
        await msg.edit(f"**Step 1/2**: get_entity\n✅ OK\n**Step 2/2**: get_topics\n✅ OK\nTopics found: `{len(res.topics)}`")
    except Exception as e:
        await msg.edit(f"Error: {e}")

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

# ==================== MAIN ====================
async def main():
    await client.start()
    await load_sources()
    await send_log("✅ Bot started successfully")
    print("✅ Bot is running...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())