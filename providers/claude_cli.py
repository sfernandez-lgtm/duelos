"""Provider que invoca el CLI de Claude (siempre modelo Opus)."""

import shutil
import subprocess
import time
from typing import Optional

from providers.base import AIProvider, ProviderResponse

TIMEOUT_SECONDS = 300
SYSTEM_SEPARATOR = "\n\n---\n\n"


class ClaudeCLIProvider(AIProvider):
    """Ejecuta `claude --model opus -p '<prompt>'` via subprocess."""

    name = "claude"
    display_name = "Claude Opus"
    color = "blue"
    enabled = True

    def __init__(self, display_name: Optional[str] = None, color: Optional[str] = None):
        if display_name:
            self.display_name = display_name
        if color:
            self.color = color

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> ProviderResponse:
        """Genera una respuesta ejecutando el CLI de Claude con modelo Opus."""
        full_prompt = prompt
        if system_prompt:
            full_prompt = "[INSTRUCCIONES DE SISTEMA]\n" + system_prompt + SYSTEM_SEPARATOR + prompt

        # shutil.which resuelve también claude.cmd (shim de npm en Windows),
        # que subprocess no encuentra por nombre pelado.
        cli_path = shutil.which("claude")
        if cli_path is None:
            return self._error_response(full_prompt, 0.0, "CLI 'claude' no encontrado en PATH")

        start = time.monotonic()
        try:
            # El prompt va por stdin: evita el límite de longitud de la línea
            # de comandos de Windows y el mangling de argumentos del shim .cmd.
            result = subprocess.run(
                [cli_path, "--model", "opus", "-p"],
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start
            return self._error_response(full_prompt, elapsed, "timeout tras {}s".format(TIMEOUT_SECONDS))
        except (FileNotFoundError, OSError) as exc:
            elapsed = time.monotonic() - start
            return self._error_response(full_prompt, elapsed, "no se pudo ejecutar el CLI: {}".format(exc))

        elapsed = time.monotonic() - start
        text = result.stdout.strip()

        if result.returncode != 0:
            stderr = result.stderr.strip() or "exit code {}".format(result.returncode)
            return self._error_response(full_prompt, elapsed, stderr)

        return ProviderResponse(
            text=text,
            input_tokens=len(full_prompt) // 4,
            output_tokens=len(text) // 4,
            cost_usd=0.0,  # cubierto por suscripción Max
            elapsed_seconds=elapsed,
            estimated=True,  # el CLI no reporta usage
        )

    @staticmethod
    def _error_response(prompt: str, elapsed: float, error: str) -> ProviderResponse:
        return ProviderResponse(
            text="",
            input_tokens=len(prompt) // 4,
            output_tokens=0,
            cost_usd=0.0,
            elapsed_seconds=elapsed,
            error=error,
        )
