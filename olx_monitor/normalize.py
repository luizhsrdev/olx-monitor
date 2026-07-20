from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Callable

from .models import Anuncio

logger = logging.getLogger(__name__)

_PRECO_PATTERN = re.compile(r"[\d.,]+")

# Nomes de campo confirmados em 2026-07 contra uma amostra real da OLX
# (formato RSC/App Router — ver sources/olx.py). "pricevalue"/"price" e
# "listid" vêm de "priceValue"/"price" e "listId" no JSON original (a
# comparação é case-insensitive). Mantidos como tuplas de candidatos, e
# não um nome único, porque isso já mudou uma vez e pode mudar de novo —
# se a extração vier vazia, inspecione debug_page.html (ver README) e
# adicione o nome real aqui.
_CHAVES_ID = ("id", "listid", "ad_id", "adid")
_CHAVES_TITULO = ("subject", "title", "titulo")
_CHAVES_PRECO = ("pricevalue", "price", "preco")
_CHAVES_URL = ("url", "link", "friendlyurl")
_CHAVES_LOCAL = ("location", "local", "locationstring", "locationdetails")
_CHAVES_LOCAL_DETALHE = ("neighbourhood", "bairro", "municipality", "city", "cidade", "name", "state", "uf")
_CHAVES_DATA = ("date", "listtime", "publishedat", "created_at", "createdat")


def _pega_por_chave(item: dict, candidatos: tuple[str, ...]):
    mapa = {k.lower(): v for k, v in item.items()}
    for candidato in candidatos:
        if candidato in mapa and mapa[candidato] not in (None, ""):
            return mapa[candidato]
    return None


def _parse_preco(valor: object) -> float | None:
    if valor is None:
        return None
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, dict):
        for chave in ("value", "valor", "amount", "price"):
            if chave in valor:
                return _parse_preco(valor[chave])
        return None
    if isinstance(valor, str):
        match = _PRECO_PATTERN.search(valor)
        if not match:
            return None
        # formato brasileiro sempre: "." é separador de milhar, "," é
        # decimal. "R$ 2.000" -> "2000" (não "2.0"!) e "R$ 2.500,00" ->
        # "2500.00". Sem essa remoção incondicional do ponto, preços
        # sem centavos (o caso comum na OLX) ficavam ~1000x menores.
        numero = numero_bruto = match.group(0)
        numero = numero.replace(".", "").replace(",", ".")
        try:
            return float(numero)
        except ValueError:
            logger.debug("olx: preço '%s' não pôde ser convertido", numero_bruto)
            return None
    return None


def _parse_local(valor: object) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, str):
        texto = re.sub(r"\s+", " ", valor).strip()
        return texto or None
    if isinstance(valor, dict):
        partes: list[str] = []
        for chave in _CHAVES_LOCAL_DETALHE:
            v = valor.get(chave)
            if isinstance(v, str) and v.strip() and v.strip() not in partes:
                partes.append(v.strip())
        return ", ".join(partes) if partes else None
    return None


def _parse_url(valor: object, base: str = "https://www.olx.com.br") -> str | None:
    if not isinstance(valor, str) or not valor:
        return None
    if valor.startswith("http://") or valor.startswith("https://"):
        return valor
    return base.rstrip("/") + "/" + valor.lstrip("/")


def _parse_data_publicacao(valor: object) -> str | None:
    """A OLX manda `date` como timestamp Unix (segundos). Convertido
    para ISO 8601 UTC para ficar legível; qualquer outro formato é só
    convertido para string (preservado como veio da fonte)."""
    if valor is None:
        return None
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, (int, float)):
        try:
            return datetime.fromtimestamp(valor, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(valor)
    return str(valor)


def normalize_olx(raw: dict, coletado_em: datetime) -> Anuncio | None:
    """Converte um item cru retornado por OlxSource.collect() no
    formato canônico Anuncio. Retorna None se faltar campo essencial
    (id, título ou url) — o item é descartado silenciosamente (logado
    em debug) em vez de derrubar o pipeline."""
    id_bruto = _pega_por_chave(raw, _CHAVES_ID)
    titulo_bruto = _pega_por_chave(raw, _CHAVES_TITULO)
    url_bruta = _pega_por_chave(raw, _CHAVES_URL)

    if id_bruto is None or not titulo_bruto or not url_bruta:
        logger.debug("olx: item cru descartado por falta de id/titulo/url: %r", raw)
        return None

    url = _parse_url(url_bruta)
    if url is None:
        logger.debug("olx: item cru descartado por url inválida: %r", url_bruta)
        return None

    preco = _parse_preco(_pega_por_chave(raw, _CHAVES_PRECO))
    local = _parse_local(_pega_por_chave(raw, _CHAVES_LOCAL))
    publicado_em = _parse_data_publicacao(_pega_por_chave(raw, _CHAVES_DATA))

    return Anuncio(
        id=str(id_bruto),
        titulo=str(titulo_bruto),
        preco=preco,
        url=url,
        local=local,
        fonte="olx",
        publicado_em=publicado_em,
        coletado_em=coletado_em,
    )


NORMALIZADORES: dict[str, Callable[[dict, datetime], Anuncio | None]] = {
    "olx": normalize_olx,
}


def normalize(fonte: str, raw: dict, coletado_em: datetime) -> Anuncio | None:
    normalizador = NORMALIZADORES.get(fonte)
    if normalizador is None:
        raise ValueError(f"Sem normalizador registrado para a fonte '{fonte}'.")
    return normalizador(raw, coletado_em)
