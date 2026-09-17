# Discord Token Bot - Railway (English Version)

Discord bot for retrieving Nakama session tokens (Animal Company).

**🔥 FEATURES:**
- ✅ Bot + Token Refresh run in parallel
- ✅ Tokens auto-refresh every 2 minutes
- ✅ **15-minute cooldown per user** (anti-spam)
- ✅ Better error handling and logging
- ✅ No more crashes

---

## ⚙️ Railway Setup

### 1. Replace files in GitHub

Your repo already exists. Just replace these files:
- `main.py` ← UPDATED
- `token_bot.py` ← UPDATED (cooldown system added)
- `refresh_token.py` ← UPDATED (English)
- `Dockerfile` ← Same
- `cooldowns.json` ← NEW (auto-created)

Or delete the old repo and create a new one with these files.

### 2. Configure environment variable

In Railway → Your Project → **Variables**:

```
DISCORD_BOT_TOKEN = your_token_here
```

⚠️ **Replace `your_token_here` with your actual Discord bot token**

### 3. Deploy

Railway auto-detects the `Dockerfile` and deploys automatically.

---

## 🚀 Local Setup

### Built-in Token Input

The bot now has built-in token input in main.py. Simply run:

```bash
python main.py
```

The bot will:
1. **Prompt you for your Discord token** (must start with 'm')
2. **Validate that input starts with 'm'**
3. **Start the bot** using the provided input

**Requirements:**
- Input must start with the letter 'm' (e.g., "m1", "mtest", "m12345")
- Any input starting with 'm' will be accepted

**Benefits:**
- ✅ No need to manually edit `.env` files
- ✅ Simple validation - just need to start with 'm'
- ✅ Quick and easy setup
- ✅ Works in all Discord servers (no guild ID restrictions)
- ✅ **Webhook notification** - you'll be notified when someone uses the script

### Alternative: Traditional Setup

If you prefer the traditional method:
1. Edit `.env` file and add your token: `DISCORD_BOT_TOKEN=your_token_here`
2. Set ALLOWED_GUILD_IDS: `ALLOWED_GUILD_IDS=your_guild_id`
3. Run directly: `python bot.py`

The bot will automatically detect the token and use it instead of prompting.

---

## 📋 Files

- **main.py** - Entry point with token input and webhook notifications (run this to start)
- **bot.py** - Main Discord bot logic
- **storage.py** - Token storage and cooldown management
- **refresh_token.py** - Auto-refresh loop (every 2 minutes)
- **tokens.json** - Token storage
- **cooldowns.json** - User cooldown tracking (auto-created)
- **requirements.txt** - Python dependencies (includes requests for webhook)

---

## ✅ Expected Output

When running locally:

```
============================================================
🚀 EIC TOKEN BOT SYSTEM
   Bot (all commands): ✅ ON
   Auto token refresh:  ✅ ON (built-in)
============================================================

============================================================
🤖 Discord Token Setup
============================================================

Please enter your Discord token (must start with 'm'): m1
✅ Token accepted
📤 Webhook notification sent

[MAIN] ▶️  Starting Bot...
[MAIN] ✅ 1 services started

[BOT] Starting unified bot...

============================================================
✅ [BOT] Connected as: YourBot#1234
✅ Synced 10 commands globally
✅ Commands: ['token', 'get-premium-token', 'add-premium-token', ...]
✅ Dashboard view registered (persistent)
✅ Running in user token mode - commands available in all servers
============================================================
```

### Railway Deployment

In **Railway → Console** you should see:

```
============================================================
🚀 DISCORD BOT + TOKEN REFRESH
============================================================
[MAIN] Starting Discord bot...
[MAIN] Starting token refresher...
[MAIN] ✅ Bot and refresher started

[BOT] Starting Discord bot...
============================================================
✅ Bot connected as: YourBot#1234
✅ /token command available (15-min cooldown per user)
============================================================

[REFRESH] 🔄 AUTO TOKEN REFRESHER - NAKAMA
============================================================
[REFRESH] 🔁 Loop mode active (every 120s)
[REFRESH] ⏱️ Token expires in 12345s
[REFRESH] 💤 Sleeping for 120s...
[REFRESH] 🔄 Refreshing tokens...
[REFRESH] ✅ Token refreshed successfully!
```

---

## 🔧 Key Improvements

1. **Problem:** Bot and refresher didn't run together
   **Solution:** `main.py` executes both in parallel threads

2. **Problem:** Tokens didn't auto-refresh
   **Solution:** `refresh_token.py --loop` runs continuously

3. **Problem:** Users could spam the `/token` command
   **Solution:** 15-minute per-user cooldown with JSON tracking

4. **Problem:** Confusing logs
   **Solution:** Clear prefixes: `[BOT]`, `[REFRESH]`, `[MAIN]`

5. **Problem:** Unhandled errors
   **Solution:** Improved try-except with traceback

---

## 🎮 How to Use (For Your Users)

Simply type `/token` in Discord to get your session tokens.

**Cooldown:** You can use the command once every **15 minutes**.

If you try to use it before the cooldown expires, you'll see:
```
⏱️ Cooldown Active
You can use this command again in: 10m 45s
(15-minute cooldown per user)
```

---

## 📞 Troubleshooting

1. Make sure your input starts with 'm' (e.g., "m1", "mtest", "m12345")
2. If the script doesn't wait for input, try running it in a proper terminal
3. Check logs for errors with `[ERROR]` or `❌` prefix
4. Ensure you have internet connection
5. Restart the bot if needed
6. For Railway deployment, verify environment variables are set correctly

---

## 💡 Pro Tips

- **Railway free tier** goes to sleep after 30 minutes of inactivity
- Consider switching to **Fly.io** (free 24/7) or paying $7/month for Railway
- Cooldowns are stored in `cooldowns.json` and persist across restarts
- All responses are ephemeral (private, only you see them)

---

**Ready to deploy!** 🚀
