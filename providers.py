"""Best-effort usage lines for Cursor / Grok / OpenCode Go / AGY / Devin.

Every number on the board is percent USED (higher = more consumed).
Vendors that only publish remaining (AGY remainingFraction, Devin remaining
percent) are converted with 100 - remaining.

Most rows are a 5-hour window and a week window. Cursor has no 5h pool: the
two bars are first-party Auto (`autoPercentUsed`) and third-party API
(`apiPercentUsed`) for the billing cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

logger = logging.getLogger("usage")

# Paths and keys are set by the plugin through configure(); nothing is read
# from the environment. Files live under the plugin's data dir (the operator
# mounts them there), keys come from the hub's secret store.
CURSOR_DB = Path("/nonexistent/cursor-state.vscdb")
GROK_AUTH = Path("/nonexistent/grok/auth.json")
OPENCODE_AUTH = Path("/nonexistent/opencode-auth.json")
OPENCODE_CONFIG = Path("/nonexistent/opencode.json")
AGY_ACCOUNTS = Path("/nonexistent/agy-accounts.json")
DEVIN_DB = Path("/nonexistent/devin-state.vscdb")
DEVIN_CREDENTIALS = Path("/nonexistent/devin-credentials.toml")
AGY_USAGE_JSON = Path("/nonexistent/agy-usage.json")
AGY_CLIENT_ID = ""
AGY_CLIENT_SECRET = ""
KEYS: dict[str, str] = {}


def configure(secrets_dir: Path, cache_dir: Path, keys: dict[str, str]) -> None:
    """secrets_dir: mounted credential files; cache_dir: writable; keys: hub secrets."""
    global CURSOR_DB, GROK_AUTH, OPENCODE_AUTH, OPENCODE_CONFIG, AGY_ACCOUNTS, DEVIN_DB, DEVIN_CREDENTIALS
    global AGY_USAGE_JSON, AGY_CLIENT_ID, AGY_CLIENT_SECRET
    CURSOR_DB = secrets_dir / "cursor-state.vscdb"
    GROK_AUTH = secrets_dir / "grok" / "auth.json"
    OPENCODE_AUTH = secrets_dir / "opencode-auth.json"
    OPENCODE_CONFIG = secrets_dir / "opencode.json"
    AGY_ACCOUNTS = secrets_dir / "agy-accounts.json"
    DEVIN_DB = secrets_dir / "devin-state.vscdb"
    DEVIN_CREDENTIALS = secrets_dir / "devin-credentials.toml"
    AGY_USAGE_JSON = cache_dir / "agy-usage.json"
    AGY_CLIENT_ID = keys.get("agy_client_id", "")
    AGY_CLIENT_SECRET = keys.get("agy_client_secret", "")
    KEYS.clear()
    KEYS.update(keys)


DEVIN_SEAT_RPC = "exa.seat_management_pb.SeatManagementService/GetUserStatus"


def _clip(used_pct: float | None) -> float | None:
    if used_pct is None:
        return None
    return max(0.0, min(100.0, float(used_pct)))


def _row(
    name: str,
    pct_5h: float | None = None,
    pct_week: float | None = None,
    extra: str = "",
    tag_5h: str = "5h",
    tag_week: str = "W",
) -> dict:
    five = _clip(pct_5h)
    week = _clip(pct_week)
    primary = week if week is not None else five
    return {
        "name": name,
        "pct_5h": five,
        "pct_week": week,
        "pct": primary,
        "note": extra or "",
        "tag_5h": tag_5h or "5h",
        "tag_week": tag_week or "W",
    }


async def fetch_all() -> list[dict]:
    cursor = grok = ocgo = agy = devin = None
    try:
        cursor = await _cursor()
    except Exception:
        logger.exception("cursor usage")
    try:
        grok = await _grok()
    except Exception:
        logger.exception("grok usage")
    try:
        ocgo = await _opencode_go()
    except Exception:
        logger.exception("opencode-go usage")
    try:
        agy = await _agy()
    except Exception:
        logger.exception("agy usage")
    try:
        devin = await _devin()
    except Exception:
        logger.exception("devin usage")
    return [
        cursor or _row("CURSOR"),
        grok or _row("GROK"),
        ocgo or _row("OC.GO"),
        agy or _row("AGY"),
        devin or _row("DEVIN"),
    ]



def _sqlite_rows(path: Path, sql: str) -> list:
    """Query a possibly huge app database in place: read-only, immutable, no copy."""
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, timeout=5)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()

def _read_cursor_token() -> str | None:
    try:
        rows = _sqlite_rows(CURSOR_DB, "SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'")
    except Exception:
        logger.warning("cursor db unreadable")
        return None
    return str(rows[0][0]) if rows and rows[0][0] else None


async def _cursor() -> dict:
    token = await asyncio.to_thread(_read_cursor_token)
    if not token:
        return _row("CURSOR")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": "rlcd-usage/1.0",
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.post(
            "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage",
            headers=headers,
            content=b"{}",
        )
        resp.raise_for_status()
        data = resp.json()
    plan = data.get("planUsage") or {}
    # Cursor splits included usage: Auto (1st-party) vs API (3rd-party).
    # totalPercentUsed is a blend (~15) and hid the 30% API bucket.
    auto = plan.get("autoPercentUsed")
    api = plan.get("apiPercentUsed")
    five = float(auto) if isinstance(auto, (int, float)) else None
    week = float(api) if isinstance(api, (int, float)) else None
    return _row("CURSOR", pct_5h=five, pct_week=week, tag_5h="1方", tag_week="3方")


def _read_grok_auth() -> dict | None:
    if not GROK_AUTH.exists():
        return None
    try:
        auth = json.loads(GROK_AUTH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("grok auth unreadable")
        return None
    return auth if isinstance(auth, dict) else None


def _grok_account(auth: dict) -> tuple[str, dict] | None:
    """Return the OIDC session that can be refreshed, else any session with a key."""
    oidc: tuple[str, dict] | None = None
    keyed: tuple[str, dict] | None = None
    for name, value in auth.items():
        if not isinstance(value, dict):
            continue
        if value.get("key") and keyed is None:
            keyed = (str(name), value)
        if value.get("auth_mode") == "oidc" and value.get("refresh_token"):
            oidc = (str(name), value)
            break
    return oidc or keyed


def _grok_expires_at(account: dict) -> datetime | None:
    raw = account.get("expires_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _grok_token_fresh(account: dict, skew_seconds: int = 120) -> bool:
    token = str(account.get("key") or "").strip()
    if not token:
        return False
    expires_at = _grok_expires_at(account)
    if expires_at is None:
        return True
    return expires_at > datetime.now(timezone.utc) + timedelta(seconds=skew_seconds)


def _write_grok_auth(auth: dict) -> None:
    """Atomically replace auth.json inside its directory so a file bind-mount is not pinned to a deleted inode."""
    payload = json.dumps(auth, indent=2) + "\n"
    temporary = GROK_AUTH.with_name(GROK_AUTH.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, GROK_AUTH)
    os.chmod(GROK_AUTH, 0o600)


async def _grok_refresh(
    client: httpx.AsyncClient,
    auth: dict,
    account_key: str,
    account: dict,
) -> str | None:
    """Exchange refresh_token for a new access token and store it next to the old session."""
    refresh = str(account.get("refresh_token") or "").strip()
    client_id = str(account.get("oidc_client_id") or "").strip()
    issuer = str(account.get("oidc_issuer") or "https://auth.x.ai").rstrip("/")
    if not refresh or not client_id:
        logger.warning("grok refresh skipped: missing refresh token or client id")
        return None
    resp = await client.post(
        f"{issuer}/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        },
        headers={"Accept": "application/json"},
    )
    if resp.status_code != 200:
        logger.warning("grok token refresh %s", resp.status_code)
        return None
    body = resp.json() if resp.content else {}
    if not isinstance(body, dict):
        return None
    access = body.get("access_token")
    if not isinstance(access, str) or not access.strip():
        return None
    account["key"] = access
    rotated = body.get("refresh_token")
    if isinstance(rotated, str) and rotated.strip():
        account["refresh_token"] = rotated
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
        account["expires_at"] = expires_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    auth[account_key] = account
    try:
        _write_grok_auth(auth)
        logger.info("grok token refreshed")
    except OSError:
        logger.warning("grok token refreshed in memory; auth file was not updated")
    return access


def _grok_week_percent(data: dict) -> float | None:
    cfg = data.get("config") or data
    if not isinstance(cfg, dict):
        return None
    pct = cfg.get("creditUsagePercent")
    if not isinstance(pct, (int, float)):
        for product in cfg.get("productUsage") or []:
            if isinstance(product, dict) and isinstance(product.get("usagePercent"), (int, float)):
                pct = product["usagePercent"]
                break
    if not isinstance(pct, (int, float)):
        return None
    return float(pct)


async def _grok_billing(client: httpx.AsyncClient, token: str) -> tuple[int, float | None]:
    resp = await client.get(
        "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if resp.status_code != 200:
        logger.warning("grok billing %s", resp.status_code)
        return resp.status_code, None
    data = resp.json() if resp.content else {}
    if not isinstance(data, dict):
        return resp.status_code, None
    return resp.status_code, _grok_week_percent(data)


def _grok_week_row(week: float | None) -> dict:
    if week is None:
        return _row("GROK")
    # No separate 5h pool from this API — show weekly on both bars so the
    # left cell is not a permanent "5h —" dead slot.
    return _row("GROK", pct_5h=week, pct_week=week, tag_5h="周", tag_week="W")


async def _grok_from_api_key() -> dict:
    api_key = KEYS.get("xai", "").strip()
    if not api_key:
        return _row("GROK")
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(
            "https://api.x.ai/v1/api-key",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code != 200:
            return _row("GROK")
        data = resp.json()
    remaining = data.get("remaining_credits") or data.get("credits")
    limit = data.get("monthly_limit") or data.get("limit")
    if isinstance(remaining, (int, float)) and isinstance(limit, (int, float)) and limit:
        week = 100.0 * (1.0 - remaining / limit)
        return _row("GROK", pct_5h=week, pct_week=week, tag_5h="月", tag_week="W")
    return _row("GROK")


async def _grok() -> dict:
    """Grok/xAI quota. Billing is weekly-only. Access tokens last ~6h; refresh them."""
    auth = _read_grok_auth()
    found = _grok_account(auth) if auth else None
    if not auth or not found:
        return await _grok_from_api_key()
    account_key, account = found
    async with httpx.AsyncClient(timeout=12.0) as client:
        token = str(account.get("key") or "").strip()
        if not _grok_token_fresh(account):
            refreshed = await _grok_refresh(client, auth, account_key, account)
            token = refreshed or token
        if not token:
            return await _grok_from_api_key()
        status, week = await _grok_billing(client, token)
        if status == 401:
            refreshed = await _grok_refresh(client, auth, account_key, account)
            if refreshed:
                _status, week = await _grok_billing(client, refreshed)
        return _grok_week_row(week)


def _opencode_keys() -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()

    def add(raw: object) -> None:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.add(text)
            keys.append(text)

    add(KEYS.get("opencode", ""))
    add(KEYS.get("opencode_go", ""))
    if OPENCODE_AUTH.exists():
        try:
            auth = json.loads(OPENCODE_AUTH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            auth = {}
        if isinstance(auth, dict):
            for name in ("opencode-go", "opencode"):
                entry = auth.get(name)
                if isinstance(entry, dict):
                    add(entry.get("key"))
    if OPENCODE_CONFIG.exists():
        try:
            cfg = json.loads(OPENCODE_CONFIG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cfg = {}
        providers = cfg.get("provider") if isinstance(cfg, dict) else None
        if isinstance(providers, dict):
            for name in ("openai", "opencode-go", "opencode", "coding-plan"):
                entry = providers.get(name)
                if isinstance(entry, dict):
                    options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
                    add(options.get("apiKey") or entry.get("apiKey"))
    return keys


def _bucket_pct(bucket: dict | None) -> float | None:
    if not isinstance(bucket, dict):
        return None
    if bucket.get("status") not in (None, "ok"):
        return None
    value = bucket.get("percent")
    if isinstance(value, (int, float)):
        return float(value)
    used = bucket.get("used")
    limit = bucket.get("limit") or bucket.get("quota")
    if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit:
        return 100.0 * used / limit
    return None


def _tryseo_windows(data: dict) -> tuple[float | None, float | None]:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    today = usage.get("today") if isinstance(usage.get("today"), dict) else {}
    five = 0.0 if today else None
    week = 0.0
    daily = data.get("daily_usage") if isinstance(data.get("daily_usage"), list) else []
    if daily:
        week = 0.0
    if data.get("isValid") or data.get("mode"):
        five = 0.0 if five is None else five
        week = 0.0 if week is None else week
    remaining = data.get("remaining")
    if isinstance(remaining, (int, float)) and remaining <= 0:
        five = 100.0
        week = 100.0
    return five, week


async def _opencode_go() -> dict:
    keys = _opencode_keys()
    if not keys:
        return _row("OC.GO")
    headers_base = {"Accept": "application/json", "User-Agent": "rlcd-usage/1.0"}
    async with httpx.AsyncClient(timeout=12.0) as client:
        for key in keys:
            headers = {**headers_base, "Authorization": f"Bearer {key}"}
            try:
                resp = await client.get(
                    "https://opencode.ai/zen/go/v1/usage",
                    headers=headers,
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                resp = None
            if resp is not None and resp.status_code == 200:
                data = resp.json()
                usage = (data.get("usage") or data) if isinstance(data, dict) else {}
                five = _bucket_pct(usage.get("rolling") or usage.get("five_hour") or usage.get("5h"))
                week = _bucket_pct(usage.get("weekly") or usage.get("week"))
                return _row("OC.GO", pct_5h=five, pct_week=week)
            else:
                logger.info("opencode zen/go usage %s", getattr(resp, "status_code", None))
    # tryseo /v1/usage is a USD wallet (planName 钱包余额), not Go 5h/week quota.
    return _row("OC.GO")


def _parse_reset(reset: str | None) -> datetime | None:
    if not reset or not isinstance(reset, str):
        return None
    try:
        return datetime.fromisoformat(reset.replace("Z", "+00:00"))
    except ValueError:
        return None


def _classify_fraction_windows(items: list[tuple[float, datetime | None]]) -> tuple[float | None, float | None]:
    now = datetime.now(timezone.utc)
    five = None
    week = None
    for remaining, reset in items:
        used = 100.0 * (1.0 - remaining)
        if reset is None:
            continue
        hours = (reset - now).total_seconds() / 3600.0
        if 0 <= hours <= 6.5:
            five = used if five is None else max(five, used)
        elif 0 <= hours <= 8 * 24:
            week = used if week is None else max(week, used)
    return five, week


def _agy_from_cache() -> tuple[float | None, float | None]:
    if not AGY_ACCOUNTS.exists():
        return None, None
    try:
        data = json.loads(AGY_ACCOUNTS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, None
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not accounts:
        return None, None
    account = accounts[0]
    updated_ms = account.get("cachedQuotaUpdatedAt")
    if isinstance(updated_ms, (int, float)):
        age_h = (datetime.now(timezone.utc).timestamp() - updated_ms / 1000.0) / 3600.0
        if age_h > 24:
            return None, None
    quota = account.get("cachedQuota") or {}
    items = []
    for bucket in quota.values():
        if not isinstance(bucket, dict):
            continue
        frac = bucket.get("remainingFraction")
        if not isinstance(frac, (int, float)):
            continue
        items.append((float(frac), _parse_reset(bucket.get("resetTime"))))
    return _classify_fraction_windows(items)


def _agy_windows_from_groups(groups: list) -> tuple[float | None, float | None]:
    """Parse `agy -p /usage` groups. TUI prints remaining; board wants used."""
    five_remain: dict[str, float] = {}
    week_remain: dict[str, float] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or group.get("displayName") or "")
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            frac = bucket.get("remaining_fraction")
            if frac is None:
                frac = bucket.get("remainingFraction")
            if not isinstance(frac, (int, float)):
                continue
            window = str(bucket.get("window") or "").lower()
            if window in ("5h", "five_hour", "five-hour"):
                five_remain[name] = float(frac)
            elif window in ("weekly", "week"):
                week_remain[name] = float(frac)
    week_frac = min(week_remain.values()) if week_remain else None
    five_frac = None
    for key, frac in five_remain.items():
        if "gemini" in key.lower():
            five_frac = frac
            break
    if five_frac is None and five_remain:
        five_frac = max(five_remain.values())
    five_used = 100.0 * (1.0 - five_frac) if five_frac is not None else None
    week_used = 100.0 * (1.0 - week_frac) if week_frac is not None else None
    return five_used, week_used


def _agy_from_cli_json() -> tuple[float | None, float | None]:
    if not AGY_USAGE_JSON.exists():
        return None, None
    try:
        data = json.loads(AGY_USAGE_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    cmd = data.get("command") if isinstance(data.get("command"), dict) else {}
    inner = cmd.get("data") if isinstance(cmd.get("data"), dict) else data
    groups = inner.get("groups") if isinstance(inner, dict) else None
    if not isinstance(groups, list):
        return None, None
    return _agy_windows_from_groups(groups)


def _agy_account() -> dict | None:
    if not AGY_ACCOUNTS.exists():
        return None
    try:
        data = json.loads(AGY_ACCOUNTS.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not accounts:
        return None
    account = accounts[0]
    return account if isinstance(account, dict) else None


async def _agy_live() -> tuple[float | None, float | None]:
    account = _agy_account()
    if not account:
        return None, None
    refresh = str(account.get("refreshToken") or "").strip()
    if not refresh:
        return None, None
    project = str(account.get("managedProjectId") or account.get("projectId") or "")
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": AGY_CLIENT_ID,
                "client_secret": AGY_CLIENT_SECRET,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        if token_resp.status_code != 200:
            logger.warning("agy token refresh %s", token_resp.status_code)
            return None, None
        access = (token_resp.json() or {}).get("access_token")
        if not access:
            return None, None
        headers = {
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity/windows/amd64",
        }
        resp = await client.post(
            "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
            headers=headers,
            json={},
        )
        if resp.status_code != 200:
            logger.warning("agy quota summary %s", resp.status_code)
            return None, None
        payload = resp.json() or {}
    five = None
    week = None
    for group in payload.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            frac = bucket.get("remainingFraction")
            if not isinstance(frac, (int, float)):
                continue
            used = 100.0 * (1.0 - float(frac))
            window = str(bucket.get("window") or "").lower()
            if window in ("5h", "five_hour", "five-hour"):
                five = used if five is None else max(five, used)
            elif window in ("weekly", "week"):
                week = used if week is None else max(week, used)
    return five, week


async def _agy() -> dict:
    five, week = _agy_from_cli_json()
    if five is None and week is None:
        five, week = await _agy_live()
    if five is None and week is None:
        five, week = _agy_from_cache()
    return _row("AGY", pct_5h=five, pct_week=week)


def _devin_remaining_to_used(value: object) -> float | None:
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return 100.0 - float(value)
    return None


def _devin_credentials() -> dict:
    if not DEVIN_CREDENTIALS.exists():
        return {}
    try:
        data = tomllib.loads(DEVIN_CREDENTIALS.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        logger.warning("devin credentials unreadable")
        return {}
    return data if isinstance(data, dict) else {}


def _devin_api_key() -> str | None:
    env = KEYS.get("devin", "").strip()
    if env:
        return env
    key = _devin_credentials().get("windsurf_api_key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def _devin_seat_url() -> str:
    override = KEYS.get("devin_seat_url", "").strip()
    if override:
        return override
    base = str(_devin_credentials().get("api_server_url") or "").rstrip("/")
    if not base:
        base = "https://server.self-serve.windsurf.com"
    return f"{base}/{DEVIN_SEAT_RPC}"


async def _devin_live() -> tuple[float | None, float | None]:
    key = _devin_api_key()
    if not key:
        return None, None
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "User-Agent": "rlcd-usage/1.0",
    }
    body = {
        "metadata": {
            "ideName": "devin",
            "ideVersion": "1.104.0",
            "extensionName": "windsurf",
            "extensionVersion": "1.104.0",
            "apiKey": key,
            "locale": "en",
            "os": "linux",
            "hardware": "arm64",
        }
    }
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.post(_devin_seat_url(), headers=headers, json=body)
        if resp.status_code != 200:
            logger.warning("devin GetUserStatus %s", resp.status_code)
            return None, None
        data = resp.json() if resp.content else {}
    if not isinstance(data, dict):
        return None, None
    user = data.get("userStatus") or data.get("user_status") or {}
    plan = user.get("planStatus") or user.get("plan_status") or {}
    if not isinstance(plan, dict):
        return None, None
    five = _devin_remaining_to_used(
        plan.get("dailyQuotaRemainingPercent") or plan.get("daily_quota_remaining_percent")
    )
    week = _devin_remaining_to_used(
        plan.get("weeklyQuotaRemainingPercent") or plan.get("weekly_quota_remaining_percent")
    )
    return five, week


def _devin_from_cache() -> tuple[float | None, float | None]:
    try:
        rows = _sqlite_rows(DEVIN_DB, "SELECT key, value FROM ItemTable WHERE key LIKE 'windsurf.reactSettings.cachedPlanInfoData:%'")
    except Exception:
        logger.warning("devin db unreadable")
        return None, None
    best = None
    for _key, raw in rows:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("isDevinUser") or best is None:
            best = data
    if not best:
        return None, None
    daily = best.get("dailyRemainingPercent")
    weekly = best.get("weeklyRemainingPercent")
    five = 100.0 - float(daily) if isinstance(daily, (int, float)) else None
    week = 100.0 - float(weekly) if isinstance(weekly, (int, float)) else None
    return five, week


async def _devin() -> dict:
    five, week = await _devin_live()
    if five is None and week is None:
        five, week = await asyncio.to_thread(_devin_from_cache)
    return _row("DEVIN", pct_5h=five, pct_week=week)
