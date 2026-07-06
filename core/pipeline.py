"""Pipeline N-provider: generación, cross-review todos-contra-todos y merge.

Con un solo provider habilitado el cross-review degenera en self-review y el
merge en una pasada de refinamiento; la arquitectura queda lista para que en
Etapa 2 alcance con habilitar otro provider en config.json.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.config import save_config
from core.costs import get_tracker
from providers.base import AIProvider
from ui.console import console, error, info, warn

MAX_STAGE_CHARS = 15000  # truncado de generaciones/reviews en prompts de etapas siguientes

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


def generate_all(providers: List[AIProvider], task_context: str,
                 system_prompt: Optional[str] = None,
                 label: str = "") -> Dict[str, GenerationResult]:
    """Etapa 1: una generación por provider (secuencial; firma lista para paralelizar).

    Los providers que fallan se excluyen con un aviso; dict vacío si fallaron todos.
    """
    tracker = get_tracker()
    generations: Dict[str, GenerationResult] = {}
    for provider in providers:
        with _status(provider, label, "generando"):
            response = provider.generate(task_context, system_prompt)
        tracker.record(provider.name, response, "generate")
        if response.ok and response.text.strip():
            generations[provider.name] = GenerationResult(
                provider_name=provider.name,
                display_name=provider.display_name,
                text=response.text.strip(),
            )
        else:
            warn("{}: generación falló ({})".format(
                provider.display_name, response.error or "respuesta vacía"
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
               label: str = "") -> Dict[str, Dict[str, str]]:
    """Etapa 2: cross-review todos-contra-todos.

    Cada provider revisa las generaciones de todos los otros. Con una sola
    generación disponible, el autor se revisa a sí mismo (self-review).
    Devuelve {reviewer_name: {author_name: review}}.
    """
    tracker = get_tracker()
    reviews: Dict[str, Dict[str, str]] = {}
    for reviewer in providers:
        for author_name, generation in generations.items():
            is_self = reviewer.name == author_name
            if is_self and len(generations) > 1:
                continue  # con N>1 solo se revisan entre sí
            with _status(reviewer, label, "review de {}".format(generation.display_name)):
                response = reviewer.generate(
                    _review_prompt(task_context, generation, is_self),
                    REVIEW_SYSTEM_PROMPT,
                )
            tracker.record(reviewer.name, response, "review")
            if response.ok and response.text.strip():
                reviews.setdefault(reviewer.name, {})[author_name] = response.text.strip()
            else:
                warn("{}: review de {} falló ({})".format(
                    reviewer.display_name, generation.display_name,
                    response.error or "respuesta vacía"
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
    generations = generate_all(providers, task_context, system_prompt, label)
    if not generations:
        error("{}todas las generaciones fallaron; pipeline abortado".format(label))
        return None

    reviews = review_all(providers, task_context, generations, label)
    if not reviews:
        warn("{}sin reviews disponibles; el merge sigue solo con las generaciones".format(label))

    merger = pick_merger(providers, config)
    final = merge_final(merger, task_context, generations, reviews, system_prompt, label)
    if final is not None:
        return final

    fallback = next(iter(generations.values()))
    warn("{}merge falló; se usa la generación de {} sin refinar".format(label, fallback.display_name))
    return fallback.text
