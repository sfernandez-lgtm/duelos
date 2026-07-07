"""Director multi-fase sobre un repo EXISTENTE (Etapa 7.2).

Reusa el roadmap por fases del Director y la mecánica del modo Repo: rama
única duelo/<proyecto> para todo el trabajo, línea base de tests del repo
antes de la fase 1, evaluación de dos niveles comparando contra esa línea
base (lo que ya fallaba antes no cuenta contra la fase), loop de corrección
por fase (max_fix_iterations), y COMMIT por fase en verde como checkpoint
real. La rama nunca se mergea ni pushea sola; el phase_log.json va a
~/ai-projects/logs/ para no ensuciar el repo del usuario.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from core.cleaner import clean_code
from core.costs import get_tracker
from core.director import (
    DIRECTOR_SYSTEM_PROMPT,
    _fix_feedback,
    _phase_calls,
    _resolve_mode,
    _tracker_totals,
    get_director,
)
from core.evaluator import (
    EvaluationResult,
    _evaluate_judgment,
    _implicated_by_tests,
    _show_judgment,
)
from core.project import MAX_CONTEXT_CHARS_PER_FILE, _kebab_case, _safe_relpath
from core.repo import (
    DEFAULT_REPO_CONTEXT_CHARS,
    MAX_FEEDBACK_CHARS,
    RepoContext,
    _current_branch,
    _failing_test_files,
    _generate_repo_file,
    _git,
    _git_error,
    _unique_branch,
    build_context_summary,
    new_failures,
    run_repo_tests,
    tests_summary,
)
from core.validator import validate_file
from providers.base import AIProvider
from ui.console import console, error, info, success, warn

LOGS_DIR = Path.home() / "ai-projects" / "logs"

REPO_ROADMAP_SCHEMA = (
    '{"project_name": "nombre-en-kebab-case", "description": "resumen del trabajo", '
    '"phases": [{"number": 1, "title": "...", "objective": "qué debe existir al terminar", '
    '"existing_files_to_modify": ["path/existente.py"], '
    '"files": [{"path": "relativo/nuevo.py", "purpose": "qué hace"}], '
    '"acceptance_criteria": ["criterio verificable 1"], '
    '"suggested_mode": "rapido|pro", "depends_on": []}], '
    '"run_instructions": "cómo verificar el resultado"}'
)


def _repo_roadmap_prompt(request: str, summary: str) -> str:
    return (
        "Armá el roadmap por fases de este trabajo SOBRE UN REPO EXISTENTE:\n\n"
        + request
        + "\n\nMAPA DEL REPO:\n" + summary
        + "\n\nRespondé ÚNICAMENTE con un JSON válido con esta estructura exacta:\n"
        + REPO_ROADMAP_SCHEMA
        + "\n\nReglas del roadmap:\n"
        "- Fases chicas y ordenadas; cada fase deja el repo funcionando.\n"
        "- existing_files_to_modify: SOLO archivos que figuran en el mapa, con el "
        "path EXACTAMENTE como aparece ahí.\n"
        "- files: SOLO archivos nuevos que no existen todavía.\n"
        "- Los tests van como archivos test_*.py dentro de la fase a la que corresponden.\n"
        "- suggested_mode: 'rapido' para lo trivial, 'pro' solo para lo crítico o difícil.\n"
        "- Paths relativos sin '..'. Numeración desde 1. Sin texto fuera del JSON."
    )


def _parse_repo_roadmap(text: str, ctx: RepoContext) -> Tuple[Optional[Dict[str, Any]], List[str], str]:
    """Parseo tolerante + validación contra el mapa real.

    Devuelve (roadmap, problemas de validación, error de parseo).
    """
    candidate = clean_code(text)
    if not candidate:
        return None, [], "respuesta vacía"
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None, [], str(exc)
        try:
            data = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as exc2:
            return None, [], str(exc2)
    if not isinstance(data, dict):
        return None, [], "el JSON no es un objeto"

    path_set = set(ctx.paths)
    planned_new: set = set()
    issues: List[str] = []
    phases: List[Dict[str, Any]] = []

    for raw in data.get("phases", []):
        if not isinstance(raw, dict):
            continue
        modify: List[str] = []
        for item in raw.get("existing_files_to_modify", []) or []:
            path = str(item).strip().strip("`\"' ").replace("\\", "/").lstrip("./")
            if not path:
                continue
            if path in path_set or path in planned_new:
                if path not in modify:
                    modify.append(path)
            else:
                issues.append("existing_files_to_modify tiene un archivo que NO existe en el mapa: {}".format(path))
        files: List[Dict[str, str]] = []
        for entry in raw.get("files", []) or []:
            if not isinstance(entry, dict):
                continue
            path = _safe_relpath(str(entry.get("path", "")))
            if path is None:
                issues.append("path inválido en files: {}".format(entry.get("path")))
                continue
            if path in path_set or path in planned_new or (ctx.root / path).exists():
                # ya existe (o lo crea una fase anterior): se trata como modificación
                if path not in modify:
                    modify.append(path)
                continue
            planned_new.add(path)
            files.append({"path": path, "purpose": str(entry.get("purpose", ""))})
        if not modify and not files:
            continue
        mode = str(raw.get("suggested_mode", "rapido")).lower()
        phases.append({
            "number": len(phases) + 1,  # renumera por orden, ignora numeración rota
            "title": str(raw.get("title", "fase {}".format(len(phases) + 1))),
            "objective": str(raw.get("objective", "")),
            "modify": modify,
            "files": files,
            "acceptance_criteria": [str(c) for c in raw.get("acceptance_criteria", []) if str(c).strip()],
            "suggested_mode": "pro" if "pro" in mode else "rapido",
            "depends_on": raw.get("depends_on", []),
        })

    if not phases:
        return None, issues, "el roadmap no tiene fases con archivos válidos"

    return {
        "project_name": _kebab_case(str(data.get("project_name", ""))),
        "description": str(data.get("description", "")),
        "phases": phases,
        "run_instructions": str(data.get("run_instructions", "")),
    }, issues, ""


def _make_repo_roadmap(director: AIProvider, request: str, ctx: RepoContext,
                       summary: str) -> Optional[Dict[str, Any]]:
    """Pide el roadmap sobre el repo, con 1 reintento por JSON roto o paths inválidos."""
    tracker = get_tracker()
    base_prompt = _repo_roadmap_prompt(request, summary)
    feedback = ""
    salvage: Optional[Dict[str, Any]] = None

    for attempt in (1, 2):
        with console.status("[{0}]{1} armando el roadmap sobre el repo...[/{0}]".format(
                director.color, director.display_name)):
            response = director.generate(base_prompt + feedback, DIRECTOR_SYSTEM_PROMPT)
        tracker.record(director.name, response, "direct")
        if not response.ok:
            error("Roadmap falló: {}".format(response.error))
            return None

        roadmap, issues, parse_error = _parse_repo_roadmap(response.text, ctx)
        if roadmap is None:
            if attempt == 1:
                warn("El roadmap no vino válido ({}); reintentando...".format(parse_error))
                feedback = (
                    "\n\nTu respuesta anterior no fue un JSON válido con la estructura "
                    "pedida (error: {}).\nRespuesta anterior:\n{}\n\n"
                    "Respondé de nuevo ÚNICAMENTE con el JSON corregido, estructura:\n{}".format(
                        parse_error, response.text[:2000], REPO_ROADMAP_SCHEMA
                    )
                )
                continue
            break
        if not issues:
            return roadmap
        salvage = roadmap
        if attempt == 1:
            warn("Roadmap con problemas; se le pide corregir: {}".format("; ".join(issues)))
            feedback = (
                "\n\nCORRECCIÓN: tu roadmap anterior tuvo estos problemas:\n{}\n"
                "Respondé de nuevo ÚNICAMENTE con el JSON corregido, estructura:\n{}".format(
                    "\n".join("- " + issue for issue in issues), REPO_ROADMAP_SCHEMA
                )
            )

    if salvage is not None:
        warn("El roadmap siguió con problemas; se usa la parte válida del último intento")
        return salvage
    error("El director no logró un roadmap válido sobre el repo; abortando")
    return None


def _show_repo_roadmap(roadmap: Dict[str, Any], ctx: RepoContext, n_providers: int) -> None:
    console.print(Panel(
        "[bold]{}[/bold] sobre el repo [bold]{}[/bold]\n{}\n\n[dim]Cómo verificarlo:[/dim] {}".format(
            roadmap["project_name"], ctx.name, roadmap["description"],
            roadmap["run_instructions"] or "—"
        ),
        title="🎬 Roadmap del Director sobre repo",
        border_style="cyan",
    ))
    table = Table(header_style="bold")
    table.add_column("Fase", justify="right")
    table.add_column("Título")
    table.add_column("Modifica")
    table.add_column("Crea")
    table.add_column("Modo")
    table.add_column("Llamadas", justify="right")

    total_calls = 0
    for phase in roadmap["phases"]:
        files_count = len(phase["modify"]) + len(phase["files"])
        calls = _phase_calls(files_count, phase["suggested_mode"], n_providers)
        total_calls += calls
        table.add_row(
            str(phase["number"]),
            phase["title"],
            "\n".join(phase["modify"]) or "[dim]—[/dim]",
            "\n".join(f["path"] for f in phase["files"]) or "[dim]—[/dim]",
            phase["suggested_mode"],
            str(calls),
        )
    table.add_row("", "[bold]TOTAL[/bold]", "", "", "", "[bold]{}[/bold]".format(total_calls))
    console.print(table)


def _phase_paths(phase: Dict[str, Any]) -> List[str]:
    return phase["modify"] + [f["path"] for f in phase["files"]]


def _repo_phase_prompt(summary: str, roadmap: Dict[str, Any], phase: Dict[str, Any],
                       workspace: Dict[str, str], target: str, purpose: str) -> str:
    """Prompt de un archivo de fase: contexto del repo + workspace + instrucción."""
    parts: List[str] = [
        "Estás ejecutando por fases un trabajo grande sobre un repo de código EXISTENTE.",
        "RESUMEN DEL REPO:\n" + summary,
        "PEDIDO COMPLETO: " + roadmap["description"],
        "Roadmap:\n" + "\n".join(
            "- Fase {}: {} (modifica: {} · crea: {})".format(
                p["number"], p["title"],
                ", ".join(p["modify"]) or "—",
                ", ".join(f["path"] for f in p["files"]) or "—",
            )
            for p in roadmap["phases"]
        ),
        "FASE ACTUAL {}: {}\nObjetivo: {}".format(phase["number"], phase["title"], phase["objective"])
        + ("\nCriterios de aceptación:\n" + "\n".join("- " + c for c in phase["acceptance_criteria"])
           if phase["acceptance_criteria"] else ""),
    ]
    if workspace:
        chunks = []
        for path, content in workspace.items():
            body = content
            if path != target and len(body) > MAX_CONTEXT_CHARS_PER_FILE:
                body = body[:MAX_CONTEXT_CHARS_PER_FILE] + "\n... [truncado]"
            chunks.append("--- {} ---\n{}".format(path, body))
        parts.append("ARCHIVOS DE TRABAJO (contenido actual):\n" + "\n\n".join(chunks))
    if target in workspace:
        parts.append(
            "Ahora generá la versión NUEVA COMPLETA del archivo '{}'. Su contenido "
            "actual está arriba (completo). Modificá SOLAMENTE lo necesario para esta "
            "fase y preservá TODO el resto exactamente igual: estructura, imports, "
            "comentarios y formato.".format(target)
        )
    else:
        parts.append(
            "Ahora generá el archivo NUEVO '{}'{}, consistente con el estilo y las "
            "convenciones del repo.".format(target, " (propósito: {})".format(purpose) if purpose else "")
        )
    parts.append(
        "Si es un archivo de tests, usá unittest de la stdlib (NO pytest) salvo que el "
        "repo declare esa dependencia.\n"
        "Respondé ÚNICAMENTE con el contenido completo del archivo, sin explicaciones, "
        "sin fences de markdown y sin repetir el nombre del archivo."
    )
    return "\n\n".join(parts)


def _generate_phase_targets(director: AIProvider, providers: List[AIProvider],
                            config: Dict[str, Any], ctx: RepoContext, summary: str,
                            roadmap: Dict[str, Any], phase: Dict[str, Any],
                            workspace: Dict[str, str], targets: List[str],
                            phase_state: Dict[str, Dict[str, Any]], mode: str,
                            feedback: str = "", attempt: int = 0) -> None:
    """Genera (o regenera con feedback) los `targets` de la fase, actualizando
    in place `workspace` (contexto acumulado) y `phase_state` (estado por archivo)."""
    purposes = {f["path"]: f["purpose"] for f in phase["files"]}
    for rel_path in targets:
        # el contenido actual en disco manda (fases/correcciones anteriores)
        try:
            workspace[rel_path] = (ctx.root / rel_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            workspace.pop(rel_path, None)
        label = "[fase {} · {}] ".format(phase["number"], rel_path)
        prompt = _repo_phase_prompt(summary, roadmap, phase, workspace, rel_path,
                                    purposes.get(rel_path, ""))
        if feedback:
            prompt += _fix_feedback(attempt, feedback, rel_path, workspace.get(rel_path, ""))
        target, fail_reason = _generate_repo_file(
            director, providers, config, ctx.root, prompt, rel_path, mode == "pro", label
        )
        if target is None:
            error("{}: {}".format(rel_path, fail_reason))
            phase_state[rel_path] = {"path": rel_path, "valid": False, "reason": fail_reason}
            continue
        workspace[rel_path] = target.read_text(encoding="utf-8", errors="replace")
        phase_state[rel_path] = {"path": rel_path, "valid": True, "bytes": target.stat().st_size}
        success("{} escrito ({:,} B)".format(rel_path, target.stat().st_size))


def _evaluate_repo_phase(director: AIProvider, ctx: RepoContext, phase: Dict[str, Any],
                         paths: List[str], baseline: Optional[Dict[str, Any]],
                         run_tests: bool) -> EvaluationResult:
    """Nivel 1: validator + tests del repo comparados contra la línea base.
    Nivel 2 (si pasa): juicio del director con los criterios de la fase."""
    invalid: List[Tuple[str, str]] = []
    for path in paths:
        result = validate_file(ctx.root / path)
        if not result.valid:
            invalid.append((path, result.reason))

    tests = run_repo_tests(ctx.root, "fase {}".format(phase["number"])) if run_tests else None
    broken = new_failures(baseline, tests)
    if tests is not None and tests["returncode"] != 0 and not broken:
        info("Los fallos de tests ya estaban en la línea base; no cuentan contra la fase")
        tests = dict(tests)
        tests["output"] = ("(nota: estos fallos ya existían en la línea base del repo, "
                           "NO fueron causados por esta fase)\n" + tests["output"])

    feedback_lines: List[str] = []
    files_to_fix: List[str] = []
    for path, reason in invalid:
        feedback_lines.append("- El archivo {} es inválido: {}".format(path, reason))
        files_to_fix.append(path)
    if broken:
        feedback_lines.append("- Tests rotos respecto de la línea base: {}\nOutput:\n{}".format(
            ", ".join(broken), (tests or {}).get("output", "")[-MAX_FEEDBACK_CHARS:]
        ))
        fail_files = _failing_test_files(ctx.root, broken)
        for path in _implicated_by_tests(ctx.root, fail_files, paths):
            if path not in files_to_fix:
                files_to_fix.append(path)

    if feedback_lines:
        return EvaluationResult(
            passed=False, level="objetivo",
            feedback="\n".join(feedback_lines),
            files_to_fix=files_to_fix or list(paths),
            tests=tests,
        )

    files: Dict[str, str] = {}
    for path in paths:
        try:
            files[path] = (ctx.root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    judgment = _evaluate_judgment(director, phase, files, tests, paths)
    if judgment is None:
        warn("Evaluador sin veredicto parseable; se considera pass (nivel 1 ya estaba en verde)")
        return EvaluationResult(passed=True, level="juicio", tests=tests)

    _show_judgment(judgment)
    if judgment["verdict"] == "pass":
        return EvaluationResult(passed=True, level="juicio", tests=tests, judgment=judgment)
    feedback_lines = ["- " + issue for issue in judgment["issues"]]
    for entry in judgment["criteria_check"]:
        if not entry["met"]:
            feedback_lines.append("- Criterio no cumplido: {} ({})".format(entry["criterion"], entry["note"]))
    return EvaluationResult(
        passed=False, level="juicio",
        feedback="\n".join(feedback_lines) or "el evaluador marcó fail sin detalle",
        files_to_fix=judgment["files_to_fix"] or list(paths),
        tests=tests,
        judgment=judgment,
    )


def _execute_repo_phase(director: AIProvider, providers: List[AIProvider],
                        config: Dict[str, Any], ctx: RepoContext, summary: str,
                        roadmap: Dict[str, Any], phase: Dict[str, Any],
                        workspace: Dict[str, str], mode: str, max_fix: int,
                        baseline: Optional[Dict[str, Any]], run_tests: bool) -> Dict[str, Any]:
    """Fase completa: generación + evaluación vs línea base + loop de corrección."""
    calls_before, cost_before = _tracker_totals()
    paths = _phase_paths(phase)
    phase_state: Dict[str, Dict[str, Any]] = {}

    _generate_phase_targets(director, providers, config, ctx, summary, roadmap,
                            phase, workspace, paths, phase_state, mode)

    attempts: List[Dict[str, Any]] = []
    tests_last: Optional[Dict[str, Any]] = None
    final_verdict: Optional[str] = None

    for attempt in range(1, max_fix + 2):
        evaluation = _evaluate_repo_phase(director, ctx, phase, paths, baseline, run_tests)
        if evaluation.tests is not None:
            tests_last = evaluation.tests
        attempts.append({
            "attempt": attempt,
            "level": evaluation.level,
            "passed": evaluation.passed,
            "feedback": evaluation.feedback[:1500],
            "tests": {k: v for k, v in (evaluation.tests or {}).items() if k != "output"} or None,
            "judgment": evaluation.judgment,
        })
        if evaluation.passed:
            final_verdict = "pass"
            success("Fase {} en verde (intento {})".format(phase["number"], attempt))
            break
        if attempt == max_fix + 1:
            final_verdict = "fail"
            error("Fase {} sigue en fail tras {} corrección(es)".format(phase["number"], max_fix))
            break

        resumen = evaluation.feedback.strip().splitlines()[0][:70] if evaluation.feedback else "evaluación en fail"
        console.rule("[bold yellow]FASE {} · corrección {}/{}: {}[/bold yellow]".format(
            phase["number"], attempt, max_fix, resumen
        ))
        targets = [p for p in paths if p in evaluation.files_to_fix] or paths
        attempts[-1]["files_fixed"] = targets
        _generate_phase_targets(director, providers, config, ctx, summary, roadmap,
                                phase, workspace, targets, phase_state, mode,
                                feedback=evaluation.feedback, attempt=attempt)

    calls_after, cost_after = _tracker_totals()
    tests_log = {k: v for k, v in (tests_last or {}).items() if k != "output"} or None
    if tests_log is not None:
        tests_log["summary"] = tests_summary(tests_last)
    return {
        "number": phase["number"],
        "title": phase["title"],
        "mode": mode,
        "files": list(phase_state.values()),
        "tests": tests_log,
        "attempts": attempts,
        "final_verdict": final_verdict,
        "commit": None,
        "calls": calls_after - calls_before,
        "cost_usd": round(cost_after - cost_before, 6),
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }


def _commit_phase(root: Path, phase: Dict[str, Any]) -> Optional[str]:
    """git add de los archivos de la fase + commit en la rama duelo/. Hash o None."""
    existing = [p for p in _phase_paths(phase) if (root / p).exists()]
    if not existing:
        return None
    add = _git(root, "add", "--", *existing)
    if add.returncode != 0:
        warn("git add falló: {}".format(_git_error(add)))
        return None
    message = "duelo: fase {} - {}".format(phase["number"], phase["title"])
    result = _git(root, "commit", "-m", message)
    if result.returncode != 0:
        warn("git commit falló: {}".format(_git_error(result)))
        return None
    return _git(root, "rev-parse", "--short", "HEAD").stdout.strip() or None


def _write_log(log_dir: Path, log: Dict[str, Any]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "phase_log.json"
    with open(str(path), "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def run_director_repo(providers: List[AIProvider], config: Dict[str, Any],
                      ctx: RepoContext, request: str) -> None:
    """Flujo completo del Director sobre repo: roadmap -> rama única -> fases con
    commit por fase en verde -> reporte final. Nada se mergea ni pushea jamás."""
    tracker = get_tracker()
    director = get_director(config, providers)
    if director is None:
        return
    if not ctx.is_git:
        error("El Director sobre repo necesita git (hace un commit por fase); "
              "inicializá git o usá el modo 🔧 Repo para tareas simples")
        return

    try:
        context_chars = max(1000, int(config.get("repo_context_chars", DEFAULT_REPO_CONTEXT_CHARS)))
    except (TypeError, ValueError):
        context_chars = DEFAULT_REPO_CONTEXT_CHARS
    summary = build_context_summary(ctx, context_chars)

    roadmap = _make_repo_roadmap(director, request, ctx, summary)
    if roadmap is None:
        return
    n = len(providers)
    _show_repo_roadmap(roadmap, ctx, n)

    if Prompt.ask("¿Ejecutar el roadmap sobre el repo?", choices=["s", "n"], default="s") != "s":
        info("Director cancelado")
        return
    global_mode = Prompt.ask(
        "Modo global: (d) como sugiere el director / (r)ápido / (p)ro",
        choices=["d", "r", "p"], default="d",
    )
    auto = Prompt.ask("Ejecución", choices=["paso", "auto"], default="paso") == "auto"

    dirty = _git(ctx.root, "status", "--porcelain").stdout.strip()
    if dirty:
        warn("El repo tiene cambios sin commitear; se mezclarían con los commits de DUELO")
        if Prompt.ask("¿Continuar igual?", choices=["s", "n"], default="n") != "s":
            info("Director cancelado")
            return

    # SEGURIDAD PRIMERO: rama única para todo el trabajo multi-fase
    previous_branch = _current_branch(ctx.root)
    branch = _unique_branch(ctx.root, "duelo/" + roadmap["project_name"])
    result = _git(ctx.root, "checkout", "-b", branch)
    if result.returncode != 0:
        error("No se pudo crear la rama {}: {}".format(branch, _git_error(result)))
        return
    success("Trabajando en la rama única [bold]{}[/bold]".format(branch))

    baseline = run_repo_tests(ctx.root, "línea base") if ctx.has_tests else None
    have_tests = ctx.has_tests

    log_dir = LOGS_DIR / "{}-{}".format(ctx.name, datetime.now().strftime("%Y%m%d-%H%M%S"))
    log: Dict[str, Any] = {
        "project": roadmap["project_name"],
        "repo": str(ctx.root),
        "branch": branch,
        "request": request,
        "director": director.name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_tests": {k: v for k, v in (baseline or {}).items() if k != "output"} or None,
        "phases": [],
    }
    workspace: Dict[str, str] = {}
    total_phases = len(roadmap["phases"])
    completed = 0
    final_tests: Optional[Dict[str, Any]] = baseline
    try:
        max_fix = max(0, int(config.get("max_fix_iterations", 2)))
    except (TypeError, ValueError):
        max_fix = 2

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

            phase_has_tests = any(
                Path(p).name.startswith("test_") and p.endswith(".py")
                for p in _phase_paths(phase)
            )
            run_tests = have_tests or phase_has_tests

            entry = _execute_repo_phase(director, providers, config, ctx, summary,
                                        roadmap, phase, workspace, mode, max_fix,
                                        baseline, run_tests)
            have_tests = run_tests
            # COMMIT POR FASE: checkpoint real solo si la fase quedó en verde
            if entry["final_verdict"] == "pass":
                sha = _commit_phase(ctx.root, phase)
                entry["commit"] = sha
                if sha:
                    success("Fase {} commiteada en {}: [bold]{}[/bold]".format(i, branch, sha))
                else:
                    warn("Fase {} en verde pero el commit no se pudo hacer; queda sin commitear".format(i))
            log["phases"].append(entry)
            log_path = _write_log(log_dir, log)
            completed += 1

            if entry["final_verdict"] == "fail":
                if auto:
                    error("Fase {} en fail en modo auto: se aborta preservando los "
                          "commits de las fases en verde".format(i))
                    break
                choice = Prompt.ask(
                    "Fase {} agotó los reintentos: (c)ontinuar sin commitear esta fase "
                    "/ (a)bortar dejando los commits de las fases buenas".format(i),
                    choices=["c", "a"], default="a",
                )
                if choice == "a":
                    warn("Abortado: los commits de las fases en verde quedan en {}".format(branch))
                    break

            if not auto and i < total_phases:
                if Prompt.ask("¿Continuar con fase {}?".format(i + 1), choices=["s", "n"], default="s") != "s":
                    warn("Checkpoint: {} de {} fases en la rama {}".format(completed, total_phases, branch))
                    break
    except KeyboardInterrupt:
        log_path = _write_log(log_dir, log)
        warn("Director interrumpido; registro en {}".format(log_path))
        raise

    # última corrida de tests real registrada (para la comparación final)
    for entry in reversed(log["phases"]):
        if entry.get("tests") is not None:
            final_tests = entry["tests"]
            break

    _show_repo_final_summary(log, ctx, previous_branch, branch, baseline, final_tests,
                             completed, total_phases, log_dir)
    tracker.render_summary(console)
    tracker.save()


def _tests_cell(tests: Optional[Dict[str, Any]]) -> str:
    if tests is None:
        return "—"
    if "output" not in tests:  # entrada del log: usa el resumen guardado
        return tests.get("summary") or "exit {}".format(tests["returncode"])
    return tests_summary(tests)


def _show_repo_final_summary(log: Dict[str, Any], ctx: RepoContext, previous_branch: str,
                             branch: str, baseline: Optional[Dict[str, Any]],
                             final_tests: Optional[Dict[str, Any]], completed: int,
                             total_phases: int, log_dir: Path) -> None:
    table = Table(title="🎬 Resumen del Director sobre repo", header_style="bold")
    table.add_column("Fase", justify="right")
    table.add_column("Título")
    table.add_column("Veredicto")
    table.add_column("Intentos", justify="right")
    table.add_column("Commit")
    for entry in log["phases"]:
        verdict = entry.get("final_verdict")
        if verdict == "pass":
            verdict_cell = "[green]✔ pass[/green]"
        elif verdict == "fail":
            verdict_cell = "[red]✖ fail[/red]"
        else:
            verdict_cell = "[dim]—[/dim]"
        attempts = entry.get("attempts") or []
        commit = entry.get("commit")
        table.add_row(
            str(entry["number"]), entry["title"], verdict_cell,
            str(len(attempts)) if attempts else "[dim]—[/dim]",
            "[bold]{}[/bold]".format(commit) if commit else "[dim]sin commit[/dim]",
        )
    console.print(table)

    if baseline is not None or final_tests is not None:
        info("Tests — línea base: {} · final: {}".format(
            _tests_cell(baseline), _tests_cell(final_tests)
        ))

    status = "completo" if completed == total_phases else "parcial ({}/{})".format(completed, total_phases)
    lines = [
        "Roadmap {} en la rama [bold]{}[/bold] (tu rama anterior: {})".format(status, branch, previous_branch),
        "La rama NO se mergea sola: revisá y mergeá vos.",
        "[dim]Commits de las fases:  git -C {} log --oneline {}[/dim]".format(ctx.root, branch),
        "[dim]Diff contra tu rama:   git -C {} diff {}...{}[/dim]".format(ctx.root, previous_branch, branch),
        "[dim]Mergear a mano:        git -C {} checkout {} && git -C {} merge {}[/dim]".format(
            ctx.root, previous_branch, ctx.root, branch
        ),
        "[dim]Registro: {}[/dim]".format(log_dir / "phase_log.json"),
    ]
    console.print(Panel("\n".join(lines), title="🎬 Resultado", border_style="green" if completed else "red"))
