from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Anuncio:
    """Formato normalizado e único de anúncio, comum a todas as fontes.

    Filtro, dedupe e alerta só conhecem este tipo — nunca o formato cru
    de uma fonte específica.
    """

    id: str
    titulo: str
    preco: float | None
    url: str
    local: str | None
    fonte: str
    publicado_em: str | None
    coletado_em: datetime
