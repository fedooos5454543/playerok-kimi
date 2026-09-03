# Playerok -> Telegram bot

Notifications about sales, forwarding messages from buyers, reviews, auto-relisting,
scheduled auto-publishing, reports and management via Telegram commands.

## Warning

Playerok does not provide an official public API. The bot uses an unofficial library
`PlayerokAPI` that works through cookies of an authorized session.

- Cookies periodically expire - update from the browser.
- It is recommended to test on a non-critical account.

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/items` | List of items and prices |
| `/chats` | Active chats with buyers |
| `/reply @user text` | Reply to buyer on Playerok |
| `/report day/week/month` | Sales report |
| `/peakhours` | Peak sales hours and days |
| `/autopub on HH:MM [ids]` | Scheduled auto-publishing |
| `/autopub off` | Disable auto-publishing |
| `/help` | Help |

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill .env with your data
python main.py
```

## .env Setup

1. **Telegram**: get token from @BotFather, ID from @userinfobot
2. **Playerok**: login in browser, copy Cookie and User-Agent from F12 -> Network
3. **AUTO_RELIST**: `true` for auto-relisting sold items
