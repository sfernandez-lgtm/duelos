"""Registro de consumo de tokens y costos por provider durante la sesión."""

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from providers.base import ProviderResponse

COSTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "costs.json")


class CostTracker:
    """Acumula llamadas, tokens, costo y tiempo por provider en la sesión actual."""

    def __init__(self):
        self.session_start = datetime.now().isoformat(timespec="seconds")
        self.providers: Dict[str, Dict[str, Any]] = {}

    def record(self, provider_name: str, response: ProviderResponse, operation: str) -> None:
        """Registra una llamada a un provider (coder, health_check, generate, ...)."""
        stats = self.providers.setdefault(provider_name, {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "elapsed_seconds": 0.0,
            "operations": {},
        })
        stats["calls"] += 1
        stats["input_tokens"] += response.input_tokens
        stats["output_tokens"] += response.output_tokens
        stats["cost_usd"] += response.cost_usd
        stats["elapsed_seconds"] += response.elapsed_seconds
        stats["operations"][operation] = stats["operations"].get(operation, 0) + 1

    @property
    def has_data(self) -> bool:
        """True si se registró al menos una llamada en la sesión."""
        return bool(self.providers)

    def total_cost_usd(self) -> float:
        """Costo total en USD de la sesión actual."""
        return sum(stats["cost_usd"] for stats in self.providers.values())

    def session_summary(self) -> Dict[str, Any]:
        """Datos para la tabla resumen: filas por provider + fila TOTAL."""
        rows: List[Dict[str, Any]] = []
        total = {
            "provider": "TOTAL",
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "elapsed_seconds": 0.0,
        }
        total_operations: Dict[str, int] = {}
        for name, stats in self.providers.items():
            rows.append({
                "provider": name,
                "calls": stats["calls"],
                "input_tokens": stats["input_tokens"],
                "output_tokens": stats["output_tokens"],
                "cost_usd": stats["cost_usd"],
                "elapsed_seconds": stats["elapsed_seconds"],
                "operations": dict(stats["operations"]),
            })
            for op, count in stats["operations"].items():
                total_operations[op] = total_operations.get(op, 0) + count
            total["calls"] += stats["calls"]
            total["input_tokens"] += stats["input_tokens"]
            total["output_tokens"] += stats["output_tokens"]
            total["cost_usd"] += stats["cost_usd"]
            total["elapsed_seconds"] += stats["elapsed_seconds"]
        total["operations"] = total_operations
        return {"rows": rows, "total": total}

    def render_summary(self, console) -> None:
        """Imprime la tabla Rich con el resumen de consumo de la sesión."""
        from rich.table import Table

        if not self.has_data:
            console.print("[yellow]⚠[/yellow] Todavía no hay consumo registrado en esta sesión")
            return

        styles = _provider_styles()
        summary = self.session_summary()

        table = Table(title="💰 Consumo de la sesión", header_style="bold")
        table.add_column("Provider")
        table.add_column("Llamadas", justify="right")
        table.add_column("Operaciones")
        table.add_column("Tokens in", justify="right")
        table.add_column("Tokens out", justify="right")
        table.add_column("Costo USD", justify="right")
        table.add_column("Tiempo", justify="right")

        for row in summary["rows"]:
            style = styles.get(row["provider"], {})
            color = style.get("color", "white")
            display = style.get("display_name", row["provider"])
            table.add_row(
                "[{}]{}[/{}]".format(color, display, color),
                str(row["calls"]),
                _format_operations(row["operations"]),
                "{:,}".format(row["input_tokens"]),
                "{:,}".format(row["output_tokens"]),
                _format_cost(row["cost_usd"], is_subscription=style.get("type") == "cli"),
                "{:.1f}s".format(row["elapsed_seconds"]),
            )

        total = summary["total"]
        table.add_row(
            "[bold]TOTAL[/bold]",
            "[bold]{}[/bold]".format(total["calls"]),
            _format_operations(total["operations"]),
            "[bold]{:,}[/bold]".format(total["input_tokens"]),
            "[bold]{:,}[/bold]".format(total["output_tokens"]),
            "[bold]{}[/bold]".format(_format_cost(total["cost_usd"])),
            "[bold]{:.1f}s[/bold]".format(total["elapsed_seconds"]),
        )
        console.print(table)

    def save(self) -> Optional[str]:
        """Guarda (o actualiza) la sesión actual en costs.json y devuelve la ruta.

        costs.json es una lista de sesiones; la sesión actual se identifica
        por session_start, así llamadas sucesivas a save() no duplican entradas.
        """
        if not self.has_data:
            return None

        sessions = load_history()
        sessions = [s for s in sessions if s.get("session_start") != self.session_start]
        sessions.append({
            "session_start": self.session_start,
            "session_end": datetime.now().isoformat(timespec="seconds"),
            "providers": self.providers,
            "total_cost_usd": self.total_cost_usd(),
        })
        with open(COSTS_PATH, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return COSTS_PATH


def load_history() -> List[Dict[str, Any]]:
    """Lee la lista de sesiones de costs.json; lista vacía si no existe o está corrupto."""
    if not os.path.exists(COSTS_PATH):
        return []
    try:
        with open(COSTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _provider_styles() -> Dict[str, Dict[str, str]]:
    """Mapa name -> {display_name, color, type} desde config.json."""
    from core.config import load_config

    styles: Dict[str, Dict[str, str]] = {}
    for entry in load_config().get("providers", []):
        name = entry.get("name")
        if name:
            styles[name] = {
                "display_name": entry.get("display_name", name),
                "color": entry.get("color", "white"),
                "type": entry.get("type", ""),
            }
    return styles


def _format_operations(operations: Dict[str, int]) -> str:
    """Formatea el desglose por operación, ej. 'generate:3 review:3 merge:3'."""
    if not operations:
        return "[dim]—[/dim]"
    return " ".join("{}:{}".format(op, count) for op, count in sorted(operations.items()))


def _format_cost(cost_usd: float, is_subscription: bool = False) -> str:
    """Formatea un costo; los providers CLI por suscripción se marcan '(sub)'."""
    if is_subscription and cost_usd == 0.0:
        return "$0.00 [dim](sub)[/dim]"
    return "${:.4f}".format(cost_usd)


_tracker: Optional[CostTracker] = None


def get_tracker() -> CostTracker:
    """Devuelve la instancia global compartida del tracker de costos."""
    global _tracker
    if _tracker is None:
        _tracker = CostTracker()
    return _tracker
