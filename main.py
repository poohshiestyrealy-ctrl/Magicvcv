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