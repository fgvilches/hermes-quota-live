# Publicar en el catálogo de Hermes

Convención oficial (resumen de la guía *Submitting to the Plugin Catalog*):

1. Repo **público** y clonable por HTTPS.
2. `plugin.yaml` + al menos un entrypoint: `__init__.py`, `desktop/plugin.js` y/o
   `dashboard/manifest.json`. Este repo trae los tres.
3. Validar exactamente el commit que se va a fijar:

   ```sh
   hermes plugins validate . --install-deps
   ```

4. PR a `NousResearch/hermes-agent` agregando **un** archivo
   `plugin-catalog/<name>.yaml` con `repo`, `sha` de 40 caracteres, `version`
   entre comillas, `category: desktop`, `maintainer` y `capabilities`.
5. El CI clona el SHA fijado y corre el validador; un maintainer revisa y mergea.
6. Actualizar = nuevo PR que cambia el SHA (nunca self-updating code).

Reglas que ya respeta este plugin: secretos vía Hermes, sin parcheos de core,
sin `eval`/`new Function` ni imports fuera del SDK en `desktop/plugin.js`,
superficies públicas únicamente, sin telemetría propia.

Pendiente para una submission prolija: bilingüe en la UI (la interfaz hoy es español),
screenshots fijados al SHA del release y `description` de catálogo con los providers
que se consultan.

## Estado actual

El repo público es https://github.com/fgvilches/hermes-quota-live y el borrador del entry está en `docs/catalog-entry.yaml`, fijado al commit `<SHA_DEL_COMMIT_PUBLICADO>`. **No se abrió ningún PR**; se deja para revisión.

Antes de submittear hay que resolver la regla 11 (credenciales): hoy el backend lee `~/.hermes/auth.json` para obtener los punteros `env:...` y usa `gh auth token` para Copilot. El camino conforme es declarar `requires_env` en `plugin.yaml` y usar únicamente `agent.secret_scope.get_secret()`, sin leer `auth.json` ni el login de otra CLI.

Referencias:
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission
- https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk
