#!/usr/bin/env python3
"""DUELO — orquestador de IAs de código. Entry point y menú principal."""

import json
import os

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import shutil

from core.cleaner import clean_code, clean_filename, last_code_block
from core.config import CONFIG_PATH, load_config, load_providers, save_config
from core.costs import get_tracker, load_history
from core.env import ENV_PATH, load_env
from core.pipeline import run_pipeline
from core.project import run_project
from core.session import Session
from core.version import VERSION
from ui.console import console, error, error_panel, info, print_banner, success, warn

CODER_SYSTEM_PROMPT = (
    "Sos un desarrollador senior en sesión de pair programming. "
    "Respondé de forma directa y concisa. "
    "Cuando entregues código, entregalo completo y funcional."
)

CODER_COMMANDS = [
    ("/ayuda", "muestra esta lista de comandos"),
    ("/pro <consulta>", "corre la consulta por el pipeline completo (generate + review + merge)"),
    ("/guardar <archivo>", "guarda el último bloque de código en ~/ai-projects/snippets/"),
    ("/costos", "resumen parcial de consumo de la sesión"),
    ("/limpiar", "resetea el historial de la conversación"),
    ("/salir", "cierra la sesión Coder y vuelve al menú"),
]

SNIPPETS_DIR = os.path.join(os.path.expanduser("~"), "ai-projects", "snippets")


def provider_hint(error_message: str) -> str:
    """Sugerencia de acción según el tipo de error del provider."""
    message = (error_message or "").lower()
    if "path" in message or "no encontrado" in message:
        return "Instalá el CLI (npm install -g @anthropic-ai/claude-code) o verificá el PATH"
    if "timeout" in message:
        return "Reintentá; si persiste, achicá la consulta"
    return "Reintentá; si persiste, corré el 🩺 Test de conectividad desde el menú"


def ensure_env() -> None:
    """Carga .env; si no existe lo crea desde .env.example y avisa completarlo."""
    if not os.path.exists(ENV_PATH):
        example = ENV_PATH + ".example"
        if os.path.exists(example):
            shutil.copyfile(example, ENV_PATH)
            warn("Se creó .env desde .env.example — completá tus API keys ahí")
        else:
            warn(".env no existe; los providers API necesitan sus keys en el entorno")
    load_env()


def load_config_ui():
    """Carga config.json con manejo de faltante (regenera) y corrupto (confirma y respalda)."""
    if not os.path.exists(CONFIG_PATH):
        warn("config.json no encontrado; se regenera con los defaults")
        return load_config()
    try:
        return load_config()
    except (json.JSONDecodeError, ValueError) as exc:
        error_panel(
            "config.json está corrupto: {}".format(exc),
            hint="Se puede regenerar con los defaults (tu versión rota queda en config.json.bak)",
            title="Configuración",
        )
        if Prompt.ask("¿Regenerar config.json con los defaults?", choices=["s", "n"], default="s") != "s":
            error("No se puede continuar sin configuración válida")
            raise SystemExit(1)
        os.replace(CONFIG_PATH, CONFIG_PATH + ".bak")
        return load_config()


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
    tracker = get_tracker()
    info("Modo Coder con [{}]{}[/{}] — /ayuda para ver los comandos".format(
        provider.color, provider.display_name, provider.color
    ))

    while True:
        console.print()
        user_input = Prompt.ask("[bold green]vos[/bold green]").strip()
        if not user_input:
            continue

        if user_input == "/ayuda":
            for command, description in CODER_COMMANDS:
                console.print("  [bold]{:<20}[/bold] [dim]{}[/dim]".format(command, description))
            continue
        if user_input == "/salir":
            if session.turns:
                path = session.save_log()
                success("Sesión guardada en {}".format(path))
            if tracker.has_data:
                tracker.render_summary(console)
                tracker.save()
            return
        if user_input == "/limpiar":
            session.clear()
            success("Historial reseteado")
            continue
        if user_input == "/costos":
            tracker.render_summary(console)
            continue
        if user_input.startswith("/guardar"):
            raw_filename = user_input[len("/guardar"):].strip()
            if not raw_filename:
                warn("Uso: /guardar <filename>")
            else:
                save_snippet(session, raw_filename)
            continue
        if user_input.startswith("/pro"):
            query = user_input[len("/pro"):].strip()
            if not query:
                warn("Uso: /pro <consulta>")
                continue
            final = run_pipeline(providers, config, session.build_prompt(query), CODER_SYSTEM_PROMPT)
            if final is None:
                continue
            session.add_turn("user", query)
            session.add_turn("assistant", final)
            console.print(Panel(
                final,
                title="[bold]⚔ PRO[/bold] · generate + review + merge",
                border_style="cyan",
            ))
            continue

        prompt = session.build_prompt(user_input)
        with console.status("[{}]{} pensando...[/{}]".format(provider.color, provider.display_name, provider.color)):
            response = provider.generate(prompt, CODER_SYSTEM_PROMPT)
        tracker.record(provider.name, response, "coder")

        if not response.ok:
            error_panel(
                "{}: {}".format(provider.display_name, response.error),
                hint=provider_hint(response.error),
                title="Provider",
            )
            continue

        session.add_turn("user", user_input)
        session.add_turn("assistant", response.text)
        console.print(Panel(
            response.text,
            title="[{}]{}[/{}]".format(provider.color, provider.display_name, provider.color),
            subtitle="{:.1f}s".format(response.elapsed_seconds),
            border_style=provider.color,
        ))


def project_mode(config) -> None:
    """Pide la descripción del proyecto y corre el pipeline de core/project.py."""
    providers, warnings = load_providers(config)
    for message in warnings:
        warn(message)
    if not providers:
        warn("No hay providers habilitados; activá alguno en 🤖 Modelos")
        return

    provider = pick_provider(providers)
    info("Describí el proyecto a generar (terminá con una línea vacía o /fin)")
    lines = []
    while True:
        line = console.input("[bold green]> [/bold green]")
        if not line.strip() or line.strip() == "/fin":
            break
        lines.append(line)

    description = "\n".join(lines).strip()
    if not description:
        warn("Descripción vacía; volviendo al menú")
        return
    run_project(provider, providers, config, description)


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
            if entry.get("type") == "cli":
                key = "[dim]— (CLI)[/dim]"
            else:
                key_env = entry.get("api_key_env", "")
                key = "[green]✔ {}[/green]".format(key_env) if os.environ.get(key_env) else "[red]✖ {}[/red]".format(key_env)
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

    tracker = get_tracker()
    for provider in providers:
        info("Testeando {}...".format(provider.display_name))
        response = provider.health_check()
        tracker.record(provider.name, response, "health_check")
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


def show_costs() -> None:
    """Resumen de costos de la sesión actual + histórico de costs.json."""
    tracker = get_tracker()
    tracker.render_summary(console)

    history = load_history()
    if not history:
        info("Sin histórico de sesiones todavía (costs.json vacío)")
        return

    table = Table(title="📜 Histórico de sesiones (últimas 10)", header_style="bold")
    table.add_column("Fecha")
    table.add_column("Llamadas", justify="right")
    table.add_column("Costo total USD", justify="right")

    for entry in history[-10:]:
        calls = sum(p.get("calls", 0) for p in entry.get("providers", {}).values())
        table.add_row(
            entry.get("session_start", "?").replace("T", " "),
            str(calls),
            "${:.4f}".format(entry.get("total_cost_usd", 0.0)),
        )
    console.print(table)


def _active_provider_labels(config) -> str:
    """Nombres (coloreados) de los providers habilitados, para banner y menú."""
    labels = [
        "[{}]{}[/{}]".format(e.get("color", "white"), e.get("display_name", e.get("name")), e.get("color", "white"))
        for e in config.get("providers", []) if e.get("enabled")
    ]
    return " + ".join(labels) if labels else "[red]ninguno[/red]"


def _save_tracker_if_needed() -> None:
    tracker = get_tracker()
    if tracker.has_data:
        tracker.save()


def main() -> None:
    """Loop del menú principal."""
    ensure_env()
    config = load_config_ui()
    print_banner("[bold]v{}[/bold] · {}".format(VERSION, _active_provider_labels(config)))

    while True:
        console.print()
        status_line = "[dim]provider:[/dim] {}".format(_active_provider_labels(config))
        last_mode = config.get("last_project_mode")
        if last_mode:
            status_line += " [dim]· último proyecto: modo {}[/dim]".format(last_mode)
        console.print(status_line)
        console.print("[bold]1[/bold] 💻 Coder")
        console.print("[bold]2[/bold] 📦 Proyecto")
        console.print("[bold]3[/bold] 💰 Costos")
        console.print("[bold]4[/bold] 🤖 Modelos")
        console.print("[bold]5[/bold] 🩺 Test de conectividad")
        console.print("[bold]6[/bold] 🚪 Salir")
        choice = Prompt.ask("Opción", choices=["1", "2", "3", "4", "5", "6"], default="6")

        try:
            if choice == "1":
                coder_mode(config)
            elif choice == "2":
                project_mode(config)
            elif choice == "3":
                show_costs()
            elif choice == "4":
                show_models(config)
            elif choice == "5":
                run_health_checks(config)
            else:
                _save_tracker_if_needed()
                info("¡Hasta el próximo duelo!")
                return
        except KeyboardInterrupt:
            console.print()
            warn("Operación cancelada (Ctrl+C); volviendo al menú")
            _save_tracker_if_needed()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        console.print()
        _save_tracker_if_needed()
        warn("Sesión interrumpida; consumo guardado")
