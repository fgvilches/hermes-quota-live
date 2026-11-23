"""hermes-quota backend: OQG quota/usage snapshot with graceful Hermes fallback.

Mounted by Hermes at /api/plugins/hermes-quota/ (dashboard/manifest.json declares
``api: plugin_api.py`` and the module exposes a FastAPI ``router``).

Sources, in priority order:
  * opencode-quota-gui (OQG) on 127.0.0.1:4144 for real quota cards/analytics;
  * the active profile's ~/.hermes/state.db (read-only SQLite) for real usage
    analytics/models whenever OQG is unavailable or partial.

No credentials are read, forwarded or logged. OQG calls are loopback-only,
with a hard timeout below 2 seconds. Every number returned comes from one of
those two sources; no value is fabricated.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from fastapi import APIRouter, Body, Query

router = APIRouter()

PLUGIN_ID = "hermes-quota"
OQG_BASE_URL = "http://127.0.0.1:4144"
OQG_TIMEOUT = 1.5  # hard ceiling: never block the renderer for more than 2s
CACHE_TTL_SECONDS = 5.0
RANGE_KEYS = ("24h", "7d", "30d", "lifetime")
RANGE_SECONDS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "lifetime": None}

NO_QUOTA_NOTE = (
    "Sin fuente de cuota: opencode-quota-gui no responde en 127.0.0.1:4144. "
    "Inicie el plugin GUI de OpenCode (o `opencode-quota-gui`) para ver ventanas "
    "de cuota reales; el uso y la analítica mostrados vienen de ~/.hermes/state.db."
)
DB_NOTE = "Uso/analítica leídos de ~/.hermes/state.db (datos reales de sesiones locales)."
CACHE_WRITE_NOTE = "totalTokens incluye cache_write_tokens; el contrato no expone esa categoría por separado."
UTC_NOTE = "Las fechas daily están agrupadas en UTC."
NO_API_EQUIV_NOTE = "apiEquivalent no disponible sin el motor de precios de opencode-quota-gui."

#: Test seam. When set, this callable replaces urllib.request.urlopen.
DEFAULT_OPENER: Optional[Callable[..., Any]] = None
_CACHE: Dict[str, Any] = {"key": None, "data": None, "expires": 0.0}


# --- small helpers ---------------------------------------------------------

def normalize_range(value: Optional[str]) -> str:
    """Return a supported range key, defaulting to 7d for anything else."""
    return value if value in RANGE_KEYS else "7d"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: Any) -> Optional[float]:
    """Numeric JSON value (bools excluded), or None. Keeps integers integral."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return int(value) if float(value).is_integer() else value


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _iso_date_from_ms(value: Any) -> Optional[str]:
    ms = _number(value)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _iso_date_from_s(value: Any) -> Optional[str]:
    seconds = _number(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _err_text(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return "%s: %s" % (type(exc).__name__, message[:160])


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def state_db_path() -> Path:
    return hermes_home() / "state.db"


def invalidate_cache() -> None:
    _CACHE.update(key=None, data=None, expires=0.0)


# --- OQG transport ---------------------------------------------------------

def fetch_json(
    path: str,
    params: Optional[Mapping[str, str]] = None,
    *,
    base_url: str = OQG_BASE_URL,
    timeout: float = OQG_TIMEOUT,
    opener: Optional[Callable[..., Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """GET one OQG JSON endpoint. Never raises; returns (payload, error)."""
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with (opener or DEFAULT_OPENER or urllib.request.urlopen)(request, timeout=timeout) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8")) if isinstance(raw, bytes) else json.loads(raw)
    except Exception as exc:  # URLError, timeout, bad JSON, mocked failures
        return None, "%s %s" % (path, _err_text(exc))
    if not isinstance(payload, dict):
        return None, "%s: respuesta JSON no es un objeto" % path
    return payload, None


# --- OQG -> contract mapping ----------------------------------------------

def _identity_id(value: Any) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    if value.get("kind") == "known":
        return _text(value.get("id"))
    return None


def _costed_tokens(value: Any) -> Dict[str, Any]:
    """Copy only the additive token fields that the contract exposes."""
    source = value if isinstance(value, dict) else {}
    result: Dict[str, Any] = {}
    for key in ("total", "input", "output", "cacheRead", "reasoning"):
        parsed = _number(source.get(key))
        if parsed is not None:
            result[key] = parsed
    api = _number(source.get("apiEquivalentCostUsd"))
    if api is not None:
        result["apiEquivalent"] = api
    return result


def _is_quota_window(row: Mapping[str, Any]) -> bool:
    """Rows with a quota window measurement or a quota/rate-limit result."""
    if row.get("resultType") in ("quota", "rate_limit"):
        return True
    if row.get("kind") in (
        "aggregate", "rpm", "hour", "five_hour", "day", "week", "month", "year", "mcp", "code_review"
    ):
        return True
    return any(k in row for k in ("percentRemaining", "percentUsed", "resetAtIso", "quantity"))


def map_quota_cards(quota: Mapping[str, Any]) -> List[Dict[str, Any]]:
    providers = quota.get("providers") if isinstance(quota.get("providers"), dict) else {}
    raw_cards = providers.get("cards") if isinstance(providers.get("cards"), list) else []
    cards: List[Dict[str, Any]] = []
    for raw in raw_cards:
        if not isinstance(raw, dict):
            continue
        provider = _text(raw.get("providerId")) or "unknown"
        card: Dict[str, Any] = {
            "id": _text(raw.get("cardId")) or provider,
            "provider": provider,
            "label": _text(raw.get("providerLabel")) or provider,
            "active": not bool(raw.get("stale")),
            "windows": [],
        }
        account = _text(raw.get("accountLabel"))
        plan = _text(raw.get("planLabel"))
        if account:
            card["account"] = account
        if plan:
            card["plan"] = plan
        errors = []
        for entry in raw.get("errors") if isinstance(raw.get("errors"), list) else []:
            message = _text(entry.get("message")) if isinstance(entry, dict) else None
            if message:
                errors.append(message[:300])
        if errors:
            card["error"] = " | ".join(errors[:5])

        for row in raw.get("rows") if isinstance(raw.get("rows"), list) else []:
            if not isinstance(row, dict) or not _is_quota_window(row):
                continue
            window: Dict[str, Any] = {
                "id": _text(row.get("rowId")) or _text(row.get("label")) or "row",
                "label": _text(row.get("label")) or _text(row.get("kind")) or "window",
            }
            remaining = _number(row.get("percentRemaining"))
            used = _number(row.get("percentUsed"))
            if remaining is not None:
                window["remainingPct"] = remaining
            if used is not None:
                window["used"] = used
            if remaining is not None or used is not None:
                window["limit"] = 100  # percentages are on a 0..100 scale
                window["unit"] = "percent"
            else:
                quantity = row.get("quantity")
                if isinstance(quantity, dict):
                    unit = quantity.get("unit")
                    if isinstance(unit, dict):
                        symbol = (
                            _text(unit.get("unit")) or _text(unit.get("code")) or _text(unit.get("symbol"))
                        )
                        if symbol:
                            window["unit"] = symbol
                    decimal = _text(quantity.get("decimal"))
                    if decimal:
                        window["used"] = decimal  # already formatted by OQG
            reset = _text(row.get("resetAtIso"))
            if reset:
                window["resetAt"] = reset
            status = (
                _text(row.get("resultType"))
                or _text(row.get("kind"))
                or _text(row.get("value"))
                or _text(row.get("countdown"))
            )
            if status:
                window["status"] = status
            card["windows"].append(window)
        cards.append(card)
    return cards


def map_analytics(home: Mapping[str, Any], range_key: str) -> Dict[str, Any]:
    usage = home.get("usage") if isinstance(home.get("usage"), dict) else {}
    tokens = _costed_tokens(usage.get("totals"))
    analytics: Dict[str, Any] = {
        "range": range_key,  # always the range the caller asked for
        "totalTokens": tokens.get("total", 0),
        "cacheRead": tokens.get("cacheRead", 0),
        "input": tokens.get("input", 0),
        "reasoning": tokens.get("reasoning", 0),
        "output": tokens.get("output", 0),
        "daily": [],
        "projects": [],
    }
    if "apiEquivalent" in tokens:
        analytics["apiEquivalent"] = tokens["apiEquivalent"]

    by_date: Dict[str, float] = {}
    for bucket in usage.get("buckets") if isinstance(usage.get("buckets"), list) else []:
        if not isinstance(bucket, dict):
            continue
        date = _iso_date_from_ms(bucket.get("startMs"))
        if date is None:
            continue
        bucket_tokens = _costed_tokens(bucket.get("tokens"))
        by_date[date] = by_date.get(date, 0) + bucket_tokens.get("total", 0)
    analytics["daily"] = [{"date": date, "tokens": total} for date, total in sorted(by_date.items())]

    for project in home.get("projects") if isinstance(home.get("projects"), list) else []:
        if not isinstance(project, dict):
            continue
        project_id = _text(project.get("projectId"))
        if project_id is None:
            continue
        project_tokens = _costed_tokens(project.get("tokens"))
        analytics["projects"].append({
            "id": project_id,
            "label": _text(project.get("name")) or project_id,
            "tokens": project_tokens.get("total", 0),
            "sessions": _number(project.get("rootSessionCount")) or 0,
        })
    return analytics


def map_models(group: Mapping[str, Any], range_key: str) -> List[Dict[str, Any]]:
    dimension = group.get("dimension")
    rows = group.get("rows") if isinstance(group.get("rows"), list) else []
    models: List[Dict[str, Any]] = []

    def add(model: str, provider: str, raw_tokens: Any) -> None:
        tokens = _costed_tokens(raw_tokens)
        entry: Dict[str, Any] = {
            "provider": provider,
            "model": model,
            "tokens": tokens.get("total", 0),
            "input": tokens.get("input", 0),
            "output": tokens.get("output", 0),
            "cacheRead": tokens.get("cacheRead", 0),
            "reasoning": tokens.get("reasoning", 0),
        }
        if "apiEquivalent" in tokens:
            entry["apiEquivalent"] = tokens["apiEquivalent"]
        models.append(entry)

    for row in rows:
        if not isinstance(row, dict):
            continue
        parent_id = _identity_id(row.get("identity")) or "unknown"
        children = [c for c in row.get("children") if isinstance(c, dict)] if isinstance(row.get("children"), list) else []
        if dimension == "provider":
            for child in children:
                model_id = _identity_id(child.get("identity"))
                if model_id:
                    add(model_id, parent_id, child.get("tokens"))
        else:
            provider = "unknown"
            for child in children:
                candidate = _identity_id(child.get("identity"))
                if candidate:
                    provider = candidate
                    break
            add(parent_id, provider, row.get("tokens"))
    models.sort(key=lambda item: item["tokens"], reverse=True)
    return models


# --- Hermes state.db mapping (fallback) ------------------------------------

def _empty_analytics(range_key: str) -> Dict[str, Any]:
    return {
        "range": range_key,
        "totalTokens": 0,
        "cacheRead": 0,
        "input": 0,
        "reasoning": 0,
        "output": 0,
        "daily": [],
        "projects": [],
    }


def read_hermes_usage(
    db_path: Optional[Path] = None,
    range_key: str = "7d",
    now_s: Optional[float] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Read real local usage from state.db; returns (analytics, models, cards, notes).

    Read-only URI connection, no writes, no credentials. Missing/corrupt DB or
    schema yields an empty dataset plus an explanatory note instead of raising.
    """
    range_key = normalize_range(range_key)
    analytics = _empty_analytics(range_key)
    notes: List[str] = []
    path = Path(db_path) if db_path is not None else state_db_path()
    if not path.is_file():
        return analytics, [], _degraded_cards([]), ["No se encontró %s; analítica vacía." % path]
    now_s = time.time() if now_s is None else now_s
    window = RANGE_SECONDS[range_key]
    since_s = None if window is None else now_s - window

    try:
        conn = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(str(path)), uri=True, timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT u.session_id, u.model, u.billing_provider, u.last_seen, "
                "u.api_call_count, u.input_tokens, u.output_tokens, u.cache_read_tokens, "
                "u.cache_write_tokens, u.reasoning_tokens, "
                "s.cwd, s.git_repo_root, s.display_name, s.title, "
                "s.billing_provider AS session_billing_provider "
                "FROM session_model_usage u LEFT JOIN sessions s ON s.id = u.session_id"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return analytics, [], _degraded_cards([]), ["state.db ilegible (%s); analítica vacía." % _err_text(exc)]

    day_tokens: Dict[str, float] = {}
    models: Dict[Tuple[str, str], Dict[str, Any]] = {}
    projects: Dict[str, Dict[str, Any]] = {}
    providers = set()
    totals = dict(input=0, output=0, cacheRead=0, reasoning=0, cacheWrite=0)

    for row in rows:
        seen = row["last_seen"]
        if since_s is not None:
            if seen is None:
                continue
            try:
                if float(seen) < since_s:
                    continue
            except (TypeError, ValueError):
                continue
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0)
        cache_read = int(row["cache_read_tokens"] or 0)
        cache_write = int(row["cache_write_tokens"] or 0)
        reasoning = int(row["reasoning_tokens"] or 0)
        total = input_tokens + output_tokens + cache_read + cache_write + reasoning
        for key, value in (
            ("input", input_tokens), ("output", output_tokens), ("cacheRead", cache_read),
            ("reasoning", reasoning), ("cacheWrite", cache_write),
        ):
            totals[key] += value

        day = _iso_date_from_s(seen)
        if day:
            day_tokens[day] = day_tokens.get(day, 0) + total

        provider = (
            _text(row["billing_provider"]) or _text(row["session_billing_provider"]) or "unknown"
        )
        model = _text(row["model"]) or "unknown"
        providers.add(provider)
        key = (provider, model)
        bucket = models.setdefault(key, {
            "provider": provider, "model": model, "tokens": 0, "input": 0,
            "output": 0, "cacheRead": 0, "reasoning": 0,
        })
        bucket["tokens"] += total
        bucket["input"] += input_tokens
        bucket["output"] += output_tokens
        bucket["cacheRead"] += cache_read
        bucket["reasoning"] += reasoning

        root = _text(row["git_repo_root"]) or _text(row["cwd"])
        label = (Path(root).name if root else _text(row["display_name"]) or _text(row["title"])) or "desconocido"
        project_id = root or label
        project = projects.setdefault(project_id, {"id": project_id, "label": label, "tokens": 0, "sessions": set()})
        project["tokens"] += total
        if row["session_id"]:
            project["sessions"].add(row["session_id"])

    analytics.update({
        "totalTokens": sum(totals.values()),
        "cacheRead": totals["cacheRead"],
        "input": totals["input"],
        "reasoning": totals["reasoning"],
        "output": totals["output"],
        "daily": [{"date": date, "tokens": tokens} for date, tokens in sorted(day_tokens.items())],
        "projects": [
            {"id": p["id"], "label": p["label"], "tokens": p["tokens"], "sessions": len(p["sessions"])}
            for p in sorted(projects.values(), key=lambda item: item["tokens"], reverse=True)
        ],
    })
    model_rows = sorted(models.values(), key=lambda item: item["tokens"], reverse=True)
    notes = [DB_NOTE, CACHE_WRITE_NOTE, UTC_NOTE, NO_API_EQUIV_NOTE]
    return analytics, model_rows, _degraded_cards(sorted(providers)), notes


def _degraded_cards(providers: List[str]) -> List[Dict[str, Any]]:
    """Explicitly labelled placeholder cards — no quota numbers without a source."""
    names = list(providers) or ["unknown"]
    return [{
        "id": "hermes-noquota-%d" % index,
        "provider": provider,
        "label": "%s — sin fuente de cuota" % provider,
        "active": False,
        "windows": [{"id": "sinfuente", "label": "Cuota", "status": "sinfuente de cuota"}],
    } for index, provider in enumerate(names)]


# --- snapshot composition --------------------------------------------------

def build_snapshot(
    range_key: str = "7d",
    *,
    force: bool = False,
    base_url: str = OQG_BASE_URL,
    timeout: float = OQG_TIMEOUT,
    opener: Optional[Callable[..., Any]] = None,
    db_path: Optional[Path] = None,
    direct_fetcher: Optional[Callable[[], Tuple[List[Dict[str, Any]], List[str]]]] = None,
) -> Dict[str, Any]:
    """Build the contract snapshot. Any partial failure degrades cleanly."""
    range_key = normalize_range(range_key)
    cache_key = "%s|%s" % (range_key, base_url)
    now = time.monotonic()
    if not force and _CACHE["key"] == cache_key and _CACHE["expires"] > now:
        return _CACHE["data"]

    errors: List[str] = []
    kwargs = {"base_url": base_url, "timeout": min(float(timeout), 2.0), "opener": opener}
    health, health_error = fetch_json("/api/v1/health", **kwargs)
    quota, quota_error = fetch_json("/api/v1/quota", **kwargs)
    home, home_error = fetch_json("/api/v1/home", {"range": range_key}, **kwargs)
    models_payload, models_error = fetch_json("/api/v1/models", {"range": range_key}, **kwargs)
    for error in (health_error, quota_error, home_error, models_error):
        if error:
            errors.append(error)

    version = _text(health.get("version")) if health and health.get("version") else None
    connected = health is not None or quota is not None or home is not None or models_payload is not None
    if health is not None and quota is not None and home is not None and models_payload is not None:
        source = "opencode"
    elif connected:
        source = "mixed"
    else:
        source = "hermes"

    opencode: Dict[str, Any] = {"connected": connected}
    if version:
        opencode["version"] = version
    if errors:
        opencode["error"] = " | ".join(errors[:4])[:600]

    if direct_fetcher is not None:
        direct_cards, direct_notes = direct_fetcher()
    elif opener is None:  # production: use the credentials Hermes already stores
        try:
            try:
                from .quota_adapters import collect_direct_cards
            except Exception:
                import sys
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from quota_adapters import collect_direct_cards
            direct_cards, direct_notes = collect_direct_cards()
        except Exception as exc:
            direct_cards, direct_notes = [], ["Cuota directa: %s" % type(exc).__name__]
    else:  # offline tests inject their own opener; never call live provider APIs
        direct_cards, direct_notes = [], []

    notes: List[str] = list(direct_notes)
    # A single read-only DB query fills exactly the pieces OQG did not provide.
    needs_db = quota is None or home is None or models_payload is None
    if needs_db:
        hermes_analytics, hermes_models, hermes_cards, hermes_notes = read_hermes_usage(db_path, range_key)
        notes.extend(hermes_notes)

    if quota is not None:
        cards = map_quota_cards(quota)
        if not cards:
            cards = _degraded_cards([])
            if not direct_cards:
                notes.append(NO_QUOTA_NOTE)
    else:
        cards = hermes_cards if needs_db else _degraded_cards([])
        if not direct_cards:
            notes.append(NO_QUOTA_NOTE)

    if direct_cards:
        direct_providers = {str(card.get("provider")) for card in direct_cards}
        cards = list(direct_cards) + [
            card for card in cards if str(card.get("provider")) not in direct_providers
        ]
        source = "mixed"

    analytics = map_analytics(home, range_key) if home is not None else hermes_analytics
    models = map_models(models_payload, range_key) if models_payload is not None else hermes_models

    # De-duplicate notes without changing their order.
    seen_notes = set()
    notes = [n for n in notes if not (n in seen_notes or seen_notes.add(n))]

    snapshot = {
        "generatedAt": _iso_now(),
        "source": source,
        "opencode": opencode,
        "quota": {"cards": cards},
        "analytics": analytics,
        "models": models,
        "notes": notes,
    }
    _CACHE.update(key=cache_key, data=snapshot, expires=time.monotonic() + CACHE_TTL_SECONDS)
    return snapshot


def safe_snapshot(range_key: str, force: bool = False) -> Dict[str, Any]:
    """Endpoint wrapper: a plugin handler must never raise towards the renderer."""
    try:
        return build_snapshot(range_key, force=force)
    except Exception as exc:  # last-resort, still a valid contract payload
        return {
            "generatedAt": _iso_now(),
            "source": "hermes",
            "opencode": {"connected": False, "error": _err_text(exc)},
            "quota": {"cards": _degraded_cards([])},
            "analytics": _empty_analytics(normalize_range(range_key)),
            "models": [],
            "notes": [NO_QUOTA_NOTE, "Error interno del plugin: %s" % _err_text(exc)],
        }


# --- HTTP routes -----------------------------------------------------------

@router.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "plugin": PLUGIN_ID, "generatedAt": _iso_now()}


@router.get("/snapshot")
async def snapshot(range: str = Query("7d", description="24h | 7d | 30d | lifetime")) -> Dict[str, Any]:
    return safe_snapshot(range, force=False)


@router.post("/refresh")
async def refresh(payload: Optional[Dict[str, Any]] = Body(None)) -> Dict[str, Any]:
    requested = normalize_range(payload.get("range")) if isinstance(payload, dict) else "7d"
    invalidate_cache()
    return safe_snapshot(requested, force=True)
