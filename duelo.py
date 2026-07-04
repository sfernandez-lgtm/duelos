#!/usr/bin/env python3
"""DUELO — orquestador de IAs de código. Entry point y menú principal."""

import os

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from core.cleaner import clean_code, clean_filename, last_code_block
from core.config import load_config, load_providers, save_config
from core.session import Session
from ui.console import console, error, info, print_banner, success, warn

CODER_SYSTEM_PROMPT = (
    "Sos un desarrollador senior en sesión de pair programming. "
    "Respondé de forma directa y concisa. "
    "Cuando entregues código, entregalo completo y funcional."
)

SNIPPETS_DIR = os.path.join(os.path.expanduser("~"), "ai-projects", "snippets")


def pick_provider(providers):
    """Elige un provider: directo si hay uno solo, con prompt si hay varios."""
    if len(providers) == 1:
        return providers[0]
    console.print("[bold]Providers disponibles:[/bold]")
    for i, p in enumerate(providers, start=1):
        console.print("  [bold]{}[/bold] [{}]{}[/{}]".format(i, p.color, p.display_name, p.color))
    choice = Prompt.ask("Provider", choices=[str(i) for i in range(1, len(providers) + 1)], default="1")
    return providers[int(choice) - 1]


def save_snippet(session: Session, raw_filename: str) -> None:
    """Guarda el último bloque de código de la última respuesta en snippets/."""
    last_response = next((t["content"] for t in reversed(session.turns) if t["role"] == "assistant"), None)
    if last_response is None:
        warn("Todavía no hay ninguna respuesta de la que guardar código")
        return
    block = last_code_block(last_response)
    code = clean_code(block if block is not None else last_response)
    if not code:
        warn("La última respuesta no tiene código para guardar")
        return
    filename = clean_filename(raw_filename)
    os.makedirs(SNIPPETS_DIR, exist_ok=True)
    path = os.path.join(SNIPPETS_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
        if not code.endswith("\n"):
            f.write("\n")
    success("Código guardado en {}".format(path))


def coder_mode(config) -> None:
    """Sesión de chat de pair programming con un provider."""
    providers, warnings = load_providers(config)
    for message in warnings:
        warn(message)
    if not providers:
        warn("No hay providers habilitados; activá alguno en 🤖 Modelos")
        return

    provider = pick_provider(providers)
    session = Session(provider.name)
    info("Modo Coder con [{}]{}[/{}] — comandos: /salir, /limpiar, /guardar <archivo>".format(
        provider.color, provider.display_name, provider.color
    ))

    while True:
        console.print()
        user_input = Prompt.ask("[bold green]vos[/bold green]").strip()
        if not user_input:
            continue

        if user_input == "/salir":
            if session.turns:
                path = session.save_log()
                success("Sesión guardada en {}".format(path))
            return
        if user_input == "/limpiar":
            session.clear()
            success("Historial reseteado")
            continue
        if user_input.startswith("/guardar"):
            raw_filename = user_input[len("/guardar"):].strip()
            if not raw_filename:
                warn("Uso: /guardar <filename>")
            else:
                save_snippet(session, raw_filename)
            continue

        prompt = session.build_prompt(user_input)
        with console.status("[{}]{} pensando...[/{}]".format(provider.color, provider.display_name, provider.color)):
            response = provider.generate(prompt, CODER_SYSTEM_PROMPT)

        if not response.ok:
            error("{}: {}".format(provider.display_name, response.error))
            continue

        session.add_turn("user", user_input)
        session.add_turn("assistant", response.text)
        console.print(Panel(
            response.text,
            title="[{}]{}[/{}]".format(provider.color, provider.display_name, provider.color),
            subtitle="{:.1f}s".format(response.elapsed_seconds),
            border_style=provider.color,
        ))


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
        console.print("[bold]1[/bold] 💻 Coder")
        console.print("[bold]2[/bold] 🤖 Modelos")
        console.print("[bold]3[/bold] 🩺 Test de conectividad")
        console.print("[bold]4[/bold] 🚪 Salir")
        choice = Prompt.ask("Opción", choices=["1", "2", "3", "4"], default="4")

        if choice == "1":
            coder_mode(config)
        elif choice == "2":
            show_models(config)
        elif choice == "3":
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
