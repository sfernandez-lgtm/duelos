# DUELO ⚔️

CLI en Python que orquesta múltiples IAs de código: generación paralela, cross-review y merge de resultados.

## Estado actual (fase 1.1)

- **Claude Opus** vía CLI (`claude --model opus -p ...`), cubierto por suscripción Max.
- **Modo Coder**: chat de pair programming con historial de sesión, logs en `logs/` y comandos `/salir`, `/limpiar`, `/guardar <archivo>` (guarda el último bloque de código en `~/ai-projects/snippets/`).
- Providers API (Gemini, GLM, Kimi) ya definidos en `config.json` pero deshabilitados — se implementan en próximos pasos.
- Los modos de orquestación (debate, generación paralela, merge) llegan en pasos futuros.

## Requisitos

- Python 3.9+
- `pip install rich`
- CLI `claude` instalado y autenticado (para el provider Claude)

## Uso

```bash
python3 duelo.py
```

Menú:

1. 💻 **Coder** — sesión de chat de pair programming con el provider elegido.
2. 🤖 **Modelos** — lista providers (nombre, tipo, estado, API key) y permite habilitar/deshabilitar.
3. 🩺 **Test de conectividad** — health check de cada provider habilitado.
4. 🚪 **Salir**

`config.json` se genera solo con defaults la primera vez que se ejecuta.

## Estructura

```
duelo.py              # entry point, menú principal
config.json           # configuración de providers (se autogenera)
providers/
  base.py             # AIProvider + ProviderResponse
  claude_cli.py       # ClaudeCLIProvider
core/
  config.py           # carga/guardado de config.json
  cleaner.py          # limpieza de markdown artifacts (fences, preámbulos)
  session.py          # historial de chat, prompt con contexto, logs
ui/
  console.py          # consola Rich compartida, banner, helpers
```
