"""
bot.py — Unified Discord Bot (single app, single CommandTree)
================================================================
Pool commands + per-user nakama token refresh system.
All commands live under ONE discord.Client + ONE CommandTree.

Pool commands:
  /token                → public pool (everyone, rotating env accounts)
  /get-premium-token    → premium pool (role/whitelist)
  /add-premium-token    → [ADMIN] add token to premium pool
  /add-premium-user     → [ADMIN] whitelist a user for premium
  /status               → live pool stats
  /donate-token         → [ADMIN] gift a token to a user
  /my-tokens            → user's gifted tokens
  /revoke-token         → [ADMIN] revoke a user's gifted tokens
  /view-tokens          → [ADMIN] view all available tokens

Refresh system (from bot2):
  /refresh              → paste a token, save + refresh it (numbered)
  /get-refresh          → refresh a saved numbered token (10min cooldown)
  /check                → validate a JWT and show claims
  /my-saved-tokens      → list your saved tokens
  /panel                → [ADMIN] post the interactive panel
  background task       → auto-refresh every 30 min via Nakama
"""

import io
import sys
import json
import uuid
import asyncio
import traceback
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone, timedelta

import aiohttp
import discord
from discord import app_commands, ui

try:
    from zoneinfo import ZoneInfo
    CST = ZoneInfo("America/Chicago")
except Exception:
    CST = timezone(timedelta(hours=-5))

# Fix Windows console encoding BEFORE anything else
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


# ── load .env manually (no dotenv dependency, matches bot2) ──────────────────
def load_env():
    env_file = ".env"
    if not os.path.exists(env_file):
        return
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if not os.environ.get(k.strip()):
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

import os  # noqa: E402  — kept after load_env def only for readability
load_env()


# ── storage import (your existing module) ────────────────────────────────────
from storage import (
    get_public_token,
    get_public_token_with_fallback,
    check_cooldown,
    set_cooldown,
    format_time,
    seconds_until_expiry,
    add_premium_token,
    pop_premium_token,
    premium_pool_status,
    is_premium_user,
    add_premium_user,
    increment_premium_uses,
    global_status,
    add_donated,
    get_donated,
    revoke_donated,
    is_expired,
    _read,
    _write,
    COOLDOWNS_FILE,
    get_premium_pool,
    get_env_accounts,
    reset_all_cooldowns,
    set_permanent_cooldown,
    remove_permanent_cooldown,
    get_rotating_token,
    get_public_token_raw,
)


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_USER_TOKEN") or os.getenv("DISCORD_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Missing Discord token — set DISCORD_BOT_TOKEN / DISCORD_USER_TOKEN / DISCORD_TOKEN")

raw_guild_ids = os.getenv("ALLOWED_GUILD_IDS", "")
ALLOWED_GUILD_IDS = [int(g.strip()) for g in raw_guild_ids.split(",") if g.strip().isdigit()]
print(f"[CONFIG] Parsed guild IDs: {ALLOWED_GUILD_IDS}")

PUBLIC_COOLDOWN_SECONDS = int(os.getenv("PUBLIC_COOLDOWN_SECONDS", str(20 * 60)))

_t1_role = os.getenv("PREMIUM_TIER1_ROLE_ID", "")
_t2_role = os.getenv("PREMIUM_TIER2_ROLE_ID", "")
PREMIUM_TIER1_ROLE_ID  = int(_t1_role) if _t1_role.isdigit() else None
PREMIUM_TIER2_ROLE_ID  = int(_t2_role) if _t2_role.isdigit() else None
PREMIUM_TIER1_COOLDOWN = int(os.getenv("PREMIUM_TIER1_COOLDOWN", str(5 * 60)))
PREMIUM_TIER2_COOLDOWN = int(os.getenv("PREMIUM_TIER2_COOLDOWN", str(13 * 60)))

_legacy_role = os.getenv("PREMIUM_ROLE_ID", "")
if _legacy_role.isdigit() and PREMIUM_TIER2_ROLE_ID is None:
    PREMIUM_TIER2_ROLE_ID = int(_legacy_role)

ADMIN_ROLE_ID_STR = os.getenv("ADMIN_ROLE_ID", "")
ADMIN_ROLE_ID     = int(ADMIN_ROLE_ID_STR) if ADMIN_ROLE_ID_STR.isdigit() else None
ADMIN_USER_IDS    = {s.strip() for s in os.getenv("ADMIN_USER_IDS", "").split(",") if s.strip().isdigit()}

# ── Refresh-system config (from bot2) ────────────────────────────────────────
SUPABASE_URL      = os.getenv("SUPABASE_URL", "https://enkuceyfgjyzdvmsqpul.supabase.co")
SUPABASE_KEY      = os.getenv("SUPABASE_SERVICE_KEY", "")
GUILD_ID          = int(os.getenv("PANEL_GUILD_ID", "1543807650939277472"))
SUPPORTER_ROLE    = int(os.getenv("SUPPORTER_ROLE_ID", "1548720942752993400"))
PANEL_CHANNEL     = int(os.getenv("PANEL_CHANNEL_ID", "1547010774877348061"))
LOG_CHANNEL       = int(os.getenv("LOG_CHANNEL_ID", "1548825576972750889"))
API_URL           = os.getenv("API_URL", "https://steam-token-service.fly.dev")
CLIENT_KEY        = os.getenv("CLIENT_KEY", "xerox-tokens-001")
SERVER_KEY        = os.getenv("SERVER_KEY", "")

NAKAMA_URL        = "https://animalcompany.us-east1.nakamacloud.io"
NAKAMA_BASIC      = "Basic NlVSdVRTbERLS2ZZYnVEVzo="

GET_REFRESH_COOLDOWN  = 10 * 60
AUTO_REFRESH_INTERVAL = 30 * 60

user_last_gen:         dict = {}
user_last_get_refresh: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
#  SUPABASE
# ══════════════════════════════════════════════════════════════════════════════
async def sb_get(path: str) -> list:
    if not SUPABASE_KEY:
        return []
    async with aiohttp.ClientSession() as s:
        async with s.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey": SUPABASE_KEY,
                "Accept": "application/json",
            },
        ) as r:
            return await r.json()


async def sb_upsert(table: str, data: dict):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "apikey": SUPABASE_KEY,
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=data,
        ) as r:
            return r.status


async def sb_delete(table: str, path: str):
    async with aiohttp.ClientSession() as s:
        async with s.delete(
            f"{SUPABASE_URL}/rest/v1/{table}?{path}",
            headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY},
        ) as r:
            return r.status


async def get_user_tokens(discord_id: str) -> list:
    return await sb_get(f"user_tokens?discord_id=eq.{discord_id}&order=token_number.asc")


async def save_user_token(discord_id: str, token_number: int, bearer: str, refresh: str, expires_at: str):
    await sb_upsert("user_tokens", {
        "discord_id":    discord_id,
        "token_number":  token_number,
        "bearer":        bearer,
        "refresh_token": refresh,
        "saved_at":      datetime.now(timezone.utc).isoformat(),
        "expires_at":    expires_at,
    })


async def delete_user_token(discord_id: str, token_number: int):
    await sb_delete("user_tokens", f"discord_id=eq.{discord_id}&token_number=eq.{token_number}")


async def get_next_token_number(discord_id: str) -> int:
    tokens = await get_user_tokens(discord_id)
    if not tokens:
        return 1
    used = {t["token_number"] for t in tokens}
    n = 1
    while n in used:
        n += 1
    return n


# ══════════════════════════════════════════════════════════════════════════════
#  NAKAMA
# ══════════════════════════════════════════════════════════════════════════════
async def nakama_refresh(refresh_token: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{NAKAMA_URL}/v2/account/session/refresh",
                headers={"Authorization": NAKAMA_BASIC, "Content-Type": "application/json"},
                json={"token": refresh_token},
            ) as r:
                if r.ok:
                    return await r.json()
                return None
    except Exception:
        return None


async def nakama_validate(token: str) -> bool:
    result = await nakama_refresh(token)
    if result and result.get("token"):
        return True
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{NAKAMA_URL}/v2/account",
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                return r.ok
    except Exception:
        return False


def decode_jwt(jwt: str) -> dict:
    try:
        import base64
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def get_jwt_exp(jwt: str) -> int:
    return decode_jwt(jwt).get("exp", 0)


def jwt_is_expired(jwt: str) -> bool:
    exp = get_jwt_exp(jwt)
    return exp < datetime.now(timezone.utc).timestamp()


def format_cooldown(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m {s}s" if m > 0 else f"{s}s"


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN API (supporter generate)
# ══════════════════════════════════════════════════════════════════════════════
async def fetch_token_from_api(discord_id: str, username: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{API_URL}/client/token",
            headers={
                "Content-Type": "application/json",
                "x-client-key": CLIENT_KEY,
                "x-server-key": SERVER_KEY or "",
            },
            json={
                "discord_id":       discord_id,
                "discord_username": username,
                "ip":               "discord-bot",
                "token_id":         str(uuid.uuid4()),
                "expires_at":       (datetime.now(timezone.utc) + timedelta(minutes=25)).isoformat(),
            },
        ) as resp:
            data = await resp.json()
            if not resp.ok:
                raise Exception(data.get("error", "Unknown error"))
            return data


# ══════════════════════════════════════════════════════════════════════════════
#  DM SENDER
# ══════════════════════════════════════════════════════════════════════════════
async def send_token_dm(user: discord.User, bearer: str, refresh: str,
                        token_number: int | None = None, label: str = "Token"):
    exp_ts  = get_jwt_exp(bearer)
    exp_str = datetime.fromtimestamp(exp_ts, CST).strftime("%I:%M:%S %p CST") if exp_ts else "Unknown"

    title = f"🔑 Your {label}"
    if token_number is not None:
        title += f" #{token_number}"

    embed = discord.Embed(title=title, color=0x2b2d31)
    embed.add_field(name="Bearer:", value=f"```{bearer}```", inline=False)
    embed.add_field(name="━━━━━━━━━━━━━━━━━━━━━━", value="\u200b", inline=False)
    embed.add_field(name="Refresh:", value=f"```{refresh}```", inline=False)
    embed.set_footer(text=f"Expires: {exp_str}")

    try:
        await user.send(embed=embed)
        return True
    except discord.Forbidden:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
async def resolve_member(interaction: discord.Interaction) -> discord.Member | None:
    if isinstance(interaction.user, discord.Member) and interaction.user.roles:
        return interaction.user
    if interaction.guild_id is None:
        return None
    guild = client.get_guild(interaction.guild_id)
    if guild is None:
        try:
            guild = await client.fetch_guild(interaction.guild_id)
        except (discord.Forbidden, discord.HTTPException):
            return None
    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except (discord.NotFound, discord.HTTPException):
            return None
    return member


async def has_admin_access(interaction: discord.Interaction) -> bool:
    if str(interaction.user.id) in ADMIN_USER_IDS:
        return True
    member = await resolve_member(interaction)
    if member is None:
        return False
    if isinstance(member, discord.Member):
        try:
            if member.guild_permissions.administrator:
                return True
        except Exception:
            pass
        if ADMIN_ROLE_ID and any(r is not None and r.id == ADMIN_ROLE_ID for r in (member.roles or [])):
            return True
    return False


def get_premium_tier(member: discord.Member, user_id: str) -> tuple[int, int] | None:
    role_ids = {r.id for r in (member.roles or []) if r is not None}
    if PREMIUM_TIER1_ROLE_ID and PREMIUM_TIER1_ROLE_ID in role_ids:
        return 1, PREMIUM_TIER1_COOLDOWN
    if PREMIUM_TIER2_ROLE_ID and PREMIUM_TIER2_ROLE_ID in role_ids:
        return 2, PREMIUM_TIER2_COOLDOWN
    if is_premium_user(user_id):
        return 2, PREMIUM_TIER2_COOLDOWN
    return None


ALLOWED_GUILD_IDS_STR = {str(g) for g in ALLOWED_GUILD_IDS}


def guild_allowed(interaction: discord.Interaction) -> bool:
    if not ALLOWED_GUILD_IDS:
        return True
    return str(interaction.guild_id) in ALLOWED_GUILD_IDS_STR


# ══════════════════════════════════════════════════════════════════════════════
#  BOT SETUP
# ══════════════════════════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ══════════════════════════════════════════════════════════════════════════════
#  AUTO REFRESH (background task)
# ══════════════════════════════════════════════════════════════════════════════
async def auto_refresh_all_tokens():
    while True:
        await asyncio.sleep(AUTO_REFRESH_INTERVAL)
        print(f"[auto-refresh] running at {datetime.now(CST).strftime('%H:%M:%S')}")
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"{SUPABASE_URL}/rest/v1/user_tokens"
                    f"?select=discord_id,token_number,refresh_token,expires_at",
                    headers={"Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY},
                ) as r:
                    tokens = await r.json()

            for t in tokens:
                discord_id   = t["discord_id"]
                token_number = t["token_number"]
                refresh      = t.get("refresh_token", "")
                if not refresh:
                    continue

                result = await nakama_refresh(refresh)
                if result and result.get("token"):
                    new_bearer  = result["token"]
                    new_refresh = result.get("refresh_token", refresh)
                    new_exp     = get_jwt_exp(new_bearer)
                    new_exp_str = datetime.fromtimestamp(new_exp, timezone.utc).isoformat() if new_exp else ""
                    await save_user_token(discord_id, token_number, new_bearer, new_refresh, new_exp_str)
                    print(f"[auto-refresh] refreshed #{token_number} for {discord_id}")
                else:
                    await delete_user_token(discord_id, token_number)
                    print(f"[auto-refresh] deleted expired #{token_number} for {discord_id}")
        except Exception as e:
            print(f"[auto-refresh] error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  REFRESH MODALS + VIEWS
# ══════════════════════════════════════════════════════════════════════════════
class PasteTokenModal(ui.Modal, title="Paste Your Token"):
    token_input = ui.TextInput(
        label="Bearer or Refresh Token",
        style=discord.TextStyle.paragraph,
        placeholder="Paste your eyJhbGci... token here",
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        token = self.token_input.value.strip()
        discord_id = str(interaction.user.id)

        if not token.startswith("eyJ"):
            await interaction.followup.send("❌ Invalid token format.", ephemeral=True)
            return

        if jwt_is_expired(token):
            await interaction.followup.send("❌ This token is expired. Discarding.", ephemeral=True)
            return

        result = await nakama_refresh(token)
        if result and result.get("token"):
            bearer  = result["token"]
            refresh = result.get("refresh_token", token)
        else:
            valid = await nakama_validate(token)
            if not valid:
                await interaction.followup.send("❌ Token failed validation. Discarding.", ephemeral=True)
                return
            bearer  = token
            refresh = token

        token_number = await get_next_token_number(discord_id)
        exp_ts  = get_jwt_exp(bearer)
        exp_str = datetime.fromtimestamp(exp_ts, timezone.utc).isoformat() if exp_ts else ""
        await save_user_token(discord_id, token_number, bearer, refresh, exp_str)

        sent = await send_token_dm(interaction.user, bearer, refresh,
                                   token_number=token_number, label="Refreshed Token")
        if sent:
            await interaction.followup.send(
                f"✅ Token saved as **#{token_number}** and sent to your DMs!", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"✅ Token saved as **#{token_number}**. Enable DMs to receive it.", ephemeral=True
            )


class GetRefreshModal(ui.Modal, title="Get Refreshed Token"):
    number = ui.TextInput(
        label="Token Number",
        placeholder="Enter token number (e.g. 1, 2, 3...)",
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        discord_id = str(interaction.user.id)

        try:
            token_number = int(self.number.value.strip())
        except ValueError:
            await interaction.followup.send("❌ Enter a valid number.", ephemeral=True)
            return

        rl_key = f"{discord_id}:{token_number}"
        now    = datetime.now(timezone.utc)
        if rl_key in user_last_get_refresh:
            elapsed = (now - user_last_get_refresh[rl_key]).total_seconds()
            if elapsed < GET_REFRESH_COOLDOWN:
                await interaction.followup.send(
                    f"⏰ Cooldown. Try again in **{format_cooldown(GET_REFRESH_COOLDOWN - elapsed)}**.",
                    ephemeral=True,
                )
                return

        rows = await sb_get(f"user_tokens?discord_id=eq.{discord_id}&token_number=eq.{token_number}")
        if not rows:
            await interaction.followup.send(f"❌ No token saved as **#{token_number}**.", ephemeral=True)
            return

        t       = rows[0]
        refresh = t.get("refresh_token", "")
        if not refresh:
            await interaction.followup.send(f"❌ No refresh token for **#{token_number}**.", ephemeral=True)
            return

        result = await nakama_refresh(refresh)
        if not result or not result.get("token"):
            await delete_user_token(discord_id, token_number)
            await interaction.followup.send(
                f"❌ Token **#{token_number}** is invalid or expired. Deleted.", ephemeral=True
            )
            return

        bearer      = result["token"]
        new_refresh = result.get("refresh_token", refresh)
        exp_ts      = get_jwt_exp(bearer)
        exp_str     = datetime.fromtimestamp(exp_ts, timezone.utc).isoformat() if exp_ts else ""

        await save_user_token(discord_id, token_number, bearer, new_refresh, exp_str)
        user_last_get_refresh[rl_key] = now

        sent = await send_token_dm(interaction.user, bearer, new_refresh,
                                   token_number=token_number, label="Refreshed Token")
        if sent:
            await interaction.followup.send(
                f"✅ Token **#{token_number}** refreshed and sent to your DMs!", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"✅ Token **#{token_number}** refreshed. Enable DMs to receive it.", ephemeral=True
            )


class CheckTokenModal(ui.Modal, title="Check Token"):
    token = ui.TextInput(
        label="Paste your JWT token",
        style=discord.TextStyle.paragraph,
        placeholder="eyJhbGci...",
    )

    async def on_submit(self, interaction: discord.Interaction):
        jwt = self.token.value.strip()
        try:
            claims   = decode_jwt(jwt)
            exp      = claims.get("exp", 0)
            now      = datetime.now(timezone.utc).timestamp()
            uid      = claims.get("uid", "N/A")
            usn      = claims.get("usn", "N/A")
            device   = claims.get("vrs", {}).get("deviceID", "N/A")
            is_exp   = exp < now
            exp_str  = datetime.fromtimestamp(exp, CST).strftime("%I:%M:%S %p CST")
            secs_left = int(exp - now)

            if is_exp:
                status, color = "❌ Expired", 0xed4245
            elif secs_left < 300:
                status, color = f"⚠️ Expiring soon ({format_cooldown(secs_left)} left)", 0xfaa61a
            else:
                h = secs_left // 3600; m = (secs_left % 3600) // 60
                status, color = f"✅ Valid ({h}h {m}m remaining)", 0x3ba55c

            embed = discord.Embed(title="🔍 Token Check", color=color)
            embed.add_field(name="Status",    value=status,           inline=False)
            embed.add_field(name="Expires",   value=exp_str,          inline=True)
            embed.add_field(name="User ID",   value=f"`{uid}`",       inline=True)
            embed.add_field(name="Username",  value=f"`{usn}`",       inline=True)
            embed.add_field(name="Device ID", value=f"`{device}`",    inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            await interaction.response.send_message("❌ Invalid token — not a valid JWT.", ephemeral=True)


class GetRefreshView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.button(label="Enter Token Number", style=discord.ButtonStyle.primary)
    async def enter_number(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(GetRefreshModal())


# ══════════════════════════════════════════════════════════════════════════════
#  LOG
# ══════════════════════════════════════════════════════════════════════════════
async def log_generate(interaction: discord.Interaction, bearer: str, gen_type: str):
    try:
        guild   = interaction.client.get_guild(GUILD_ID)
        channel = guild.get_channel(LOG_CHANNEL) if guild else None
        if not channel:
            return
        embed = discord.Embed(
            title=f"🔑 Token Generated ({gen_type})",
            color=0x5865f2,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User",  value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
        embed.add_field(name="Type",  value=gen_type, inline=True)
        embed.add_field(name="Token", value=f"`{bearer[:20]}...`", inline=False)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[log] failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  /token — public pool
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="token", description="Get your session token")
async def token_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Access denied — unauthorized server.", ephemeral=True)
            return

        tokens = get_rotating_token()
        if not tokens:
            await interaction.response.send_message(
                "❌ **No valid token available** — add tokens to .env", ephemeral=True
            )
            return

        ttl = seconds_until_expiry(tokens["token"])
        payload = {
            "token":         tokens["token"],
            "refresh_token": tokens["refresh_token"],
            "expires_in":    ttl,
            "_note":         "Made by Forest and Mestro_ac",
        }
        account_num  = tokens.get("account")
        account_note = f" (account {account_num})" if account_num else ""

        json_bytes = json.dumps(payload, indent=2).encode("utf-8")
        file = discord.File(fp=io.BytesIO(json_bytes), filename="token.json")

        await interaction.response.send_message(
            f"✅ **Token**{account_note}\n```json\n{json.dumps(payload, indent=2)}\n```",
            file=file, ephemeral=True,
        )
        print(f"[PUBLIC] ✅ Token sent to {interaction.user} ({interaction.user.id}){account_note}")
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[PUBLIC] ❌ Error: {e}"); traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  /get-premium-token
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="get-premium-token", description="Get a premium session token (buyers only)")
async def get_premium_token_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True)
            return

        user_id = str(interaction.user.id)
        member  = await resolve_member(interaction)

        member_roles = [r.id for r in (member.roles or []) if r is not None] if member else []
        print(f"[PREMIUM] user={interaction.user} ({user_id}) | "
              f"member={'OK' if member else 'NONE'} | "
              f"role_ids={member_roles} | whitelist={is_premium_user(user_id)}")

        if not member:
            await interaction.response.send_message(
                "❌ Member lookup failed — bot may be missing **Server Members Intent**.",
                ephemeral=True,
            )
            return

        tier_info = get_premium_tier(member, user_id)
        if tier_info is None:
            hints = []
            if PREMIUM_TIER1_ROLE_ID:
                hints.append(f"<@&{PREMIUM_TIER1_ROLE_ID}>")
            if PREMIUM_TIER2_ROLE_ID:
                hints.append(f"<@&{PREMIUM_TIER2_ROLE_ID}>")
            role_hint = " or ".join(hints) if hints else "a buyer role"
            await interaction.response.send_message(
                f"💎 **Premium Required** — only buyers with {role_hint} can use this.",
                ephemeral=True,
            )
            return

        tier_num, cooldown_secs = tier_info
        token_entry = pop_premium_token()
        if not token_entry:
            await interaction.response.send_message(
                "❌ **Premium pool is empty** — ask an admin to run `/add-premium-token`.",
                ephemeral=True,
            )
            return

        increment_premium_uses(user_id)
        ttl = seconds_until_expiry(token_entry["token"])
        payload = {
            "token":         token_entry["token"],
            "refresh_token": token_entry["refresh_token"],
            "expires_in":    ttl,
            "tier":          tier_num,
            "_note":         "Made by Forest and Mestro_ac",
        }

        await interaction.response.send_message(
            f"💎 **Premium Token** (Tier {tier_num})\n```json\n{json.dumps(payload, indent=2)}\n```",
            ephemeral=True,
        )
        print(f"[PREMIUM] ✅ Token sent to {interaction.user} ({user_id}) — tier {tier_num}")
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        print(f"[PREMIUM] ❌ Error: {e}"); traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  /add-premium-token, /add-premium-user
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="add-premium-token", description="[ADMIN] Add a token to the premium pool")
@app_commands.describe(token="JWT bearer token", refresh_token="JWT refresh token")
async def add_premium_token_cmd(interaction: discord.Interaction, token: str, refresh_token: str):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return
        if not token.startswith("ey") or not refresh_token.startswith("ey"):
            await interaction.response.send_message(
                "❌ **Invalid tokens** — must be JWT strings starting with `ey...`", ephemeral=True); return

        ttl = seconds_until_expiry(token)
        if ttl < 60:
            await interaction.response.send_message(
                f"❌ **Token already expired** ({ttl}s remaining).", ephemeral=True); return

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


@tree.command(name="add-premium-user", description="[ADMIN] Grant premium access to a user")
@app_commands.describe(user="Discord user to grant premium access")
async def add_premium_user_cmd(interaction: discord.Interaction, user: discord.Member):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return
        add_premium_user(str(user.id), str(interaction.user.id))
        await interaction.response.send_message(
            f"✅ **{user.mention} now has premium access** — can use `/get-premium-token`.", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  /status, /view-tokens
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="status", description="View live token pool status")
async def status_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        s = global_status()
        pub, prem, don = s["public"], s["premium"], s["donated"]
        payload = {
            "public_pool":  {"status": "active" if pub["valid"] else "expired", "expires_in": pub["expires_in"]},
            "premium_pool": {"valid_tokens": prem["valid"], "expired_tokens": prem["expired"], "total_tokens": prem["total"]},
            "donated_tokens": {"users_with_gifts": don["users_with_donated"], "total_donated": don["total_donated_tokens"]},
            "premium_users_whitelisted": s["premium_users"],
        }
        await interaction.response.send_message(
            f"📊 **System Status**\n```json\n{json.dumps(payload, indent=2)}\n```", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


@tree.command(name="view-tokens", description="[ADMIN] View all available tokens")
async def view_tokens_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return

        payload = {
            "public_token": get_public_token() or "None or expired",
            "premium_pool": get_premium_pool(),
            "env_accounts": get_env_accounts(),
            "_note":        "Made by Forest and Mestro_ac",
        }
        json_bytes = json.dumps(payload, indent=2).encode("utf-8")
        file = discord.File(fp=io.BytesIO(json_bytes), filename="all_tokens.json")
        await interaction.response.send_message("🔑 **All Available Tokens**", file=file, ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  COOLDOWN ADMIN COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="remove-cooldown-all", description="[ADMIN] Remove all cooldowns for all users")
async def remove_cooldown_all_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return
        count = reset_all_cooldowns()
        await interaction.response.send_message(
            f"✅ **Removed all cooldowns for {count} users**", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


@tree.command(name="add-cooldown", description="[ADMIN] Add permanent cooldown to a user")
@app_commands.describe(user="Discord user", pool="Pool type (public/premium)")
async def add_cooldown_cmd(interaction: discord.Interaction, user: discord.Member, pool: str = "public"):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return
        if pool not in ["public", "premium"]:
            await interaction.response.send_message("❌ **Pool must be 'public' or 'premium'**", ephemeral=True); return
        set_permanent_cooldown(str(user.id), pool)
        await interaction.response.send_message(
            f"✅ **Added permanent {pool} cooldown to {user.mention}**", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


@tree.command(name="add-cooldown-all", description="[ADMIN] Add permanent cooldowns to all users")
@app_commands.describe(pool="Pool type (public/premium)")
async def add_cooldown_all_cmd(interaction: discord.Interaction, pool: str = "public"):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return
        if pool not in ["public", "premium"]:
            await interaction.response.send_message("❌ **Pool must be 'public' or 'premium'**", ephemeral=True); return

        guild = client.get_guild(interaction.guild_id)
        if not guild:
            await interaction.response.send_message("❌ **Could not access guild**", ephemeral=True); return
        count = 0
        async for member in guild.fetch_members(limit=None):
            set_permanent_cooldown(str(member.id), pool)
            count += 1
        await interaction.response.send_message(
            f"✅ **Added permanent {pool} cooldowns to {count} users**", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  DONATE / MY-TOKENS / REVOKE
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="donate-token", description="[ADMIN] Gift a token to a specific user")
@app_commands.describe(user="Discord user", token="JWT bearer token", refresh_token="JWT refresh token")
async def donate_token_cmd(interaction: discord.Interaction, user: discord.Member, token: str, refresh_token: str):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return
        if not token.startswith("ey") or not refresh_token.startswith("ey"):
            await interaction.response.send_message("❌ **Invalid tokens**.", ephemeral=True); return

        ttl = seconds_until_expiry(token)
        if ttl < 60:
            await interaction.response.send_message(
                f"❌ **Token already expired** ({ttl}s remaining).", ephemeral=True); return

        add_donated(str(user.id), token, refresh_token, str(interaction.user.id))
        await interaction.response.send_message(
            f"🎁 **Token donated to {user.mention}**\n"
            f"```json\n{json.dumps({'expires_in': ttl, 'recipient': str(user.id), 'given_by': str(interaction.user.id)}, indent=2)}\n```\n"
            f">>> They can claim it with `/my-tokens`.", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


@tree.command(name="my-tokens", description="See all tokens gifted to you")
async def my_tokens_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        user_id = str(interaction.user.id)
        on_cd, remaining = check_cooldown(user_id, "my_tokens", 60)
        if on_cd:
            await interaction.response.send_message(
                f"⏱️ Slow down — try again in `{format_time(remaining)}`.", ephemeral=True); return
        set_cooldown(user_id, "my_tokens")

        donated = get_donated(user_id)
        if not donated:
            await interaction.response.send_message(
                "🎁 **No gifted tokens** — ask an admin to run `/donate-token`.", ephemeral=True); return

        valid = [t for t in donated if not is_expired(t["token"])]
        expired_count = len(donated) - len(valid)
        if not valid:
            await interaction.response.send_message(
                f"❌ **All {expired_count} gifted token(s) have expired**.", ephemeral=True); return

        payload = [{"gift": i, "token": t["token"], "refresh_token": t["refresh_token"],
                    "expires_in": seconds_until_expiry(t["token"]), "given_by": t["given_by"]}
                   for i, t in enumerate(valid, 1)]
        raw = json.dumps(payload, indent=2)
        header = f"🎁 **Your Gifted Tokens** — `{len(valid)}` valid, `{expired_count}` expired\n"

        if len(header) + len(raw) + 10 <= 1990:
            await interaction.response.send_message(f"{header}```json\n{raw}\n```", ephemeral=True)
        else:
            await interaction.response.send_message(
                f"{header}*(Sending {len(valid)} token(s) separately)*", ephemeral=True)
            for entry in payload:
                await interaction.followup.send(
                    f"```json\n{json.dumps(entry, indent=2)}\n```", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


@tree.command(name="reset-cooldown", description="[ADMIN] Reset cooldowns for a user or everyone")
@app_commands.describe(user="User to reset (empty = everyone)", pool="Pool to reset (empty = all)")
@app_commands.choices(pool=[
    app_commands.Choice(name="public",    value="public"),
    app_commands.Choice(name="premium",   value="premium"),
    app_commands.Choice(name="my_tokens", value="my_tokens"),
])
async def reset_cooldown_cmd(interaction: discord.Interaction, user: discord.Member = None, pool: str = None):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return

        data = _read(COOLDOWNS_FILE, {})
        if user:
            uid = str(user.id)
            if uid not in data:
                await interaction.response.send_message(
                    f"❌ **{user.mention}** has no active cooldowns.", ephemeral=True); return
            if pool:
                data[uid].pop(pool, None); desc = f"pool `{pool}`"
            else:
                del data[uid]; desc = "all pools"
            _write(COOLDOWNS_FILE, data)
            await interaction.response.send_message(
                f"✅ Cooldown reset for {user.mention} — {desc}.", ephemeral=True)
        else:
            if pool:
                for uid in data: data[uid].pop(pool, None)
                desc = f"pool `{pool}` for all users"
            else:
                data = {}; desc = "all pools for all users"
            _write(COOLDOWNS_FILE, data)
            await interaction.response.send_message(f"✅ Cooldowns reset — {desc}.", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


@tree.command(name="revoke-token", description="[ADMIN] Remove all donated tokens from a user")
@app_commands.describe(user="User whose donated tokens to revoke")
async def revoke_token_cmd(interaction: discord.Interaction, user: discord.Member):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return
        count = revoke_donated(str(user.id))
        await interaction.response.send_message(
            f"✅ **Revoked `{count}` donated token(s)** from {user.mention}."
            if count > 0 else
            f"❌ **{user.mention} had no donated tokens**.", ephemeral=True)
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  REFRESH SYSTEM SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════
@tree.command(name="refresh", description="Paste a token to save and refresh it")
async def refresh_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(PasteTokenModal())


@tree.command(name="get-refresh", description="Get a saved numbered token refreshed (10 min cooldown)")
async def get_refresh_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tokens = await get_user_tokens(str(interaction.user.id))
    if not tokens:
        await interaction.followup.send(
            "❌ No saved tokens. Use `/refresh` to save one first.", ephemeral=True); return
    token_list = "\n".join([f"**#{t['token_number']}** — saved {t['saved_at'][:10]}" for t in tokens])
    await interaction.followup.send(
        f"Your saved tokens:\n{token_list}", ephemeral=True, view=GetRefreshView())


@tree.command(name="check", description="Validate a JWT and show its claims")
async def check_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(CheckTokenModal())


@tree.command(name="my-saved-tokens", description="List your saved tokens from the refresh system")
async def my_saved_tokens_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    tokens = await get_user_tokens(str(interaction.user.id))
    if not tokens:
        await interaction.followup.send("❌ You have no saved tokens.", ephemeral=True); return

    lines = []
    now = datetime.now(timezone.utc).timestamp()
    for t in tokens:
        exp = get_jwt_exp(t.get("bearer", ""))
        if exp > now:
            secs = int(exp - now); h = secs // 3600; m = (secs % 3600) // 60
            status = f"✅ valid ({h}h {m}m)"
        else:
            status = "❌ expired"
        lines.append(f"**#{t['token_number']}** — {status}")

    embed = discord.Embed(
        title="📦 Your Saved Tokens",
        description="\n".join(lines),
        color=0x2b2d31,
    )
    embed.set_footer(text=f"Total: {len(tokens)}")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  DASHBOARD BUTTON (original pool button)
# ══════════════════════════════════════════════════════════════════════════════
class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Get Token", style=discord.ButtonStyle.primary,
                       emoji="🎫", custom_id="dashboard_get_token")
    async def get_token_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            user_id = str(interaction.user.id)
            on_cd, remaining = check_cooldown(user_id, "public", PUBLIC_COOLDOWN_SECONDS)
            if on_cd:
                await interaction.response.send_message(
                    f"⏱️ **Cooldown Active** — available in `{format_time(remaining)}`", ephemeral=True); return

            tokens, source = get_public_token_with_fallback()
            if not tokens:
                await interaction.response.send_message(
                    "❌ **No valid token available** — try again in 30 seconds.", ephemeral=True); return

            set_cooldown(user_id, "public")
            ttl = seconds_until_expiry(tokens["token"])
            payload = {"token": tokens["token"], "refresh_token": tokens["refresh_token"],
                       "expires_in": ttl, "next_use_in": PUBLIC_COOLDOWN_SECONDS}
            fallback_note = ("\n> ⚡ *Public token refreshing — backup token served*"
                             if source != "public" else "")

            await interaction.response.send_message(
                f"✅ **Token** | Next use in `{format_time(PUBLIC_COOLDOWN_SECONDS)}`{fallback_note}\n"
                f"```json\n{json.dumps(payload, indent=2)}\n```", ephemeral=True)
        except Exception as e:
            try:
                await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
            except Exception:
                pass
            traceback.print_exc()


@tree.command(name="dashboard", description="[ADMIN] Post a token button visible to everyone")
async def dashboard_cmd(interaction: discord.Interaction):
    try:
        if not guild_allowed(interaction):
            await interaction.response.send_message("🚫 Unauthorized server.", ephemeral=True); return
        if not await has_admin_access(interaction):
            await interaction.response.send_message("🚫 **Admin Only**.", ephemeral=True); return

        embed = discord.Embed(
            title="🎫 Token Station",
            description=(f"Press the button below to get your session token.\n"
                         f"Cooldown: **{format_time(PUBLIC_COOLDOWN_SECONDS)}** per user."),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="Token is only visible to you • Ephemeral")
        await interaction.response.send_message(embed=embed, view=DashboardView())
    except Exception as e:
        try:
            await interaction.response.send_message(f"❌ **Error:** `{e}`", ephemeral=True)
        except Exception:
            pass
        traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
#  XEROX PANEL (from bot2) — merged in, different button set
# ══════════════════════════════════════════════════════════════════════════════
class XeroxPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="Supporter Generate", style=discord.ButtonStyle.success,
               custom_id="xerox_supporter_generate", row=0)
    async def supporter_generate(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        if not any(r.id == SUPPORTER_ROLE for r in interaction.user.roles):
            await interaction.followup.send("❌ You need the **Supporter** role.", ephemeral=True); return

        user_id = interaction.user.id
        now     = datetime.now(timezone.utc)
        rl_key  = f"supporter:{user_id}"
        if rl_key in user_last_gen:
            elapsed = (now - user_last_gen[rl_key]).total_seconds()
            if elapsed < 2:
                await interaction.followup.send(
                    f"⏰ Wait **{format_cooldown(2 - elapsed)}** before generating again.", ephemeral=True); return
        user_last_gen[rl_key] = now

        try:
            data = await fetch_token_from_api(str(interaction.user.id), interaction.user.name)
        except Exception as e:
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True); return

        bearer  = data.get("bearer", "")
        refresh = data.get("refresh_token", "")
        sent    = await send_token_dm(interaction.user, bearer, refresh, label="Supporter Token")
        if sent:
            await interaction.followup.send("✅ Token sent to DMs!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Couldn't DM you.", ephemeral=True)
        await log_generate(interaction, bearer, "Supporter")

    @ui.button(label="Refresh", style=discord.ButtonStyle.secondary, custom_id="xerox_refresh", row=1)
    async def refresh_token(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(PasteTokenModal())

    @ui.button(label="Get Refresh Token", style=discord.ButtonStyle.secondary, custom_id="xerox_get_refresh", row=1)
    async def get_refresh(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        tokens = await get_user_tokens(str(interaction.user.id))
        if not tokens:
            await interaction.followup.send(
                "❌ You have no saved tokens. Use **Refresh** first.", ephemeral=True); return
        token_list = "\n".join([f"**#{t['token_number']}** — saved {t['saved_at'][:10]}" for t in tokens])
        await interaction.followup.send(
            f"Your saved tokens:\n{token_list}\n\nClick **Get Refresh Token** again and enter the number.",
            ephemeral=True, view=GetRefreshView())

    @ui.button(label="Check", style=discord.ButtonStyle.secondary, custom_id="xerox_check", row=2)
    async def check_token(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(CheckTokenModal())

    @ui.button(label="Stats", style=discord.ButtonStyle.secondary, custom_id="xerox_stats", row=2)
    async def stats(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        tokens = await get_user_tokens(str(interaction.user.id))
        embed = discord.Embed(title="📊 Your Stats", color=0x2b2d31)
        embed.add_field(name="Saved Tokens", value=str(len(tokens)), inline=True)
        embed.set_footer(text="XEROX Token System")
        await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="panel", description="Post the XEROX token system panel")
async def panel_cmd(interaction: discord.Interaction):
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message("This command only works in the Xerox server.", ephemeral=True); return
    if interaction.channel_id != PANEL_CHANNEL:
        await interaction.response.send_message(f"Use this in <#{PANEL_CHANNEL}>.", ephemeral=True); return

    embed = discord.Embed(
        title="XEROX TOKEN SYSTEM",
        color=0x2b2d31,
        description=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "**Supporter Generate** — Supporter role only, 2s cooldown\n"
            "**Refresh** — Paste a token to save & refresh it (numbered)\n"
            "**Get Refresh Token** — Get a numbered saved token refreshed (10 min cooldown)\n"
            "**Check** — Check if a token is valid\n"
            "**Stats** — View your saved tokens\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🗒️ Supports: Text tokens & .json files"
        ),
    )
    embed.set_footer(text="XEROX Token System")

    try:
        file = discord.File("logo.png", filename="logo.png")
        embed.set_thumbnail(url="attachment://logo.png")
        await interaction.channel.send(embed=embed, view=XeroxPanelView(), file=file)
    except FileNotFoundError:
        await interaction.channel.send(embed=embed, view=XeroxPanelView())

    await interaction.response.send_message("✅ Panel sent!", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  HEALTH SERVER (from bot2, for fly.io / render)
# ══════════════════════════════════════════════════════════════════════════════
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Xerox Token Bot alive")

    def log_message(self, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"Health server on port {port}")
    server.serve_forever()


# ══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════════
@client.event
async def on_ready():
    # register persistent views
    client.add_view(DashboardView())
    client.add_view(XeroxPanelView())

    if ALLOWED_GUILD_IDS:
        guild_objects = [discord.Object(id=gid) for gid in ALLOWED_GUILD_IDS]
        for guild_obj in guild_objects:
            tree.copy_global_to(guild=guild_obj)
            synced = await tree.sync(guild=guild_obj)
            print(f"\n{'='*55}")
            print(f"✅ [BOT] Connected as: {client.user}")
            print(f"✅ Synced {len(synced)} commands to guilds: {ALLOWED_GUILD_IDS}")
            print(f"✅ Commands: {[c.name for c in synced]}")
            print(f"✅ Persistent views registered")
            print(f"{'='*55}\n")
    else:
        synced = await tree.sync()
        print(f"\n{'='*55}")
        print(f"✅ [BOT] Connected as: {client.user}")
        print(f"✅ Synced {len(synced)} commands globally")
        print(f"✅ Commands: {[c.name for c in synced]}")
        print(f"✅ Persistent views registered")
        print(f"{'='*55}\n")

    # start auto-refresh loop
    asyncio.create_task(auto_refresh_all_tokens())
    print("[auto-refresh] task started (every 30 min)")


@client.event
async def on_disconnect():
    print("[BOT] ❌ Disconnected")


@client.event
async def on_resumed():
    print("[BOT] ✅ Reconnected")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    print("[BOT] Starting unified bot...")
    threading.Thread(target=run_health_server, daemon=True).start()
    await client.start(BOT_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Fatal error: {e}")
        raise
