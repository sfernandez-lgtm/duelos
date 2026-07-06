"""Consola Rich compartida, banner y helpers de salida."""

from typing import Optional

from rich.console import Console
from rich.panel import Panel

console = Console()

BANNER = r"""
██████╗ ██╗   ██╗███████╗██╗      ██████╗
██╔══██╗██║   ██║██╔════╝██║     ██╔═══██╗
██║  ██║██║   ██║█████╗  ██║     ██║   ██║
██║  ██║██║   ██║██╔══╝  ██║     ██║   ██║
██████╔╝╚██████╔╝███████╗███████╗╚██████╔╝
╚═════╝  ╚═════╝ ╚══════╝╚══════╝ ╚═════╝
"""


def print_banner(subtitle: Optional[str] = None) -> None:
    """Muestra el banner ASCII de DUELO, con subtítulo opcional (versión, providers)."""
    body = "[bold cyan]{}[/bold cyan]\n[dim]Orquestador de IAs de código[/dim]".format(BANNER.rstrip())
    if subtitle:
        body += "\n{}".format(subtitle)
    console.print(Panel(body, border_style="cyan", expand=False))


def error_panel(message: str, hint: Optional[str] = None, title: str = "Error") -> None:
    """Panel de error consistente para toda la app, con sugerencia de acción opcional."""
    body = "[red]{}[/red]".format(message)
    if hint:
        body += "\n[dim]💡 {}[/dim]".format(hint)
    console.print(Panel(body, title="[bold red]✖ {}[/bold red]".format(title),
                        border_style="red", expand=False))


def info(message: str) -> None:
    """Mensaje informativo."""
    console.print("[cyan]ℹ[/cyan] {}".format(message))


def success(message: str) -> None:
    """Mensaje de éxito."""
    console.print("[green]✔[/green] {}".format(message))


def warn(message: str) -> None:
    """Mensaje de advertencia."""
    console.print("[yellow]⚠[/yellow] {}".format(message))


def error(message: str) -> None:
    """Mensaje de error."""
    console.print("[red]✖[/red] {}".format(message))
