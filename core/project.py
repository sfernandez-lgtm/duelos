"""Modo Proyecto: planifica y genera un proyecto multi-archivo con un provider."""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from core.cleaner import clean_code
from core.config import save_config
from core.costs import get_tracker
from core.pipeline import run_pipeline
from core.validator import validate_file
from providers.base import AIProvider
from ui.console import console, error, info, success, warn

PROJECTS_DIR = Path.home() / "ai-projects"
MAX_CONTEXT_CHARS_PER_FILE = 4000

PLAN_SYSTEM_PROMPT = (
    "Sos un arquitecto de software. Respondés únicamente con JSON válido, "
    "sin explicaciones ni fences de markdown."
)

GEN_SYSTEM_PROMPT = (
    "Sos un desarrollador senior. Respondés únicamente con el contenido crudo "
    "del archivo pedido, completo y funcional, sin explicaciones ni fences de markdown."
)

RETRY_ONLY_CODE = (
    "\n\nIMPORTANTE: el intento anterior incluyó texto que no era parte del archivo "
    "y quedó inválido. Respondé con SOLO código, sin NINGUNA línea de texto antes o después."
)

PLAN_SCHEMA = (
    '{"project_name": "nombre-en-kebab-case", "description": "resumen del proyecto", '
    '"files": [{"path": "relativo/al/proyecto.py", "purpose": "qué hace este archivo"}], '
    '"run_instructions": "cómo correr el proyecto"}'
)


def _plan_prompt(description: str) -> str:
    return (
        "Planificá un proyecto de software a partir de esta descripción:\n\n"
        + description
        + "\n\nRespondé ÚNICAMENTE con un JSON válido con esta estructura exacta:\n"
        + PLAN_SCHEMA
        + "\n\nReglas: paths relativos sin '..', proyecto acotado a los archivos "
        "estrictamente necesarios, sin texto fuera del JSON."
    )


def _fix_plan_prompt(previous: str, parse_error: str) -> str:
    return (
        "Tu respuesta anterior no era un JSON válido con la estructura pedida "
        "(error: {}).\n\nRespuesta anterior:\n{}\n\n"
        "Respondé de nuevo ÚNICAMENTE con el JSON corregido, estructura:\n{}".format(
            parse_error, previous[:2000], PLAN_SCHEMA
        )
    )


def _file_prompt(description: str, plan: Dict[str, Any], generated: List[Tuple[str, str]],
                 path: str, purpose: str) -> str:
    parts: List[str] = [
        "Estás generando uno por uno los archivos de este proyecto.",
        "Descripción del proyecto: " + description,
        "Plan completo de archivos:\n" + "\n".join(
            "- {}: {}".format(f["path"], f.get("purpose", "")) for f in plan["files"]
        ),
    ]
    if generated:
        chunks = []
        for gen_path, content in generated:
            body = content
            if len(body) > MAX_CONTEXT_CHARS_PER_FILE:
                body = body[:MAX_CONTEXT_CHARS_PER_FILE] + "\n... [truncado]"
            chunks.append("--- {} ---\n{}".format(gen_path, body))
        parts.append("Archivos ya generados (para mantener consistencia):\n" + "\n\n".join(chunks))
    parts.append(
        "Ahora generá el archivo '{}' (propósito: {}).\n"
        "Respondé ÚNICAMENTE con el contenido completo del archivo, "
        "sin explicaciones, sin fences de markdown y sin repetir el nombre del archivo.".format(path, purpose)
    )
    return "\n\n".join(parts)


def _parse_plan(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Parsea el JSON del plan con tolerancia. Devuelve (plan, error)."""
    candidate = clean_code(text)
    if not candidate:
        return None, "respuesta vacía"
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        # último recurso: recortar del primer '{' al último '}'
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None, str(exc)
        try:
            data = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as exc2:
            return None, str(exc2)
    if not isinstance(data, dict):
        return None, "el JSON no es un objeto"

    files = []
    for entry in data.get("files", []):
        if not isinstance(entry, dict):
            continue
        path = _safe_relpath(str(entry.get("path", "")))
        if path is None:
            continue
        files.append({"path": path, "purpose": str(entry.get("purpose", ""))})
    if not files:
        return None, "el plan no tiene archivos válidos en 'files'"

    return {
        "project_name": _kebab_case(str(data.get("project_name", ""))),
        "description": str(data.get("description", "")),
        "files": files,
        "run_instructions": str(data.get("run_instructions", "")),
    }, ""


def _kebab_case(name: str) -> str:
    """Normaliza el nombre del proyecto a kebab-case seguro para directorio."""
    name = name.strip().lower()
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"[^a-z0-9-]", "", name).strip("-")
    return name or "proyecto"


def _safe_relpath(raw: str) -> Optional[str]:
    """Sanitiza un path relativo del plan; None si es absoluto, vacío o con '..'."""
    path = raw.strip().strip("`\"' ").replace("\\", "/")
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return None
    parts = [seg for seg in path.split("/") if seg not in ("", ".")]
    if not parts or any(seg == ".." for seg in parts):
        return None
    return "/".join(parts)


def _unique_project_dir(name: str) -> Path:
    """Devuelve ~/ai-projects/<name>, sufijando -2, -3, ... si ya existe."""
    base = PROJECTS_DIR / name
    if not base.exists():
        return base
    n = 2
    while (PROJECTS_DIR / "{}-{}".format(name, n)).exists():
        n += 1
    return PROJECTS_DIR / "{}-{}".format(name, n)


def _make_plan(provider: AIProvider, description: str) -> Optional[Dict[str, Any]]:
    """PASO A: pide el plan al provider, con 1 reintento si el JSON viene roto."""
    tracker = get_tracker()
    with console.status("[{}]{} planificando...[/{}]".format(provider.color, provider.display_name, provider.color)):
        response = provider.generate(_plan_prompt(description), PLAN_SYSTEM_PROMPT)
    tracker.record(provider.name, response, "plan")
    if not response.ok:
        error("Planificación falló: {}".format(response.error))
        return None

    plan, parse_error = _parse_plan(response.text)
    if plan is not None:
        return plan

    warn("El plan no vino como JSON válido ({}); reintentando...".format(parse_error))
    with console.status("[{}]{} corrigiendo el plan...[/{}]".format(provider.color, provider.display_name, provider.color)):
        retry = provider.generate(_fix_plan_prompt(response.text, parse_error), PLAN_SYSTEM_PROMPT)
    tracker.record(provider.name, retry, "plan")
    if not retry.ok:
        error("Reintento de planificación falló: {}".format(retry.error))
        return None
    plan, parse_error = _parse_plan(retry.text)
    if plan is None:
        error("El plan sigue sin ser JSON válido ({}); abortando".format(parse_error))
    return plan


def _show_plan(plan: Dict[str, Any]) -> None:
    console.print(Panel(
        "[bold]{}[/bold]\n{}\n\n[dim]Cómo correrlo:[/dim] {}".format(
            plan["project_name"], plan["description"], plan["run_instructions"] or "—"
        ),
        title="📦 Plan del proyecto",
        border_style="cyan",
    ))
    table = Table(header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("Archivo")
    table.add_column("Propósito")
    for i, entry in enumerate(plan["files"], start=1):
        table.add_row(str(i), entry["path"], entry["purpose"])
    console.print(table)


def _write_file(project_dir: Path, rel_path: str, content: str) -> Path:
    target = project_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if content and not content.endswith("\n"):
        content += "\n"
    target.write_text(content, encoding="utf-8")
    return target


def _default_readme(plan: Dict[str, Any]) -> str:
    lines = ["# {}".format(plan["project_name"]), "", plan["description"], ""]
    lines.append("## Archivos")
    lines.append("")
    for entry in plan["files"]:
        lines.append("- `{}` — {}".format(entry["path"], entry["purpose"]))
    if plan["run_instructions"]:
        lines.extend(["", "## Cómo correrlo", "", plan["run_instructions"]])
    return "\n".join(lines)


def run_project(provider: AIProvider, providers: List[AIProvider],
                config: Dict[str, Any], description: str) -> None:
    """Pipeline completo del modo Proyecto: plan -> confirmación -> generación -> resumen.

    provider es el principal (planifica y genera en modo rápido); providers es la
    lista completa habilitada, usada por el pipeline PRO (generate/review/merge).
    """
    tracker = get_tracker()

    plan = _make_plan(provider, description)
    if plan is None:
        return
    _show_plan(plan)

    if Prompt.ask("¿Generar el proyecto?", choices=["s", "n"], default="s") != "s":
        info("Generación cancelada")
        return

    n = len(providers)
    files_count = len(plan["files"])
    pro_calls = files_count * (n + n * (n - 1) + 1)
    info(
        "Llamadas estimadas — rápido: {} · pro con {} provider(s): {} "
        "(por archivo: {} generaciones + {} reviews + 1 merge)".format(
            files_count, n, pro_calls, n, n * (n - 1)
        )
    )
    pro_mode = Prompt.ask("Modo: (r)ápido o (p)ro?", choices=["r", "p"], default="r") == "p"
    config["last_project_mode"] = "pro" if pro_mode else "rápido"
    save_config(config)

    project_dir = _unique_project_dir(plan["project_name"])
    total = len(plan["files"])
    generated: List[Tuple[str, str]] = []   # (path, contenido) para contexto
    written: List[Tuple[str, int]] = []     # (path, bytes) para el resumen
    failed: List[Tuple[str, str]] = []      # (path, error)

    for i, entry in enumerate(plan["files"], start=1):
        rel_path, purpose = entry["path"], entry["purpose"]
        prompt = _file_prompt(description, plan, generated, rel_path, purpose)

        if pro_mode:
            label = "[archivo {}/{}: {}] ".format(i, total, rel_path)
            raw = run_pipeline(providers, config, prompt, GEN_SYSTEM_PROMPT, label)
            if raw is None:
                failed.append((rel_path, "pipeline PRO falló"))
                continue
        else:
            status_msg = "[{}]Generando archivo {}/{}: {}[/{}]".format(
                provider.color, i, total, rel_path, provider.color
            )
            with console.status(status_msg):
                response = provider.generate(prompt, GEN_SYSTEM_PROMPT)
            tracker.record(provider.name, response, "generate")
            if not response.ok:
                error("{}: {}".format(rel_path, response.error))
                failed.append((rel_path, response.error or "error desconocido"))
                continue
            raw = response.text

        content = clean_code(raw)  # regla sagrada: todo pasa por el cleaner
        if not content:
            warn("{}: respuesta vacía".format(rel_path))
            failed.append((rel_path, "respuesta vacía"))
            continue

        target = _write_file(project_dir, rel_path, content)
        result = validate_file(target)
        if result.repaired:
            info("{}: auto-reparado — {}".format(rel_path, result.reason))

        if not result.valid:
            warn("{}: inválido ({}); se regenera 1 vez pidiendo solo código".format(rel_path, result.reason))
            with console.status("[{}]Regenerando {}...[/{}]".format(provider.color, rel_path, provider.color)):
                retry = provider.generate(prompt + RETRY_ONLY_CODE, GEN_SYSTEM_PROMPT)
            tracker.record(provider.name, retry, "generate")
            retry_content = clean_code(retry.text) if retry.ok else ""
            if retry_content:
                target = _write_file(project_dir, rel_path, retry_content)
                result = validate_file(target)
                if result.repaired:
                    info("{}: auto-reparado — {}".format(rel_path, result.reason))
            if not result.valid:
                failed.append((rel_path, "inválido: {}".format(result.reason)))
                warn("{}: sigue inválido; queda en disco para arreglar a mano".format(rel_path))
                continue

        content = target.read_text(encoding="utf-8")  # contenido final (post reparación/reintento)
        generated.append((rel_path, content))
        written.append((rel_path, target.stat().st_size))
        success("{}/{} {}".format(i, total, rel_path))

    # README automático si el plan no lo incluía y se generó al menos algo
    if written and not any(p.lower() == "readme.md" for p, _ in written):
        target = _write_file(project_dir, "README.md", _default_readme(plan))
        written.append(("README.md", target.stat().st_size))
        info("README.md generado automáticamente")

    _show_result(project_dir, written, failed)
    tracker.render_summary(console)
    tracker.save()


def _show_result(project_dir: Path, written: List[Tuple[str, int]],
                 failed: List[Tuple[str, str]]) -> None:
    if not written and not failed:
        return
    table = Table(title="Resultado de la generación", header_style="bold")
    table.add_column("Archivo")
    table.add_column("Tamaño", justify="right")
    table.add_column("Estado")
    for path, size in written:
        table.add_row(path, "{:,} B".format(size), "[green]✔ escrito[/green]")
    for path, err in failed:
        table.add_row(path, "—", "[red]✖ {}[/red]".format(err))
    console.print(table)

    if written:
        success("Proyecto en {}".format(project_dir))
    if failed:
        warn("{} archivo(s) fallaron; podés regenerarlos a mano: {}".format(
            len(failed), ", ".join(p for p, _ in failed)
        ))
