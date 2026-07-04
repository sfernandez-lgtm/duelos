"""Carga y guardado de config.json, e instanciación de providers."""

import json
import os
from typing import Any, Dict, List, Tuple

from providers.base import AIProvider
from providers.claude_cli import ClaudeCLIProvider

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

DEFAULT_CONFIG = {
    "providers": [
        {
            "name": "claude",
            "type": "cli",
            "display_name": "Claude Opus",
            "color": "blue",
            "enabled": True,
        },
        {
            "name": "gemini",
            "type": "api",
            "display_name": "Gemini 3 Pro",
            "color": "green",
            "enabled": False,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key_env": "GEMINI_API_KEY",
            "model": "gemini-3-pro",
            "price_input_per_m": 2.0,
            "price_output_per_m": 12.0,
        },
        {
            "name": "glm",
            "type": "api",
            "display_name": "GLM-5.2",
            "color": "red",
            "enabled": False,
            "base_url": "https://api.z.ai/api/paas/v4",
            "api_key_env": "GLM_API_KEY",
            "model": "glm-5.2",
            "price_input_per_m": 0.6,
            "price_output_per_m": 2.2,
        },
        {
            "name": "kimi",
            "type": "api",
            "display_name": "Kimi K2.6",
            "color": "magenta",
            "enabled": False,
            "base_url": "https://api.moonshot.ai/v1",
            "api_key_env": "KIMI_API_KEY",
            "model": "kimi-k2.6",
            "price_input_per_m": 1.0,
            "price_output_per_m": 3.0,
        },
    ]
}


def load_config() -> Dict[str, Any]:
    """Carga config.json; si no existe, lo crea con los defaults."""
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: Dict[str, Any]) -> None:
    """Guarda la configuración en config.json."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_providers(config: Dict[str, Any]) -> Tuple[List[AIProvider], List[str]]:
    """Instancia los providers habilitados.

    Devuelve (providers, avisos). Los de type 'api' todavía no se
    instancian: si están enabled se agrega un aviso.
    """
    providers: List[AIProvider] = []
    warnings: List[str] = []

    for entry in config.get("providers", []):
        if not entry.get("enabled", False):
            continue
        ptype = entry.get("type")
        if ptype == "cli" and entry.get("name") == "claude":
            providers.append(
                ClaudeCLIProvider(
                    display_name=entry.get("display_name"),
                    color=entry.get("color"),
                )
            )
        elif ptype == "api":
            warnings.append(
                "{}: provider API disponible en próximo paso".format(entry.get("display_name", entry.get("name")))
            )
        else:
            warnings.append("{}: tipo de provider desconocido".format(entry.get("name")))

    return providers, warnings
