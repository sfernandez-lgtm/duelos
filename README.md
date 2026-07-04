# DUELO ⚔️

CLI en Python que orquesta múltiples IAs de código: generación paralela, cross-review y merge de resultados.

## Estado actual (paso 1)

Esqueleto del proyecto con un solo provider funcionando:

- **Claude Opus** vía CLI (`claude --model opus -p ...`), cubierto por suscripción Max.
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

1. 🤖 **Modelos** — lista providers (nombre, tipo, estado, API key) y permite habilitar/deshabilitar.
2. 🩺 **Test de conectividad** — health check de cada provider habilitado.
3. 🚪 **Salir**

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
ui/
  console.py          # consola Rich compartida, banner, helpers
```
