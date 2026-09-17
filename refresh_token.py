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
    is_expired,
    seconds_until_expiry,
    get_env_accounts,
    refresh_account_token,
    update_env_token,
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
    Refresh every env account (TOKEN_1, TOKEN_2, etc.) on each cycle.
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


def load_tokens(accounts: list[dict]) -> dict:
    """
    Pick the active token to use as the public token.
    Priority: existing storage → first valid account → error.
    """
    data = get_public_token_raw()
    if data.get("token") and data.get("refresh_token") and not is_expired(data["token"]):
        print("[REFRESH] Tokens loaded from storage")
        return data

    acc = get_active_account(accounts)
    if not acc:
        raise ValueError(
            "No valid accounts found.\n"
            "Set TOKEN_1/REFRESH_TOKEN_1 (and TOKEN_2/REFRESH_TOKEN_2, etc.) in Railway Variables."
        )

    print(f"[REFRESH] Loading from env — using {acc['label']}")
    set_public_token_raw({"token": acc["token"], "refresh_token": acc["refresh_token"]})
    return {"token": acc["token"], "refresh_token": acc["refresh_token"]}


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

    with request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    tokens["token"]         = data.get("token",         tokens["token"])
    tokens["refresh_token"] = data.get("refresh_token", tokens["refresh_token"])

    set_public_token_raw(tokens)

    ttl      = seconds_until_expiry(tokens["token"])
    exp_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(decode_jwt_exp(tokens["token"])))
    print(f"[REFRESH] Token refreshed! Expires: {exp_time} (in {ttl}s)")

    return tokens


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
    tokens.setdefault("label", accounts[0]["label"])

    try:
        primary = refresh_all_env_accounts(accounts)
        if primary:
            tokens.update(primary)
    except Exception as e:
        print(f"[REFRESH] Refresh failed: {e}")
        traceback.print_exc()
        if not args.loop:
            return

    if not args.loop:
        return

    # ── Loop ──────────────────────────────────────────────────────────────────
    print(f"[REFRESH] Loop active — every {REFRESH_INTERVAL}s\n")
    fails = 0
    MAX_FAILS = 5

    while True:
        try:
            primary = refresh_all_env_accounts(accounts)
            if primary:
                tokens.update(primary)

            ttl = seconds_until_expiry(tokens["token"])
            print(f"[REFRESH] All accounts refreshed — public token valid for {ttl}s, sleeping {REFRESH_INTERVAL}s...")
            time.sleep(REFRESH_INTERVAL)
            fails = 0

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
