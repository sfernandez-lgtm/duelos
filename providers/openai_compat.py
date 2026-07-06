"""Provider genérico para APIs OpenAI-compatible (POST /chat/completions).

Usa urllib de la stdlib: cero dependencias nuevas. Sirve para Gemini
(endpoint openai/), GLM, Kimi y cualquier API compatible.
"""

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from providers.base import AIProvider, ProviderResponse

TIMEOUT_SECONDS = 300
MAX_ERROR_DETAIL = 300  # caracteres del cuerpo de error HTTP que se muestran


class OpenAICompatProvider(AIProvider):
    """Cliente de chat/completions para cualquier API OpenAI-compatible."""

    def __init__(self, name: str, base_url: str, api_key_env: str, model: str,
                 display_name: Optional[str] = None, color: Optional[str] = None,
                 pricing: Optional[Dict[str, float]] = None,
                 max_output_tokens: Optional[int] = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.model = model
        self.display_name = display_name or name
        self.color = color or "white"
        # pricing: {'input_per_1m': float, 'output_per_1m': float} en USD
        self.pricing = pricing or {}
        # max_tokens del request; None = no enviar. Los modelos razonadores
        # (ej. GLM-5.2) devuelven content vacío si el límite es bajo: usar >= 4000.
        self.max_output_tokens = max_output_tokens

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> ProviderResponse:
        """Genera una respuesta vía POST {base_url}/chat/completions."""
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            return self._error_response(prompt, 0.0, "API key {} no seteada (agregala a .env)".format(self.api_key_env))

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        payload: Dict[str, Any] = {"model": self.model, "messages": messages}
        if self.max_output_tokens is not None:
            payload["max_tokens"] = self.max_output_tokens
        body = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer {}".format(api_key),
            },
            method="POST",
        )

        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:MAX_ERROR_DETAIL]
            except OSError:
                pass
            return self._error_response(prompt, time.monotonic() - start,
                                        "HTTP {}: {}".format(exc.code, detail or exc.reason))
        except socket.timeout:
            return self._error_response(prompt, time.monotonic() - start,
                                        "timeout tras {}s".format(TIMEOUT_SECONDS))
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                message = "timeout tras {}s".format(TIMEOUT_SECONDS)
            else:
                message = "error de conexión: {}".format(exc.reason)
            return self._error_response(prompt, time.monotonic() - start, message)
        except OSError as exc:
            return self._error_response(prompt, time.monotonic() - start, "error de red: {}".format(exc))

        elapsed = time.monotonic() - start
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return self._error_response(prompt, elapsed, "respuesta no es JSON: {}".format(raw[:MAX_ERROR_DETAIL]))

        choices = data.get("choices") or []
        if not choices:
            api_error = (data.get("error") or {})
            detail = api_error.get("message") or raw[:MAX_ERROR_DETAIL]
            return self._error_response(prompt, elapsed, "respuesta sin choices: {}".format(detail))

        message = choices[0].get("message") or {}
        # Los modelos razonadores separan el razonamiento en reasoning_content;
        # solo usamos el content final.
        text = (message.get("content") or "").strip()
        if not text:
            detail = "respuesta con contenido vacío"
            if (message.get("reasoning_content") or "").strip():
                detail += " (vino solo reasoning_content: subí max_output_tokens en config.json)"
            return self._error_response(prompt, elapsed, detail)

        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if prompt_tokens is None or completion_tokens is None:
            return ProviderResponse(
                text=text,
                input_tokens=len(prompt) // 4,
                output_tokens=len(text) // 4,
                cost_usd=0.0,
                elapsed_seconds=elapsed,
                estimated=True,
            )
        return ProviderResponse(
            text=text,
            input_tokens=prompt_tokens,
            output_tokens=completion_tokens,
            cost_usd=self._cost(prompt_tokens, completion_tokens),
            elapsed_seconds=elapsed,
        )

    def _cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens / 1_000_000 * self.pricing.get("input_per_1m", 0.0)
            + completion_tokens / 1_000_000 * self.pricing.get("output_per_1m", 0.0)
        )

    def _error_response(self, prompt: str, elapsed: float, error: str) -> ProviderResponse:
        return ProviderResponse(
            text="",
            input_tokens=len(prompt) // 4,
            output_tokens=0,
            cost_usd=0.0,
            elapsed_seconds=elapsed,
            error=error,
            estimated=True,
        )
