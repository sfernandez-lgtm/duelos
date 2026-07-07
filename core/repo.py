"""Modo Repo: trabajar sobre código existente (repo git o carpeta local).

Carga el repo (clone o in-place), arma un mapa filtrado de archivos, el
director selecciona los archivos relevantes para la tarea, y la modificación
se aplica en una rama nueva (o con backups .duelo-backup en modo sin-git)
con la disciplina de siempre: cleaner + validator + auto-reparación +
1 reintento estricto. Si el repo tiene tests, se corren antes y después
como línea base de comparación (el loop de corrección llega en 7.2).
"""

import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from core.cleaner import clean_code
from core.costs import get_tracker
from core.director import get_director
from core.pipeline import run_pipeline
from core.project import GEN_SYSTEM_PROMPT, RETRY_ONLY_CODE, _kebab_case, _safe_relpath, _write_file
from core.validator import validate_file
from providers.base import AIProvider
from ui.console import console, error, info, success, warn

REPOS_DIR = Path.home() / "ai-projects" / "repos"
MAX_FILE_BYTES = 100 * 1024            # archivos más grandes quedan fuera del mapa
DEFAULT_REPO_CONTEXT_CHARS = 12000     # límite del resumen de contexto (config: repo_context_chars)
DEFAULT_MAX_REPO_FILES = 8             # tope de archivos seleccionados (config: max_repo_files)
TESTS_TIMEOUT_SECONDS = 300
CLONE_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 120
BACKUP_SUFFIX = ".duelo-backup"
README_MAX_CHARS = 2000                # truncado del README en el resumen
KEY_FILE_LINES = 30                    # primeras líneas de archivos clave en el resumen
KEY_FILES = ("pyproject.toml", "setup.py", "package.json", "requirements.txt",
             "Cargo.toml", "go.mod", "Makefile")

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode",
    "dist", "build", ".eggs", "site-packages",
}

BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".pdf", ".zip",
    ".gz", ".bz2", ".xz", ".7z", ".tar", ".whl", ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyd", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp3", ".mp4",
    ".avi", ".mov", ".sqlite", ".db", ".bin", ".jar", ".class", ".o", ".a",
}

LANG_EXTS = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".java": "Java", ".go": "Go", ".rb": "Ruby", ".rs": "Rust",
    ".php": "PHP", ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cs": "C#",
    ".swift": "Swift", ".kt": "Kotlin", ".sh": "Shell", ".html": "HTML", ".css": "CSS",
}

SELECT_SYSTEM_PROMPT = (
    "Sos el director técnico de un equipo de IAs que modifica repos de código "
    "existentes. Respondés únicamente con JSON válido, sin explicaciones ni "
    "fences de markdown."
)

SELECT_SCHEMA = (
    '{"relevant_files": ["path/existente.py"], "reason": "por qué estos archivos", '
    '"files_to_create": ["path/nuevo.py"], "branch_name": "duelo/kebab-de-la-tarea"}'
)


@dataclass
class RepoFile:
    """Un archivo del mapa del repo (ya filtrado: texto, <= 100KB)."""

    path: str   # relativo al root, con '/'
    size: int
    lines: int


@dataclass
class RepoContext:
    """Repo cargado: root, mapa de archivos y metadatos para el resumen."""

    root: Path
    name: str
    is_git: bool
    files: List[RepoFile]
    language: str
    has_tests: bool
    readme: str

    @property
    def paths(self) -> List[str]:
        return [f.path for f in self.files]


# ---------------------------------------------------------------- git helpers

def _git(root: Optional[Path], *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Corre git con cwd=root; nunca levanta: errores quedan en returncode/stderr."""
    command = ["git"] + list(args)
    try:
        return subprocess.run(
            command, cwd=str(root) if root else None, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(command, returncode=-1, stdout="", stderr=str(exc))


def _git_error(result: subprocess.CompletedProcess) -> str:
    return (result.stderr or "").strip() or (result.stdout or "").strip() or "error desconocido"


def _is_git_repo(root: Path) -> bool:
    result = _git(root, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def _current_branch(root: Path) -> str:
    result = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip() if result.returncode == 0 else "?"


def _is_git_url(source: str) -> bool:
    return bool(re.match(r"^(https?|git|ssh)://", source)) or source.startswith("git@")


def _repo_name_from_url(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1].split(":")[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    tail = re.sub(r"[^A-Za-z0-9._-]", "-", tail).strip("-.")
    return tail or "repo"


# ------------------------------------------------------------------ carga

def _clone_or_reuse(url: str) -> Optional[Path]:
    """Clona a ~/ai-projects/repos/<nombre>; si ya existe pregunta qué hacer."""
    dest = REPOS_DIR / _repo_name_from_url(url)
    if dest.exists():
        info("El repo ya está clonado en {}".format(dest))
        choice = Prompt.ask(
            "(p) actualizar con pull / (u) usar como está / (a) abortar",
            choices=["p", "u", "a"], default="u",
        )
        if choice == "a":
            return None
        if choice == "p":
            with console.status("git pull en {}...".format(dest.name)):
                result = _git(dest, "pull", "--ff-only", timeout=CLONE_TIMEOUT_SECONDS)
            if result.returncode != 0:
                warn("git pull falló: {}".format(_git_error(result)))
                if Prompt.ask("¿Usar el clon como está?", choices=["s", "n"], default="s") != "s":
                    return None
            else:
                success("Repo actualizado")
        return dest

    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    with console.status("Clonando {}...".format(url)):
        result = _git(None, "clone", url, str(dest), timeout=CLONE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        error("git clone falló: {}".format(_git_error(result)))
        return None
    success("Clonado en {}".format(dest))
    return dest


def _prepare_local(source: str) -> Optional[Tuple[Path, bool]]:
    """Valida un path local. Devuelve (root, is_git) o None si se aborta."""
    root = Path(source).expanduser()
    try:
        root = root.resolve()
    except OSError:
        pass
    if not root.is_dir():
        error("'{}' no existe o no es una carpeta".format(source))
        return None
    if _is_git_repo(root):
        return root, True

    warn("La carpeta no es un repo git; sin git no hay rama de seguridad")
    choice = Prompt.ask(
        "(i) git init / (s) seguir sin git con backups {} / (a) abortar".format(BACKUP_SUFFIX),
        choices=["i", "s", "a"], default="i",
    )
    if choice == "a":
        return None
    if choice == "i":
        result = _git(root, "init")
        if result.returncode != 0:
            error("git init falló: {}".format(_git_error(result)))
            return None
        success("Repo git inicializado en {}".format(root))
        return root, True
    return root, False


def _looks_binary(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTS:
        return True
    try:
        with open(str(path), "rb") as f:
            return b"\x00" in f.read(1024)
    except OSError:
        return True


def _scan_files(root: Path) -> List[RepoFile]:
    """Recorre el repo filtrando ruido: dirs ignorados, binarios y > 100KB."""
    files: List[RepoFile] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (p.is_dir(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in IGNORE_DIRS or entry.name.endswith(".egg-info"):
                    continue
                walk(entry)
            elif entry.is_file():
                if entry.name.endswith(BACKUP_SUFFIX):
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    continue
                if size > MAX_FILE_BYTES or _looks_binary(entry):
                    continue
                try:
                    text = entry.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                files.append(RepoFile(
                    path=entry.relative_to(root).as_posix(),
                    size=size,
                    lines=len(text.splitlines()),
                ))

    walk(root)
    return files


def _detect_language(files: List[RepoFile]) -> str:
    counts: Counter = Counter()
    for f in files:
        lang = LANG_EXTS.get(Path(f.path).suffix.lower())
        if lang:
            counts[lang] += f.lines
    return counts.most_common(1)[0][0] if counts else "—"


def _detect_tests(files: List[RepoFile]) -> bool:
    for f in files:
        name = Path(f.path).name
        parts = f.path.split("/")
        if name.startswith("test_") or re.search(r"_test\.\w+$", name) or "tests" in parts[:-1]:
            return True
    return False


def _read_readme(root: Path) -> str:
    try:
        for entry in sorted(root.iterdir()):
            if entry.is_file() and entry.name.lower().startswith("readme"):
                text = entry.read_text(encoding="utf-8", errors="replace").strip()
                if len(text) > README_MAX_CHARS:
                    text = text[:README_MAX_CHARS] + "\n... [truncado]"
                return text
    except OSError:
        pass
    return ""


def load_repo(source: str) -> Optional[RepoContext]:
    """Carga un repo desde URL git (clona) o path local (in-place) y arma su mapa."""
    source = source.strip().strip("\"'")
    if not source:
        return None
    if _is_git_url(source):
        root = _clone_or_reuse(source)
        if root is None:
            return None
        is_git = True
    else:
        prepared = _prepare_local(source)
        if prepared is None:
            return None
        root, is_git = prepared

    with console.status("Armando el mapa del repo..."):
        files = _scan_files(root)
    if not files:
        error("El repo no tiene archivos de texto legibles (fuera de los filtros)")
        return None

    return RepoContext(
        root=root,
        name=root.name,
        is_git=is_git,
        files=files,
        language=_detect_language(files),
        has_tests=_detect_tests(files),
        readme=_read_readme(root),
    )


# ------------------------------------------------------------------ resumen

def _render_tree(files: List[RepoFile]) -> str:
    """Árbol indentado con líneas y tamaño por archivo."""
    lines: List[str] = []
    seen_dirs = set()
    for f in sorted(files, key=lambda x: x.path):
        parts = f.path.split("/")
        for depth in range(len(parts) - 1):
            prefix = "/".join(parts[:depth + 1])
            if prefix not in seen_dirs:
                seen_dirs.add(prefix)
                lines.append("{}{}/".format("  " * depth, parts[depth]))
        lines.append("{}{} ({} líneas, {:,} B)".format("  " * (len(parts) - 1), parts[-1], f.lines, f.size))
    return "\n".join(lines)


def build_context_summary(ctx: RepoContext, limit: int = DEFAULT_REPO_CONTEXT_CHARS) -> str:
    """Resumen compacto del repo para los prompts: árbol + README + archivos clave."""
    header = "Repo '{}' — lenguaje dominante: {} — {} archivos en el mapa — tests: {}".format(
        ctx.name, ctx.language, len(ctx.files), "sí" if ctx.has_tests else "no"
    )
    parts = [header, "ÁRBOL DE ARCHIVOS:\n" + _render_tree(ctx.files)]
    if ctx.readme:
        parts.append("README:\n" + ctx.readme)
    path_set = set(ctx.paths)
    for key_file in KEY_FILES:
        if key_file in path_set:
            try:
                head = "\n".join(
                    (ctx.root / key_file).read_text(encoding="utf-8", errors="replace").splitlines()[:KEY_FILE_LINES]
                )
            except OSError:
                continue
            parts.append("--- {} (primeras líneas) ---\n{}".format(key_file, head))
    text = "\n\n".join(parts)
    if len(text) > limit:
        text = text[:limit] + "\n... [mapa truncado]"
    return text


def show_repo_map(ctx: RepoContext, max_lines: int = 40) -> None:
    """Muestra al usuario el mapa resumido del repo."""
    total_lines = sum(f.lines for f in ctx.files)
    tree_lines = _render_tree(ctx.files).splitlines()
    body = "\n".join(tree_lines[:max_lines])
    if len(tree_lines) > max_lines:
        body += "\n[dim]... y {} líneas más del árbol[/dim]".format(len(tree_lines) - max_lines)
    console.print(Panel(
        "[bold]{}[/bold] — {}\n"
        "[dim]git:[/dim] {} · [dim]lenguaje:[/dim] {} · [dim]archivos:[/dim] {} · "
        "[dim]líneas:[/dim] {:,} · [dim]tests:[/dim] {}\n\n{}".format(
            ctx.name, ctx.root,
            "sí" if ctx.is_git else "[red]no (modo backups)[/red]",
            ctx.language, len(ctx.files), total_lines,
            "sí" if ctx.has_tests else "no", body,
        ),
        title="🔧 Mapa del repo",
        border_style="cyan",
    ))


# ------------------------------------------------------------------ selección

def _select_prompt(summary: str, task: str, max_files: int) -> str:
    return (
        "Hay que modificar un repo de código existente para cumplir una tarea.\n\n"
        "MAPA DEL REPO:\n{}\n\n"
        "TAREA:\n{}\n\n"
        "Seleccioná los archivos EXISTENTES del mapa que hay que leer y modificar "
        "para cumplir la tarea (máximo {}, priorizá los imprescindibles), y los "
        "archivos NUEVOS a crear solo si son estrictamente necesarios.\n"
        "Respondé ÚNICAMENTE con un JSON válido con esta estructura exacta:\n{}\n\n"
        "Reglas: en relevant_files usá los paths relativos EXACTAMENTE como figuran "
        "en el mapa; files_to_create es una lista vacía si no hay que crear nada; "
        "branch_name corto y descriptivo en kebab-case con prefijo 'duelo/'. "
        "Sin texto fuera del JSON.".format(summary, task, max_files, SELECT_SCHEMA)
    )


def _parse_selection_json(text: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Parseo tolerante del JSON de selección. Devuelve (data, error)."""
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
    return data, ""


def _normalize_branch(raw: str) -> str:
    name = raw.strip().strip("`\"' ")
    if name.lower().startswith("duelo/"):
        name = name[len("duelo/"):]
    return "duelo/" + (_kebab_case(name) if name.strip() else "tarea")


def _validate_selection(data: Dict[str, Any], ctx: RepoContext,
                        max_files: int) -> Tuple[Dict[str, Any], List[str]]:
    """Valida la selección contra el mapa real. Devuelve (selección saneada, problemas)."""
    issues: List[str] = []
    path_set = set(ctx.paths)

    relevant: List[str] = []
    missing: List[str] = []
    for item in data.get("relevant_files", []) or []:
        path = str(item).strip().strip("`\"' ").replace("\\", "/").lstrip("./")
        if not path:
            continue
        if path in path_set:
            if path not in relevant:
                relevant.append(path)
        else:
            missing.append(path)
    if missing:
        issues.append("estos archivos NO existen en el mapa del repo: {}".format(", ".join(missing)))

    creates: List[str] = []
    for item in data.get("files_to_create", []) or []:
        path = _safe_relpath(str(item))
        if path is None:
            issues.append("path inválido en files_to_create: {}".format(item))
            continue
        if path in path_set:
            # ya existe: se trata como archivo a modificar
            if path not in relevant:
                relevant.append(path)
            continue
        if (ctx.root / path).exists():
            issues.append("files_to_create incluye un archivo que ya existe: {}".format(path))
            continue
        if path not in creates:
            creates.append(path)

    if len(relevant) > max_files:
        issues.append("seleccionaste {} archivos existentes y el máximo es {}: "
                      "priorizá los imprescindibles".format(len(relevant), max_files))
        relevant = relevant[:max_files]

    if not relevant and not creates:
        issues.append("la selección quedó sin ningún archivo válido")

    return {
        "relevant_files": relevant,
        "files_to_create": creates,
        "reason": str(data.get("reason", "")).strip(),
        "branch_name": _normalize_branch(str(data.get("branch_name", ""))),
    }, issues


def select_files(director: AIProvider, ctx: RepoContext, task: str,
                 summary: str, max_files: int) -> Optional[Dict[str, Any]]:
    """El director elige los archivos relevantes; parseo tolerante + 1 reintento."""
    tracker = get_tracker()
    base_prompt = _select_prompt(summary, task, max_files)
    feedback = ""
    salvage: Optional[Dict[str, Any]] = None

    for attempt in (1, 2):
        prompt = base_prompt + feedback
        with console.status("[{0}]{1} seleccionando archivos relevantes...[/{0}]".format(
                director.color, director.display_name)):
            response = director.generate(prompt, SELECT_SYSTEM_PROMPT)
        tracker.record(director.name, response, "repo")
        if not response.ok:
            error("Selección falló: {}".format(response.error))
            return None

        data, parse_error = _parse_selection_json(response.text)
        if data is None:
            if attempt == 1:
                warn("La selección no vino como JSON válido ({}); reintentando...".format(parse_error))
                feedback = (
                    "\n\nTu respuesta anterior no fue un JSON válido (error: {}).\n"
                    "Respuesta anterior:\n{}\n\n"
                    "Respondé de nuevo ÚNICAMENTE con el JSON corregido, estructura:\n{}".format(
                        parse_error, response.text[:2000], SELECT_SCHEMA
                    )
                )
                continue
            break

        selection, issues = _validate_selection(data, ctx, max_files)
        if not issues:
            return selection
        salvage = selection if (selection["relevant_files"] or selection["files_to_create"]) else salvage
        if attempt == 1:
            warn("Selección con problemas; se le pide corregir: {}".format("; ".join(issues)))
            feedback = (
                "\n\nCORRECCIÓN: tu selección anterior tuvo estos problemas:\n{}\n"
                "Respondé de nuevo ÚNICAMENTE con el JSON corregido, estructura:\n{}".format(
                    "\n".join("- " + issue for issue in issues), SELECT_SCHEMA
                )
            )

    if salvage is not None:
        warn("La selección siguió con problemas; se usa la parte válida del último intento")
        return salvage
    error("El director no logró una selección válida de archivos; abortando")
    return None


def _show_selection(selection: Dict[str, Any], ctx: RepoContext) -> None:
    sizes = {f.path: f.lines for f in ctx.files}
    table = Table(title="Archivos seleccionados", header_style="bold")
    table.add_column("Archivo")
    table.add_column("Estado")
    table.add_column("Líneas", justify="right")
    for path in selection["relevant_files"]:
        table.add_row(path, "[cyan]existente → modificar[/cyan]", str(sizes.get(path, "?")))
    for path in selection["files_to_create"]:
        table.add_row(path, "[green]nuevo → crear[/green]", "—")
    console.print(table)
    if selection["reason"]:
        info("Razón del director: {}".format(selection["reason"]))
    info("Rama propuesta: [bold]{}[/bold]".format(selection["branch_name"]))


# ------------------------------------------------------------------ tests

def run_repo_tests(root: Path, label: str) -> Dict[str, Any]:
    """Corre los tests del repo (pytest si está disponible, si no unittest discover)."""
    has_pytest = subprocess.run(
        [sys.executable, "-c", "import pytest"], capture_output=True
    ).returncode == 0
    if has_pytest:
        command = [sys.executable, "-m", "pytest", "-q"]
    else:
        command = [sys.executable, "-m", "unittest", "discover"]

    # bytecode viejo puede enmascarar la versión regenerada (mismo tamaño y mtime)
    for cache_dir in root.rglob("__pycache__"):
        shutil.rmtree(str(cache_dir), ignore_errors=True)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    info("Corriendo tests ({}): {}".format(label, " ".join(command[1:])))
    try:
        result = subprocess.run(
            command, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TESTS_TIMEOUT_SECONDS, env=env,
        )
        output = (result.stdout + "\n" + result.stderr).strip()
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        output, returncode = "timeout tras {}s".format(TESTS_TIMEOUT_SECONDS), -1
    except OSError as exc:
        output, returncode = "no se pudieron correr: {}".format(exc), -1

    tests = {"command": " ".join(command), "returncode": returncode,
             "output": output[-4000:], "label": label}
    summary = tests_summary(tests)
    if returncode == 0:
        success("Tests ({}): {}".format(label, summary))
    else:
        warn("Tests ({}): {}\n{}".format(label, summary, "\n".join(output.splitlines()[-5:])))
    return tests


def tests_summary(tests: Optional[Dict[str, Any]]) -> str:
    """Resumen corto de una corrida de tests: '34 pass' / '30 pass, 4 fail' / 'exit N'."""
    if tests is None:
        return "—"
    output = tests["output"]
    pieces = []
    for pattern, tag in ((r"(\d+) passed", "pass"), (r"(\d+) failed", "fail"),
                         (r"(\d+) error", "error")):
        match = re.search(pattern, output)
        if match:
            pieces.append("{} {}".format(match.group(1), tag))
    if pieces:
        return ", ".join(pieces)
    match = re.search(r"Ran (\d+) tests?", output)
    if match:
        return "{} tests, {}".format(match.group(1), "OK" if tests["returncode"] == 0 else "FAILED")
    return "exit {}".format(tests["returncode"])


# ------------------------------------------------------------------ ejecución

def _unique_branch(root: Path, base: str) -> str:
    name, n = base, 2
    while _git(root, "rev-parse", "--verify", "--quiet", "refs/heads/" + name).returncode == 0:
        name = "{}-{}".format(base, n)
        n += 1
    return name


def _backup_original(root: Path, rel_path: str) -> None:
    source = root / rel_path
    if source.exists():
        shutil.copy2(str(source), str(source) + BACKUP_SUFFIX)
        info("{}: backup en {}{}".format(rel_path, rel_path, BACKUP_SUFFIX))


def _repo_file_prompt(summary: str, task: str, file_contents: List[Tuple[str, str]],
                      target: str, exists: bool) -> str:
    parts = [
        "Estás modificando un repo de código EXISTENTE para cumplir una tarea puntual.",
        "RESUMEN DEL REPO:\n" + summary,
        "TAREA:\n" + task,
    ]
    if file_contents:
        chunks = ["--- {} ---\n{}".format(path, content) for path, content in file_contents]
        parts.append("ARCHIVOS RELEVANTES DEL REPO (contenido actual COMPLETO):\n" + "\n\n".join(chunks))
    if exists:
        parts.append(
            "Ahora generá la versión NUEVA COMPLETA del archivo '{}'. Su contenido "
            "actual está arriba. Modificá SOLAMENTE lo necesario para cumplir la tarea "
            "y preservá TODO el resto exactamente igual: estructura, imports, "
            "comentarios, docstrings y formato.".format(target)
        )
    else:
        parts.append(
            "Ahora generá el archivo NUEVO '{}', consistente con el estilo y las "
            "convenciones del repo.".format(target)
        )
    parts.append(
        "Respondé ÚNICAMENTE con el contenido completo del archivo, sin explicaciones, "
        "sin fences de markdown y sin repetir el nombre del archivo."
    )
    return "\n\n".join(parts)


def _generate_repo_file(director: AIProvider, providers: List[AIProvider],
                        config: Dict[str, Any], root: Path, prompt: str,
                        rel_path: str, pro_mode: bool, label: str) -> Tuple[Optional[Path], str]:
    """Genera un archivo del repo (rápido o pro), lo limpia, escribe, valida y
    reintenta 1 vez en estricto si queda inválido. Devuelve (path o None, motivo)."""
    tracker = get_tracker()

    if pro_mode:
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

    content = clean_code(raw)  # regla sagrada: todo pasa por el cleaner
    if not content:
        return None, "respuesta vacía"

    target = _write_file(root, rel_path, content)
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
            target = _write_file(root, rel_path, retry_content)
            result = validate_file(target)
            if result.repaired:
                info("{}: auto-reparado — {}".format(rel_path, result.reason))
        if not result.valid:
            return None, "inválido: {}".format(result.reason)

    return target, ""


def _show_final_report(ctx: RepoContext, branch: Optional[str], previous_branch: Optional[str],
                       written: List[str], failed: List[Tuple[str, str]],
                       baseline: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]]) -> None:
    if ctx.is_git:
        status = _git(ctx.root, "status", "--short").stdout.rstrip()
        diffstat = _git(ctx.root, "diff", "--stat").stdout.rstrip()
        if status:
            console.print(Panel(status, title="git status --short", border_style="cyan", expand=False))
        if diffstat:
            console.print(Panel(diffstat, title="git diff --stat", border_style="cyan", expand=False))

    if baseline is not None or after is not None:
        info("Tests — antes: {} · después: {}".format(tests_summary(baseline), tests_summary(after)))
        if baseline is not None and after is not None:
            if after["returncode"] == 0:
                success("Los tests siguen en verde")
            elif baseline["returncode"] == 0:
                warn("Los cambios ROMPIERON tests que antes pasaban (el loop de corrección llega en 7.2)")
            else:
                warn("Los tests ya fallaban en la línea base; revisá si son los mismos fallos")

    lines = []
    if branch:
        lines.append("Rama: [bold]{}[/bold] (tu rama anterior: {})".format(branch, previous_branch or "?"))
    else:
        lines.append("Modo sin git: originales respaldados como <archivo>{}".format(BACKUP_SUFFIX))
    lines.append("Archivos tocados: {}".format(", ".join(written) if written else "ninguno"))
    if failed:
        lines.append("[red]Fallaron: {}[/red]".format(
            "; ".join("{} ({})".format(p, r) for p, r in failed)
        ))
    if ctx.is_git:
        lines.append("Nada quedó commiteado: revisá y commiteá vos.")
        lines.append("[dim]Para ver el diff completo: cd {} && git diff[/dim]".format(ctx.root))
        if previous_branch and previous_branch != "?":
            lines.append("[dim]Para volver a tu rama: git checkout {}[/dim]".format(previous_branch))
    console.print(Panel("\n".join(lines), title="🔧 Resultado del modo Repo", border_style="green" if written else "red"))


def run_repo_task(providers: List[AIProvider], config: Dict[str, Any],
                  ctx: RepoContext, task: str) -> None:
    """Flujo completo: selección -> confirmación -> rama -> generación -> tests -> reporte."""
    tracker = get_tracker()
    director = get_director(config, providers)
    if director is None:
        return

    try:
        context_chars = max(1000, int(config.get("repo_context_chars", DEFAULT_REPO_CONTEXT_CHARS)))
    except (TypeError, ValueError):
        context_chars = DEFAULT_REPO_CONTEXT_CHARS
    try:
        max_files = max(1, int(config.get("max_repo_files", DEFAULT_MAX_REPO_FILES)))
    except (TypeError, ValueError):
        max_files = DEFAULT_MAX_REPO_FILES

    summary = build_context_summary(ctx, context_chars)
    selection = select_files(director, ctx, task, summary, max_files)
    if selection is None:
        return
    _show_selection(selection, ctx)
    if Prompt.ask("¿Aplicar la tarea sobre estos archivos?", choices=["s", "n"], default="s") != "s":
        info("Tarea cancelada")
        return

    if ctx.is_git:
        dirty = _git(ctx.root, "status", "--porcelain").stdout.strip()
        if dirty:
            warn("El repo tiene cambios sin commitear; se mezclarían con los de DUELO en la rama nueva")
            if Prompt.ask("¿Continuar igual?", choices=["s", "n"], default="n") != "s":
                info("Tarea cancelada")
                return

    baseline = run_repo_tests(ctx.root, "línea base") if ctx.has_tests else None

    n = len(providers)
    total_files = len(selection["relevant_files"]) + len(selection["files_to_create"])
    pro_calls = total_files * (n + n * (n - 1) + 1)
    info(
        "Llamadas estimadas — rápido: {} · pro con {} provider(s): {} "
        "(por archivo: {} generaciones + {} reviews + 1 merge)".format(
            total_files, n, pro_calls, n, n * (n - 1)
        )
    )
    pro_mode = Prompt.ask("Modo: (r)ápido o (p)ro?", choices=["r", "p"], default="r") == "p"

    # SEGURIDAD PRIMERO: rama nueva antes de tocar nada (o backups en modo sin-git)
    branch: Optional[str] = None
    previous_branch: Optional[str] = None
    if ctx.is_git:
        previous_branch = _current_branch(ctx.root)
        branch = _unique_branch(ctx.root, selection["branch_name"])
        result = _git(ctx.root, "checkout", "-b", branch)
        if result.returncode != 0:
            error("No se pudo crear la rama {}: {}".format(branch, _git_error(result)))
            return
        success("Trabajando en la rama nueva [bold]{}[/bold]".format(branch))
    else:
        info("Modo sin git: cada archivo existente se respalda como <archivo>{} antes de tocarlo".format(BACKUP_SUFFIX))

    file_contents: List[Tuple[str, str]] = []
    for rel_path in selection["relevant_files"]:
        try:
            file_contents.append((rel_path, (ctx.root / rel_path).read_text(encoding="utf-8", errors="replace")))
        except OSError as exc:
            warn("{}: no se pudo leer ({}); queda fuera del contexto".format(rel_path, exc))

    targets = [(p, True) for p in selection["relevant_files"]] + \
              [(p, False) for p in selection["files_to_create"]]
    written: List[str] = []
    failed: List[Tuple[str, str]] = []

    for i, (rel_path, exists) in enumerate(targets, start=1):
        label = "[repo {}/{}: {}] ".format(i, len(targets), rel_path)
        prompt = _repo_file_prompt(summary, task, file_contents, rel_path, exists)
        if not ctx.is_git and exists:
            _backup_original(ctx.root, rel_path)
        target, fail_reason = _generate_repo_file(
            director, providers, config, ctx.root, prompt, rel_path, pro_mode, label
        )
        if target is None:
            error("{}: {}".format(rel_path, fail_reason))
            failed.append((rel_path, fail_reason))
            continue
        written.append(rel_path)
        success("{}/{} {} escrito ({:,} B)".format(i, len(targets), rel_path, target.stat().st_size))

    after = run_repo_tests(ctx.root, "post-cambios") if ctx.has_tests and written else None

    _show_final_report(ctx, branch, previous_branch, written, failed, baseline, after)
    tracker.render_summary(console)
    tracker.save()
