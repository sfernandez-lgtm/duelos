"""Loader minimalista de .env (KEY=value), sin dependencias externas."""

import os
from typing import Optional

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def load_env(path: Optional[str] = None) -> int:
    """Carga las variables de un .env al entorno y devuelve cuántas cargó.

    Formato KEY=value, una por línea. Ignora comentarios y líneas vacías.
    No pisa variables ya presentes en el entorno.
    """
    path = path or ENV_PATH
    if not os.path.exists(path):
        return 0
    loaded = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and value and key not in os.environ:
                os.environ[key] = value
                loaded += 1
    return loaded
