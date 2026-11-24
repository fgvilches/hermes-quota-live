# Changelog

## Unreleased

- Catalog review fixes: English is now the default UI language across the pane, notices and
  backend notes; all provider keys moved to `optional_env` (`GH_TOKEN` included); `state.db`
  follows the active Hermes profile via `hermes_constants.get_hermes_home()`; the missing
  `dist/index.js` manifest entry was dropped.
- Removed the local-only `gh auth token` fallback. Copilot now uses `GH_TOKEN`/`GITHUB_TOKEN`
  exclusively through Hermes' secret scope, as required by the catalog rules.

- Fix: GitHub Copilot vuelve a aparecer con cuota real. El adapter vuelve a usar el token de la
  CLI de GitHub ya logueada cuando no hay `GH_TOKEN`/`GITHUB_TOKEN` declarado. Es comportamiento
  **local-only**: quitar este fallback antes de publicar al catálogo y exigir `GH_TOKEN`.

- Fix: el backend ya no consulta `opencode-quota-gui` en 4144 en cada refresco. Ese
  servidor exige OpenCode >=2.0.16 y con CLI 1.x nunca entregó cuota real; solo
  llenaba el panel de `URLError [Errno 61]`. Ahora es opt-in con `HERMES_QUOTA_OQG=1`.

- Fix: direct provider adapters now run under the active profile's secret scope. Plugin API routes
  are served outside a conversation turn, so `get_secret()` used to see only `os.environ` and
  every provider card disappeared after reinstalling (`Fuente: hermes`, `Sin fuente de cuota`).
  The backend installs `build_profile_secret_scope(hermes_home())` for the credential read and
  restores it afterwards; an existing scope is never replaced.
- Tests: regression coverage for the profile-scope wrapper.

## 0.1.0

- Panel Quota & Usage para Hermes Desktop con pestañas Cuota / Análisis / Modelos / Info.
- Adapters directos de OpenCode Go, OpenRouter, GitHub Copilot y Z.ai.
- Fallback a opencode-quota-gui y a `~/.hermes/state.db`.
- Selector de porcentaje Usado / Libre y chip `Uso N%`.
- 15 tests unitarios sin secretos ni HTTP real.
