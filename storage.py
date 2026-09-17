"""
storage.py — Shared Storage Layer
==================================
Thread-safe file I/O used by all bots.
All state lives in /data/ folder as JSON.

Files:
  data/tokens.json          → shared token pool (public)
  data/premium_tokens.json  → premium token pool
  data/premium_users.json   → map of discord_id → list of tokens they received
  data/donated_tokens.json  → map of discord_id → list of {token, refresh_token, given_by, given_at}
  data/cooldowns.json       → per-user cooldown timestamps  {user_id: {pool: timestamp}}
"""

import json
import os
import threading
import time
import base64
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

# ── File paths ────────────────────────────────────────────────────────────────
TOKENS_FILE         = DATA_DIR / "tokens.json"
PREMIUM_TOKENS_FILE = DATA_DIR / "premium_tokens.json"
PREMIUM_USERS_FILE  = DATA_DIR / "premium_users.json"
DONATED_FILE        = DATA_DIR / "donated_tokens.json"
COOLDOWNS_FILE      = DATA_DIR / "cooldowns.json"
ROTATE_INDEX_FILE   = DATA_DIR / "rotate_index.json"

# ── Generic helpers ───────────────────────────────────────────────────────────
def _read(path: Path, default):
    with _lock:
        try:
            if path.exists():
                raw = path.read_text().strip()
                if raw:
                    return json.loads(raw)
        except (json.JSONDecodeError, OSError):
            pass
        return default


def _write(path: Path, data) -> None:
    with _lock:
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)          # atomic on Linux
        except OSError as e:
            print(f"[STORAGE] Write error {path}: {e}")


# ── JWT helpers ───────────────────────────────────────────────────────────────
def decode_jwt_exp(jwt_token: str) -> int:
    try:
        payload_b64 = jwt_token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return payload.get("exp", 0)
    except Exception:
        return 0


def is_expired(jwt_token: str, buffer: int = 60) -> bool:
    return int(time.time()) >= (decode_jwt_exp(jwt_token) - buffer)


def seconds_until_expiry(jwt_token: str) -> int:
    return max(0, decode_jwt_exp(jwt_token) - int(time.time()))


# ── Public token pool ─────────────────────────────────────────────────────────
def get_public_token() -> dict | None:
    """Return the current public token or None if expired/missing"""
    data = _read(TOKENS_FILE, {})
    if not data or "token" not in data:
        return None
    if is_expired(data["token"]):
        return None
    return data


def set_public_token(token: str, refresh_token: str) -> None:
    _write(TOKENS_FILE, {"token": token, "refresh_token": refresh_token})


def _load_env_vars() -> dict[str, str]:
    """Load env vars from .env file merged with os.environ (.env wins)."""
    merged = dict(os.environ)
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            merged[key.strip()] = value.strip()
    return merged


def get_env_accounts() -> list[dict]:
    """
    Read TOKEN_1/REFRESH_TOKEN_1, TOKEN_2/REFRESH_TOKEN_2, ... from .env.
    Also accepts legacy INITIAL_TOKEN/INITIAL_REFRESH_TOKEN as TOKEN_1.
    Returns list of {token, refresh_token} in order.
    """
    env = _load_env_vars()
    accounts = []
    i = 1
    while True:
        token   = env.get(f"TOKEN_{i}", "").strip()
        refresh = env.get(f"REFRESH_TOKEN_{i}", "").strip()
        if not token or not refresh:
            break
        accounts.append({"token": token, "refresh_token": refresh})
        i += 1

    if not accounts:
        token   = env.get("INITIAL_TOKEN", "").strip()
        refresh = env.get("INITIAL_REFRESH_TOKEN", "").strip()
        if token and refresh:
            accounts.append({"token": token, "refresh_token": refresh})

    return accounts


def _next_rotate_index(count: int) -> int:
    """Return the next round-robin index (0-based) and advance the counter."""
    if count <= 0:
        return 0
    data = _read(ROTATE_INDEX_FILE, {"index": 0})
    idx = data["index"] % count
    data["index"] = (data["index"] + 1) % count
    _write(ROTATE_INDEX_FILE, data)
    return idx


def get_public_token_with_fallback() -> tuple[dict | None, str]:
    """
    Three-level fallback for the public token:
      1. tokens.json (main public token, kept alive by refresh_token.py)
      2. Premium pool  — peek at the least-used valid token (not consumed)
      3. Env accounts  — TOKEN_1/REFRESH_TOKEN_1 ... TOKEN_N/REFRESH_TOKEN_N

    Returns:
        (token_dict, source)
        token_dict → {"token": ..., "refresh_token": ...} or None if everything is empty
        source     → "public" | "premium_pool" | "env_accountN" | "none"
    """
    # 1. Main public token
    public = get_public_token()
    if public:
        return public, "public"

    # 2. Premium pool (peek — does NOT consume the token)
    pool  = get_premium_pool()
    valid = [t for t in pool if not is_expired(t["token"])]
    if valid:
        backup = min(valid, key=lambda t: t["used"])
        return {"token": backup["token"], "refresh_token": backup["refresh_token"]}, "premium_pool"

    # 3. Env accounts (TOKEN_1, TOKEN_2, ...)
    for i, acc in enumerate(get_env_accounts(), start=1):
        if not is_expired(acc["token"]):
            return {"token": acc["token"], "refresh_token": acc["refresh_token"]}, f"env_account{i}"

    return None, "none"


def get_public_token_raw() -> dict:
    return _read(TOKENS_FILE, {})


def set_public_token_raw(data: dict) -> None:
    _write(TOKENS_FILE, data)


# ── Premium token pool ────────────────────────────────────────────────────────
def get_premium_pool() -> list:
    return _read(PREMIUM_TOKENS_FILE, [])


def save_premium_pool(pool: list) -> None:
    _write(PREMIUM_TOKENS_FILE, pool)


def add_premium_token(token: str, refresh_token: str) -> int:
    """Add a token to the premium pool. Returns new pool size."""
    pool = get_premium_pool()
    pool.append({
        "token": token,
        "refresh_token": refresh_token,
        "added_at": int(time.time()),
        "used": 0,
    })
    save_premium_pool(pool)
    return len(pool)


def pop_premium_token() -> dict | None:
    """
    Get a valid premium token from the pool.
    Removes expired ones, returns None if pool is empty.
    """
    pool = get_premium_pool()
    # Filter out expired ones
    valid = [t for t in pool if not is_expired(t["token"])]
    if len(valid) != len(pool):
        save_premium_pool(valid)

    if not valid:
        return None

    # Round-robin: pick least-used valid token
    token_entry = min(valid, key=lambda t: t["used"])
    token_entry["used"] += 1
    save_premium_pool(valid)
    return token_entry


def premium_pool_status() -> dict:
    pool = get_premium_pool()
    valid = [t for t in pool if not is_expired(t["token"])]
    expired = len(pool) - len(valid)
    return {
        "total": len(pool),
        "valid": len(valid),
        "expired": expired,
    }


# ── Premium users (who has access) ───────────────────────────────────────────
def get_premium_users() -> dict:
    return _read(PREMIUM_USERS_FILE, {})


def is_premium_user(discord_id: str) -> bool:
    users = get_premium_users()
    return str(discord_id) in users


def add_premium_user(discord_id: str, added_by: str) -> None:
    users = get_premium_users()
    users[str(discord_id)] = {
        "added_by": str(added_by),
        "added_at": int(time.time()),
        "uses": 0,
    }
    _write(PREMIUM_USERS_FILE, users)


def remove_premium_user(discord_id: str) -> bool:
    users = get_premium_users()
    if str(discord_id) not in users:
        return False
    del users[str(discord_id)]
    _write(PREMIUM_USERS_FILE, users)
    return True


def increment_premium_uses(discord_id: str) -> None:
    users = get_premium_users()
    uid = str(discord_id)
    if uid in users:
        users[uid]["uses"] = users[uid].get("uses", 0) + 1
        _write(PREMIUM_USERS_FILE, users)


# ── Donated tokens (per-user gift) ────────────────────────────────────────────
def get_donated(discord_id: str) -> list:
    data = _read(DONATED_FILE, {})
    return data.get(str(discord_id), [])


def add_donated(target_id: str, token: str, refresh_token: str, given_by: str) -> None:
    data = _read(DONATED_FILE, {})
    uid = str(target_id)
    if uid not in data:
        data[uid] = []
    data[uid].append({
        "token": token,
        "refresh_token": refresh_token,
        "given_by": str(given_by),
        "given_at": int(time.time()),
        "used": False,
    })
    _write(DONATED_FILE, data)


def mark_donated_used(discord_id: str, index: int) -> bool:
    data = _read(DONATED_FILE, {})
    uid = str(discord_id)
    if uid not in data or index >= len(data[uid]):
        return False
    data[uid][index]["used"] = True
    _write(DONATED_FILE, data)
    return True


def revoke_donated(discord_id: str) -> int:
    """Remove all donated tokens for a user. Returns count removed."""
    data = _read(DONATED_FILE, {})
    uid = str(discord_id)
    count = len(data.get(uid, []))
    if uid in data:
        del data[uid]
        _write(DONATED_FILE, data)
    return count


def donated_stats() -> dict:
    data = _read(DONATED_FILE, {})
    total_users = len(data)
    total_tokens = sum(len(v) for v in data.values())
    return {"users_with_donated": total_users, "total_donated_tokens": total_tokens}


# ── Cooldowns ─────────────────────────────────────────────────────────────────
def check_cooldown(discord_id: str, pool: str, cooldown_seconds: int) -> tuple[bool, int]:
    """Returns (is_on_cooldown, seconds_remaining). -1 means permanent cooldown."""
    data = _read(COOLDOWNS_FILE, {})
    uid = str(discord_id)
    now = int(time.time())
    last = data.get(uid, {}).get(pool, 0)
    
    # Check for permanent cooldown (-1)
    if last == -1:
        return True, -1
    
    elapsed = now - last
    if elapsed < cooldown_seconds:
        return True, cooldown_seconds - elapsed
    return False, 0


def set_cooldown(discord_id: str, pool: str) -> None:
    data = _read(COOLDOWNS_FILE, {})
    uid = str(discord_id)
    if uid not in data:
        data[uid] = {}
    data[uid][pool] = int(time.time())
    _write(COOLDOWNS_FILE, data)


def reset_all_cooldowns() -> int:
    """Wipe every cooldown entry. Returns number of users reset."""
    data = _read(COOLDOWNS_FILE, {})
    count = len(data)
    _write(COOLDOWNS_FILE, {})
    return count


def set_permanent_cooldown(discord_id: str, pool: str) -> None:
    """Set a permanent cooldown that won't expire until manually removed."""
    data = _read(COOLDOWNS_FILE, {})
    uid = str(discord_id)
    if uid not in data:
        data[uid] = {}
    data[uid][pool] = -1  # -1 means permanent
    _write(COOLDOWNS_FILE, data)


def remove_permanent_cooldown(discord_id: str, pool: str) -> bool:
    """Remove a permanent cooldown. Returns True if user had permanent cooldown."""
    data = _read(COOLDOWNS_FILE, {})
    uid = str(discord_id)
    if uid in data and pool in data[uid] and data[uid][pool] == -1:
        del data[uid][pool]
        if not data[uid]:  # Clean up empty user entry
            del data[uid]
        _write(COOLDOWNS_FILE, data)
        return True
    return False


def get_rotating_token() -> dict | None:
    """Return one token per call, rotating through all env accounts."""
    accounts = get_env_accounts()
    if not accounts:
        return None

    start = _next_rotate_index(len(accounts))

    for offset in range(len(accounts)):
        idx = (start + offset) % len(accounts)
        account = accounts[idx]
        if is_expired(account["token"], buffer=60):
            continue
        return {
            "token": account["token"],
            "refresh_token": account["refresh_token"],
            "account": idx + 1,
        }

    return None


def refresh_account_token(account: dict) -> dict | None:
    """Refresh a specific account's token using its refresh_token."""
    try:
        import base64
        import json
        from urllib import request, error
        
        HOST = os.getenv("NAKAMA_HOST", "https://animalcompany.us-east1.nakamacloud.io")
        SERVER_KEY = os.getenv("NAKAMA_SERVER_KEY", "6URuTSlDKKfYbuDW")
        REFRESH_URL = f"{HOST}/v2/account/session/refresh"
        
        basic = base64.b64encode(f"{SERVER_KEY}:".encode()).decode()
        body = json.dumps({"token": account["refresh_token"]}).encode()
        
        req = request.Request(
            REFRESH_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {basic}",
            },
        )
        
        with request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if "token" in data and "refresh_token" in data:
                return {
                    "token": data["token"],
                    "refresh_token": data["refresh_token"],
                }
            return None
    except Exception as e:
        print(f"[REFRESH] Error refreshing token: {e}")
        return None


def update_env_token(account_index: int, new_token: str, new_refresh_token: str) -> bool:
    """Update the .env file with new token values for a specific account."""
    try:
        env_path = Path(".env")
        if not env_path.exists():
            print(f"[ENV] .env file not found")
            return False
        
        # Read current .env content
        content = env_path.read_text()
        lines = content.split('\n')
        
        # Update the specific TOKEN_N and REFRESH_TOKEN_N lines
        token_key = f"TOKEN_{account_index}"
        refresh_key = f"REFRESH_TOKEN_{account_index}"
        
        updated_lines = []
        token_updated = False
        refresh_updated = False
        
        for line in lines:
            if line.startswith(f"{token_key}="):
                updated_lines.append(f"{token_key}={new_token}")
                token_updated = True
            elif line.startswith(f"{refresh_key}="):
                updated_lines.append(f"{refresh_key}={new_refresh_token}")
                refresh_updated = True
            else:
                updated_lines.append(line)
        
        # If the keys weren't found, add them
        if not token_updated:
            updated_lines.append(f"{token_key}={new_token}")
        if not refresh_updated:
            updated_lines.append(f"{refresh_key}={new_refresh_token}")
        
        # Write back to .env
        env_path.write_text('\n'.join(updated_lines))
        
        # Update current environment
        os.environ[token_key] = new_token
        os.environ[refresh_key] = new_refresh_token
        
        print(f"[ENV] Updated {token_key} and {refresh_key} in .env file")
        return True
    except Exception as e:
        print(f"[ENV] Error updating .env file: {e}")
        return False


def reset_user_cooldown(discord_id: str, pool: str | None = None) -> bool:
    """
    Reset cooldowns for a single user.
    If pool is given, only that pool is cleared.
    Returns True if the user existed.
    """
    data = _read(COOLDOWNS_FILE, {})
    uid = str(discord_id)
    if uid not in data:
        return False
    if pool:
        data[uid].pop(pool, None)
        if not data[uid]:
            del data[uid]
    else:
        del data[uid]
    _write(COOLDOWNS_FILE, data)
    return True


def format_time(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


# ── Global stats ──────────────────────────────────────────────────────────────
def global_status() -> dict:
    public_raw = get_public_token_raw()
    public_valid = bool(public_raw.get("token")) and not is_expired(public_raw.get("token", ""))
    public_ttl = seconds_until_expiry(public_raw.get("token", "")) if public_valid else 0

    premium = premium_pool_status()
    donated = donated_stats()
    premium_users = get_premium_users()

    return {
        "public": {
            "valid": public_valid,
            "expires_in": public_ttl,
        },
        "premium": premium,
        "premium_users": len(premium_users),
        "donated": donated,
    }
