"""Limpieza de markdown artifacts en respuestas de las IAs."""

import re
from typing import List, Optional

FENCE_RE = re.compile(r"^\s*```")


def _fenced_blocks(text: str) -> List[str]:
    """Extrae el contenido de cada bloque ```...``` del texto.

    Un fence de apertura sin cierre toma todo lo que le sigue.
    """
    blocks: List[str] = []
    current: Optional[List[str]] = None
    for line in text.splitlines():
        if FENCE_RE.match(line):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
        elif current is not None:
            current.append(line)
    if current:  # fence sin cerrar
        blocks.append("\n".join(current))
    return [b for b in blocks if b.strip()]


def clean_code(text: str) -> str:
    """Devuelve solo el código de una respuesta con fences markdown.

    Remueve fences de apertura/cierre, el preámbulo explicativo antes del
    primer fence y el texto posterior al último. Si no hay fences,
    devuelve el texto tal cual (trim).
    """
    blocks = _fenced_blocks(text)
    if not blocks:
        return text.strip()
    return "\n\n".join(b.strip("\n") for b in blocks)


def last_code_block(text: str) -> Optional[str]:
    """Devuelve el contenido del último bloque de código, o None si no hay fences."""
    blocks = _fenced_blocks(text)
    if not blocks:
        return None
    return blocks[-1].strip("\n")


def clean_filename(text: str) -> str:
    """Sanitiza un nombre de archivo: sin backticks, espacios raros ni paths absolutos."""
    name = text.strip().strip("`\"' ")
    name = name.replace("\\", "/").split("/")[-1]  # descarta cualquier path
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    name = name.lstrip(".")  # evita ocultos y '..'
    return name or "snippet.txt"


if __name__ == "__main__":
    # fence al inicio, con lenguaje
    assert clean_code("```python\nprint('hola')\n```") == "print('hola')"
    # preámbulo + fence + texto posterior
    assert clean_code("Acá va la función:\n```python\ndef f():\n    return 1\n```\nEspero que sirva.") == "def f():\n    return 1"
    # sin fences: devuelve tal cual (trim)
    assert clean_code("  x = 1\ny = 2  ") == "x = 1\ny = 2"
    # fence sin lenguaje
    assert clean_code("```\na = 5\n```") == "a = 5"
    # dos bloques: se conservan ambos; last_code_block devuelve el último
    doble = "Primero:\n```python\na = 1\n```\nDespués:\n```python\nb = 2\n```"
    assert clean_code(doble) == "a = 1\n\nb = 2"
    assert last_code_block(doble) == "b = 2"
    # clean_filename
    assert clean_filename("`suma.py`") == "suma.py"
    assert clean_filename("/etc/passwd") == "passwd"
    assert clean_filename("mi archivo raro!.py") == "mi_archivo_raro.py"
    assert clean_filename("../../evil.sh") == "evil.sh"
    print("cleaner.py: todos los tests inline OK")
