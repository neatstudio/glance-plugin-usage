"""Best-effort usage lines for Cursor / Open Design / OpenCode Go / AGY / Devin.

Every number on the board is percent USED (higher = more consumed).
Vendors that only publish remaining (AGY remainingFraction, Devin remaining
percent) are converted with 100 - remaining.

Most rows are a 5-hour window and a week window. Cursor has no 5h pool: the
two bars are first-party Auto (`autoPercentUsed`) and third-party API
(`apiPercentUsed`) for the billing cycle.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import shutil
import sqlite3
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .ide_quota import chrome_safe_decrypt, trae_userinfo_decrypt

logger = logging.getLogger("usage")

# Paths and keys are set by the plugin through configure(); nothing is read
# from the environment. Files live under the plugin's data dir (the operator
# mounts them there), keys come from the hub's secret store.
CURSOR_DB = Path("/nonexistent/cursor-state.vscdb")
AMR_CONFIG = Path("/nonexistent/amr-config.json")
TRAE_STORAGE = Path("/nonexistent/trae-storage.json")
QODER_AUTH = Path("/nonexistent/qoder-auth.v1.dat")
QODER_SAFE_KEY = Path("/nonexistent/qoder-safe-storage")
OPENCODE_AUTH = Path("/nonexistent/opencode-auth.json")
OPENCODE_CONFIG = Path("/nonexistent/opencode.json")
OPENCODE_DB = Path("/nonexistent/opencode.db")
AGY_ACCOUNTS = Path("/nonexistent/agy-accounts.json")
DEVIN_DB = Path("/nonexistent/devin-state.vscdb")
DEVIN_CREDENTIALS = Path("/nonexistent/devin-credentials.toml")
AGY_USAGE_JSON = Path("/nonexistent/agy-usage.json")
AGY_CLIENT_ID = ""
AGY_CLIENT_SECRET = ""
KEYS: dict[str, str] = {}


def configure(secrets_dir: Path, cache_dir: Path, keys: dict[str, str]) -> None:
    """secrets_dir: mounted credential files; cache_dir: writable; keys: hub secrets."""
    global CURSOR_DB, AMR_CONFIG, TRAE_STORAGE, QODER_AUTH, QODER_SAFE_KEY
    global OPENCODE_AUTH, OPENCODE_CONFIG, OPENCODE_DB, AGY_ACCOUNTS, DEVIN_DB, DEVIN_CREDENTIALS
    global AGY_USAGE_JSON, AGY_CLIENT_ID, AGY_CLIENT_SECRET
    CURSOR_DB = secrets_dir / "cursor-state.vscdb"
    AMR_CONFIG = secrets_dir / "amr-config.json"
    TRAE_STORAGE = secrets_dir / "trae-storage.json"
    QODER_AUTH = secrets_dir / "qoder-auth.v1.dat"
    QODER_SAFE_KEY = secrets_dir / "qoder-safe-storage"
    OPENCODE_AUTH = secrets_dir / "opencode-auth.json"
    OPENCODE_CONFIG = secrets_dir / "opencode.json"
    OPENCODE_DB = secrets_dir / "opencode-share" / "opencode.db"
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


def _used_percent(used: object, limit: object) -> float | None:
    if isinstance(used, bool) or isinstance(limit, bool):
        return None
    if not isinstance(used, (int, float)) or not isinstance(limit, (int, float)):
        return None
    if limit <= 0:
        return None
    return 100.0 * float(used) / float(limit)


def _fraction_percent(value: object) -> float | None:
    """Qoder reports 0.73 for 73 percent. Values already on a 0–100 scale pass through."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number <= 1.5:
        number *= 100.0
    return number


def _qoder_password() -> str:
    if QODER_SAFE_KEY.exists():
        try:
            text = QODER_SAFE_KEY.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return text
    security = shutil.which("security")
    if not security:
        return ""
    try:
        result = subprocess.run(
            [security, "find-generic-password", "-s", "Qoder CN App Safe Storage", "-w"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


async def _qoder() -> dict:
    """Qoder CN plan credits. 套 is the plan pool, 总 is every pool together."""
    password = _qoder_password()
    if not password or not QODER_AUTH.exists():
        return _row("QODER", tag_5h="套", tag_week="总")
    try:
        auth = json.loads(chrome_safe_decrypt(QODER_AUTH.read_bytes(), password))
    except (OSError, ValueError, json.JSONDecodeError):
        logger.warning("qoder auth unreadable")
        return _row("QODER", tag_5h="套", tag_week="总")
    token = str(auth.get("token") or "").strip() if isinstance(auth, dict) else ""
    if not token:
        return _row("QODER", tag_5h="套", tag_week="总")
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.get(
            "https://openapi.qoder.com.cn/api/v2/quota/usage",  # Qoder CN, not qoder.com
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    if resp.status_code != 200:
        logger.warning("qoder quota %s", resp.status_code)
        return _row("QODER", tag_5h="套", tag_week="总")
    data = resp.json() if resp.content else {}
    if not isinstance(data, dict):
        return _row("QODER", tag_5h="套", tag_week="总")
    plan = data.get("userQuota") if isinstance(data.get("userQuota"), dict) else {}
    return _row(
        "QODER",
        pct_5h=_used_percent(plan.get("used"), plan.get("total")),
        pct_week=_fraction_percent(data.get("totalUsagePercentage")),
        tag_5h="套",
        tag_week="总",
    )


def _trae_userinfo() -> dict | None:
    if not TRAE_STORAGE.exists():
        return None
    try:
        stored = json.loads(TRAE_STORAGE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("trae storage unreadable")
        return None
    raw = stored.get("iCubeAuthInfo://icube.cloudide") if isinstance(stored, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        blob = base64.b64decode(raw + "=" * ((4 - len(raw) % 4) % 4))
        info = json.loads(trae_userinfo_decrypt(blob))
    except (ValueError, json.JSONDecodeError):
        logger.warning("trae auth unreadable")
        return None
    return info if isinstance(info, dict) else None


def _trae_international_host(info: dict) -> str:
    """International Trae only. api.trae.cn is the China app and is ignored."""
    host = str(info.get("host") or "").strip().rstrip("/")
    if host.startswith("https://") and "trae.ai" in host and "trae.cn" not in host:
        return host
    return "https://growsg-normal.trae.ai"


async def _trae() -> dict:
    """International Trae (trae.ai). 基 is the included dollar pool, 奖 is bonus."""
    info = _trae_userinfo() or {}
    token = str(info.get("token") or "").strip()
    if not token:
        return _row("TRAE", tag_5h="基", tag_week="奖")
    host = _trae_international_host(info)
    async with httpx.AsyncClient(timeout=12.0) as client:
        resp = await client.post(
            f"{host}/trae/api/v1/pay/ide_user_ent_usage",
            headers={"Authorization": f"Cloud-IDE-JWT {token}", "Accept": "application/json"},
            json={"require_usage": True},
        )
    if resp.status_code != 200:
        logger.warning("trae usage %s", resp.status_code)
        return _row("TRAE", tag_5h="基", tag_week="奖")
    data = resp.json() if resp.content else {}
    packs = data.get("user_entitlement_pack_list") if isinstance(data, dict) else None
    chosen = None
    for pack in packs or []:
        if isinstance(pack, dict) and pack.get("status") == 1 and not pack.get("is_hide"):
            chosen = pack
            break
    if not isinstance(chosen, dict):
        return _row("TRAE", tag_5h="基", tag_week="奖")
    base = chosen.get("entitlement_base_info") if isinstance(chosen.get("entitlement_base_info"), dict) else {}
    quota = base.get("quota") if isinstance(base.get("quota"), dict) else {}
    usage = chosen.get("usage") if isinstance(chosen.get("usage"), dict) else {}
    return _row(
        "TRAE",
        pct_5h=_used_percent(usage.get("basic_usage_amount"), quota.get("basic_usage_limit")),
        pct_week=_used_percent(usage.get("bonus_usage_amount"), quota.get("bonus_usage_limit")),
        tag_5h="基",
        tag_week="奖",
    )


async def fetch_all() -> list[dict]:
    cursor = trae = qoder = design = ocgo = agy = devin = None
    try:
        cursor = await _cursor()
    except Exception:
        logger.exception("cursor usage")
    try:
        trae = await _trae()
    except Exception:
        logger.exception("trae usage")
    try:
        qoder = await _qoder()
    except Exception:
        logger.exception("qoder usage")
    try:
        design = await _opendesign()
    except Exception:
        logger.exception("opendesign usage")
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
        trae or _row("TRAE", tag_5h="基", tag_week="奖"),
        qoder or _row("QODER", tag_5h="套", tag_week="总"),
        design or _row("DESIGN"),
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


def _credit_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _opendesign_used_pct(window: dict) -> float | None:
    used = _credit_number(window.get("usedCredits"))
    limit = _credit_number(window.get("limitCredits"))
    if used is None or limit is None or limit <= 0:
        return None
    return 100.0 * used / limit


def _opendesign_windows(payload: dict) -> tuple[float | None, float | None]:
    """Map preflight windows onto the 5h bar and the week bar.

    OpenDesign identifies a window by durationSeconds (18000 = 5h, 604800 = 7d).
    Credits arrive as strings. The board stores percent used.
    """
    plan = payload.get("codingPlan") if isinstance(payload.get("codingPlan"), dict) else {}
    windows = plan.get("windows") if isinstance(plan.get("windows"), list) else []
    five = None
    week = None
    for window in windows:
        if not isinstance(window, dict):
            continue
        duration = _credit_number(window.get("durationSeconds"))
        used = _opendesign_used_pct(window)
        if duration is None or used is None:
            continue
        if duration <= 5 * 3600 * 1.3:
            five = used if five is None else max(five, used)
        elif duration <= 7 * 24 * 3600 * 1.15:
            week = used if week is None else max(week, used)
    return five, week


def _read_amr_profile() -> tuple[str, str] | None:
    """Return (api base, control key) from the OpenDesign login file."""
    if not AMR_CONFIG.exists():
        return None
    try:
        data = json.loads(AMR_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("opendesign config unreadable")
        return None
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, dict):
        return None
    profile = profiles.get("prod")
    if not isinstance(profile, dict):
        profile = next(
            (item for item in profiles.values() if isinstance(item, dict) and item.get("controlKey")),
            None,
        )
    if not isinstance(profile, dict):
        return None
    key = str(profile.get("controlKey") or "").strip()
    if not key:
        return None
    base = str(profile.get("apiUrl") or "https://amr-api.open-design.ai").rstrip("/")
    return base, key


def _opendesign_workspace_id(payload: dict) -> str | None:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    first = None
    for item in items:
        if not isinstance(item, dict):
            continue
        workspace_id = str(item.get("workspaceId") or "").strip()
        if not workspace_id:
            continue
        if first is None:
            first = workspace_id
        if item.get("role") == "owner":
            return workspace_id
    return first


async def _opendesign() -> dict:
    """OpenDesign plan usage. Uses the control key in the login file, not an inference key."""
    found = _read_amr_profile()
    if not found:
        return _row("DESIGN")
    base, key = found
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "rlcd-usage/1.0",
    }
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
        listed = await client.get(f"{base}/api/v1/workspaces", headers=headers)
        if listed.status_code != 200:
            logger.warning("opendesign workspaces %s", listed.status_code)
            return _row("DESIGN")
        body = listed.json() if listed.content else {}
        workspace_id = _opendesign_workspace_id(body if isinstance(body, dict) else {})
        if not workspace_id:
            return _row("DESIGN")
        scoped = {**headers, "x-vela-workspace-id": workspace_id}
        resp = await client.get(
            f"{base}/api/v1/workspaces/{workspace_id}/billing/preflight",
            headers=scoped,
        )
        if resp.status_code != 200:
            logger.warning("opendesign preflight %s", resp.status_code)
            return _row("DESIGN")
        payload = resp.json() if resp.content else {}
    if not isinstance(payload, dict):
        return _row("DESIGN")
    five, week = _opendesign_windows(payload)
    return _row("DESIGN", pct_5h=five, pct_week=week)


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
    if OPENCODE_DB.exists():
        try:
            con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=5)
            try:
                rows = con.execute(
                    "SELECT value FROM credential WHERE label = 'OpenCode Go' AND ifnull(active, 1) = 1"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            logger.warning("opencode db unreadable")
            rows = []
        for (raw,) in rows:
            try:
                entry = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
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
    if bucket.get("status") not in (None, "ok", "rate-limited"):
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
                month = _bucket_pct(usage.get("monthly") or usage.get("month"))
                if month is not None and (week is None or month >= week):
                    return _row("OC.GO", pct_5h=five, pct_week=month, tag_5h="5h", tag_week="月")
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
