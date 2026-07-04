"""Manejo de sesión de chat: historial, prompt con contexto y logs."""

import json
import os
from datetime import datetime
from typing import Any, Dict, List

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
MAX_HISTORY_CHARS = 80000

ROLE_LABELS = {"user": "USUARIO", "assistant": "ASISTENTE"}


class Session:
    """Historial de turnos de una sesión de chat con un provider."""

    def __init__(self, provider_name: str = ""):
        self.provider_name = provider_name
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.turns: List[Dict[str, Any]] = []

    def add_turn(self, role: str, content: str) -> None:
        """Agrega un turno y trunca el historial si supera el límite."""
        self.turns.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })
        self._truncate()

    def _truncate(self) -> None:
        """Descarta los turnos más viejos si el historial supera ~80k caracteres."""
        total = sum(len(t["content"]) for t in self.turns)
        while total > MAX_HISTORY_CHARS and len(self.turns) > 1:
            oldest = self.turns.pop(0)
            total -= len(oldest["content"])

    def clear(self) -> None:
        """Resetea el historial de la sesión."""
        self.turns = []

    def build_prompt(self, new_message: str) -> str:
        """Arma el prompt con el historial completo + el mensaje nuevo.

        Formato con etiquetas USUARIO:/ASISTENTE:, pensado para providers
        sin memoria entre llamadas. No modifica el historial.
        """
        parts: List[str] = []
        if self.turns:
            parts.append("Historial de la conversación hasta ahora:")
            for turn in self.turns:
                label = ROLE_LABELS.get(turn["role"], turn["role"].upper())
                parts.append("{}: {}".format(label, turn["content"]))
            parts.append("Continuá la conversación respondiendo al último mensaje.")
        parts.append("USUARIO: {}".format(new_message))
        parts.append("ASISTENTE:")
        return "\n\n".join(parts)

    def save_log(self, prefix: str = "coder") -> str:
        """Guarda la sesión en logs/<prefix>_YYYYMMDD_HHMMSS.json y devuelve la ruta."""
        os.makedirs(LOGS_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(LOGS_DIR, "{}_{}.json".format(prefix, stamp))
        data = {
            "provider": self.provider_name,
            "started_at": self.started_at,
            "turns": self.turns,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return path
