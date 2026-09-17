"""
refresh_token.py — Nakama Token Refresher (Multi-Account)
==========================================================
Keeps the public token pool alive using multiple accounts as fallback.

Variables en Railway:
    TOKEN_1, REFRESH_TOKEN_1   ← cuenta principal
    TOKEN_2, REFRESH_TOKEN_2   ← backup 1
    TOKEN_3, REFRESH_TOKEN_3   ← backup 2
    ... (sin límite)

    NAKAMA_HOST               ← opcional
    NAKAMA_SERVER_KEY         ← opcional
    REFRESH_INTERVAL_SECONDS  ← opcional (default 120)

Usage:
    python refresh_token.py          → refresh once
    python refresh_token.py --loop   → loop every REFRESH_INTERVAL seconds
"""

import base64
import json
import os
import sys
import time
import argparse
from urllib import request, error
import traceback
from dotenv import load_dotenv

# Fix Windows console encoding
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

load_dotenv()

from storage import (
    get_public_token_raw,
    set_public_token_raw,
    decode_jwt_exp,
    decode_jwt_uid,
    is_expired,
    seconds_until_expiry,
    get_env_accounts,
    refresh_account_token,
    update_env_token,
    add_to_shared_pool,
    get_shared_pool,
    set_shared_pool,
    refresh_shared_pool_tokens,
    get_user_account_map,
    set_user_account_map,
)

# ── Config ────────────────────────────────────────────────────────────────────
HOST        = os.getenv("NAKAMA_HOST", "https://animalcompany.us-east1.nakamacloud.io")
SERVER_KEY  = os.getenv("NAKAMA_SERVER_KEY", "6URuTSlDKKfYbuDW")
REFRESH_URL = f"{HOST}/v2/account/session/refresh"
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL_SECONDS", "120"))


# ── Load accounts from env (TOKEN_1/REFRESH_TOKEN_1, TOKEN_2/..., etc.) ──────
def load_accounts() -> list[dict]:
    """
    Reads TOKEN_1/REFRESH_TOKEN_1, TOKEN_2/REFRESH_TOKEN_2, ...
    Returns a list of {token, refresh_token, label} in order.
    Stops at the first missing pair.
    """
    accounts = []
    i = 1
    while True:
        token   = os.getenv(f"TOKEN_{i}", "").strip()
        refresh = os.getenv(f"REFRESH_TOKEN_{i}", "").strip()
        if not token or not refresh:
            break
        accounts.append({
            "token":         token,
            "refresh_token": refresh,
            "label":         f"account_{i}",
        })
        i += 1

    # Backward-compat: also accept old INITIAL_TOKEN / INITIAL_REFRESH_TOKEN
    # as a fallback if no TOKEN_N vars are set
    if not accounts:
        token   = os.getenv("INITIAL_TOKEN", "").strip()
        refresh = os.getenv("INITIAL_REFRESH_TOKEN", "").strip()
        if token and refresh:
            accounts.append({
                "token":         token,
                "refresh_token": refresh,
                "label":         "account_1 (legacy)",
            })

    return accounts


def refresh_all_env_accounts(accounts: list[dict]) -> dict | None:
    """
    Refresh every env account (TOKEN_1, TOKEN_2, etc.) on each cycle while maintaining user associations.
    Returns the refreshed primary account tokens for public storage, or None.
    """
    print(f"[REFRESH_ALL] Refreshing {len(accounts)} env accounts...")

    primary: dict | None = None

    for i, account in enumerate(accounts, start=1):
        if is_expired(account["refresh_token"], buffer=60):
            print(f"[REFRESH_ALL] Account {i} refresh token expired, skipping")
            continue

        print(f"[REFRESH_ALL] Refreshing account {i}...")
        new_tokens = refresh_account_token(account)

        if new_tokens:
            # Update user associations if UID changed during refresh
            old_uid = decode_jwt_uid(account["token"])
            new_uid = decode_jwt_uid(new_tokens["token"])
            
            if new_uid and old_uid and new_uid != old_uid:
                print(f"[REFRESH_ALL] UID changed during refresh for account {i}: {old_uid} -> {new_uid}")
                # Update user mappings to point to new UID
                user_map = get_user_account_map()
                for discord_id, user_data in user_map.items():
                    if user_data.get("nakama_uid") == old_uid:
                        user_data["nakama_uid"] = new_uid
                        print(f"[REFRESH_ALL] Updated user {discord_id} mapping to new UID {new_uid}")
                set_user_account_map(user_map)
            
            if update_env_token(i, new_tokens["token"], new_tokens["refresh_token"]):
                print(f"[REFRESH_ALL] Successfully refreshed and persisted account {i}")
            else:
                print(f"[REFRESH_ALL] Refreshed account {i} but failed to persist")

            account["token"] = new_tokens["token"]
            account["refresh_token"] = new_tokens["refresh_token"]

            if i == 1:
                primary = new_tokens
        else:
            print(f"[REFRESH_ALL] Failed to refresh account {i}")

    if primary:
        set_public_token_raw({
            "token": primary["token"],
            "refresh_token": primary["refresh_token"],
        })

    return primary


def get_active_account(accounts: list[dict]) -> dict | None:
    """Return the first account whose refresh_token is still valid."""
    for acc in accounts:
        if not is_expired(acc["refresh_token"], buffer=60):
            return acc
    return None


def get_any_account(accounts: list[dict]) -> dict | None:
    """Return the first account regardless of token validity (for refresh attempts)."""
    if accounts:
        return accounts[0]
    return None


def load_tokens(accounts: list[dict]) -> dict:
    """
    Pick the active token to use as the public token.
    Priority: existing storage → first valid account → any account (for refresh attempt) → error.
    """
    data = get_public_token_raw()
    if data.get("token") and data.get("refresh_token") and not is_expired(data["token"]):
        print("[REFRESH] Tokens loaded from storage")
        return data

    acc = get_active_account(accounts)
    if not acc:
        # If no valid accounts, try to use the first one anyway for refresh attempt
        acc = get_any_account(accounts)
        if not acc:
            raise ValueError(
                "No accounts found.\n"
                "Set TOKEN_1/REFRESH_TOKEN_1 (and TOKEN_2/REFRESH_TOKEN_2, etc.) in Railway Variables."
            )
        print(f"[REFRESH] Using expired account {acc['label']} for refresh attempt")

    print(f"[REFRESH] Loading from env — using {acc['label']}")
    set_public_token_raw({"token": acc["token"], "refresh_token": acc["refresh_token"]})
    return {"token": acc["token"], "refresh_token": acc["refresh_token"], "label": acc["label"]}


# ── Refresh call ──────────────────────────────────────────────────────────────
def do_refresh(tokens: dict) -> dict:
    print(f"\n[REFRESH] Refreshing token...")

    basic = base64.b64encode(f"{SERVER_KEY}:".encode()).decode()
    body  = json.dumps({"token": tokens["refresh_token"]}).encode()

    req = request.Request(
        REFRESH_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Basic {basic}",
        },
    )

    # Add retry logic for connection errors
    max_retries = 3
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            with request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            old_token = tokens["token"]
            tokens["token"]         = data.get("token",         tokens["token"])
            tokens["refresh_token"] = data.get("refresh_token", tokens["refresh_token"])

            # Update user associations if UID changed during refresh
            old_uid = decode_jwt_uid(old_token)
            new_uid = decode_jwt_uid(tokens["token"])
            
            if new_uid and old_uid and new_uid != old_uid:
                print(f"[REFRESH] UID changed during refresh: {old_uid} -> {new_uid}")
                # Update user mappings to point to new UID
                user_map = get_user_account_map()
                for discord_id, user_data in user_map.items():
                    if user_data.get("nakama_uid") == old_uid:
                        user_data["nakama_uid"] = new_uid
                        print(f"[REFRESH] Updated user {discord_id} mapping to new UID {new_uid}")
                set_user_account_map(user_map)

            set_public_token_raw(tokens)
            
            # Add to shared pool for multi-user continuity
            add_to_shared_pool(tokens["token"], tokens["refresh_token"])
            
            # Update .env file if this is from account_1
            if tokens.get("label") == "account_1":
                update_env_token(1, tokens["token"], tokens["refresh_token"])

            ttl      = seconds_until_expiry(tokens["token"])
            exp_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(decode_jwt_exp(tokens["token"])))
            print(f"[REFRESH] Token refreshed! Expires: {exp_time} (in {ttl}s)")

            return tokens
            
        except error.URLError as e:
            if attempt < max_retries - 1:
                print(f"[REFRESH] Connection error (attempt {attempt + 1}/{max_retries}): {e}")
                print(f"[REFRESH] Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                raise


def switch_to_next_account(accounts: list[dict], current: dict) -> dict | None:
    """
    Try each account in order after the current one fails.
    Returns the first valid account, or None if all are dead.
    """
    current_label = current.get("label", "")
    # Try accounts after the current one first, then wrap around
    ordered = sorted(accounts, key=lambda a: a["label"] != current_label)
    for acc in ordered:
        if acc["label"] == current_label:
            continue
        if not is_expired(acc["refresh_token"], buffer=60):
            print(f"[REFRESH] Switching to {acc['label']}")
            return {"token": acc["token"], "refresh_token": acc["refresh_token"], "label": acc["label"]}
    return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()

    accounts = load_accounts()

    print("\n" + "=" * 55)
    print("PUBLIC TOKEN REFRESHER (Multi-Account)")
    print(f"   Host:     {HOST}")
    print(f"   Interval: {REFRESH_INTERVAL}s")
    print(f"   Accounts: {len(accounts)} loaded ({', '.join(a['label'] for a in accounts)})")
    print("=" * 55)

    if not accounts:
        print("[REFRESH] No accounts configured — set TOKEN_1/REFRESH_TOKEN_1 in Railway Variables")
        return

    tokens = load_tokens(accounts)
    # Track which account is active for fallback purposes
    if "label" not in tokens:
        tokens["label"] = accounts[0]["label"]

    if not args.loop:
        try:
            do_refresh(tokens)
        except Exception as e:
            print(f"[REFRESH] Single refresh failed: {e}")
            traceback.print_exc()
        return

    # ── Loop ──────────────────────────────────────────────────────────────────
    print(f"[REFRESH] Loop active — every {REFRESH_INTERVAL}s\n")
    fails = 0
    MAX_FAILS = 5

    while True:
        try:
            # Check current token status
            ttl = seconds_until_expiry(tokens["token"])
            print(f"[REFRESH] Current token valid for {ttl}s")
            
            # Refresh all env accounts first to keep them fresh
            refresh_all_env_accounts(accounts)
            
            # Refresh shared pool tokens for multi-user continuity
            shared_refreshed = refresh_shared_pool_tokens()
            if shared_refreshed > 0:
                print(f"[REFRESH] Refreshed {shared_refreshed} shared pool tokens")
            
            # Then refresh the main public token using the current active account
            print(f"[REFRESH] Refreshing main token now...")
            tokens = do_refresh(tokens)
            fails = 0
            
            # Sleep for the interval before next refresh cycle
            print(f"[REFRESH] Sleeping {REFRESH_INTERVAL}s until next refresh...")
            time.sleep(REFRESH_INTERVAL)

        except KeyboardInterrupt:
            print("\n[REFRESH] Stopped by user")
            break

        except error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            fails += 1
            print(f"[REFRESH] HTTP {e.code}: {body} (fail {fails}/{MAX_FAILS})")

            # Auth error — try next account
            if e.code in (401, 403):
                print(f"[REFRESH] Auth error on {tokens.get('label')} — trying next account...")
                next_acc = switch_to_next_account(accounts, tokens)
                if next_acc:
                    tokens = next_acc
                    set_public_token_raw({"token": tokens["token"], "refresh_token": tokens["refresh_token"]})
                    fails = 0
                    continue
                else:
                    print("[REFRESH] All accounts failed auth — stopping.")
                    break

            if fails >= MAX_FAILS:
                # Try next account before giving up completely
                print(f"[REFRESH] {MAX_FAILS} consecutive failures — trying next account...")
                next_acc = switch_to_next_account(accounts, tokens)
                if next_acc:
                    tokens = next_acc
                    set_public_token_raw({"token": tokens["token"], "refresh_token": tokens["refresh_token"]})
                    fails = 0
                    continue
                print("[REFRESH] All accounts exhausted — stopping.")
                break

            time.sleep(30 * fails)

        except Exception as e:
            fails += 1
            print(f"[REFRESH] Error: {e} (fail {fails}/{MAX_FAILS})")
            traceback.print_exc()

            if fails >= MAX_FAILS:
                print(f"[REFRESH] {MAX_FAILS} consecutive failures — trying next account...")
                next_acc = switch_to_next_account(accounts, tokens)
                if next_acc:
                    tokens = next_acc
                    set_public_token_raw({"token": tokens["token"], "refresh_token": tokens["refresh_token"]})
                    fails = 0
                    continue
                print("[REFRESH] All accounts exhausted — stopping.")
                break

            time.sleep(30 * fails)


if __name__ == "__main__":
    main()
