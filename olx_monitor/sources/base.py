from __future__ import annotations

from typing import Protocol


class Source(Protocol):
    """Interface de um adaptador de fonte (estágio de coleta do pipeline).

    Implementações não fazem normalização nem filtro: apenas baixam a
    URL de busca e devolvem os itens crus, no formato nativo da fonte.
    Adicionar uma fonte nova (Mercado Livre, Facebook Marketplace, ...)
    é criar um novo módulo implementando isto — nada mais no pipeline
    precisa mudar.
    """

    nome: str

    def collect(self, url: str) -> list[dict]:
        """Baixa a URL de busca e retorna anúncios crus (dicts com os
        campos nativos da fonte, ainda não normalizados)."""
        ...
