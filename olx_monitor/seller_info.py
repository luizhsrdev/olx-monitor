"""Extração dos dados do vendedor a partir da página individual de um
anúncio da OLX (a `url` de um `Anuncio`).

**A página individual não usa o formato RSC da listagem** — confirmado
inspecionando um `debug_seller.html` real: zero ocorrências de
`self.__next_f.push`. Os dados do vendedor (tempo de conta, verificações,
avaliações) vêm como texto/HTML já renderizado, carregados por uma
chamada separada do lado do cliente (o `initial-data` embutido na página
tem os dados do anúncio em si — preço, categoria, `user.name` — mas não
"Na OLX desde", verificações nem avaliações; isso só aparece no HTML
depois que o JS roda, e como o `OlxSource` já usa Playwright pra
renderizar a página, esse conteúdo chega pronto no `page.content()`).

A extração aqui é via regex sobre o HTML renderizado — não vale a
complexidade de um parser de árvore (BeautifulSoup) pra poucos campos.
Os padrões abaixo foram confirmados contra uma amostra real; a única
ressalva é o selo de "conta verificada" e o formato de quando HÁ
avaliações (a amostra inspecionada tinha 0), que continuam best-effort —
ver comentários em cada função.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# "Na OLX desde novembro de 2024" — confirmado contra uma amostra real.
_PADRAO_MEMBRO_DESDE = re.compile(r"Na OLX desde ([^<]+)</span>")

# <img ... alt="Foto de Jozelio Frutuozo" ...> — confirmado. O nome
# também aparece num <span> logo depois, mas o alt da foto é um alvo de
# regex mais estável (menos provável de mudar de classe CSS).
_PADRAO_NOME = re.compile(r'alt="Foto de ([^"]+)"')

# Cada item de "Informações verificadas" é um <svg> com um <path> cujo
# fill indica verificado (#24A148, verde) ou não (#8994A9, cinza),
# seguido por um <p ...>RÓTULO</p> — confirmado contra E-mail/Telefone
# (verdes) e Facebook (cinza) numa amostra real. "Identidade" não
# apareceu na amostra (o vendedor não tinha), mas o mesmo padrão deve
# valer.
_PADRAO_ITEM_VERIFICACAO = re.compile(
    r'fill="(#24A148|#8994A9)"[^>]*></path>.*?<p[^>]*>([^<]+)</p>',
    re.DOTALL,
)
_JANELA_VERIFICACOES_CHARS = 4000  # o bloco inteiro cabe bem dentro disso

_ROTULO_PARA_CHAVE = {
    "e-mail": "email",
    "email": "email",
    "telefone": "telefone",
    "identidade": "identidade",
    "facebook": "facebook",
}

# "Este anunciante ainda não possui avaliações" — confirmado.
_PADRAO_SEM_AVALIACOES = re.compile(r"ainda não possui avalia", re.IGNORECASE)

# NÃO confirmado: a amostra inspecionada não tinha avaliações, então o
# formato de quando HÁ estrelas/avaliações é um chute razoável (número
# decimal perto da palavra "avalia"), não uma estrutura vista de
# verdade. Se vier errado, `estrelas`/`tem_avaliacoes` ficam None nesse
# caso — não quebra nada, só não enriquece esse campo.
_PADRAO_COM_AVALIACOES = re.compile(r"(\d+[.,]\d+)\s*(?:<[^>]+>\s*)*avalia", re.IGNORECASE)

# NÃO confirmado: nenhuma amostra inspecionada mostrou esse selo (o
# vendedor da amostra não tinha "conta verificada"), então não há como
# validar o padrão positivo. Só reconhece o texto literal, se um dia
# aparecer — do contrário fica None (não é o mesmo que "não
# verificada": é "não sabemos").
_PADRAO_CONTA_VERIFICADA = re.compile(r"conta verificada", re.IGNORECASE)


@dataclass
class SellerInfo:
    nome: str | None = None
    membro_desde: str | None = None
    conta_verificada: bool | None = None
    verificacoes: dict[str, bool] = field(default_factory=dict)
    estrelas: float | None = None
    tem_avaliacoes: bool | None = None


def _extrair_nome(html: str) -> str | None:
    match = _PADRAO_NOME.search(html)
    return match.group(1).strip() if match else None


def _extrair_membro_desde(html: str) -> str | None:
    match = _PADRAO_MEMBRO_DESDE.search(html)
    return match.group(1).strip() if match else None


def _extrair_conta_verificada(html: str) -> bool | None:
    return True if _PADRAO_CONTA_VERIFICADA.search(html) else None


def _extrair_verificacoes(html: str) -> dict[str, bool]:
    idx = html.find("Informações verificadas")
    if idx == -1:
        return {}
    janela = html[idx : idx + _JANELA_VERIFICACOES_CHARS]

    verificacoes: dict[str, bool] = {}
    for cor, rotulo in _PADRAO_ITEM_VERIFICACAO.findall(janela):
        chave = _ROTULO_PARA_CHAVE.get(rotulo.strip().lower(), rotulo.strip().lower())
        verificacoes[chave] = cor == "#24A148"
    return verificacoes


def _extrair_avaliacoes(html: str) -> tuple[float | None, bool | None]:
    if _PADRAO_SEM_AVALIACOES.search(html):
        return None, False

    match = _PADRAO_COM_AVALIACOES.search(html)
    if match is None:
        return None, None  # não achou nem "sem avaliações" nem número — não sabemos

    try:
        return float(match.group(1).replace(",", ".")), True
    except ValueError:
        return None, True


def extract_seller_info(html: str) -> SellerInfo | None:
    """Extrai os dados do vendedor da página individual de um anúncio
    (HTML já renderizado, não RSC — ver docstring do módulo).

    Cada campo é extraído independentemente e fica `None`/vazio se não
    for encontrado — um campo faltando não invalida os outros. Só
    retorna `None` (falha total) se **nenhum** campo for encontrado,
    sinal de que a página não é o que esperávamos (bloqueio, challenge,
    mudança de estrutura) — nesse caso `enrichment.py` salva um
    `debug_seller.html` pra investigação.
    """
    nome = _extrair_nome(html)
    membro_desde = _extrair_membro_desde(html)
    conta_verificada = _extrair_conta_verificada(html)
    verificacoes = _extrair_verificacoes(html)
    estrelas, tem_avaliacoes = _extrair_avaliacoes(html)

    nada_encontrado = (
        nome is None
        and membro_desde is None
        and conta_verificada is None
        and not verificacoes
        and estrelas is None
        and tem_avaliacoes is None
    )
    if nada_encontrado:
        return None

    return SellerInfo(
        nome=nome,
        membro_desde=membro_desde,
        conta_verificada=conta_verificada,
        verificacoes=verificacoes,
        estrelas=estrelas,
        tem_avaliacoes=tem_avaliacoes,
    )
