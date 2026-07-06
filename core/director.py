"""Director: orquestación por fases arriba del pipeline.

Un provider director arma un roadmap de fases ordenadas a partir de un pedido
grande y las ejecuta una por una con el pipeline existente (rápido o pro),
acumulando contexto entre fases. El loop de evaluación/corrección llega en 6.2.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from core.cleaner import clean_code
from core.costs import get_tracker
from core.pipeline import run_pipeline
from core.project import (
    GEN_SYSTEM_PROMPT,
    MAX_CONTEXT_CHARS_PER_FILE,
    RETRY_ONLY_CODE,
    _kebab_case,
    _safe_relpath,
    _unique_project_dir,
    _write_file,
)
from core.validator import validate_file
from providers.base import AIProvider
from ui.console import console, error, info, success, warn

TESTS_TIMEOUT_SECONDS = 120

DIRECTOR_SYSTEM_PROMPT = (
    "Sos el director técnico de un equipo de IAs de código. Respondés únicamente "
    "con JSON válido, sin explicaciones ni fences de markdown."
)

ROADMAP_SCHEMA = (
    '{"project_name": "nombre-en-kebab-case", "description": "resumen del proyecto", '
    '"phases": [{"number": 1, "title": "...", "objective": "qué debe existir al terminar", '
    '"files": [{"path": "relativo/al/proyecto.py", "purpose": "qué hace"}], '
    '"acceptance_criteria": ["criterio verificable 1"], '
    '"suggested_mode": "rapido|pro", "depends_on": []}], '
    '"run_instructions": "cómo correr el proyecto"}'
)


def _roadmap_prompt(request: str) -> str:
    return (
        "Armá el roadmap por fases de este proyecto:\n\n"
        + request
        + "\n\nRespondé ÚNICAMENTE con un JSON válido con esta estructura exacta:\n"
        + ROADMAP_SCHEMA
        + "\n\nReglas del roadmap:\n"
        "- Fases chicas y ordenadas: esqueleto -> módulo core -> features -> pulido.\n"
        "- Cada fase deja el proyecto funcionando (nada de fases que rompen todo hasta la siguiente).\n"
        "- Los tests van como archivos test_*.py dentro de la fase a la que corresponden.\n"
        "- suggested_mode: 'rapido' para lo trivial, 'pro' solo para lo crítico o difícil.\n"
        "- Paths relativos sin '..'. Numeración desde 1. Sin texto fuera del JSON."
    )


def _fix_roadmap_prompt(previous: str, parse_error: str) -> str:
    return (
        "Tu respuesta anterior no era un JSON válido con la estructura pedida "
        "(error: {}).\n\nRespuesta anterior:\n{}\n\n"
        "Respondé de nuevo ÚNICAMENTE con el JSON corregido, estructura:\n{}".format(
            parse_error, previous[:2000], ROADMAP_SCHEMA
        )
    )


def _parse_roadmap(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Parsea el JSON del roadmap con tolerancia. Devuelve (roadmap, error)."""
    candidate = clean_code(text)
    if not candidate:
        return None, "respuesta vacía"
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None, str(exc)
        try:
            data = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as exc2:
            return None, str(exc2)
    if not isinstance(data, dict):
        return None, "el JSON no es un objeto"

    phases = []
    for raw in data.get("phases", []):
        if not isinstance(raw, dict):
            continue
        files = []
        for entry in raw.get("files", []):
            if not isinstance(entry, dict):
                continue
            path = _safe_relpath(str(entry.get("path", "")))
            if path is not None:
                files.append({"path": path, "purpose": str(entry.get("purpose", ""))})
        if not files:
            continue
        mode = str(raw.get("suggested_mode", "rapido")).lower()
        phases.append({
            "number": len(phases) + 1,  # renumera por orden, ignora numeración rota
            "title": str(raw.get("title", "fase {}".format(len(phases) + 1))),
            "objective": str(raw.get("objective", "")),
            "files": files,
            "acceptance_criteria": [str(c) for c in raw.get("acceptance_criteria", []) if str(c).strip()],
            "suggested_mode": "pro" if "pro" in mode else "rapido",
            "depends_on": raw.get("depends_on", []),
        })
    if not phases:
        return None, "el roadmap no tiene fases con archivos válidos"

    return {
        "project_name": _kebab_case(str(data.get("project_name", ""))),
        "description": str(data.get("description", "")),
        "phases": phases,
        "run_instructions": str(data.get("run_instructions", "")),
    }, ""


def _make_roadmap(director: AIProvider, request: str) -> Optional[Dict[str, Any]]:
    """PASO A: pide el roadmap al director, con 1 reintento si el JSON viene roto."""
    tracker = get_tracker()
    with console.status("[{0}]{1} armando el roadmap...[/{0}]".format(director.color, director.display_name)):
        response = director.generate(_roadmap_prompt(request), DIRECTOR_SYSTEM_PROMPT)
    tracker.record(director.name, response, "direct")
    if not response.ok:
        error("Roadmap falló: {}".format(response.error))
        return None

    roadmap, parse_error = _parse_roadmap(response.text)
    if roadmap is not None:
        return roadmap

    warn("El roadmap no vino como JSON válido ({}); reintentando...".format(parse_error))
    with console.status("[{0}]{1} corrigiendo el roadmap...[/{0}]".format(director.color, director.display_name)):
        retry = director.generate(_fix_roadmap_prompt(response.text, parse_error), DIRECTOR_SYSTEM_PROMPT)
    tracker.record(director.name, retry, "direct")
    if not retry.ok:
        error("Reintento del roadmap falló: {}".format(retry.error))
        return None
    roadmap, parse_error = _parse_roadmap(retry.text)
    if roadmap is None:
        error("El roadmap sigue sin ser JSON válido ({}); abortando".format(parse_error))
    return roadmap


def _phase_calls(files_count: int, mode: str, n_providers: int) -> int:
    """Llamadas estimadas de una fase (misma cuenta que la advertencia de Proyecto)."""
    if mode == "pro":
        return files_count * (n_providers + n_providers * (n_providers - 1) + 1)
    return files_count


def _resolve_mode(phase: Dict[str, Any], global_mode: str) -> str:
    if global_mode == "r":
        return "rapido"
    if global_mode == "p":
        return "pro"
    return phase["suggested_mode"]


def _show_roadmap(roadmap: Dict[str, Any], n_providers: int) -> None:
    console.print(Panel(
        "[bold]{}[/bold]\n{}\n\n[dim]Cómo correrlo:[/dim] {}".format(
            roadmap["project_name"], roadmap["description"], roadmap["run_instructions"] or "—"
        ),
        title="🎬 Roadmap del Director",
        border_style="cyan",
    ))
    table = Table(header_style="bold")
    table.add_column("Fase", justify="right")
    table.add_column("Título")
    table.add_column("Archivos")
    table.add_column("Modo")
    table.add_column("Llamadas", justify="right")

    total_calls = 0
    for phase in roadmap["phases"]:
        calls = _phase_calls(len(phase["files"]), phase["suggested_mode"], n_providers)
        total_calls += calls
        table.add_row(
            str(phase["number"]),
            phase["title"],
            "\n".join(f["path"] for f in phase["files"]),
            phase["suggested_mode"],
            str(calls),
        )
    table.add_row("", "[bold]TOTAL[/bold]", "", "", "[bold]{}[/bold]".format(total_calls))
    console.print(table)


def _phase_file_prompt(roadmap: Dict[str, Any], phase: Dict[str, Any],
                       generated: List[Tuple[str, str]], path: str, purpose: str) -> str:
    parts: List[str] = [
        "Estás generando los archivos de un proyecto que se construye por fases.",
        "Proyecto: " + roadmap["description"],
        "Roadmap completo:\n" + "\n".join(
            "- Fase {}: {} ({})".format(p["number"], p["title"], ", ".join(f["path"] for f in p["files"]))
            for p in roadmap["phases"]
        ),
        "FASE ACTUAL {}: {}\nObjetivo: {}".format(phase["number"], phase["title"], phase["objective"])
        + ("\nCriterios de aceptación:\n" + "\n".join("- " + c for c in phase["acceptance_criteria"])
           if phase["acceptance_criteria"] else ""),
    ]
    if generated:
        chunks = []
        for gen_path, content in generated:
            body = content
            if len(body) > MAX_CONTEXT_CHARS_PER_FILE:
                body = body[:MAX_CONTEXT_CHARS_PER_FILE] + "\n... [truncado]"
            chunks.append("--- {} ---\n{}".format(gen_path, body))
        parts.append("Archivos ya generados en fases anteriores y en esta fase:\n" + "\n\n".join(chunks))
    parts.append(
        "Ahora generá el archivo '{}' (propósito: {}).\n"
        "Si es un archivo de tests, usá unittest de la stdlib (NO pytest) salvo que el "
        "proyecto declare esa dependencia.\n"
        "Respondé ÚNICAMENTE con el contenido completo del archivo, "
        "sin explicaciones, sin fences de markdown y sin repetir el nombre del archivo.".format(path, purpose)
    )
    return "\n\n".join(parts)


def _generate_phase_file(director: AIProvider, providers: List[AIProvider],
                         config: Dict[str, Any], project_dir: Path, prompt: str,
                         rel_path: str, mode: str, label: str) -> Tuple[Optional[Path], str]:
    """Genera un archivo (pipeline pro o pasada rápida del director), lo limpia,
    escribe, valida y reintenta 1 vez si queda inválido.

    Devuelve (path escrito o None, motivo de fallo)."""
    tracker = get_tracker()

    if mode == "pro":
        raw = run_pipeline(providers, config, prompt, GEN_SYSTEM_PROMPT, label)
        if raw is None:
            return None, "pipeline PRO falló"
    else:
        with console.status("[{0}]{1}{2} generando...[/{0}]".format(director.color, label, director.display_name)):
            response = director.generate(prompt, GEN_SYSTEM_PROMPT)
        tracker.record(director.name, response, "generate")
        if not response.ok:
            return None, response.error or "error desconocido"
        raw = response.text

    content = clean_code(raw)  # regla sagrada
    if not content:
        return None, "respuesta vacía"

    target = _write_file(project_dir, rel_path, content)
    result = validate_file(target)
    if result.repaired:
        info("{}: auto-reparado — {}".format(rel_path, result.reason))

    if not result.valid:
        warn("{}: inválido ({}); se regenera 1 vez pidiendo solo código".format(rel_path, result.reason))
        with console.status("[{0}]Regenerando {1}...[/{0}]".format(director.color, rel_path)):
            retry = director.generate(prompt + RETRY_ONLY_CODE, GEN_SYSTEM_PROMPT)
        tracker.record(director.name, retry, "generate")
        retry_content = clean_code(retry.text) if retry.ok else ""
        if retry_content:
            target = _write_file(project_dir, rel_path, retry_content)
            result = validate_file(target)
            if result.repaired:
                info("{}: auto-reparado — {}".format(rel_path, result.reason))
        if not result.valid:
            return None, "inválido: {}".format(result.reason)

    return target, ""


def _run_phase_tests(project_dir: Path, test_files: List[str]) -> Dict[str, Any]:
    """Corre los tests de la fase (pytest si está disponible, si no unittest)."""
    has_pytest = subprocess.run(
        [sys.executable, "-c", "import pytest"], capture_output=True
    ).returncode == 0
    if has_pytest:
        command = [sys.executable, "-m", "pytest", "-q"] + test_files
    else:
        modules = [f[:-3].replace("/", ".") for f in test_files]
        command = [sys.executable, "-m", "unittest"] + modules

    try:
        result = subprocess.run(
            command, cwd=str(project_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TESTS_TIMEOUT_SECONDS,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        summary = "\n".join(output.splitlines()[-5:])
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        summary, returncode = "timeout tras {}s".format(TESTS_TIMEOUT_SECONDS), -1
    except OSError as exc:
        summary, returncode = "no se pudieron correr: {}".format(exc), -1

    if returncode == 0:
        success("Tests de la fase OK ({})".format(" ".join(command[2:])))
    else:
        warn("Tests de la fase FALLARON (exit {}) — en 6.1 solo se registra:\n{}".format(returncode, summary))
    return {"command": " ".join(command), "returncode": returncode, "summary": summary}


def _tracker_totals() -> Tuple[int, float]:
    tracker = get_tracker()
    calls = sum(stats["calls"] for stats in tracker.providers.values())
    return calls, tracker.total_cost_usd()


def _write_log(project_dir: Path, log: Dict[str, Any]) -> None:
    path = project_dir / "phase_log.json"
    project_dir.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
        f.write("\n")


def get_director(config: Dict[str, Any], providers: List[AIProvider]) -> Optional[AIProvider]:
    """Devuelve el provider director según config['director'] (default 'claude')."""
    name = config.get("director", "claude")
    for provider in providers:
        if provider.name == name:
            return provider
    error("El director '{}' no está entre los providers habilitados ({})".format(
        name, ", ".join(p.name for p in providers) or "ninguno"
    ))
    info("Configurá 'director' en config.json con un provider enabled")
    return None


def run_director(providers: List[AIProvider], config: Dict[str, Any], request: str) -> None:
    """Flujo completo del Director: roadmap -> confirmación -> fases -> resumen."""
    tracker = get_tracker()
    director = get_director(config, providers)
    if director is None:
        return

    roadmap = _make_roadmap(director, request)
    if roadmap is None:
        return
    n = len(providers)
    _show_roadmap(roadmap, n)

    if Prompt.ask("¿Ejecutar el roadmap?", choices=["s", "n"], default="s") != "s":
        info("Director cancelado")
        return
    global_mode = Prompt.ask(
        "Modo global: (d) como sugiere el director / (r)ápido / (p)ro",
        choices=["d", "r", "p"], default="d",
    )
    auto = Prompt.ask("Ejecución", choices=["paso", "auto"], default="paso") == "auto"

    project_dir = _unique_project_dir(roadmap["project_name"])
    start_calls, start_cost = _tracker_totals()
    log: Dict[str, Any] = {
        "project": roadmap["project_name"],
        "request": request,
        "director": director.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "phases": [],
    }
    generated: List[Tuple[str, str]] = []
    total_phases = len(roadmap["phases"])
    completed = 0

    try:
        for phase in roadmap["phases"]:
            i = phase["number"]
            mode = _resolve_mode(phase, global_mode)
            console.print()
            console.rule("[bold]🎬 FASE {}/{}: {}[/bold] [dim]({})[/dim]".format(
                i, total_phases, phase["title"], mode
            ))
            if phase["objective"]:
                info(phase["objective"])
            for criterion in phase["acceptance_criteria"]:
                console.print("  [dim]· {}[/dim]".format(criterion))

            phase_calls_before, phase_cost_before = _tracker_totals()
            phase_files: List[Dict[str, Any]] = []
            new_test_files: List[str] = []

            for entry in phase["files"]:
                rel_path, purpose = entry["path"], entry["purpose"]
                label = "[fase {} · {}] ".format(i, rel_path)
                prompt = _phase_file_prompt(roadmap, phase, generated, rel_path, purpose)
                target, fail_reason = _generate_phase_file(
                    director, providers, config, project_dir, prompt, rel_path, mode, label
                )
                if target is None:
                    error("{}: {}".format(rel_path, fail_reason))
                    phase_files.append({"path": rel_path, "valid": False, "reason": fail_reason})
                    continue
                content = target.read_text(encoding="utf-8")
                generated.append((rel_path, content))
                phase_files.append({"path": rel_path, "valid": True, "bytes": target.stat().st_size})
                success("{} escrito ({:,} B)".format(rel_path, target.stat().st_size))
                if Path(rel_path).name.startswith("test_") and rel_path.endswith(".py"):
                    new_test_files.append(rel_path)

            tests_result = _run_phase_tests(project_dir, new_test_files) if new_test_files else None

            phase_calls_after, phase_cost_after = _tracker_totals()
            log["phases"].append({
                "number": i,
                "title": phase["title"],
                "mode": mode,
                "files": phase_files,
                "tests": tests_result,
                "calls": phase_calls_after - phase_calls_before,
                "cost_usd": round(phase_cost_after - phase_cost_before, 6),
                "completed_at": datetime.now().isoformat(timespec="seconds"),
            })
            _write_log(project_dir, log)
            completed += 1

            if not auto and i < total_phases:
                if Prompt.ask("¿Continuar con fase {}?".format(i + 1), choices=["s", "n"], default="s") != "s":
                    warn("Checkpoint: {} de {} fases; lo hecho queda en {}".format(completed, total_phases, project_dir))
                    break
    except KeyboardInterrupt:
        _write_log(project_dir, log)
        warn("Director interrumpido; checkpoint de {} fase(s) en {}".format(completed, project_dir / "phase_log.json"))
        raise

    _show_final_summary(project_dir, log, completed, total_phases, start_calls, start_cost)
    tracker.render_summary(console)
    tracker.save()


def _show_final_summary(project_dir: Path, log: Dict[str, Any], completed: int,
                        total_phases: int, start_calls: int, start_cost: float) -> None:
    end_calls, end_cost = _tracker_totals()
    table = Table(title="🎬 Resumen del Director", header_style="bold")
    table.add_column("Fase", justify="right")
    table.add_column("Título")
    table.add_column("Archivos")
    table.add_column("Tests")
    for entry in log["phases"]:
        ok_files = sum(1 for f in entry["files"] if f.get("valid"))
        tests = entry.get("tests")
        if tests is None:
            tests_cell = "[dim]—[/dim]"
        elif tests["returncode"] == 0:
            tests_cell = "[green]✔ OK[/green]"
        else:
            tests_cell = "[red]✖ exit {}[/red]".format(tests["returncode"])
        table.add_row(
            str(entry["number"]), entry["title"],
            "{}/{}".format(ok_files, len(entry["files"])), tests_cell,
        )
    console.print(table)

    status = "completo" if completed == total_phases else "parcial ({}/{})".format(completed, total_phases)
    info("Roadmap {} · {} llamadas · costo API ${:.4f}".format(
        status, end_calls - start_calls, end_cost - start_cost
    ))
    if completed:
        success("Proyecto en {} (registro: phase_log.json)".format(project_dir))
