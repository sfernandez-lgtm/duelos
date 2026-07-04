#!/usr/bin/env python3
"""DUELO — orquestador de IAs de código. Entry point y menú principal."""

import os

from rich.prompt import Prompt
from rich.table import Table

from core.config import load_config, load_providers, save_config
from ui.console import console, error, info, print_banner, success, warn


def show_models(config) -> None:
    """Lista los providers configurados y permite togglear enabled."""
    while True:
        table = Table(title="🤖 Modelos configurados", header_style="bold")
        table.add_column("#", justify="right")
        table.add_column("Nombre")
        table.add_column("Tipo")
        table.add_column("Estado")
        table.add_column("API key")

        entries = config.get("providers", [])
        for i, entry in enumerate(entries, start=1):
            color = entry.get("color", "white")
            estado = "[green]enabled[/green]" if entry.get("enabled") else "[dim]disabled[/dim]"
            if entry.get("type") == "api":
                key_env = entry.get("api_key_env", "")
                key = "[green]✔ {}[/green]".format(key_env) if os.environ.get(key_env) else "[red]✖ {}[/red]".format(key_env)
            else:
                key = "[dim]— (CLI)[/dim]"
            table.add_row(
                str(i),
                "[{}]{}[/{}]".format(color, entry.get("display_name", entry.get("name")), color),
                entry.get("type", "?"),
                estado,
                key,
            )

        console.print(table)
        choice = Prompt.ask(
            "Número para togglear enabled/disabled ([b]v[/b] para volver)",
            default="v",
        ).strip().lower()
        if choice == "v":
            return
        if choice.isdigit() and 1 <= int(choice) <= len(entries):
            entry = entries[int(choice) - 1]
            entry["enabled"] = not entry.get("enabled", False)
            save_config(config)
            estado = "habilitado" if entry["enabled"] else "deshabilitado"
            success("{} {}".format(entry.get("display_name", entry.get("name")), estado))
        else:
            warn("Opción inválida")


def run_health_checks(config) -> None:
    """Ejecuta health_check de cada provider habilitado y muestra los resultados."""
    providers, warnings = load_providers(config)
    for message in warnings:
        warn(message)
    if not providers:
        warn("No hay providers habilitados para testear")
        return

    table = Table(title="🩺 Test de conectividad", header_style="bold")
    table.add_column("Provider")
    table.add_column("Respuesta")
    table.add_column("Tiempo", justify="right")
    table.add_column("Resultado")

    for provider in providers:
        info("Testeando {}...".format(provider.display_name))
        response = provider.health_check()
        if response.ok:
            result = "[green]✔ OK[/green]"
            text = response.text or "[dim](vacío)[/dim]"
        else:
            result = "[red]✖ FALLO[/red]"
            text = "[red]{}[/red]".format(response.error)
        table.add_row(
            "[{}]{}[/{}]".format(provider.color, provider.display_name, provider.color),
            text,
            "{:.1f}s".format(response.elapsed_seconds),
            result,
        )

    console.print(table)


def main() -> None:
    """Loop del menú principal."""
    print_banner()
    config = load_config()

    while True:
        console.print()
        console.print("[bold]1[/bold] 🤖 Modelos")
        console.print("[bold]2[/bold] 🩺 Test de conectividad")
        console.print("[bold]3[/bold] 🚪 Salir")
        choice = Prompt.ask("Opción", choices=["1", "2", "3"], default="3")

        if choice == "1":
            show_models(config)
        elif choice == "2":
            run_health_checks(config)
        else:
            info("¡Hasta el próximo duelo!")
            return


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        console.print()
        error("Interrumpido")
