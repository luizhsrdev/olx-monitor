"""Extração dos dados do vendedor a partir da página individual de um
anúncio da OLX (a `url` de um `Anuncio`).

Reaproveita o mesmo mecanismo de decodificação RSC usado pela listagem
de busca (ver `rsc.py`) — a suposição é que a página individual usa o
mesmo formato de streaming do App Router. **Isso não foi validado
contra uma amostra real** (diferente do parser de listagem, que só foi
corrigido depois de inspecionar um `debug_page.html` de verdade — ver
README). Se a extração vier sempre `None`, é sinal de que a estrutura
real diverge do que `_CHAVES_*`/`_parece_dict_de_vendedor` esperam, ou
de que os dados vêm de uma chamada XHR separada em vez de embutidos no
HTML. `enrichment.py` salva um `debug_seller.html` na primeira falha
pra facilitar essa investigação.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import rsc

logger = logging.getLogger(__name__)

# "Na OLX desde maio de 2023" / "desde maio de 2023" — captura só a
# parte "maio de 2023".
_PADRAO_MEMBRO_DESDE = re.compile(r"desde\s+([a-zç]+\s+de\s+\d{4})", re.IGNORECASE)

_CHAVES_NOME = ("sellername", "name", "nome", "nickname", "username", "displayname")
_CHAVES_DESDE = (
    "membersince",
    "member_since",
    "since",
    "joinedat",
    "createdat",
    "datacadastro",
)
_CHAVES_VERIFICADA = ("accountverified", "isverified", "verified", "contaverificada")
_CHAVES_ESTRELAS = ("rating", "stars", "estrelas", "score", "avgrating")
_CHAVES_TEM_AVALIACOES = ("hasreviews", "reviewcount", "totalreviews", "avaliacoes")

# Cada "tipo" de verificação pode aparecer sob nomes diferentes — só
# reconhecemos como sinal positivo/negativo se o valor for booleano de
# verdade (presença de uma chave sem ser bool não conta, é ambíguo
# demais pra afirmar verificado ou não).
_MAPA_VERIFICACOES = {
    "email": ("email", "e-mail"),
    "telefone": ("phone", "telefone", "mobile", "celular"),
    "identidade": ("identity", "document", "identidade", "documento"),
    "facebook": ("facebook",),
}

# Quantos sinais independentes um dict precisa ter pra ser considerado
# "o card do vendedor" e não coincidência. "desde" sozinho já vale 2
# pontos (é o sinal mais específico e difícil de aparecer à toa).
_PONTUACAO_MINIMA = 2


@dataclass
class SellerInfo:
    nome: str | None = None
    membro_desde: str | None = None
    conta_verificada: bool | None = None
    verificacoes: dict[str, bool] = field(default_factory=dict)
    estrelas: float | None = None
    tem_avaliacoes: bool | None = None


def _pega_por_chave(item: dict, candidatos: tuple[str, ...]):
    mapa = {k.lower(): v for k, v in item.items()}
    for candidato in candidatos:
        if candidato in mapa and mapa[candidato] not in (None, ""):
            return mapa[candidato]
    return None


def _parece_dict_de_vendedor(item: object) -> int:
    """Pontua um dict por quantos sinais de "isso é o card do
    vendedor" ele carrega. 0 = não parece nada."""
    if not isinstance(item, dict):
        return 0
    pontos = 0
    if _pega_por_chave(item, _CHAVES_NOME) is not None:
        pontos += 1
    if _pega_por_chave(item, _CHAVES_DESDE) is not None:
        pontos += 2
    if _pega_por_chave(item, _CHAVES_VERIFICADA) is not None:
        pontos += 1
    if _pega_por_chave(item, _CHAVES_ESTRELAS) is not None:
        pontos += 1
    chaves_item = {k.lower() for k in item}
    for chaves_candidatas in _MAPA_VERIFICACOES.values():
        if any(chave.lower() in chaves_item for chave in chaves_candidatas):
            pontos += 1
            break
    return pontos


def _encontrar_melhor_candidato(node: object, vistos: set[int]) -> tuple[int, dict | None]:
    node_id = id(node)
    if node_id in vistos:
        return (0, None)
    vistos.add(node_id)

    melhor_pontos, melhor_dict = 0, None

    if isinstance(node, dict):
        pontos = _parece_dict_de_vendedor(node)
        if pontos > melhor_pontos:
            melhor_pontos, melhor_dict = pontos, node
        for valor in node.values():
            sub_pontos, sub_dict = _encontrar_melhor_candidato(valor, vistos)
            if sub_pontos > melhor_pontos:
                melhor_pontos, melhor_dict = sub_pontos, sub_dict
    elif isinstance(node, list):
        for item in node:
            sub_pontos, sub_dict = _encontrar_melhor_candidato(item, vistos)
            if sub_pontos > melhor_pontos:
                melhor_pontos, melhor_dict = sub_pontos, sub_dict

    return melhor_pontos, melhor_dict


def _extrair_membro_desde(valor: object) -> str | None:
    if not isinstance(valor, str):
        return None
    match = _PADRAO_MEMBRO_DESDE.search(valor)
    if match:
        return match.group(1)
    return valor.strip() or None


def _montar_seller_info(item: dict) -> SellerInfo:
    nome = _pega_por_chave(item, _CHAVES_NOME)
    desde_bruto = _pega_por_chave(item, _CHAVES_DESDE)
    verificada = _pega_por_chave(item, _CHAVES_VERIFICADA)
    estrelas_bruto = _pega_por_chave(item, _CHAVES_ESTRELAS)
    tem_avaliacoes_bruto = _pega_por_chave(item, _CHAVES_TEM_AVALIACOES)

    verificacoes: dict[str, bool] = {}
    mapa_item = {k.lower(): v for k, v in item.items()}
    for nome_verificacao, chaves in _MAPA_VERIFICACOES.items():
        for chave in chaves:
            valor = mapa_item.get(chave.lower())
            if isinstance(valor, bool):
                verificacoes[nome_verificacao] = valor
                break

    estrelas = float(estrelas_bruto) if isinstance(estrelas_bruto, (int, float)) else None

    tem_avaliacoes: bool | None
    if isinstance(tem_avaliacoes_bruto, bool):
        tem_avaliacoes = tem_avaliacoes_bruto
    elif isinstance(tem_avaliacoes_bruto, (int, float)):
        tem_avaliacoes = tem_avaliacoes_bruto > 0
    elif estrelas is not None:
        tem_avaliacoes = estrelas > 0
    else:
        tem_avaliacoes = None

    return SellerInfo(
        nome=str(nome) if nome is not None else None,
        membro_desde=_extrair_membro_desde(desde_bruto),
        conta_verificada=verificada if isinstance(verificada, bool) else None,
        verificacoes=verificacoes,
        estrelas=estrelas,
        tem_avaliacoes=tem_avaliacoes,
    )


def extract_seller_info(html: str) -> SellerInfo | None:
    """Varre o HTML (formato RSC) procurando o card de dados do
    vendedor. Retorna None se não achar nada com confiança
    suficiente — quem chama decide o que fazer (logar e seguir sem
    enriquecer a notificação)."""
    melhor_pontos, melhor_dict = 0, None
    for valor in rsc.iter_valores_rsc(html):
        pontos, candidato = _encontrar_melhor_candidato(valor, set())
        if pontos > melhor_pontos:
            melhor_pontos, melhor_dict = pontos, candidato

    if melhor_dict is None or melhor_pontos < _PONTUACAO_MINIMA:
        logger.debug("olx: nenhum candidato a card de vendedor com pontuação suficiente")
        return None

    return _montar_seller_info(melhor_dict)
