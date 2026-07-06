"""Evaluador de fases del Director, en dos niveles.

Nivel 1 (OBJETIVO, código sin LLM): validator sobre los archivos de la fase +
tests test_*.py con su output como evidencia. Nivel 2 (JUICIO): el director
evalúa objetivo y criterios de aceptación contra los archivos y los tests,
y devuelve un veredicto JSON con files_to_fix.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.table import Table

from core.cleaner import clean_code
from core.costs import get_tracker
from core.validator import validate_file
from providers.base import AIProvider
from ui.console import console, success, warn

TESTS_TIMEOUT_SECONDS = 120
MAX_FILE_CHARS = 4000          # truncado de cada archivo en el prompt del juez
MAX_TEST_OUTPUT_CHARS = 4000   # cola del output de tests usada como evidencia

EVAL_SYSTEM_PROMPT = (
    "Sos un evaluador técnico estricto pero razonable. Respondés únicamente "
    "con JSON válido, sin explicaciones ni fences de markdown."
)

EVAL_SCHEMA = (
    '{"verdict": "pass|fail", '
    '"criteria_check": [{"criterion": "...", "met": true, "note": "..."}], '
    '"issues": ["problema concreto 1"], '
    '"files_to_fix": ["path/relativo.py"]}'
)

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)


@dataclass
class EvaluationResult:
    """Veredicto de una fase, con evidencia lista para el prompt de corrección."""

    passed: bool
    level: str                      # 'objetivo' | 'juicio'
    feedback: str = ""
    files_to_fix: List[str] = field(default_factory=list)
    tests: Optional[Dict[str, Any]] = None
    judgment: Optional[Dict[str, Any]] = None


def run_phase_tests(project_dir: Path, test_files: List[str]) -> Dict[str, Any]:
    """Corre los tests de la fase (pytest si está disponible, si no unittest)."""
    has_pytest = subprocess.run(
        [sys.executable, "-c", "import pytest"], capture_output=True
    ).returncode == 0
    if has_pytest:
        command = [sys.executable, "-m", "pytest", "-q"] + test_files
    else:
        modules = [f[:-3].replace("/", ".") for f in test_files]
        command = [sys.executable, "-m", "unittest"] + modules

    # Los archivos regenerados en el loop de corrección pueden tener el mismo
    # tamaño y mtime (mismo segundo) que la versión rota: el import usaría el
    # bytecode viejo de __pycache__. Se limpia y se evita que vuelva a escribirse.
    for cache_dir in project_dir.rglob("__pycache__"):
        shutil.rmtree(str(cache_dir), ignore_errors=True)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        result = subprocess.run(
            command, cwd=str(project_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TESTS_TIMEOUT_SECONDS, env=env,
        )
        output = (result.stdout + "\n" + result.stderr).strip()[-MAX_TEST_OUTPUT_CHARS:]
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        output, returncode = "timeout tras {}s".format(TESTS_TIMEOUT_SECONDS), -1
    except OSError as exc:
        output, returncode = "no se pudieron correr: {}".format(exc), -1

    summary = "\n".join(output.splitlines()[-5:])
    if returncode == 0:
        success("Tests de la fase OK ({})".format(" ".join(command[2:])))
    else:
        warn("Tests de la fase fallaron (exit {}):\n{}".format(returncode, summary))
    return {
        "command": " ".join(command),
        "returncode": returncode,
        "summary": summary,
        "output": output,
    }


def _implicated_by_tests(project_dir: Path, test_files: List[str],
                         phase_paths: List[str]) -> List[str]:
    """Tests que fallan + archivos de la fase que esos tests importan.

    Análisis simple de imports; ante la duda implica de más (mejor regenerar
    un archivo sano que dejar uno roto).
    """
    modules = set()
    for test_file in test_files:
        try:
            source = (project_dir / test_file).read_text(encoding="utf-8")
        except OSError:
            continue
        modules.update(IMPORT_RE.findall(source))

    implicated = set(test_files)
    for path in phase_paths:
        if not path.endswith(".py"):
            continue
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        for imported in modules:
            if imported == module or imported.startswith(module + ".") or module.startswith(imported + "."):
                implicated.add(path)
                break
    return [p for p in phase_paths if p in implicated]


def _evaluate_objective(project_dir: Path, phase_paths: List[str]) -> EvaluationResult:
    """Nivel 1: validator + tests. Sin LLM."""
    invalid = []
    for path in phase_paths:
        result = validate_file(project_dir / path)
        if not result.valid:
            invalid.append((path, result.reason))

    test_files = [p for p in phase_paths if Path(p).name.startswith("test_") and p.endswith(".py")]
    runnable_tests = [t for t in test_files if t not in [p for p, _ in invalid]]
    tests = run_phase_tests(project_dir, runnable_tests) if runnable_tests else None

    feedback_lines = []
    files_to_fix = []
    for path, reason in invalid:
        feedback_lines.append("- El archivo {} es inválido: {}".format(path, reason))
        files_to_fix.append(path)
    if tests is not None and tests["returncode"] != 0:
        feedback_lines.append("- Los tests fallaron ({}, exit {}). Output:\n{}".format(
            tests["command"], tests["returncode"], tests["output"]
        ))
        for path in _implicated_by_tests(project_dir, runnable_tests, phase_paths):
            if path not in files_to_fix:
                files_to_fix.append(path)

    if feedback_lines:
        return EvaluationResult(
            passed=False, level="objetivo",
            feedback="\n".join(feedback_lines),
            files_to_fix=files_to_fix or list(phase_paths),
            tests=tests,
        )
    return EvaluationResult(passed=True, level="objetivo", tests=tests)


def _judge_prompt(phase: Dict[str, Any], files: Dict[str, str],
                  tests: Optional[Dict[str, Any]]) -> str:
    parts = [
        "Evaluá si esta fase de un proyecto cumple su objetivo.",
        "FASE: {}\nOBJETIVO: {}".format(phase["title"], phase["objective"] or "—"),
    ]
    if phase["acceptance_criteria"]:
        parts.append("CRITERIOS DE ACEPTACIÓN:\n" + "\n".join("- " + c for c in phase["acceptance_criteria"]))
    chunks = []
    for path, content in files.items():
        body = content if len(content) <= MAX_FILE_CHARS else content[:MAX_FILE_CHARS] + "\n... [truncado]"
        chunks.append("--- {} ---\n{}".format(path, body))
    parts.append("ARCHIVOS GENERADOS:\n" + "\n\n".join(chunks))
    if tests is not None:
        parts.append("RESULTADO DE TESTS ({}): exit {}\n{}".format(
            tests["command"], tests["returncode"], tests["output"]
        ))
    else:
        parts.append("RESULTADO DE TESTS: la fase no incluyó tests.")
    parts.append(
        "Respondé ÚNICAMENTE con un JSON válido con esta estructura exacta:\n" + EVAL_SCHEMA
        + "\nEn files_to_fix listá solo archivos de esta fase que haya que regenerar "
        "(lista vacía si verdict es pass). Marcá fail solo por incumplimientos reales "
        "del objetivo o de los criterios, no por preferencias de estilo."
    )
    return "\n\n".join(parts)


def _parse_judgment(text: str, phase_paths: List[str]) -> Optional[Dict[str, Any]]:
    candidate = clean_code(text)
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict) or str(data.get("verdict", "")).lower() not in ("pass", "fail"):
        return None

    criteria = []
    for entry in data.get("criteria_check", []):
        if isinstance(entry, dict):
            criteria.append({
                "criterion": str(entry.get("criterion", "")),
                "met": bool(entry.get("met")),
                "note": str(entry.get("note", "")),
            })
    return {
        "verdict": str(data["verdict"]).lower(),
        "criteria_check": criteria,
        "issues": [str(i) for i in data.get("issues", []) if str(i).strip()],
        "files_to_fix": [p for p in (str(f).replace("\\", "/") for f in data.get("files_to_fix", []))
                         if p in phase_paths],
    }


def _show_judgment(judgment: Dict[str, Any]) -> None:
    if judgment["criteria_check"]:
        table = Table(title="⚖ Veredicto del evaluador", header_style="bold")
        table.add_column("Criterio")
        table.add_column("Cumple")
        table.add_column("Nota")
        for entry in judgment["criteria_check"]:
            table.add_row(
                entry["criterion"],
                "[green]✔[/green]" if entry["met"] else "[red]✖[/red]",
                entry["note"] or "[dim]—[/dim]",
            )
        console.print(table)
    if judgment["verdict"] == "pass":
        success("Evaluador: la fase cumple el objetivo")
    else:
        warn("Evaluador: fail — " + ("; ".join(judgment["issues"]) or "sin detalle"))


def _evaluate_judgment(director: AIProvider, phase: Dict[str, Any],
                       files: Dict[str, str], tests: Optional[Dict[str, Any]],
                       phase_paths: List[str]) -> Optional[Dict[str, Any]]:
    """Nivel 2: el director juzga la fase. None si no devolvió JSON válido."""
    tracker = get_tracker()
    prompt = _judge_prompt(phase, files, tests)
    with console.status("[{0}]{1} evaluando la fase...[/{0}]".format(director.color, director.display_name)):
        response = director.generate(prompt, EVAL_SYSTEM_PROMPT)
    tracker.record(director.name, response, "evaluate")
    if response.ok:
        judgment = _parse_judgment(response.text, phase_paths)
        if judgment is not None:
            return judgment

    warn("El evaluador no devolvió JSON válido; reintentando...")
    retry_prompt = prompt + "\n\nTu respuesta anterior no fue un JSON válido. Respondé SOLO el JSON."
    with console.status("[{0}]{1} reevaluando...[/{0}]".format(director.color, director.display_name)):
        retry = director.generate(retry_prompt, EVAL_SYSTEM_PROMPT)
    tracker.record(director.name, retry, "evaluate")
    if retry.ok:
        return _parse_judgment(retry.text, phase_paths)
    return None


def evaluate_phase(director: AIProvider, project_dir: Path,
                   phase: Dict[str, Any], phase_paths: List[str]) -> EvaluationResult:
    """Evalúa la fase completa: nivel 1 (objetivo) y, si pasa, nivel 2 (juicio)."""
    objective = _evaluate_objective(project_dir, phase_paths)
    if not objective.passed:
        return objective

    files: Dict[str, str] = {}
    for path in phase_paths:
        try:
            files[path] = (project_dir / path).read_text(encoding="utf-8")
        except OSError:
            pass

    judgment = _evaluate_judgment(director, phase, files, objective.tests, phase_paths)
    if judgment is None:
        warn("Evaluador sin veredicto parseable; se considera pass (nivel 1 ya estaba en verde)")
        return EvaluationResult(passed=True, level="juicio", tests=objective.tests)

    _show_judgment(judgment)
    if judgment["verdict"] == "pass":
        return EvaluationResult(passed=True, level="juicio", tests=objective.tests, judgment=judgment)

    feedback_lines = ["- " + issue for issue in judgment["issues"]]
    for entry in judgment["criteria_check"]:
        if not entry["met"]:
            feedback_lines.append("- Criterio no cumplido: {} ({})".format(entry["criterion"], entry["note"]))
    return EvaluationResult(
        passed=False, level="juicio",
        feedback="\n".join(feedback_lines) or "el evaluador marcó fail sin detalle",
        files_to_fix=judgment["files_to_fix"] or list(phase_paths),
        tests=objective.tests,
        judgment=judgment,
    )
