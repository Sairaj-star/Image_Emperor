# Telegram Image Bot (Stability.ai)

A Telegram bot that generates AI images using Stability.ai API.

## Features
- User registration (name, phone, OTP verification)
- Select image dimensions (only valid SDXL sizes)
- Generate images from text prompts
- After-image actions:
  - Edit (apply text edits)
  - Save to gallery
  - Skip saving
  - Generate again
- Gallery access (`/gallery`)
- Multi-user safe
- Clean logging (only user info, no HTTP logs)

## Setup
1. Clone repo
2. Create virtual environment & install dependencies:
   ```bash
   pip install -r requirements.txt
