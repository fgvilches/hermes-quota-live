# Setup

Guía corta para instalar el plugin en un Hermes local.

## 1. requisitos

- Hermes >= 0.23 con Desktop (el pane usa `@hermes/plugin-sdk`).
- Credenciales ya logueadas en Hermes para los proveedores que quieras ver.
- Python con `fastapi` (el que trae Hermes ya lo tiene).

## 2. instalar

```sh
cd ~/Desktop/code/hermes-quota-live
./scripts/install.sh
```

El script copia:

- `dashboard/*` → `~/.hermes/plugins/hermes-quota-live/dashboard/`
- `plugin.yaml` y `__init__.py` → `~/.hermes/plugins/hermes-quota-live/`
- `desktop/plugin.js` → `~/.hermes/desktop-plugins/hermes-quota-live/plugin.js`

y habilita el plugin con `hermes plugins enable hermes-quota-live`.

## 3. recargar la UI

En Hermes Desktop: **⌘K → Reload desktop plugins**. El chip `Uso N%` debería aparecer
en la barra de estado. Si no aparece, revisá `~/.hermes/logs/desktop.log`.

## 4. verificar

```sh
./scripts/verify.sh
hermes plugins doctor ~/.hermes/plugins/hermes-quota-live
```

También podés pegar `http://127.0.0.1:9119/api/plugins/hermes-quota-live/snapshot?range=7d`
en el dashboard autenticado de Hermes.

## actualizar el backend (importante)

El gateway **no remonta** una ruta REST ya cargada: cambiar `plugin_api.py` in place no
surte efecto hasta reiniciar el gateway. Opciones:

1. Reiniciar el gateway (`hermes gateway restart`), o
2. publicar el cambio con un **id de plugin nuevo** (el truco usado en esta versión).

La UI (`desktop/plugin.js`) sí se recarga con ⌘K → Reload desktop plugins.

## desinstalar

```sh
hermes plugins disable hermes-quota-live
rm -rf ~/.hermes/plugins/hermes-quota-live ~/.hermes/desktop-plugins/hermes-quota-live
```
