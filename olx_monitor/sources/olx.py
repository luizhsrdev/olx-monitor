from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

from .. import rsc

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Formato legado, mantido como fallback caso a OLX volte a servir (ou
# sirva em algum contexto específico) o antigo __NEXT_DATA__ do Pages
# Router. O formato atual (RSC streaming) é decodificado por rsc.py.
_NEXT_DATA_PATTERN = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

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

# Circuit breaker do modo requests: depois de N falhas consecutivas
# num domínio, pula direto pro playwright nas próximas tentativas —
# evita esperar ~2-3s de timeout/bloqueio do requests toda vez que já
# sabemos que ele não vai funcionar. Reseta sozinho depois de um
# tempo, pra reavaliar (a Cloudflare pode relaxar, o ambiente de
# deploy pode se comportar diferente do de dev).
_CIRCUITO_LIMIAR_FALHAS = 3
_CIRCUITO_RESET_SEGUNDOS = 30 * 60

# Browser persistente: relança depois desse número de páginas abertas,
# pra não deixar o processo do Chromium acumular memória indefinidamente.
_MAX_PAGINAS_ANTES_DE_RELANCAR = 50

_MAX_WORKERS_REQUESTS = 5


class OlxCollectionError(Exception):
    """Falha ao coletar/extrair anúncios de uma URL de busca da OLX."""


class _CircuitBreaker:
    """Circuit breaker por domínio para o modo requests. Não tenta
    detectar "o requests está de volta" ativamente — só reabre a
    possibilidade de tentar de novo depois que a janela de reset
    passa, e deixa a tentativa seguinte decidir."""

    def __init__(
        self,
        limiar: int = _CIRCUITO_LIMIAR_FALHAS,
        reset_segundos: float = _CIRCUITO_RESET_SEGUNDOS,
    ):
        self._limiar = limiar
        self._reset_segundos = reset_segundos
        self._falhas_consecutivas = 0
        self._aberto_desde: float | None = None

    def esta_aberto(self, agora: float | None = None) -> bool:
        agora = time.monotonic() if agora is None else agora
        if self._aberto_desde is None:
            return False
        if agora - self._aberto_desde >= self._reset_segundos:
            self._falhas_consecutivas = 0
            self._aberto_desde = None
            return False
        return True

    def registrar_falha(self, agora: float | None = None) -> None:
        agora = time.monotonic() if agora is None else agora
        self._falhas_consecutivas += 1
        if self._falhas_consecutivas >= self._limiar and self._aberto_desde is None:
            self._aberto_desde = agora

    def registrar_sucesso(self) -> None:
        self._falhas_consecutivas = 0
        self._aberto_desde = None


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


def extract_ads_from_rsc(html: str) -> list[dict]:
    """Varre todos os chunks self.__next_f.push(...) do HTML procurando
    anúncios, usando o scanner recursivo/genérico. Deduplicados por id
    (o mesmo anúncio pode aparecer referenciado em mais de um chunk)."""
    encontrados: list[dict] = []
    for valor in rsc.iter_valores_rsc(html):
        # `vistos` criado do zero para cada valor de nível superior:
        # cada entrada RSC é uma árvore JSON independente. Reusar um
        # único set entre elas já causou uma colisão real de id() (o
        # GC recicla o endereço de memória de um chunk já processado,
        # e o scanner ignorava por engano nós nunca visitados de fato).
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
    return rsc.tem_rsc(html) or extract_next_data_json(html) is not None


def _parece_bloqueado(status_code: int, html: str) -> bool:
    if status_code in (403, 429, 503):
        return True
    html_lower = html.lower()
    return any(termo in html_lower for termo in _INDICIOS_BLOQUEIO)


def _dominio(url: str) -> str:
    return urlparse(url).netloc


class OlxSource:
    """Adaptador de fonte para a OLX.

    `modo="requests"` baixa a página com uma requisição HTTP simples
    (leve, padrão). Se a resposta vier bloqueada (403/429/503, página
    de desafio anti-bot, ou sem conteúdo utilizável), cai
    automaticamente para `modo="playwright"`, que renderiza a página
    com um browser Chromium **persistente** (ver `_obter_context`) —
    reaproveitado entre chamadas, relançado só após um crash ou depois
    de `_MAX_PAGINAS_ANTES_DE_RELANCAR` páginas. `modo="playwright"`
    pode também ser forçado diretamente pela config de um monitor.

    Cada instância mantém seu próprio circuit breaker por domínio (ver
    `_CircuitBreaker`) e seu próprio browser — não é thread-safe entre
    threads diferentes chamando a mesma instância ao mesmo tempo (a API
    síncrona do Playwright exige isso), mas isso já é garantido pela
    arquitetura do projeto: uma `OlxSource` é sempre exclusiva de uma
    única thread (um monitor, ou o worker de enriquecimento).
    """

    nome = "olx"

    def __init__(self, modo: str = "requests", timeout_segundos: int = 20):
        self.modo = modo
        self.timeout_segundos = timeout_segundos
        self._circuitos: dict[str, _CircuitBreaker] = {}
        self._playwright = None
        self._browser = None
        self._context = None
        self._paginas_desde_relancamento = 0

    def collect(self, url: str) -> list[dict]:
        html = self._obter_html(url)
        return self._extrair_anuncios(html, url)

    def collect_many(self, urls: list[str]) -> list[dict]:
        """Busca várias URLs do mesmo monitor. As que resolvem via modo
        requests são buscadas em paralelo (requests é thread-safe pra
        isso); as que precisam de playwright são processadas
        sequencialmente nesta mesma thread — o browser persistente só
        pode ser usado pela thread que o criou (API síncrona do
        Playwright), então não dá pra paralelizar esse caminho sem
        abrir mão do reaproveitamento do browser. Ordem do resultado
        segue a ordem de `urls`, independente de qual terminou primeiro.
        """
        if self.modo == "playwright" or len(urls) <= 1:
            resultado: list[dict] = []
            for url in urls:
                resultado.extend(self.collect(url))
            return resultado

        pendentes_playwright: list[str] = []
        html_por_url: dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=min(len(urls), _MAX_WORKERS_REQUESTS)) as executor:
            futuros = {executor.submit(self._tentar_html_requests, url): url for url in urls}
            for futuro in as_completed(futuros):
                url = futuros[futuro]
                html = futuro.result()
                if html is not None:
                    html_por_url[url] = html
                else:
                    pendentes_playwright.append(url)

        anuncios_por_url: dict[str, list[dict]] = {
            url: self._extrair_anuncios(html, url) for url, html in html_por_url.items()
        }

        for url in pendentes_playwright:
            logger.warning("olx: fallback playwright para %s", url)
            html = self._fetch_playwright(url)
            anuncios_por_url[url] = self._extrair_anuncios(html, url)

        return [item for url in urls for item in anuncios_por_url.get(url, [])]

    def fetch_html(self, url: str) -> str:
        """Baixa o HTML de qualquer URL da OLX (busca ou página
        individual de anúncio), pelo mesmo caminho requests+circuit
        breaker+fallback playwright do `collect()`, mas sem tentar
        extrair anúncios dele — usado pelo enriquecimento de dados do
        vendedor (`enrichment.py`), que lê a página individual."""
        return self._obter_html(url)

    def close(self) -> None:
        """Fecha o browser persistente, se houver. Chamar ao encerrar
        a thread dona desta instância (monitor ou worker)."""
        self._fechar_browser()

    # --- fetch (requests + circuit breaker + fallback playwright) ------

    def _circuito_de(self, url: str) -> _CircuitBreaker:
        dominio = _dominio(url)
        circuito = self._circuitos.get(dominio)
        if circuito is None:
            circuito = _CircuitBreaker()
            self._circuitos[dominio] = circuito
        return circuito

    def _obter_html(self, url: str) -> str:
        if self.modo == "playwright":
            return self._fetch_playwright(url)

        html = self._tentar_html_requests(url)
        if html is not None:
            return html

        logger.warning(
            "olx: modo requests bloqueado/circuito aberto/sem conteúdo utilizável em %s, "
            "tentando fallback playwright",
            url,
        )
        return self._fetch_playwright(url)

    def _tentar_html_requests(self, url: str) -> str | None:
        """A parte requests+circuit breaker isolada (sem playwright) —
        usada tanto por `_obter_html` quanto pelo pool de threads do
        `collect_many`. Nunca chama playwright: esse fallback fica
        sempre sequencial, fora do pool."""
        circuito = self._circuito_de(url)
        if circuito.esta_aberto():
            logger.info("olx: circuito aberto para %s, pulando requests", _dominio(url))
            return None

        html = self._fetch_requests(url)
        if html is not None and _tem_conteudo_utilizavel(html):
            circuito.registrar_sucesso()
            return html

        circuito.registrar_falha()
        return None

    def _extrair_anuncios(self, html: str, url: str) -> list[dict]:
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

    # --- browser Playwright persistente ---------------------------------

    def _obter_context(self):
        from playwright.sync_api import sync_playwright

        if self._context is not None and self._paginas_desde_relancamento >= _MAX_PAGINAS_ANTES_DE_RELANCAR:
            logger.info(
                "olx: relançando browser persistente após %d páginas",
                self._paginas_desde_relancamento,
            )
            self._fechar_browser()

        if self._context is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(user_agent=USER_AGENT)
            self._paginas_desde_relancamento = 0

        return self._context

    def _fechar_browser(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                logger.debug("olx: erro ignorado ao fechar browser persistente", exc_info=True)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                logger.debug("olx: erro ignorado ao parar playwright persistente", exc_info=True)
        self._context = None
        self._browser = None
        self._paginas_desde_relancamento = 0

    def _nova_pagina_playwright(self):
        try:
            return self._obter_context().new_page()
        except Exception:
            # o browser persistente pode ter caído (processo do
            # Chromium morreu, conexão fechada etc.) — descarta e
            # relança uma única vez.
            logger.warning("olx: browser persistente indisponível, relançando")
            self._fechar_browser()
            return self._obter_context().new_page()

    def _fetch_playwright(self, url: str) -> str:
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        except ImportError as exc:
            raise OlxCollectionError(
                "Modo playwright requer o pacote 'playwright' instalado "
                "(pip install playwright && playwright install chromium)."
            ) from exc

        timeout_ms = self.timeout_segundos * 1000

        pagina = self._nova_pagina_playwright()
        self._paginas_desde_relancamento += 1
        try:
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
        finally:
            pagina.close()

        return html
