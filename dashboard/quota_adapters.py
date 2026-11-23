"""Direct provider quota adapters for the Hermes quota panel.

Credentials are resolved exclusively through Hermes' secret resolver for the
variables declared in plugin.yaml (requires_env). No credential-store files,
.env files, or another tool's login are ever read. Each provider's official
HTTPS API is called and the answer is mapped to the panel's card contract.
No secret is logged, persisted, or sent to a third party.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

try:  # canonical, profile-aware secret resolver in the Hermes runtime
    from agent.secret_scope import get_secret as _runtime_get_secret
except Exception:  # pragma: no cover - fallback for out-of-process imports
    _runtime_get_secret = None

ALLOWED_HOSTS = {
    "opencode.ai",
    "openrouter.ai",
    "api.github.com",
    "api.z.ai",
    "open.bigmodel.cn",
}
HTTP_TIMEOUT = 6.0
USER_AGENT = "hermes-quota/1.0"

Opener = Callable[..., Any]


CREDENTIAL_ENV = {
    "opencode-go": "OPENCODE_GO_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "copilot": "GH_TOKEN",
    "zai": "GLM_API_KEY",
}


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else value
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return int(number) if number.is_integer() else number
    return None


def _secret(name: str) -> Optional[str]:
    """Resolve a declared variable through Hermes, never from files."""
    if _runtime_get_secret is None:
        return None
    try:
        value = _runtime_get_secret(name)
    except Exception:
        return None
    return str(value) if value else None


def credential(provider: str) -> Optional[str]:
    name = CREDENTIAL_ENV.get(provider)
    if not name:
        return None
    value = _secret(name)
    if provider == "copilot" and not value:
        value = _secret("GITHUB_TOKEN")
    return value


def _workspace_id() -> Optional[str]:
    """Optional OpenCode Go workspace id for the USD meters endpoint."""
    return _secret("OPENCODE_WORKSPACE_ID")


def _get_json(
    url: str,
    token: str,
    *,
    scheme: Optional[str] = "Bearer",
    extra_headers: Optional[Mapping[str, str]] = None,
    opener: Optional[Opener] = None,
) -> Dict[str, Any]:
    host = urllib.parse.urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise ValueError("host not allowed")
    headers = {
        "Authorization": ("%s %s" % (scheme, token)) if scheme else token,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    for key, value in (extra_headers or {}).items():
        headers[key] = str(value)
    request = urllib.request.Request(url, headers=headers, method="GET")
    with (opener or urllib.request.urlopen)(request, timeout=HTTP_TIMEOUT) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8", "replace")) if isinstance(raw, bytes) else json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("response is not an object")
    return payload


def _window(
    wid: str,
    label: str,
    *,
    used: Any = None,
    limit: Any = None,
    remaining_pct: Any = None,
    unit: Any = None,
    reset_at: Any = None,
    status: Any = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"id": wid, "label": label}
    for key, value in (
        ("used", _num(used)),
        ("limit", _num(limit)),
        ("remainingPct", _num(remaining_pct)),
        ("unit", _text(unit)),
        ("resetAt", _text(reset_at)),
        ("status", _text(status)),
    ):
        if value is not None:
            result[key] = value
    return result


def _card(provider: str, label: str, windows: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    card: Dict[str, Any] = {
        "id": provider,
        "provider": provider,
        "label": label,
        "active": True,
        "windows": windows,
    }
    for key, value in extra.items():
        if value is not None:
            card[key] = value
    return card


def _iso_from_ms(value: Any) -> Optional[str]:
    number = _num(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000.0, tz=timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def fetch_opencode_go(
    token: str, opener: Optional[Opener] = None, workspace: Optional[str] = None
) -> Dict[str, Any]:
    if workspace is None:  # direct calls (tests) resolve in the caller's context
        workspace = _workspace_id()
    payload = _get_json(
        "https://opencode.ai/zen/go/v1/usage", token, opener=opener
    )
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    meters: Mapping[str, Any] = {}
    workspace = _workspace_id()
    if workspace:
        try:
            status_payload = _get_json(
                "https://opencode.ai/console/api/go/status",
                token,
                extra_headers={"x-org-id": workspace},
                opener=opener,
            )
            access = status_payload.get("access")
            if isinstance(access, Mapping) and isinstance(access.get("meters"), Mapping):
                meters = access["meters"]
        except Exception:
            meters = {}

    windows: List[Dict[str, Any]] = []
    for key, meter_key, label in (
        ("rolling", "fiveHour", "5 h"),
        ("weekly", "week", "7 d"),
        ("monthly", "month", "30 d"),
    ):
        part = usage.get(key) if isinstance(usage.get(key), Mapping) else {}
        meter = meters.get(meter_key) if isinstance(meters.get(meter_key), Mapping) else {}
        used_usd = _num(meter.get("usedMicroCents"))
        limit_usd = _num(meter.get("limitMicroCents"))
        if used_usd is not None and limit_usd is not None:
            used = round(used_usd / 100_000_000.0, 2)
            limit = round(limit_usd / 100_000_000.0, 2)
            remaining = round(100.0 * (limit - used) / limit, 1) if limit > 0 else None
            unit = "usd"
        else:
            percent = _num(part.get("percent"))
            used = percent
            limit = 100 if percent is not None else None
            remaining = 100 - percent if percent is not None else None
            unit = "percent"
        windows.append(
            _window(
                key,
                label,
                used=used,
                limit=limit,
                remaining_pct=remaining,
                unit=unit,
                reset_at=part.get("resetsAt") or meter.get("resetsAt"),
                status=part.get("status"),
            )
        )
    return _card("opencode-go", "OpenCode Go", windows, active=True)


def fetch_openrouter(token: str, opener: Optional[Opener] = None) -> Dict[str, Any]:
    payload = _get_json("https://openrouter.ai/api/v1/key", token, opener=opener)
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    windows: List[Dict[str, Any]] = []
    for budget in data.get("budgets") or []:
        if not isinstance(budget, Mapping):
            continue
        limit = _num(budget.get("limit_usd"))
        if limit is None or limit <= 0:
            continue
        spent = _num(budget.get("spend_usd")) or 0
        remaining = _num(budget.get("remaining_usd"))
        if remaining is None:
            remaining = limit - spent
        windows.append(
            _window(
                "budget_%s" % (budget.get("id") or len(windows)),
                _text(budget.get("name")) or "Presupuesto",
                used=spent,
                limit=limit,
                remaining_pct=100.0 * remaining / limit,
                unit="usd",
                reset_at=budget.get("resets_at"),
                status=budget.get("scope"),
            )
        )
    if not windows and _num(data.get("limit")):
        limit = _num(data.get("limit"))
        remaining = _num(data.get("limit_remaining"))
        used = _num(data.get("usage"))
        if limit and remaining is not None:
            windows.append(
                _window(
                    "key_limit",
                    "Límite de clave",
                    used=used if used is not None else limit - remaining,
                    limit=limit,
                    remaining_pct=100.0 * remaining / limit,
                    unit="usd",
                )
            )
    free = data.get("free_model_daily_requests")
    if isinstance(free, Mapping) and _num(free.get("limit")):
        limit = _num(free.get("limit"))
        used = _num(free.get("used")) or 0
        windows.append(
            _window(
                "free_daily_requests",
                "Modelos free · requests/día",
                used=used,
                limit=limit,
                remaining_pct=100.0 * (limit - used) / limit,
                unit="requests",
            )
        )
    return _card("openrouter", "OpenRouter", windows)


def fetch_copilot(token: str, opener: Optional[Opener] = None) -> Dict[str, Any]:
    payload = _get_json(
        "https://api.github.com/copilot_internal/user",
        token,
        scheme="token",
        extra_headers={"X-GitHub-Api-Version": "2022-11-28"},
        opener=opener,
    )
    snapshots = payload.get("quota_snapshots")
    windows: List[Dict[str, Any]] = []
    if isinstance(snapshots, Mapping):
        reset = payload.get("quota_reset_date_utc")
        for name, snap in snapshots.items():
            if not isinstance(snap, Mapping) or not snap.get("has_quota"):
                continue
            entitlement = _num(snap.get("entitlement"))
            remaining = _num(snap.get("remaining"))
            if entitlement is None or entitlement <= 0 or remaining is None:
                continue
            windows.append(
                _window(
                    name,
                    name.replace("_", " ").capitalize(),
                    used=entitlement - remaining,
                    limit=entitlement,
                    remaining_pct=_num(snap.get("percent_remaining")),
                    unit="credits" if snap.get("token_based_billing") else "requests",
                    reset_at=reset,
                    status=payload.get("copilot_plan"),
                )
            )
    return _card("copilot", "GitHub Copilot", windows)


def fetch_zai(token: str, opener: Optional[Opener] = None) -> Dict[str, Any]:
    windows: List[Dict[str, Any]] = []
    error: Optional[str] = None
    try:
        payload = _get_json(
            "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
            token,
            scheme=None,
            opener=opener,
        )
        if payload.get("success") is False or _num(payload.get("code")) not in (None, 0):
            error = "sin plan de coding en la clave guardada"
        else:
            data = payload.get("data")
            limits = data.get("limits") if isinstance(data, Mapping) else payload.get("limits")
            for entry in limits or []:
                if not isinstance(entry, Mapping):
                    continue
                used = _num(entry.get("percentage"))
                if used is None:
                    continue
                windows.append(
                    _window(
                        "limit_%s" % (entry.get("type") or len(windows)),
                        _text(entry.get("type")) or "Límite",
                        used=used,
                        limit=100,
                        remaining_pct=100 - used,
                        unit="percent",
                        reset_at=_iso_from_ms(entry.get("nextResetTime")),
                        status=entry.get("level"),
                    )
                )
    except Exception:
        error = "no se pudo consultar el plan"
    return _card("zai", "Z.ai GLM", windows, error=error, active=not error)


ADAPTERS = (
    ("opencode-go", "OpenCode Go", fetch_opencode_go),
    ("openrouter", "OpenRouter", fetch_openrouter),
    ("copilot", "GitHub Copilot", fetch_copilot),
    ("zai", "Z.ai GLM", fetch_zai),
)


def _run_adapter(
    name: str,
    label: str,
    fetcher: Callable[..., Dict[str, Any]],
    token: str,
    workspace: Optional[str],
    opener: Optional[Opener],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        kwargs: Dict[str, Any] = {"opener": opener}
        if name == "opencode-go":
            kwargs["workspace"] = workspace
        card = fetcher(token, **kwargs)
        if not card.get("windows") and not card.get("error"):
            return None, None
        return card, None
    except Exception as exc:  # only the exception TYPE is surfaced: never its text
        return None, "%s: %s" % (label, type(exc).__name__)


def collect_direct_cards(opener: Optional[Opener] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Resolve secrets in this context, then fetch providers concurrently.

    Hermes secret scopes are context-local: worker threads do not inherit them,
    so every credential must be resolved BEFORE the executor starts.
    """
    prepared = []
    try:
        for name, label, fetcher in ADAPTERS:
            token = credential(name)
            if not token:
                continue
            workspace = _workspace_id() if name == "opencode-go" else None
            prepared.append((name, label, fetcher, token, workspace))
    except Exception as exc:  # pragma: no cover - resolution is best effort
        return [], ["credential resolution: %s" % type(exc).__name__]

    try:
        with ThreadPoolExecutor(max_workers=max(1, len(prepared))) as pool:
            results = list(
                pool.map(lambda item: _run_adapter(*item, opener=opener), prepared)
            )
    except Exception as exc:  # pragma: no cover - executor is best effort
        return [], ["adapters: %s" % type(exc).__name__]
    cards = [card for card, _ in results if card is not None]
    notes = [note for _, note in results if note]
    if cards:
        labels = ", ".join(str(card.get("label")) for card in cards)
        notes.insert(0, "Cuota directa de proveedores: %s." % labels)
    return cards, notes
