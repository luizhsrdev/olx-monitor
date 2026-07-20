from __future__ import annotations

from dataclasses import dataclass

from .config import MonitorConfig
from .models import Anuncio
from .text_utils import contains_any, normalize_text


@dataclass(frozen=True)
class ResultadoFiltro:
    """Resultado da aplicação das regras de um monitor sobre um anúncio.

    Guardamos o motivo de descarte (mesmo quando aceito=False) para dar
    ao log a granularidade que o SPEC pede: quantos foram filtrados e
    por qual regra.
    """

    aceito: bool
    anuncio: Anuncio
    motivo: str | None = None
    prioritario: bool = False


def aplicar_filtros(
    anuncios: list[Anuncio],
    monitor: MonitorConfig,
    bloqueadas_globais: list[str],
) -> list[ResultadoFiltro]:
    """Aplica, nesta ordem: bloqueadas (globais + locais) -> obrigatorias_ou
    -> preco_max. Zero regra hardcoded: tudo vem da config do monitor."""
    bloqueadas = [*bloqueadas_globais, *monitor.bloqueadas]
    resultados: list[ResultadoFiltro] = []

    for anuncio in anuncios:
        titulo_normalizado = normalize_text(anuncio.titulo)

        termo_bloqueado = contains_any(titulo_normalizado, bloqueadas)
        if termo_bloqueado is not None:
            resultados.append(
                ResultadoFiltro(False, anuncio, f"bloqueada:'{termo_bloqueado}'")
            )
            continue

        if monitor.obrigatorias_ou and contains_any(
            titulo_normalizado, monitor.obrigatorias_ou
        ) is None:
            resultados.append(ResultadoFiltro(False, anuncio, "nenhuma obrigatoria_ou presente"))
            continue

        if (
            monitor.preco_max is not None
            and anuncio.preco is not None
            and anuncio.preco > monitor.preco_max
        ):
            resultados.append(
                ResultadoFiltro(
                    False,
                    anuncio,
                    f"preco {anuncio.preco:.2f} > preco_max {monitor.preco_max:.2f}",
                )
            )
            continue

        prioritario = contains_any(titulo_normalizado, monitor.prioritarias) is not None
        resultados.append(ResultadoFiltro(True, anuncio, None, prioritario))

    return resultados
