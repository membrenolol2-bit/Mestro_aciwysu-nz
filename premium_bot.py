"""
premium_bot.py — Premium Token Pool
=====================================
Commands:
  /get-premium-token  → buyers con rol o whitelist
  /add-premium-token  → admins — agrega token al pool
  /add-premium-user   → admins — whitelist manual
  /status             → todos — stats en vivo
"""

import json
import os
import traceback
import discord
from discord import app_commands

from storage import (
    add_premium_token,
    pop_premium_token,
    premium_pool_status,
    is_premium_user,
    add_premium_user,
    increment_premium_uses,
    check_cooldown,
    set_cooldown,
    format_time,
    global_status,
    seconds_until_expiry,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("PREMIUM_BOT_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
ALLOWED_GUILD_IDS = [
    int(g.strip())
    for g in os.getenv("ALLOWED_GUILD_IDS", "").split(",")
    if g.strip().isdigit()
]
PREMIUM_ROLE_ID_STR = os.getenv("PREMIUM_ROLE_ID", "")
ADMIN_ROLE_ID_STR   = os.getenv("ADMIN_ROLE_ID", "")
PREMIUM_ROLE_ID = int(PREMIUM_ROLE_ID_STR) if PREMIUM_ROLE_ID_STR.isdigit() else None
ADMIN_ROLE_ID   = int(ADMIN_ROLE_ID_STR)   if ADMIN_ROLE_ID_STR.isdigit()   else None
PREMIUM_COOLDOWN = int(os.getenv("PREMIUM_COOLDOWN_SECONDS", str(60 * 60)))
POOL = "premium"

if not BOT_TOKEN:
    raise ValueError("❌ Missing PREMIUM_BOT_TOKEN / DISCORD_BOT_TOKEN")


# ── Helpers ───────────────────────────────────────────────────────────────────
def has_premium_access(member: discord.Member, user_id: str) -> bool:
    if PREMIUM_ROLE_ID and any(r.id == PREMIUM_ROLE_ID for r in member.roles):
        return True
    return is_premium_user(user_id)

def has_admin_access(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if ADMIN_ROLE_ID and any(r.id == ADMIN_ROLE_ID for r in member.roles):
        return True
    return False


# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ── /get-premium-token ────────────────────────────────────────────────────────
@tree.command(name="get-premium-token", description="Get a premium session token (buyers only)")
async def get_premium_token_cmd(interaction: discord.Interaction):
    try:
        if interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if member is None and interaction.guild:
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except discord.NotFound:
                member = None

        if not member or not has_premium_access(member, user_id):
            role_hint = f"<@&{PREMIUM_ROLE_ID}>" if PREMIUM_ROLE_ID else "the premium role"
            await interaction.response.send_message(
                f"💎 **Premium Required** — only buyers with {role_hint} can use this.",
                ephemeral=True,
            )
            return

        on_cd, remaining = check_cooldown(user_id, POOL, PREMIUM_COOLDOWN)
        if on_cd:
            await interaction.response.send_message(
                f"⏱️ **Cooldown Active** — available in `{format_time(remaining)}`",
                ephemeral=True,
            )
            return

        token_entry = pop_premium_token()
        if not token_entry:
            await interaction.response.send_message(
                "⚠️ **Premium pool is empty** — ask an admin to run `/add-premium-token`.",
                ephemeral=True,
            )
            return

        set_cooldown(user_id, POOL)
        increment_premium_uses(user_id)

        ttl = seconds_until_expiry(token_entry["token"])
        payload = {
            "token": token_entry["token"],
            "refresh_token": token_entry["refresh_token"],
            "expires_in": ttl,
            "tier": "premium",
            "next_use_in": PREMIUM_COOLDOWN,
        }

        await interaction.response.send_message(
            f"💎 **Premium Token** | Next use in `{format_time(PREMIUM_COOLDOWN)}`\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            ephemeral=True,
        )
        print(f"[PREMIUM] ✅ Token sent to {interaction.user} ({user_id})")

    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[PREMIUM] ❌ Error in /get-premium-token: {e}")
        traceback.print_exc()


# ── /add-premium-token ────────────────────────────────────────────────────────
@tree.command(name="add-premium-token", description="[ADMIN] Add a token to the premium pool")
@app_commands.describe(token="JWT bearer token", refresh_token="JWT refresh token")
async def add_premium_token_cmd(interaction: discord.Interaction, token: str, refresh_token: str):
    try:
        if interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if member is None and interaction.guild:
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except discord.NotFound:
                member = None
        if not member or not has_admin_access(member):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        if not token.startswith("ey") or not refresh_token.startswith("ey"):
            await interaction.response.send_message(
                "❌ **Invalid tokens** — must be JWT strings starting with `ey...`",
                ephemeral=True,
            )
            return

        ttl = seconds_until_expiry(token)
        if ttl < 60:
            await interaction.response.send_message(
                f"❌ **Token already expired** ({ttl}s remaining) — use a fresh one.",
                ephemeral=True,
            )
            return

        new_size = add_premium_token(token, refresh_token)
        await interaction.response.send_message(
            f"✅ **Token added to premium pool**\n"
            f"```json\n{json.dumps({'pool_size': new_size, 'token_expires_in': ttl}, indent=2)}\n```",
            ephemeral=True,
        )
        print(f"[PREMIUM] ➕ Token added by {interaction.user} — pool now {new_size}")

    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── /add-premium-user ─────────────────────────────────────────────────────────
@tree.command(name="add-premium-user", description="[ADMIN] Grant premium access to a user")
@app_commands.describe(user="Discord user to grant premium access")
async def add_premium_user_cmd(interaction: discord.Interaction, user: discord.Member):
    try:
        if interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id) if interaction.guild else None
        if member is None and interaction.guild:
            try:
                member = await interaction.guild.fetch_member(interaction.user.id)
            except discord.NotFound:
                member = None
        if not member or not has_admin_access(member):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True)
            return

        add_premium_user(str(user.id), str(interaction.user.id))
        await interaction.response.send_message(
            f"✅ **{user.mention} now has premium access** — can use `/get-premium-token`.",
            ephemeral=True,
        )
        print(f"[PREMIUM] ✅ {interaction.user} granted premium to {user} ({user.id})")

    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── /status ───────────────────────────────────────────────────────────────────
@tree.command(name="status", description="View live token pool status")
async def status_cmd(interaction: discord.Interaction):
    try:
        if interaction.guild_id not in ALLOWED_GUILD_IDS:
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        s = global_status()
        pub  = s["public"]
        prem = s["premium"]
        don  = s["donated"]

        payload = {
            "public_pool": {
                "status": "active" if pub["valid"] else "expired",
                "expires_in": pub["expires_in"],
            },
            "premium_pool": {
                "valid_tokens": prem["valid"],
                "expired_tokens": prem["expired"],
                "total_tokens": prem["total"],
            },
            "donated_tokens": {
                "users_with_gifts": don["users_with_donated"],
                "total_donated": don["total_donated_tokens"],
            },
            "premium_users_whitelisted": s["premium_users"],
        }

        await interaction.response.send_message(
            f"📊 **System Status**\n"
            f"```json\n{json.dumps(payload, indent=2)}\n```",
            ephemeral=False,
        )
        print(f"[STATUS] Checked by {interaction.user}")

    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ── Events ────────────────────────────────────────────────────────────────────
@client.event
async def on_ready():
    await tree.sync()
    prem = premium_pool_status()
    print(f"\n{'='*55}")
    print(f"✅ [PREMIUM BOT] Connected as: {client.user}")
    print(f"✅ Premium role ID: {PREMIUM_ROLE_ID}")
    print(f"✅ Admin role ID:   {ADMIN_ROLE_ID}")
    print(f"✅ Premium pool: {prem['valid']} valid / {prem['total']} total")
    print(f"{'='*55}\n")

@client.event
async def on_disconnect():
    print("[PREMIUM BOT] ⚠️ Disconnected")

@client.event
async def on_resumed():
    print("[PREMIUM BOT] ✅ Reconnected")

if __name__ == "__main__":
    print("[PREMIUM BOT] Starting...")
    client.run(BOT_TOKEN)
