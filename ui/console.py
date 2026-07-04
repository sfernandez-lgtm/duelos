"""Consola Rich compartida, banner y helpers de salida."""

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


def print_banner() -> None:
    """Muestra el banner ASCII de DUELO."""
    console.print(
        Panel(
            "[bold cyan]{}[/bold cyan]\n[dim]Orquestador de IAs de código[/dim]".format(BANNER.rstrip()),
            border_style="cyan",
            expand=False,
        )
    )


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
