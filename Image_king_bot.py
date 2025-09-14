#!/usr/bin/env python3
"""
Telegram Image Bot (Stability.ai)
Features:
- /start -> name -> phone -> OTP (shown in chat)
- Logs only user info (no HTTP payloads) in terminal
- Dimension buttons (allowed SDXL sizes)
- Prompt input (free text)
- Generate image via Stability API (async wrapper)
- After-image inline buttons: "edit", "save gallery", "no gallery", "Generate Again"
- edit -> ask edit instructions -> generate edited image
- save gallery -> saves to user's gallery
- no gallery -> does not save
- Generate Again -> asks new prompt and generates (same dimension)
- Multi-user safe (context.user_data)
"""
import os
import asyncio
import base64
import io
import time
import random
import requests
import logging
from functools import partial
from typing import Optional, Tuple

from PIL import Image
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ----------------- CONFIG - REPLACE THESE -----------------
TELEGRAM_TOKEN = os.getenv("8069902581:AAHX4eCXdF5Ks7_jo72TeQXHS0zMHu-TYT0")      # <-- put your Telegram bot token
STABILITY_API_KEY = os.getenv("sk-PQSuf9EgudbagYGXPZwsnDBQ9uHbcvc13G7Gufm4H7IqMnv2")    # <-- put your Stability API key
# ----------------------------------------------------------------

STABILITY_ENGINE = "stable-diffusion-xl-1024-v1-0"
STABILITY_API_URL = f"https://api.stability.ai/v1/generation/{STABILITY_ENGINE}/text-to-image"

# Allowed SDXL dims (only these will be offered as buttons)
ALLOWED_DIMS = [
    "1024x1024", "1152x896", "1216x832", "1344x768",
    "1536x640", "640x1536", "768x1344", "832x1216", "896x1152"
]

# Conversation states
ASK_NAME, ASK_PHONE, VERIFY_OTP, DIMENSION_CHOICE, AWAIT_PROMPT, AFTER_IMAGE, EDIT_PROMPT, REGEN_PROMPT = range(8)

# Configure logging: suppress verbose HTTP logs
logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
# requests uses urllib3 internally; above line helps reduce noise.

# ----------------- HELPERS -----------------
def _now_ts():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

def print_user(msg: str):
    """Only print short user-related messages to terminal (no HTTP bodies)."""
    print(f"[{_now_ts()}] {msg}")

def generate_otp() -> str:
    return str(random.randint(1000, 9999))

def compress_jpeg(image_bytes: bytes, max_dim=(2048, 2048), quality=85) -> bytes:
    """Compress image to JPEG to reduce Telegram upload time."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img.thumbnail(max_dim, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        # If Pillow fails, just return original bytes
        return image_bytes

def generate_image_sync(prompt: str, width: int, height: int, cfg_scale: float = 7.0, timeout: int = 60) -> Tuple[bool, Optional[bytes]]:
    """
    Synchronous call to Stability.ai text-to-image endpoint.
    Returns (success, image_bytes) — on failure returns (False, None).
    We intentionally DO NOT print HTTP response content to terminal.
    """
    headers = {
        "Authorization": f"Bearer {STABILITY_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {
        "text_prompts": [{"text": prompt}],
        "cfg_scale": cfg_scale,
        "width": width,
        "height": height,
        "samples": 1,
    }
    try:
        r = requests.post(STABILITY_API_URL, headers=headers, json=payload, timeout=timeout)
    except Exception:
        # network or other error -> failure
        return False, None

    if r.status_code != 200:
        # do not print r.text; just return failure
        return False, None

    try:
        j = r.json()
        if "artifacts" in j and len(j["artifacts"]) > 0 and "base64" in j["artifacts"][0]:
            b64 = j["artifacts"][0]["base64"]
            return True, base64.b64decode(b64)
    except Exception:
        return False, None

    return False, None

async def generate_image_async(prompt: str, width: int, height: int) -> Tuple[bool, Optional[bytes]]:
    loop = asyncio.get_event_loop()
    func = partial(generate_image_sync, prompt, width, height)
    return await loop.run_in_executor(None, func)

async def safe_send_image_by_bot(chat_id: int, context: ContextTypes.DEFAULT_TYPE, image_bytes: bytes, caption: str):
    """Try to send JPEG compressed photo; on failure send as document."""
    try:
        send_bytes = compress_jpeg(image_bytes)
        bio = io.BytesIO(send_bytes)
        bio.name = "image.jpg"
        bio.seek(0)
        await context.bot.send_photo(chat_id=chat_id, photo=bio, caption=caption, parse_mode=ParseMode.MARKDOWN)
        return True
    except Exception:
        try:
            bio = io.BytesIO(image_bytes)
            bio.name = "image.png"
            bio.seek(0)
            await context.bot.send_document(chat_id=chat_id, document=bio, filename="image.png", caption=caption, parse_mode=ParseMode.MARKDOWN)
            return True
        except Exception:
            return False

# ----------------- HANDLER FUNCTIONS -----------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Begin: ask name."""
    context.user_data.clear()
    await update.message.reply_text("👋 Welcome! What's your *name*?", parse_mode=ParseMode.MARKDOWN)
    # Terminal log: bot started (only once ideally, so printed at main start too)
    return ASK_NAME

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data['name'] = name
    await update.message.reply_text("📱 Please enter your mobile number:")
    return ASK_PHONE

async def send_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    otp = generate_otp()
    context.user_data['otp'] = otp
    # Show OTP to user in chat
    await update.message.reply_text(f"🔐 Your OTP is: *{otp}*\n\nPlease type it here to verify.", parse_mode=ParseMode.MARKDOWN)
    # Print user info to terminal (only user-related)
    uid = update.effective_user.id
    print_user(f"[REGISTER] user_id={uid} name={context.user_data.get('name')} phone={phone} otp={otp}")
    return VERIFY_OTP

async def verify_otp_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    entered = update.message.text.strip()
    expected = context.user_data.get('otp')
    uid = update.effective_user.id
    if expected and entered == expected:
        context.user_data['verified'] = True
        print_user(f"[VERIFIED] user_id={uid} name={context.user_data.get('name')} phone={context.user_data.get('phone')}")
        # show dimension buttons (inline)
        buttons = []
        # arrange dims in rows of 3
        row = []
        for i, d in enumerate(ALLOWED_DIMS, 1):
            row.append(InlineKeyboardButton(d, callback_data=f"dim:{d}"))
            if i % 3 == 0:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        await update.message.reply_text("✅ Verified! Choose image dimensions:", reply_markup=InlineKeyboardMarkup(buttons))
        return DIMENSION_CHOICE
    else:
        await update.message.reply_text("❌ Wrong OTP. Please try again.")
        return VERIFY_OTP

async def dimension_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("dim:"):
        await query.message.reply_text("Invalid selection.")
        return DIMENSION_CHOICE
    dim = data.split(":", 1)[1]
    context.user_data['dimension'] = dim
    # Prompt user for prompt
    await query.message.reply_text(f"Dimension set to *{dim}*.\n\nNow send me the text prompt (describe the image):", parse_mode=ParseMode.MARKDOWN)
    return AWAIT_PROMPT

async def receive_prompt_and_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text.strip()
    context.user_data['last_prompt'] = prompt
    dim = context.user_data.get('dimension')
    if not dim:
        await update.message.reply_text("⚠️ Dimension not set. Please /start and choose a dimension.")
        return ConversationHandler.END
    width, height = map(int, dim.split("x"))
    uid = update.effective_user.id
    print_user(f"[PROMPT] user_id={uid} prompt={prompt} dimension={dim}")

    # Inform user
    await context.bot.send_chat_action(chat_id=uid, action=ChatAction.UPLOAD_PHOTO)
    await update.message.reply_text("🎨 Generating your image... this may take a few seconds. Please wait ⏳")

    # generate image in executor
    success, image_bytes = await generate_image_async(prompt, width, height)
    if not success or image_bytes is None:
        # do not print HTTP details; only notify user minimally and print short failure in terminal
        print_user(f"[GENERATE_FAIL] user_id={uid}")
        await update.message.reply_text("❌ Failed to generate image. Try again later or try a different prompt.")
        return AWAIT_PROMPT

    # Save last generated bytes in memory (not saved to gallery unless user chooses)
    context.user_data['last_image'] = image_bytes

    # Send image safely
    caption = f"✨ Here’s your image for:\n`{prompt}`"
    sent_ok = await safe_send_image_by_bot(uid, context, image_bytes, caption)
    if not sent_ok:
        print_user(f"[SEND_FAIL] user_id={uid}")
        await update.message.reply_text("❌ Generated image but failed to send. Try again.")
        return AWAIT_PROMPT

    print_user(f"[GENERATED] user_id={uid}")

    # Show inline action buttons (labels exactly as required)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("edit", callback_data="act:edit"),
         InlineKeyboardButton("save gallery", callback_data="act:save")],
        [InlineKeyboardButton("no gallery", callback_data="act:nosave"),
         InlineKeyboardButton("Generate Again", callback_data="act:regen")]
    ])
    await update.message.reply_text("Choose next action:", reply_markup=kb)
    return AFTER_IMAGE

# Handle after-image inline actions
async def after_image_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data  # like "act:edit"
    action = data.split(":", 1)[1]

    if action == "edit":
        # Ask what to edit
        await query.message.reply_text("✏️ What would you like to edit in the image? Describe the changes:")
        return EDIT_PROMPT

    elif action == "save":
        # Save last_image to gallery
        img = context.user_data.get('last_image')
        if img is None:
            await query.message.reply_text("No image available to save.")
            return AFTER_IMAGE
        gallery = context.user_data.get('gallery', [])
        gallery.append(img)
        # keep last 20
        context.user_data['gallery'] = gallery[-20:]
        print_user(f"[SAVED] user_id={uid} (gallery_count={len(context.user_data['gallery'])})")
        await query.message.reply_text("✅ Saved to gallery.")
        return AFTER_IMAGE

    elif action == "nosave":
        # do nothing (ensure not saved)
        # optionally clear temp last_image
        context.user_data.pop('last_image', None)
        print_user(f"[NO_SAVE] user_id={uid}")
        await query.message.reply_text("Okay — image not saved to gallery.")
        return AFTER_IMAGE

    elif action == "regen":
        # Ask the user "what type image do you want"
        await query.message.reply_text("🔁 What type of image do you want now? Send a new prompt:")
        return REGEN_PROMPT

    else:
        await query.message.reply_text("Unknown action.")
        return AFTER_IMAGE

# Edit prompt handler: user sends edit instructions
async def edit_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    edit_instructions = update.message.text.strip()
    uid = update.effective_user.id
    orig_prompt = context.user_data.get('last_prompt', '')
    if not orig_prompt:
        await update.message.reply_text("Original prompt missing. Please generate an image first.")
        return AWAIT_PROMPT

    # Create a combined prompt for edit. This is text-based editing.
    combined_prompt = f"{orig_prompt}. Edit: {edit_instructions}"
    context.user_data['last_prompt'] = combined_prompt  # update last prompt
    print_user(f"[EDIT_PROMPT] user_id={uid} edit_instructions={edit_instructions}")

    # generate with same dimension
    dim = context.user_data.get('dimension')
    if not dim:
        await update.message.reply_text("Dimension missing. Please /start and choose dimension.")
        return ConversationHandler.END
    width, height = map(int, dim.split("x"))

    await context.bot.send_chat_action(chat_id=uid, action=ChatAction.UPLOAD_PHOTO)
    await update.message.reply_text("🎨 Applying edits... please wait ⏳")

    success, image_bytes = await generate_image_async(combined_prompt, width, height)
    if not success or image_bytes is None:
        print_user(f"[GENERATE_FAIL_EDIT] user_id={uid}")
        await update.message.reply_text("❌ Failed to edit the image. Try again.")
        return AFTER_IMAGE

    # Replace last_image with edited one (temp)
    context.user_data['last_image'] = image_bytes

    # send edited image
    caption = f"✨ Here’s the *edited* image for:\n`{combined_prompt}`"
    sent = await safe_send_image_by_bot(uid, context, image_bytes, caption)
    if not sent:
        print_user(f"[SEND_FAIL_EDIT] user_id={uid}")
        await update.message.reply_text("❌ Edited image generated but failed to send.")
        return AFTER_IMAGE

    print_user(f"[GENERATED_EDIT] user_id={uid}")
    # show same action buttons again
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("edit", callback_data="act:edit"),
         InlineKeyboardButton("save gallery", callback_data="act:save")],
        [InlineKeyboardButton("no gallery", callback_data="act:nosave"),
         InlineKeyboardButton("Generate Again", callback_data="act:regen")]
    ])
    await update.message.reply_text("Choose next action:", reply_markup=kb)
    return AFTER_IMAGE

# Generate again prompt handler
async def regen_prompt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text.strip()
    uid = update.effective_user.id
    dim = context.user_data.get('dimension')
    if not dim:
        await update.message.reply_text("Dimension missing. Start with /start.")
        return ConversationHandler.END
    width, height = map(int, dim.split("x"))
    context.user_data['last_prompt'] = prompt
    print_user(f"[REGEN_PROMPT] user_id={uid} prompt={prompt} dimension={dim}")

    await context.bot.send_chat_action(chat_id=uid, action=ChatAction.UPLOAD_PHOTO)
    await update.message.reply_text("🎨 Generating new image... please wait ⏳")

    success, image_bytes = await generate_image_async(prompt, width, height)
    if not success or image_bytes is None:
        print_user(f"[GENERATE_FAIL_REGEN] user_id={uid}")
        await update.message.reply_text("❌ Failed to generate. Try again.")
        return AWAIT_PROMPT

    context.user_data['last_image'] = image_bytes
    caption = f"✨ Here’s your new image for:\n`{prompt}`"
    sent = await safe_send_image_by_bot(uid, context, image_bytes, caption)
    if not sent:
        print_user(f"[SEND_FAIL_REGEN] user_id={uid}")
        await update.message.reply_text("❌ Generated but failed to send.")
        return AWAIT_PROMPT

    print_user(f"[GENERATED] user_id={uid}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("edit", callback_data="act:edit"),
         InlineKeyboardButton("save gallery", callback_data="act:save")],
        [InlineKeyboardButton("no gallery", callback_data="act:nosave"),
         InlineKeyboardButton("Generate Again", callback_data="act:regen")]
    ])
    await update.message.reply_text("Choose next action:", reply_markup=kb)
    return AFTER_IMAGE

# Gallery command - show saved images
async def cmd_gallery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    gallery = context.user_data.get('gallery', [])
    if not gallery:
        await update.message.reply_text("📭 Your gallery is empty.")
        return
    # send up to last 5 images
    to_send = gallery[-5:]
    media = []
    for b in to_send:
        bio = io.BytesIO(b); bio.name = "g.jpg"; bio.seek(0)
        media.append(InputMediaPhoto(media=bio))
    try:
        await update.message.reply_media_group(media=media)
    except Exception:
        # fallback: send one by one
        for b in to_send:
            bio = io.BytesIO(b); bio.name = "g.jpg"; bio.seek(0)
            await update.message.reply_photo(photo=bio)

# Cancel handler
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. You can begin again with /start.")
    return ConversationHandler.END

# ----------------- MAIN -----------------
def main():
    print_user("Bot started")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler('start', cmd_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_otp)],
            VERIFY_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_otp_handler)],
            DIMENSION_CHOICE: [CallbackQueryHandler(dimension_chosen, pattern=r"^dim:")],
            AWAIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prompt_and_generate)],
            AFTER_IMAGE: [CallbackQueryHandler(after_image_action, pattern=r"^act:")],
            EDIT_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_prompt_handler)],
            REGEN_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, regen_prompt_handler)],
        },
        fallbacks=[CommandHandler('cancel', cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler('gallery', cmd_gallery))

    app.run_polling()

if __name__ == "__main__":
    main()

