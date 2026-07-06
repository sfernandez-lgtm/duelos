"""Validación (y auto-reparación) de archivos generados por las IAs.

Cubre el bug conocido: el merge a veces cuela líneas de prosa como preámbulo
sin fences, que el cleaner no puede distinguir de contenido y rompen el
archivo con SyntaxError.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAX_TRIM_LINES = 8  # cuántas líneas de prosa inicial/final intenta remover la reparación


@dataclass
class ValidationResult:
    """Resultado de validar (y eventualmente reparar) un archivo generado."""

    valid: bool
    reason: str = ""
    repaired: bool = False


def _compiles(source: str, filename: str) -> Optional[str]:
    """None si el fuente Python compila; el mensaje de error si no."""
    try:
        compile(source, filename, "exec")
        return None
    except SyntaxError as exc:
        return "línea {}: {}".format(exc.lineno, exc.msg)
    except ValueError as exc:  # p. ej. null bytes
        return str(exc)


def _is_prose_line(line: str) -> bool:
    """True si la línea parece prosa descartable: no es comentario ni compila sola."""
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("#"):
        return False  # los comentarios nunca se descartan
    return _compiles(stripped, "<line>") is not None


def _try_repair_py(source: str, filename: str) -> Optional[str]:
    """Intenta reparar quitando líneas de prosa iniciales/finales.

    Prueba recortes crecientes (primero del inicio, después del final) y
    devuelve la variante mínima que compila. Solo se descartan líneas que
    parecen prosa — nunca recorta líneas con pinta de código — para no
    'reparar' un archivo comiéndose contenido válido.
    """
    lines = source.splitlines()
    for start in range(0, min(MAX_TRIM_LINES, len(lines)) + 1):
        for end in range(0, min(MAX_TRIM_LINES, len(lines) - start) + 1):
            if start == 0 and end == 0:
                continue  # el original ya falló
            trimmed = lines[:start] + lines[len(lines) - end:]
            if not all(_is_prose_line(line) for line in trimmed):
                continue
            candidate = "\n".join(lines[start:len(lines) - end])
            if candidate.strip() and _compiles(candidate, filename) is None:
                return candidate
    return None


def validate_file(path) -> ValidationResult:
    """Valida un archivo generado según su extensión.

    .py: debe compilar; si no, intenta auto-reparar removiendo prosa
    inicial/final (reescribe el archivo si lo logra). .json: debe parsear.
    Otros: solo se verifica que no estén vacíos.
    """
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ValidationResult(valid=False, reason="no se pudo leer: {}".format(exc))

    if not content.strip():
        return ValidationResult(valid=False, reason="archivo vacío")

    suffix = path.suffix.lower()

    if suffix == ".py":
        error = _compiles(content, str(path))
        if error is None:
            return ValidationResult(valid=True)
        repaired = _try_repair_py(content, str(path))
        if repaired is not None:
            repaired = repaired.strip("\n") + "\n"
            path.write_text(repaired, encoding="utf-8")
            return ValidationResult(valid=True, repaired=True,
                                    reason="prosa inicial/final removida ({})".format(error))
        return ValidationResult(valid=False, reason="no compila: {}".format(error))

    if suffix == ".json":
        try:
            json.loads(content)
            return ValidationResult(valid=True)
        except json.JSONDecodeError as exc:
            return ValidationResult(valid=False, reason="JSON inválido: {}".format(exc))

    return ValidationResult(valid=True)
