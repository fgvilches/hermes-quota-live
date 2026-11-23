# Contribuir

- Commit con la identidad del repo: `Felipe González <45769917+fgvilches@users.noreply.github.com>`.
- Commits en inglés, imperativo y prefijo (`Add`, `Fix`, `Docs`, `Test`).
- Todo cambio del backend lleva test en `tests/`; todo cambio de UI pasa `node --check`.
- Antes de abrir un PR: `./scripts/verify.sh` y `hermes plugins validate . --install-deps`.
- Nunca commitear `.env`, `auth.json` ni tokens.
