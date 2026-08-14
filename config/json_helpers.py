"""Gemeinsame Hilfsfunktionen für persistierte JSON-Listen."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar


T = TypeVar("T")


def load_json_list(
    path: Path,
    item_from_dict: Callable[[dict[str, Any]], T],
    logger: logging.Logger,
    *,
    list_key: str | None = None,
    extra_exceptions: tuple[type[BaseException], ...] = (),
) -> list[T]:
    """Lädt eine JSON-Liste und wandelt deren Elemente in Modelle um.

    Bei fehlender, beschädigter oder strukturell inkompatibler Datei wird
    eine leere Liste zurückgegeben. `list_key` erlaubt zusätzlich ein
    rückwärtskompatibles Wrapper-Format wie ``{"sensors": [...]}``.
    """
    if not path.exists():
        logger.info("Keine Datei gefunden: %s", path)
        return []

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if list_key is not None and isinstance(data, dict):
            data = data.get(list_key, [])
        if not isinstance(data, list):
            raise ValueError("JSON-Datei muss eine Liste enthalten")
        return [item_from_dict(item) for item in data]
    except (json.JSONDecodeError, OSError, KeyError, ValueError, AttributeError, TypeError, *extra_exceptions) as exc:
        logger.warning("JSON-Datei konnte nicht gelesen werden (%s), ignoriere Datei: %s", path, exc)
        return []
