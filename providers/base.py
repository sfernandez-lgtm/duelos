"""Clase base para providers de IA y su respuesta estandarizada."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ProviderResponse:
    """Respuesta normalizada de cualquier provider."""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    elapsed_seconds: float
    error: Optional[str] = None
    estimated: bool = False  # True si los tokens son estimados (len//4), no usage real

    @property
    def ok(self) -> bool:
        """True si la generación terminó sin error."""
        return self.error is None


class AIProvider:
    """Interfaz común que implementa cada provider de IA."""

    name: str = ""          # identificador corto, ej. 'claude'
    display_name: str = ""  # nombre para mostrar, ej. 'Claude Opus'
    color: str = "white"    # color Rich para la UI
    enabled: bool = True

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> ProviderResponse:
        """Genera una respuesta para el prompt dado."""
        raise NotImplementedError

    def health_check(self) -> ProviderResponse:
        """Llamada trivial para verificar conectividad con el provider."""
        return self.generate("Respondé únicamente: OK")
