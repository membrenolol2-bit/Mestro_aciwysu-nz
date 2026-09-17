"""
token_bot.py — Public Token Pool
==================================
Command: /token
- Responde directamente en el canal (ephemeral)
- Entrega el token como JSON en un code block
- Cooldown de 20 minutos por usuario
"""

import json
import os
import traceback
import discord
from discord import app_commands

from storage import (
    get_public_token,
    check_cooldown,
    set_cooldown,
    format_time,
    seconds_until_expiry,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_GUILD_IDS = [
    int(g.strip())
    for g in os.getenv("ALLOWED_GUILD_IDS", "").split(",")
    if g.strip().isdigit()
]

if not BOT_TOKEN:
    raise ValueError("❌ Missing DISCORD_BOT_TOKEN in Railway Variables")
if not ALLOWED_GUILD_IDS:
    raise ValueError("❌ Missing ALLOWED_GUILD_IDS in Railway Variables")

COOLDOWN_SECONDS = int(os.getenv("PUBLIC_COOLDOWN_SECONDS", str(20 * 60)))
POOL = "public"

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ── /token ────────────────────────────────────────────────────────────────────
@tree.command(name="token", description="Get your session token")
async def token_cmd(interaction: discord.Interaction):
    try:
        if interaction.guild is None or interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message(
                "🚫 **Access Denied** — unauthorized server.", ephemeral=True
            )
            return

        user_id = str(interaction.user.id)

        on_cd, remaining = check_cooldown(user_id, POOL, COOLDOWN_SECONDS)
        if on_cd:
            await interaction.response.send_message(
                f"⏱️ **Cooldown Active** — available in `{format_time(remaining)}`",
                ephemeral=True,
            )
            return

        tokens = get_public_token()
        if not tokens:
            await interaction.response.send_message(
                "⚠️ **No valid token available** — try again in 30 seconds.",
                ephemeral=True,
            )
            return

        set_cooldown(user_id, POOL)

        ttl = seconds_until_expiry(tokens["token"])
        payload = {
            "token": tokens["token"],
            "refresh_token": tokens["refresh_token"],
            "expires_in": ttl,
            "next_use_in": COOLDOWN_SECONDS,
        }

        await interaction.response.send_message(
            f"✅ **Token** | Next use in `{format_time(COOLDOWN_SECONDS)}`\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            ephemeral=True,
        )
        print(f"[PUBLIC] ✅ Token sent to {interaction.user} ({interaction.user.id})")

    except Exception as e:
        try:
            await interaction.response.send_message(
                f"❌ **Error:** `{e}`", ephemeral=True
            )
        except Exception:
            pass
        print(f"[PUBLIC] ❌ Error: {e}")
        traceback.print_exc()


# ── Events ────────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    print(f"\n{'='*55}")
    print(f"✅ [PUBLIC BOT] Connected as: {client.user}")
    print(f"✅ Servers: {ALLOWED_GUILD_IDS}")
    print(f"✅ Cooldown: {format_time(COOLDOWN_SECONDS)} per user")
    print(f"{'='*55}\n")

@client.event
async def on_disconnect():
    print("[PUBLIC BOT] ⚠️ Disconnected")

@client.event
async def on_resumed():
    print("[PUBLIC BOT] ✅ Reconnected")

if __name__ == "__main__":
    print("[PUBLIC BOT] Starting...")
    client.run(BOT_TOKEN)
