from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Formato atual (confirmado em 2026-07): a OLX usa o App Router do
# Next.js, que transmite o conteúdo como RSC ("React Server
# Components") streaming — vários <script>self.__next_f.push([1,"..."])
# </script> espalhados pelo HTML, cada um carregando um pedaço da árvore
# serializada. Não existe mais um único bloco __NEXT_DATA__ centralizado
# com id fixo. Cada payload é uma string JSON-escapada contendo uma ou
# mais entradas "id:valor" separadas por \n; o "valor" costuma ser um
# array/objeto JSON, mas às vezes é uma referência de módulo interna do
# React (ex.: I[9766,[],""]) que não é JSON válido e é ignorada.
_NEXT_F_PUSH_PATTERN = re.compile(
    r'self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)'
)

# Formato legado, mantido como fallback caso a OLX volte a servir (ou
# sirva em algum contexto específico) o antigo __NEXT_DATA__ do Pages
# Router.
_NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

# Arquivo de dump de HTML para depuração manual (ver headless=False em
# _fetch_playwright) — útil se a OLX voltar a mudar de formato no futuro.
DEBUG_DUMP_PATH = Path("debug_page.html")

_INDICIOS_BLOQUEIO = (
    "attention required",
    "cf-error-details",
    "challenge-platform",
    "captcha",
    "just a moment",
    "access denied",
)

_CHAVES_ID = ("id", "listid", "ad_id", "adid")
_CHAVES_TITULO = ("subject", "title", "titulo")
_CHAVES_PRECO = ("pricevalue", "price", "preco")


class OlxCollectionError(Exception):
    """Falha ao coletar/extrair anúncios de uma URL de busca da OLX."""


def _pega_por_chave(item: dict, candidatos: tuple[str, ...]):
    """Busca o primeiro valor não-nulo entre `candidatos`, comparando
    nomes de chave sem diferenciar maiúsculas/minúsculas."""
    mapa = {k.lower(): v for k, v in item.items()}
    for candidato in candidatos:
        if candidato in mapa and mapa[candidato] is not None:
            return mapa[candidato]
    return None


def _parece_item_de_anuncio(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    return (
        _pega_por_chave(item, _CHAVES_ID) is not None
        and _pega_por_chave(item, _CHAVES_TITULO) is not None
        and _pega_por_chave(item, _CHAVES_PRECO) is not None
    )


def _varrer_listas_de_anuncio(node: object, encontrados: list[dict], vistos: set[int]) -> None:
    """Varre `node` recursivamente procurando listas cujos itens tenham
    cara de anúncio (id + título + preço). Não assume nenhum caminho
    fixo no JSON nem nome de chave específico (ex.: "ads") — a
    estrutura muda com o tempo, e já mudou uma vez durante o
    desenvolvimento deste projeto.

    Não exigimos que TODOS os itens da lista pareçam anúncio, só a
    maioria: a lista de resultados de busca da OLX vem com slots de
    banner publicitário intercalados a cada handful de posições (dicts
    só com "advertisingId"/"deviceType", sem id/título/preço), e exigir
    100% fazia a lista inteira ser descartada por causa desses poucos
    itens que não são anúncio de verdade.
    """
    node_id = id(node)
    if node_id in vistos:
        return
    vistos.add(node_id)

    if isinstance(node, list):
        if node:
            candidatos = [item for item in node if _parece_item_de_anuncio(item)]
            if len(candidatos) >= max(1, len(node) // 2):
                encontrados.extend(candidatos)
                return  # não desce dentro de uma lista de anúncios já identificada
        for item in node:
            _varrer_listas_de_anuncio(item, encontrados, vistos)
    elif isinstance(node, dict):
        for valor in node.values():
            _varrer_listas_de_anuncio(valor, encontrados, vistos)


def _dedupe_por_id(itens: list[dict]) -> list[dict]:
    vistos_ids: set[str] = set()
    unicos: list[dict] = []
    for item in itens:
        id_bruto = _pega_por_chave(item, _CHAVES_ID)
        if id_bruto is None:
            continue
        chave = str(id_bruto)
        if chave in vistos_ids:
            continue
        vistos_ids.add(chave)
        unicos.append(item)
    return unicos


def _iter_next_f_chunks(html: str) -> list[str]:
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


def _parse_rsc_entries(chunk: str) -> list[object]:
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


def extract_ads_from_rsc(html: str) -> list[dict]:
    """Varre todos os chunks self.__next_f.push(...) do HTML procurando
    anúncios, usando o mesmo scanner recursivo/genérico do formato
    legado. Deduplicados por id (o mesmo anúncio pode aparecer
    referenciado em mais de um chunk)."""
    encontrados: list[dict] = []
    for chunk in _iter_next_f_chunks(html):
        for valor in _parse_rsc_entries(chunk):
            # `vistos` é criado do zero para cada valor de nível
            # superior (não compartilhado entre chunks): cada entrada
            # RSC é uma árvore JSON independente, sem estrutura
            # compartilhada com as outras. Reusar um único set entre
            # elas é perigoso — objetos de um chunk já processado são
            # coletados pelo GC, o Python reaproveita o endereço de
            # memória em outro chunk, e id() colide, fazendo o scanner
            # ignorar por engano nós que nunca foram visitados de fato.
            _varrer_listas_de_anuncio(valor, encontrados, set())
    return _dedupe_por_id(encontrados)


def extract_next_data_json(html: str) -> dict | None:
    """[Formato legado] Extrai e faz parse do JSON dentro de
    <script id="__NEXT_DATA__">."""
    match = _NEXT_DATA_PATTERN.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        logger.warning("olx: __NEXT_DATA__ encontrado mas não é JSON válido")
        return None


def extract_ads_from_next_data(next_data: dict) -> list[dict]:
    """[Formato legado] Varre o __NEXT_DATA__ inteiro e retorna os itens
    crus de anúncio encontrados, deduplicados por id."""
    encontrados: list[dict] = []
    _varrer_listas_de_anuncio(next_data, encontrados, set())
    return _dedupe_por_id(encontrados)


def _tem_conteudo_utilizavel(html: str) -> bool:
    """True se o HTML parece ser uma página real da OLX (RSC ou
    __NEXT_DATA__ legado), mesmo que a busca não tenha retornado
    nenhum anúncio. False indica bloqueio/challenge — a página não
    chegou a renderizar o app."""
    return bool(_NEXT_F_PUSH_PATTERN.search(html)) or extract_next_data_json(html) is not None


def _parece_bloqueado(status_code: int, html: str) -> bool:
    if status_code in (403, 429, 503):
        return True
    html_lower = html.lower()
    return any(termo in html_lower for termo in _INDICIOS_BLOQUEIO)


class OlxSource:
    """Adaptador de fonte para a OLX.

    `modo="requests"` baixa a página com uma requisição HTTP simples
    (leve, padrão). Se a resposta vier bloqueada (403/429/503, página
    de desafio anti-bot, ou sem conteúdo utilizável), cai
    automaticamente para `modo="playwright"`, que renderiza a página
    com Chromium headless. `modo="playwright"` pode também ser forçado
    diretamente pela config de um monitor.
    """

    nome = "olx"

    def __init__(
        self,
        modo: str = "requests",
        timeout_segundos: int = 20,
        headless: bool = True,
    ):
        self.modo = modo
        self.timeout_segundos = timeout_segundos
        # headless=False é só para depuração manual (ex.: checar se o
        # desafio anti-bot da Cloudflare se comporta diferente com um
        # navegador visível). Não é uma opção de config permanente — ver
        # OLX_MONITOR_PLAYWRIGHT_HEADLESS em run.py.
        self.headless = headless

    def collect(self, url: str) -> list[dict]:
        if self.modo == "playwright":
            html = self._fetch_playwright(url)
        else:
            html = self._fetch_requests(url)
            if html is None or not _tem_conteudo_utilizavel(html):
                logger.warning(
                    "olx: modo requests bloqueado ou sem conteúdo utilizável em %s, "
                    "tentando fallback playwright",
                    url,
                )
                html = self._fetch_playwright(url)

        anuncios = extract_ads_from_rsc(html)
        if anuncios:
            return anuncios

        next_data = extract_next_data_json(html)
        if next_data:
            anuncios_legado = extract_ads_from_next_data(next_data)
            if anuncios_legado:
                return anuncios_legado

        if not _tem_conteudo_utilizavel(html):
            raise OlxCollectionError(f"Não foi possível extrair dados de anúncios de {url}")

        # Página renderizou normalmente, só que sem nenhum anúncio
        # batendo com a busca — não é erro, é resultado vazio mesmo.
        return []

    def _fetch_requests(self, url: str) -> str | None:
        try:
            resposta = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "pt-BR,pt;q=0.9",
                },
                timeout=self.timeout_segundos,
            )
        except requests.RequestException as exc:
            logger.warning("olx: falha de rede em modo requests para %s: %s", url, exc)
            return None

        if _parece_bloqueado(resposta.status_code, resposta.text):
            logger.warning(
                "olx: indício de bloqueio (status=%s) para %s", resposta.status_code, url
            )
            return None

        return resposta.text

    def _fetch_playwright(self, url: str) -> str:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise OlxCollectionError(
                "Modo playwright requer o pacote 'playwright' instalado "
                "(pip install playwright && playwright install chromium)."
            ) from exc

        timeout_ms = self.timeout_segundos * 1000

        if not self.headless:
            logger.warning(
                "olx: rodando playwright com headless=False (modo debug manual) — "
                "requer um ambiente com display; não use isso em deploy 24/7"
            )

        with sync_playwright() as p:
            navegador = p.chromium.launch(headless=self.headless)
            try:
                pagina = navegador.new_page(user_agent=USER_AGENT)
                try:
                    # domcontentloaded, não networkidle: a OLX nunca fica
                    # com a rede ociosa (analytics, ads, polling contínuo),
                    # então networkidle estoura timeout mesmo com a página
                    # pronta. O conteúdo RSC é transmitido como parte do
                    # HTML inicial (SSR/streaming) — não é preciso esperar
                    # mais que isso.
                    pagina.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    logger.warning(
                        "olx: timeout aguardando domcontentloaded em %s, "
                        "seguindo com o conteúdo já carregado",
                        url,
                    )

                try:
                    # Não há mais um id de script fixo para aguardar (o
                    # formato RSC usa scripts sem id) — esperamos pelo
                    # marcador do protocolo aparecer em algum lugar do DOM.
                    pagina.wait_for_function(
                        "() => document.documentElement.outerHTML.includes('__next_f.push')",
                        timeout=timeout_ms,
                    )
                except PlaywrightTimeoutError:
                    logger.warning(
                        "olx: conteúdo RSC não apareceu em %s dentro do timeout "
                        "(provável página de desafio/captcha)",
                        url,
                    )

                html = pagina.content()

                if not self.headless:
                    DEBUG_DUMP_PATH.write_text(html, encoding="utf-8")
                    logger.warning(
                        "olx: [debug] HTML completo de %s salvo em %s (%d bytes)",
                        url,
                        DEBUG_DUMP_PATH.resolve(),
                        len(html),
                    )
            finally:
                navegador.close()

        return html
