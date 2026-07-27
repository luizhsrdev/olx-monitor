"""Utilitários genéricos para decodificar o formato de streaming RSC
("React Server Components") do Next.js App Router.

A OLX migrou pra esse formato em meados de 2026 (era `__NEXT_DATA__`
antes — ver `sources/olx.py`). Tanto a página de listagem/busca quanto
a página individual de um anúncio (`seller_info.py`) usam o mesmo
mecanismo de transporte: vários `<script>self.__next_f.push([N,"..."])
</script>` espalhados pelo HTML, sem id fixo, cada um carregando um
pedaço da árvore serializada. Esse módulo só decodifica o transporte —
não sabe nada sobre "anúncio" ou "vendedor", isso é responsabilidade de
quem usa.
"""

from __future__ import annotations

import json
import re
from typing import Iterator

_NEXT_F_PUSH_PATTERN = re.compile(
    r'self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)'
)


def iter_next_f_chunks(html: str) -> list[str]:
    """Extrai e decodifica cada payload de self.__next_f.push([N, "..."]).

    O literal capturado é ele mesmo uma string JSON (com aspas e
    escapes), então um json.loads nele já devolve o texto decodificado
    (com \\n virando quebra de linha de verdade, \\u0026 virando &,
    etc.) — não uma estrutura de dados ainda.
    """
    chunks = []
    for match in _NEXT_F_PUSH_PATTERN.finditer(html):
        try:
            chunks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return chunks


def parse_rsc_entries(chunk: str) -> list[object]:
    """Cada chunk decodificado pode conter uma ou mais entradas no
    formato "id:valor" separadas por quebra de linha. "valor" às vezes
    é JSON válido (o que nos interessa) e às vezes é sintaxe interna do
    protocolo React Flight (ex.: I[9766,[],""], "$Sreact.fragment") que
    não é JSON — essas são ignoradas silenciosamente."""
    valores: list[object] = []
    for linha in chunk.split("\n"):
        _, separador, resto = linha.partition(":")
        if not separador or not resto:
            continue
        try:
            valores.append(json.loads(resto))
        except json.JSONDecodeError:
            continue
    return valores


def iter_valores_rsc(html: str) -> Iterator[object]:
    """Itera por todos os valores de nível superior decodificados de
    todos os chunks RSC do HTML — a unidade básica que qualquer
    scanner específico (anúncios, dados de vendedor, ...) varre."""
    for chunk in iter_next_f_chunks(html):
        yield from parse_rsc_entries(chunk)


def tem_rsc(html: str) -> bool:
    """True se o HTML tem pelo menos um self.__next_f.push — indica
    que a página chegou a renderizar o app (mesmo que não tenha o que
    procuramos). False é o sinal forte de bloqueio/challenge."""
    return bool(_NEXT_F_PUSH_PATTERN.search(html))
