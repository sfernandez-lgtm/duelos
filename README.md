# DUELO ⚔️

CLI en Python que orquesta múltiples IAs de código: generación, cross-review todos-contra-todos y merge de resultados en una versión final.

**Versión:** v0.1.0 (`v0.1-claude-only`) — cierre de la Etapa 1: toda la arquitectura N-provider funcionando con un solo provider (Claude vía CLI).

## Arquitectura: pipeline N-provider

El corazón de DUELO es `core/pipeline.py`, con tres etapas genéricas sobre una **lista** de providers:

1. **`generate_all`** — cada provider genera su solución de la misma tarea.
2. **`review_all`** — cross-review todos-contra-todos: cada provider critica las soluciones de los demás. Con un solo provider habilitado degenera en *self-review* (crítica de su propio código).
3. **`merge_final`** — un provider (el *merger*, rotado round-robin y persistido en `config.json`) integra soluciones + reviews en la versión final. Con N=1 es una pasada de refinamiento.

Tolerancia a fallos: un provider caído se excluye de la etapa; solo se aborta si fallan **todas** las generaciones; si falla el merge se usa la mejor generación cruda.

Para pasar de self-review a duelo real alcanza con habilitar otro provider en `config.json` (Etapa 2) — el pipeline no cambia.

Todo output de código pasa por dos barreras antes de tocar el disco:

- **`core/cleaner.py`** — remueve fences de markdown y preámbulos explicativos.
- **`core/validator.py`** — valida el archivo escrito (`.py` compila, `.json` parsea); si un `.py` no compila intenta auto-reparar removiendo líneas de prosa inicial/final, y si sigue inválido se regenera una vez pidiendo solo código.

## Requisitos e instalación

- Python 3.9+
- `pip install rich` (única dependencia)
- CLI `claude` instalado y autenticado (`npm install -g @anthropic-ai/claude-code`) — el provider Claude usa la suscripción, costo $0.00 (sub)

```bash
git clone <repo> ~/duelo
cd ~/duelo
python3 duelo.py
```

`config.json` se genera solo con defaults la primera vez. Si se corrompe, DUELO ofrece regenerarlo (respaldando en `config.json.bak`).

## Modos

### 💻 Coder
Chat de pair programming con historial de sesión (logs en `logs/`). Comandos:

| Comando | Acción |
|---|---|
| `/ayuda` | lista los comandos |
| `/pro <consulta>` | corre la consulta por el pipeline completo (generate + review + merge) |
| `/guardar <archivo>` | guarda el último bloque de código en `~/ai-projects/snippets/` |
| `/costos` | resumen parcial de consumo de la sesión |
| `/limpiar` | resetea el historial |
| `/salir` | cierra la sesión (muestra resumen de costos y guarda) |

### 📦 Proyecto
A partir de una descripción multilínea genera un proyecto completo en `~/ai-projects/<nombre>/`:

1. **Plan** — el modelo propone la estructura (JSON validado con reintento); se muestra en tabla y se confirma.
2. **Generación** — archivo por archivo, con el plan y los archivos previos como contexto. Dos modos:
   - **(r)ápido** — una pasada por archivo.
   - **(p)ro** — cada archivo pasa por el pipeline completo generate → review → merge.
3. README.md automático, resumen de archivos escritos/fallidos y de costos.

### 💰 Costos
Consumo de la sesión actual (llamadas, tokens, costo, tiempo, desglose por operación: `plan`/`generate`/`review`/`merge`/`coder`/`health_check`) + histórico de las últimas 10 sesiones (`costs.json`). Claude CLI reporta `$0.00 (sub)`; los providers API mostrarán costo real.

### 🤖 Modelos
Lista los providers configurados (tipo, estado, API key) y permite habilitar/deshabilitar.

### 🩺 Test de conectividad
Health check de cada provider habilitado.

## Estructura

```
duelo.py              # entry point, menú principal
config.json           # configuración de providers (se autogenera)
providers/
  base.py             # AIProvider + ProviderResponse (tokens, costo, tiempo)
  claude_cli.py       # ClaudeCLIProvider (prompt por stdin, shutil.which)
core/
  version.py          # VERSION
  config.py           # carga/guardado de config.json
  cleaner.py          # limpieza de markdown artifacts (fences, preámbulos)
  validator.py        # validación + auto-reparación de archivos generados
  session.py          # historial de chat, prompt con contexto, logs
  costs.py            # CostTracker (por provider y operación), costs.json
  pipeline.py         # generate_all / review_all / merge_final / run_pipeline
  project.py          # modo Proyecto: plan + generación multi-archivo
ui/
  console.py          # consola Rich compartida, banner, paneles de error
```

## Roadmap

- **Etapa 1 — `v0.1-claude-only`** ✅ CLI completa con un provider: Coder, Proyecto (rápido/pro), costos, validación y robustez. Pipeline N-provider en self-review.
- **Etapa 2** — providers API reales (Gemini, GLM, Kimi vía OpenAI-compatible), duelo real multi-modelo, usage/costos reales, generación paralela.
- **Etapa 3** — modos de orquestación avanzados (debate, votación), métricas de calidad por provider.
