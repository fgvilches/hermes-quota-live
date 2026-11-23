# Proveedores

## adapters directos

| Proveedor | Endpoint | Auth | Ventanas |
|---|---|---|---|
| OpenCode Go | `GET https://opencode.ai/zen/go/v1/usage` y `GET https://opencode.ai/console/api/go/status` | `Bearer <OPENCODE_GO_API_KEY>` + `x-org-id` | 5 h / 7 d / 30 d en USD |
| OpenRouter | `GET https://openrouter.ai/api/v1/key` | `Bearer <OPENROUTER_API_KEY>` | budgets USD y requests diarios free |
| GitHub Copilot | `GET https://api.github.com/copilot_internal/user` | `token <gh auth token>` | chat / completions en créditos |
| Z.ai GLM | `GET https://open.bigmodel.cn/api/monitor/usage/quota/limit` | clave raw | `limits[]` del Coding Plan |

Notas:

- El workspace id de OpenCode Go se lee del metadata de `auth.json` (no es secreto).
- Si la clave de Z.ai es PAYG, el adapter devuelve `error: sin plan` y **ninguna ventana**.
- NVIDIA devolvió 404 en `/v1/usage`; no hay API de cuota con `NVIDIA_API_KEY`.
- Un User-Agent explícito evita el 403 intermitente de `opencode.ai`.

## fallback

Si no hay adapters directos, se consulta el servidor local de `opencode-quota-gui`
en `127.0.0.1:4144` (cuando existe) y siempre se leen tokens/proyectos/modelos de
`~/.hermes/state.db` en modo solo lectura.

`source` del snapshot: `opencode` (fuente original completa), `mixed` (directa + local)
o `hermes` (sólo datos locales).
