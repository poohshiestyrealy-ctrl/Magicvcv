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
`/testmapping <src_id> <dst_id>` - Test if mapping exists
`/buildmapping <src_id> <dst_id>` - Build mapping without retry loop

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
`/remaparchive <src_id> <dst_id> --reset` - Restart remap from beginning
`/unmaparchive <src_id> <dst_id>` - Delete topic mapping
`/clearremapjob <src_id> <dst_id>` - Reset remap progress
`/settopicmap <src_id> <dst_id> <src_tid> <dst_tid>` - Manually link one topic

**5. Other:**
`/stats` - Show stats
`/start` - Check if bot is online
`/help` - Show this message
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
    except asyncio.TimeoutError:
        await msg.edit("❌ Timeout. Open both groups once on Telegram Desktop with the userbot account.")
        return
    except Exception as e:
        await msg.edit(f"❌ Error: {e}")
        return

    if not getattr(src_entity, 'forum', False) or not getattr(tgt_entity, 'forum', False):
        await msg.edit("❌ Both groups need topics enabled.")
        return

    all_topics = []
    offset_topic = 0
    offset_id = 0
    retries = 0

    await msg.edit("📡 Fetching source topics...")
    while retries < 3:
        try:
            res = await asyncio.wait_for(
                client(GetForumTopicsRequest(
                    channel=src_entity,
                    offset_date=0,
                    offset_id=offset_id,
                    offset_topic=offset_topic,
                    limit=20,
                )),
                timeout=20
            )
            if not res.topics:
                break
            all_topics.extend(res.topics)
            if len(res.topics) < 20:
                break
            last_topic = res.topics[-1]
            offset_topic = last_topic.id
            offset_id = last_topic.top_message
            await asyncio.sleep(2)
        except asyncio.TimeoutError:
            retries += 1
            await asyncio.sleep(3)
            continue
        except Exception as e:
            await msg.edit(f"❌ Failed fetching source: {e}")
            return

    src_topics = [t for t in all_topics if not getattr(t, 'deleted', False) and t.id!= 1]
    if not src_topics:
        await msg.edit("❌ No topics found in source.")
        return

    await msg.edit(f"✅ Found **{len(src_topics)}** source topics. Checking target...")

    try:
        tgt_res = await asyncio.wait_for(
            client(GetForumTopicsRequest(
                channel=tgt_entity,
                offset_date=0,
                offset_id=0,
                offset_topic=0,
                limit=100
            )),
            timeout=20
        )
        active_topics = [tt for tt in tgt_res.topics if not getattr(tt, 'deleted', False) and tt.id!= 1]
    except Exception as e:
        await msg.edit(f"❌ Failed to fetch target topics: {e}")
        return

    archive_topic = next((tt for tt in active_topics if tt.title == "Archive"), None)
    if not archive_topic:
        try:
            result = await client(CreateForumTopicRequest(channel=tgt_entity, title="Archive"))
            archive_topic_id = result.updates[1].topic.id
            await asyncio.sleep(2)
        except Exception as e:
            await msg.edit(f"Failed to create Archive topic: {e}")
            return
    else:
        archive_topic_id = archive_topic.id

    await save_archive_topic_id(source_id, target_id, archive_topic_id)

    new_mapping = {}
    created = 0
    skipped = 0
    available_slots = 100 - len(active_topics)

    await msg.edit(f"Target has {len(active_topics)} active topics. Available slots: {available_slots}. Starting...")

    for t in src_topics:
        if created >= available_slots:
            new_mapping[str(t.id)] = archive_topic_id
            skipped += 1
            continue

        try:
            result = await client(CreateForumTopicRequest(
                channel=tgt_entity,
                title=t.title[:128],
                icon_emoji_id=getattr(t, 'icon_emoji_id', None)
            ))
            new_id = result.updates[1].topic.id
            new_mapping[str(t.id)] = new_id
            created += 1
            await asyncio.sleep(3)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 2)
        except Exception:
            new_mapping[str(t.id)] = archive_topic_id
            skipped += 1

    await save_topic_map(source_id, target_id, new_mapping)
    await msg.edit(f"**Fresh Resync Complete**\nCreated: `{created}`\nSkipped to Archive: `{skipped}`")

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
        res = await asyncio.wait_for(client(GetForumTopicsRequest(
            channel=entity1,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=200
        )), timeout=20)
        text = f"**Group {gid1}**\nTotal: {len(res.topics)}\n"
        for t in res.topics[:50]:
            text += f"ID:`{t.id}` Title:`{t.title}`\n"

        if gid2:
            entity2 = await asyncio.wait_for(client.get_entity(gid2), timeout=15)
            res2 = await asyncio.wait_for(client(GetForumTopicsRequest(
                channel=entity2,
                offset_date=0,
                offset_id=0,
                offset_topic=0,
                limit=200
            )), timeout=20)
            text += f"\n**Group {gid2}**\nTotal: {len(res2.topics)}\n"
            for t in res2.topics[:50]:
                text += f"ID:`{t.id}` Title:`{t.title}`\n"

        await msg.edit(text[:4000])
    except asyncio.TimeoutError:
        await msg.edit("❌ Timeout. Open the group once in Telegram Desktop with the userbot account.")
    except Exception as e:
        await msg.edit(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/diag (-?[0-9]+)'))
async def diag_group(event):
    """Diagnostic command to check why topics aren't loading"""
    if not is_admin(event.sender_id):
        return
    gid = int(event.pattern_match.group(1))
    msg = await event.reply(f"Running diagnostics on `{gid}`...")

    try:
        entity = await asyncio.wait_for(client.get_entity(gid), timeout=10)
        await msg.edit(f"**Step 1/2**: get_entity\n✅ OK\nType: `{type(entity).__name__}`\nTitle: `{getattr(entity, 'title', 'N/A')}`\nForum/Topics: `{getattr(entity, 'forum', False)}`")
    except asyncio.TimeoutError:
        await msg.edit(f"**Step 1/2**: get_entity\n❌ TIMEOUT\nUserbot can't access this group.\nFix: Open the group once in Telegram Desktop.")
        return
    except ValueError:
        await msg.edit(f"**Step 1/2**: get_entity\n❌ NOT FOUND\nUserbot is not a member of this group.")
        return
    except Exception as e:
        await msg.edit(f"**Step 1/2**: get_entity\n❌ ERROR\n{e}")
        return

    try:
        res = await asyncio.wait_for(
            client(GetForumTopicsRequest(channel=entity, offset_date=0, offset_id=0, offset_topic=0, limit=5)),
            timeout=15
        )
        await msg.edit(f"**Step 1/2**: get_entity\n✅ OK\n**Step 2/2**: get_topics\n✅ OK\nTopics found: `{len(res.topics)}`\n\nIf this works, `/resyncgroupfresh` should work too.")
    except asyncio.TimeoutError:
        await msg.edit(f"**Step 1/2**: get_entity\n✅ OK\n**Step 2/2**: get_topics\n❌ TIMEOUT\nTelegram not returning topics.\nFix: Open the group in Telegram Desktop, wait 10s, try again.")
    except Exception as e:
        await msg.edit(f"**Step 1/2**: get_entity\n✅ OK\n**Step 2/2**: get_topics\n❌ FAILED\n{e}")

@client.on(events.NewMessage(pattern=r'/testmapping (-?[0-9]+) (-?[0-9]+)'))
async def test_mapping(event):
    """Test if topic mapping exists and works"""
    if not is_admin(event.sender_id):
        return
    src_id = int(event.pattern_match.group(1))
    dst_id = int(event.pattern_match.group(2))

    topic_map = await get_topic_map(src_id, dst_id)
    archive_id = await get_archive_topic_id(src_id, dst_id)

    if not topic_map:
        await event.reply(f"❌ No mapping found for `{src_id}` -> `{dst_id}`\nRun `/resyncgroupfresh {src_id} {dst_id}` first")
        return

    mapped_count = len(topic_map)
    await event.reply(f"✅ Mapping exists\nMapped topics: `{mapped_count}`\nArchive topic: `{archive_id}`\n\nSample: `{list(topic_map.items())[:3]}`")

@client.on(events.NewMessage(pattern=r'/clearmapping (-?[0-9]+) (-?[0-9]+)'))
async def clear_mapping(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    target_id = int(event.pattern_match.group(2))
    msg = await event.reply(f"Clearing mapping for `{source_id}` -> `{target_id}`...")
    try:
        supabase.table("group_topic_map").delete().eq("source_id", source_id).eq("target_id", target_id).execute()
        await msg.edit("**Mapping cleared**\nUse `/resyncgroupfresh {source_id} {target_id}` to rebuild.")
    except Exception as e:
        await msg.edit(f"Failed: {e}")

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













@client.on(events.NewMessage(pattern=r'/addauto (gif|short) (-?[0-9]+) (-?[0-9]+)'))
async def add_auto(event):
    if not is_admin(event.sender_id):
        return
    mode = event.pattern_match.group(1)
    source_id = int(event.pattern_match.group(2))
    target_id = int(event.pattern_match.group(3))

    try:
        supabase.table("auto_mappings").upsert({
            "source_id": source_id,
            "target_id": target_id,
            "mode": mode
        }, on_conflict="source_id,mode").execute()

        if mode == "gif":
            CONFIG["auto_gif"][str(source_id)] = str(target_id)
        else:
            CONFIG["auto_short"][str(source_id)] = str(target_id)

        await event.reply(f"Added {mode} mapping: `{source_id}` -> `{target_id}`")
    except Exception as e:
        await event.reply(f"Failed: {e}")

@client.on(events.NewMessage(pattern=r'/removeauto (gif|short) (-?[0-9]+)'))
async def remove_auto(event):
    if not is_admin(event.sender_id):
        return
    mode = event.pattern_match.group(1)
    source_id = int(event.pattern_match.group(2))

    try:
        supabase.table("auto_mappings").delete().eq("source_id", source_id).eq("mode", mode).execute()
        if mode == "gif":
            CONFIG["auto_gif"].pop(str(source_id), None)
        else:
            CONFIG["auto_short"].pop(str(source_id), None)
        await event.reply(f"Removed {mode} mapping for `{source_id}`")
    except Exception as e:
        await event.reply(f"Failed: {e}")

@client.on(events.NewMessage(pattern=r'/scrapegif (-?[0-9]+)'))
async def scrape_gif(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    target_id = CONFIG["auto_gif"].get(str(source_id))
    if not target_id:
        await event.reply(f"No GIF mapping for `{source_id}`")
        return

    msg = await event.reply("Scraping GIFs...")
    count = 0
    async for message in client.iter_messages(source_id, limit=500):
        if is_gif(message):
            try:
                await client.send_file(int(target_id), message.media, caption="")
                count += 1
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"GIF send failed: {e}")
    await msg.edit(f"Done. Sent {count} GIFs")

@client.on(events.NewMessage(pattern=r'/scrapeshort (-?[0-9]+)'))
async def scrape_short(event):
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    target_id = CONFIG["auto_short"].get(str(source_id))
    if not target_id:
        await event.reply(f"No short mapping for `{source_id}`")
        return

    msg = await event.reply("Scraping shorts...")
    count = 0
    async for message in client.iter_messages(source_id, limit=500):
        if is_video_message(message):
            video_attr = get_video_attr(message)
            if video_attr and video_attr.duration <= 60:
                try:
                    await client.send_file(int(target_id), message.media, caption="")
                    count += 1
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"Short send failed: {e}")
    await msg.edit(f"Done. Sent {count} shorts")

@client.on(events.NewMessage(pattern=r'/stats'))
async def stats(event):
    if not is_admin(event.sender_id):
        return
    text = f"**Stats**\nScraped: {scraped_count}\nSkipped: {skipped_count}\nMappings: {len(CONFIG['sources'])}\nGIF Auto: {len(CONFIG['auto_gif'])}\nShort Auto: {len(CONFIG['auto_short'])}"
    await event.reply(text)

# ==================== ARCHIVE REMAP FEATURES ====================

@client.on(events.NewMessage(pattern=r'/maparchive (-?[0-9]+)'))
async def map_archive_topic(event):
    """Step 1: Check if 'Archive' topic exists in source group"""
    if not is_admin(event.sender_id):
        return
    source_id = int(event.pattern_match.group(1))
    msg = await event.reply("Checking Archive topic...")
    try:
        entity = await asyncio.wait_for(client.get_entity(source_id), timeout=15)
        topics = await asyncio.wait_for(client(GetForumTopicsRequest(
            channel=entity,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=200
        )), timeout=20)
        archive_topic_id = None
        for t in topics.topics:
            if getattr(t, 'title', '').lower() == 'archive':
                archive_topic_id = t.id
                break
        if not archive_topic_id:
            await msg.edit("❌ No Archive topic found in this group.")
            return
        await msg.edit(f"✅ Found Archive topic ID: `{archive_topic_id}`\n\nNext: `/remaparchive {source_id} <target_id>`")
    except Exception as e:
        await msg.edit(f"Error: {e}")

@client.on(events.NewMessage(pattern=r'/remaparchive (-?[0-9]+) (-?[0-9]+)( --reset)?'))
async def remap_archive(event):
    """Step 2: Move messages from Archive topic to correct topics in target"""
    if not is_admin(event.sender_id):
        return
    source_group_id = int(event.pattern_match.group(1))
    target_group_id = int(event.pattern_match.group(2))
    reset_flag = event.pattern_match.group(3) is not None
    job_key = f"{source_group_id}:{target_group_id}"
    msg = await event.reply("Starting remap from Archive...")

    if reset_flag:
        await supabase.table("remap_jobs").delete().eq("job_key", job_key).execute()
        last_processed = 0
    else:
        row = await supabase.table("remap_jobs").select("last_message_id").eq("job_key", job_key).single().execute()
        last_processed = row.data["last_message_id"] if row.data else 0

    rows = await supabase.table("archive_messages").select("*").eq("source_group_id", source_group_id).eq("target_group_id", target_group_id).gt("message_id", last_processed).order("message_id").execute()

    if not rows.data:
        await msg.edit("✅ Nothing left to remap.")
        return

    topic_map = await get_topic_map(source_group_id, target_group_id) or {}
    success = failed = created_topics = 0
    total = len(rows.data)

    for row in rows.data:
        try:
            source_topic_id = str(row["source_topic_id"])
            source_topic_name = row["source_topic_name"]
            target_topic_id = topic_map.get(source_topic_id)

            if not target_topic_id:
                new_topic = await client(CreateForumTopicRequest(peer=target_group_id, title=source_topic_name[:128]))
                target_topic_id = None
                for upd in new_topic.updates:
                    if hasattr(upd, 'topic') and upd.topic:
                        target_topic_id = upd.topic.id
                        break
                if not target_topic_id:
                    failed += 1
                    continue
                topic_map[source_topic_id] = target_topic_id
                await save_topic_map(source_group_id, target_group_id, topic_map)
                created_topics += 1
                await asyncio.sleep(2)

            await client.forward_messages(target_group_id, row["message_id"], from_peer=source_group_id, reply_to=target_topic_id)
            success += 1
            last_processed = row["message_id"]

            if success % 10 == 0:
                await supabase.table("remap_jobs").upsert({"job_key": job_key, "last_message_id": last_processed}).execute()
                await msg.edit(f"Progress: {success}/{total}")

            await asyncio.sleep(1.5)
        except Exception:
            failed += 1

    await supabase.table("remap_jobs").delete().eq("job_key", job_key).execute()
    await msg.edit(f"✅ Done\n**Remapped**: {success}\n**Created**: {created_topics}\n**Failed**: {failed}")

@client.on(events.NewMessage(pattern=r'/unmaparchive (-?[0-9]+) (-?[0-9]+)'))
async def unmap_archive(event):
    """Delete topic mapping between source and target"""
    if not is_admin(event.sender_id):
        return
    source_group_id = int(event.pattern_match.group(1))
    target_group_id = int(event.pattern_match.group(2))
    await supabase.table("group_topic_map").delete().eq("source_id", source_group_id).eq("target_id", target_group_id).execute()
    await event.reply(f"✅ Mapping removed for `{source_group_id}` → `{target_group_id}`")

@client.on(events.NewMessage(pattern=r'/clearremapjob (-?[0-9]+) (-?[0-9]+)'))
async def clear_remap_job(event):
    """Reset remap progress so it starts from beginning"""
    if not is_admin(event.sender_id):
        return
    job_key = f"{event.pattern_match.group(1)}:{event.pattern_match.group(2)}"
    await supabase.table("remap_jobs").delete().eq("job_key", job_key).execute()
    await event.reply(f"✅ Checkpoint cleared for `{job_key}`")

@client.on(events.NewMessage(pattern=r'/settopicmap (-?[0-9]+) (-?[0-9]+) (\d+) (\d+)'))
async def set_topic_map_cmd(event):
    """Manually link one source topic to one target topic"""
    if not is_admin(event.sender_id):
        return
    source_gid, target_gid, source_tid, target_tid = map(int, event.pattern_match.groups())
    topic_map = await get_topic_map(source_gid, target_gid) or {}
    topic_map[str(source_tid)] = target_tid
    await save_topic_map(source_gid, target_gid, topic_map)
    await event.reply(f"✅ Mapped topic `{source_tid}` → `{target_tid}`")

# ==================== ADDED COMMAND ====================
@client.on(events.NewMessage(pattern=r'/buildmapping (-?[0-9]+) (-?[0-9]+)'))
async def build_mapping(event):
    """Build mapping using topics from last /debugtopics call - no retry loop"""
    if not is_admin(event.sender_id):
        return
    src_id = int(event.pattern_match.group(1))
    dst_id = int(event.pattern_match.group(2))
    msg = await event.reply("Building mapping...")

    try:
        src_entity = await client.get_entity(src_id)
        dst_entity = await client.get_entity(dst_id)
    except Exception as e:
        await msg.edit(f"❌ Failed to get entities: {e}")
        return

    # Get source topics
    try:
        src_res = await client(GetForumTopicsRequest(
            channel=src_entity, offset_date=0, offset_id=0, offset_topic=0, limit=200
        ))
        src_topics = [t for t in src_res.topics if not getattr(t, 'deleted', False) and t.id!= 1]
    except Exception as e:
        await msg.edit(f"❌ Failed to fetch source topics: {e}")
        return

    # Check target topics for Archive
    try:
        tgt_res = await client(GetForumTopicsRequest(
            channel=dst_entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
        ))
        active_topics = [tt for tt in tgt_res.topics if not getattr(tt, 'deleted', False) and tt.id!= 1]
        archive_topic = next((tt for tt in active_topics if tt.title == "Archive"), None)
        
        if not archive_topic:
            result = await client(CreateForumTopicRequest(channel=dst_entity, title="Archive"))
            archive_topic_id = None
            for upd in result.updates:
                if hasattr(upd, 'topic') and upd.topic:
                    archive_topic_id = upd.topic.id
                    break
            if not archive_topic_id:
                await msg.edit("❌ Could not get Archive topic ID from response")
                return
        else:
            archive_topic_id = archive_topic.id
    except Exception as e:
        await msg.edit(f"❌ Failed to setup Archive topic: {e}")
        return

    await save_archive_topic_id(src_id, dst_id, archive_topic_id)

    new_mapping = {}
    created = 0
    skipped = 0
    available_slots = 100 - len(active_topics)

    await msg.edit(f"Found {len(src_topics)} topics. Creating {min(len(src_topics), available_slots)}...")

    for t in src_topics:
        if created >= available_slots:
            new_mapping[str(t.id)] = archive_topic_id
            skipped += 1
            continue
        try:
            result = await client(CreateForumTopicRequest(
                channel=dst_entity,
                title=t.title[:128],
                icon_emoji_id=getattr(t, 'icon_emoji_id', None)
            ))
            new_id = None
            for upd in result.updates:
                if hasattr(upd, 'topic') and upd.topic:
                    new_id = upd.topic.id
                    break
            if not new_id:
                new_mapping[str(t.id)] = archive_topic_id
                skipped += 1
                continue
                
            new_mapping[str(t.id)] = new_id
            created += 1
            await asyncio.sleep(2)
        except Exception:
            new_mapping[str(t.id)] = archive_topic_id
            skipped += 1

    await save_topic_map(src_id, dst_id, new_mapping)
    await msg.edit(f"**Mapping Built**\nCreated: `{created}`\nSkipped to Archive: `{skipped}`\n\nRun `/testmapping {src_id} {dst_id}` to verify.")

async def main():
    await load_sources()
    await client.start()
    me = await client.get_me()
    await send_log(f"Bot started as {me.first_name}")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())