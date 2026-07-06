"""Pipeline N-provider: generación, cross-review todos-contra-todos y merge.

Con un solo provider habilitado el cross-review degenera en self-review y el
merge en una pasada de refinamiento; la arquitectura queda lista para que en
Etapa 2 alcance con habilitar otro provider en config.json.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from core.config import save_config
from core.costs import get_tracker
from providers.base import AIProvider, ProviderResponse
from ui.console import console, error, info, warn

MAX_STAGE_CHARS = 15000  # truncado de generaciones/reviews en prompts de etapas siguientes
DEFAULT_MAX_PARALLEL = 6  # max_workers del pool; configurable con 'max_parallel' en config.json

REVIEW_SYSTEM_PROMPT = (
    "Sos un revisor de código exigente y constructivo. Respondés con una crítica "
    "concreta y accionable (bugs, edge cases, mejoras), sin reescribir la solución completa."
)

MERGE_SYSTEM_PROMPT = (
    "Sos un desarrollador senior que integra propuestas y críticas en una versión final. "
    "Respondés únicamente con la solución final, sin explicaciones ni meta-comentarios."
)


@dataclass
class GenerationResult:
    """Salida de un provider en la etapa de generación."""

    provider_name: str
    display_name: str
    text: str


def _clip(text: str) -> str:
    if len(text) <= MAX_STAGE_CHARS:
        return text
    return text[:MAX_STAGE_CHARS] + "\n... [truncado]"


def _status(provider: AIProvider, label: str, verb: str):
    return console.status("[{}]{}{} {}...[/{}]".format(
        provider.color, label, provider.display_name, verb, provider.color
    ))


def _run_stage(jobs: List[Tuple[Any, str, Callable[[], ProviderResponse]]],
               max_workers: int) -> Dict[Any, Any]:
    """Corre en paralelo una lista de trabajos [(clave, descripción, callable)].

    Muestra una fila de progreso viva por trabajo y devuelve {clave: ProviderResponse
    o Exception}. Ctrl+C cancela los futures pendientes y se propaga (los que ya
    están en vuelo terminan en background y quedan registrados en el tracker).
    """
    results: Dict[Any, Any] = {}
    progress = Progress(
        SpinnerColumn(finished_text="•"),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )
    with progress:
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {}
            rows = {}
            for key, description, call in jobs:
                rows[key] = progress.add_task(description, total=1)
                futures[executor.submit(call)] = (key, description)
            for future in as_completed(futures):
                key, description = futures[future]
                try:
                    result = future.result()
                    mark = "[green]✔[/green]" if result.ok else "[red]✖[/red]"
                except Exception as exc:  # KeyboardInterrupt pasa de largo y cancela
                    result, mark = exc, "[red]✖[/red]"
                results[key] = result
                progress.update(rows[key], description="{} {}".format(description, mark), completed=1)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
    return results


def generate_all(providers: List[AIProvider], task_context: str,
                 system_prompt: Optional[str] = None, label: str = "",
                 max_workers: int = DEFAULT_MAX_PARALLEL) -> Dict[str, GenerationResult]:
    """Etapa 1: las N generaciones en paralelo (I/O bound, threads).

    Los providers que fallan se excluyen con un aviso; dict vacío si fallaron todos.
    """
    tracker = get_tracker()
    start = time.monotonic()

    def make_call(provider: AIProvider) -> Callable[[], ProviderResponse]:
        def call() -> ProviderResponse:
            response = provider.generate(task_context, system_prompt)
            tracker.record(provider.name, response, "generate")  # en el worker: queda registrado aun con Ctrl+C
            return response
        return call

    jobs = [
        (p.name, "[{0}]{1}{2}[/{0}] generando".format(p.color, label, p.display_name), make_call(p))
        for p in providers
    ]
    results = _run_stage(jobs, max_workers)

    generations: Dict[str, GenerationResult] = {}
    sum_elapsed = 0.0
    for provider in providers:
        result = results.get(provider.name)
        if isinstance(result, ProviderResponse):
            sum_elapsed += result.elapsed_seconds
            if result.ok and result.text.strip():
                generations[provider.name] = GenerationResult(
                    provider_name=provider.name,
                    display_name=provider.display_name,
                    text=result.text.strip(),
                )
            else:
                warn("{}: generación falló ({})".format(
                    provider.display_name, result.error or "respuesta vacía"
                ))
        elif result is not None:
            warn("{}: generación falló (excepción: {})".format(provider.display_name, result))

    info("{}generación: {:.1f}s en paralelo (suma de llamadas: {:.1f}s)".format(
        label, time.monotonic() - start, sum_elapsed
    ))
    return generations


def _review_prompt(task_context: str, generation: GenerationResult, is_self: bool) -> str:
    autoria = (
        "La solución es TUYA: hacé un self-review honesto y despiadado."
        if is_self else
        "La solución fue escrita por otro modelo ({}).".format(generation.display_name)
    )
    return (
        "TAREA ORIGINAL:\n{}\n\n"
        "SOLUCIÓN PROPUESTA:\n{}\n\n"
        "{}\n"
        "Revisá la solución: bugs, edge cases no cubiertos, problemas de seguridad "
        "o rendimiento, y mejoras concretas. Respondé con una lista breve y accionable; "
        "no reescribas la solución completa.".format(
            _clip(task_context), _clip(generation.text), autoria
        )
    )


def review_all(providers: List[AIProvider], task_context: str,
               generations: Dict[str, GenerationResult],
               label: str = "",
               max_workers: int = DEFAULT_MAX_PARALLEL) -> Dict[str, Dict[str, str]]:
    """Etapa 2: cross-review todos-contra-todos, las N*(N-1) reviews en paralelo.

    Cada provider revisa las generaciones de todos los otros. Con una sola
    generación disponible, el autor se revisa a sí mismo (self-review).
    Devuelve {reviewer_name: {author_name: review}}.
    """
    tracker = get_tracker()
    start = time.monotonic()

    pairs = []
    for reviewer in providers:
        for author_name, generation in generations.items():
            is_self = reviewer.name == author_name
            if is_self and len(generations) > 1:
                continue  # con N>1 solo se revisan entre sí
            pairs.append((reviewer, author_name, generation, is_self))
    if not pairs:
        return {}

    def make_call(reviewer: AIProvider, generation: GenerationResult,
                  is_self: bool) -> Callable[[], ProviderResponse]:
        def call() -> ProviderResponse:
            response = reviewer.generate(
                _review_prompt(task_context, generation, is_self),
                REVIEW_SYSTEM_PROMPT,
            )
            tracker.record(reviewer.name, response, "review")
            return response
        return call

    jobs = []
    for reviewer, author_name, generation, is_self in pairs:
        description = "[{0}]{1}{2}[/{0}] review de {3}".format(
            reviewer.color, label, reviewer.display_name, generation.display_name
        )
        jobs.append(((reviewer.name, author_name), description, make_call(reviewer, generation, is_self)))

    results = _run_stage(jobs, max_workers)

    reviews: Dict[str, Dict[str, str]] = {}
    sum_elapsed = 0.0
    for reviewer, author_name, generation, _ in pairs:
        result = results.get((reviewer.name, author_name))
        if isinstance(result, ProviderResponse):
            sum_elapsed += result.elapsed_seconds
            if result.ok and result.text.strip():
                reviews.setdefault(reviewer.name, {})[author_name] = result.text.strip()
            else:
                warn("{}: review de {} falló ({})".format(
                    reviewer.display_name, generation.display_name,
                    result.error or "respuesta vacía"
                ))
        elif result is not None:
            warn("{}: review de {} falló (excepción: {})".format(
                reviewer.display_name, generation.display_name, result
            ))

    info("{}reviews: {:.1f}s en paralelo (suma de llamadas: {:.1f}s)".format(
        label, time.monotonic() - start, sum_elapsed
    ))
    return reviews


def _merge_prompt(task_context: str, generations: Dict[str, GenerationResult],
                  reviews: Dict[str, Dict[str, str]]) -> str:
    parts: List[str] = ["TAREA ORIGINAL:\n{}".format(_clip(task_context))]

    chunks = []
    for generation in generations.values():
        chunks.append("--- solución de {} ---\n{}".format(
            generation.display_name, _clip(generation.text)
        ))
    parts.append("SOLUCIONES PROPUESTAS:\n" + "\n\n".join(chunks))

    review_chunks = []
    for reviewer_name, per_author in reviews.items():
        for author_name, review in per_author.items():
            review_chunks.append("--- review de {} sobre la solución de {} ---\n{}".format(
                reviewer_name, author_name, _clip(review)
            ))
    if review_chunks:
        parts.append("REVIEWS:\n" + "\n\n".join(review_chunks))

    parts.append(
        "Producí la VERSIÓN FINAL de la solución: integrá lo mejor de las propuestas "
        "y aplicá las críticas válidas de las reviews. Respondé ÚNICAMENTE con la "
        "solución final completa, sin explicaciones, sin meta-comentarios y sin "
        "mencionar las reviews."
    )
    return "\n\n".join(parts)


def merge_final(merger: AIProvider, task_context: str,
                generations: Dict[str, GenerationResult],
                reviews: Dict[str, Dict[str, str]],
                system_prompt: Optional[str] = None,
                label: str = "") -> Optional[str]:
    """Etapa 3: el merger integra generaciones + reviews en la versión final.

    Con N=1 es una pasada de refinamiento del provider sobre su propio código.
    Devuelve el texto final, o None si el merger falló.
    """
    tracker = get_tracker()
    with _status(merger, label, "merge"):
        response = merger.generate(
            _merge_prompt(task_context, generations, reviews),
            system_prompt or MERGE_SYSTEM_PROMPT,
        )
    tracker.record(merger.name, response, "merge")
    if response.ok and response.text.strip():
        return response.text.strip()
    warn("{}: merge falló ({})".format(merger.display_name, response.error or "respuesta vacía"))
    return None


def pick_merger(providers: List[AIProvider], config: Dict[str, Any]) -> AIProvider:
    """Round-robin del merger entre los providers habilitados, persistido en config.json."""
    names = [p.name for p in providers]
    last = config.get("last_merger")
    index = (names.index(last) + 1) % len(names) if last in names else 0
    merger = providers[index]
    config["last_merger"] = merger.name
    save_config(config)
    return merger


def run_pipeline(providers: List[AIProvider], config: Dict[str, Any],
                 task_context: str, system_prompt: Optional[str] = None,
                 label: str = "") -> Optional[str]:
    """Pipeline completo: generate_all -> review_all -> merge_final.

    Tolerante a fallos por etapa; aborta solo si TODAS las generaciones fallaron.
    Devuelve el texto final (sin pasar por el cleaner: eso es del caller).
    """
    try:
        max_workers = max(1, int(config.get("max_parallel", DEFAULT_MAX_PARALLEL)))
    except (TypeError, ValueError):
        max_workers = DEFAULT_MAX_PARALLEL

    generations = generate_all(providers, task_context, system_prompt, label, max_workers)
    if not generations:
        error("{}todas las generaciones fallaron; pipeline abortado".format(label))
        return None

    reviews = review_all(providers, task_context, generations, label, max_workers)
    if not reviews:
        warn("{}sin reviews disponibles; el merge sigue solo con las generaciones".format(label))

    merger = pick_merger(providers, config)
    final = merge_final(merger, task_context, generations, reviews, system_prompt, label)
    if final is not None:
        return final

    fallback = next(iter(generations.values()))
    warn("{}merge falló; se usa la generación de {} sin refinar".format(label, fallback.display_name))
    return fallback.text
