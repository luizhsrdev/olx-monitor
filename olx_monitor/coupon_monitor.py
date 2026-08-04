"""Monitor de cupons de desconto da OLX (https://www.olx.com.br/cupons).

Módulo deliberadamente separado dos monitores de produto: cupom não tem
preço, filtro de palavras nem faixa de valor, e o que se faz com ele é
copiar o código, não correr pra comprar. Reaproveita a infraestrutura já
existente — `OlxSource` pra buscar a página (requests + circuit breaker +
fallback playwright com browser persistente), `Store` pra dedupe (numa
tabela própria, `cupons_vistos`, não a de anúncios) e `TelegramNotifier`
pra avisar — mas com ciclo, extração e dedupe próprios; não força o
cupom dentro da abstração de `Anuncio`/`MonitorConfig`.

**A página de cupons usa RSC**, igual à listagem de anúncios — confirmado
depois inspecionando um `debug_cupons.html` com vários cupons reais (uma
suposição inicial de que era HTML puro, baseada numa amostra com só um
cupom, se mostrou errada). Os dados vêm num chunk `self.__next_f.push`,
num elemento React com um array `data` de objetos
`{"coupon","title","description","expiresAt","categoryId",...}` — dado
estruturado de verdade, não texto pra raspar de classe CSS. A extração
via HTML renderizado (`_dividir_em_cartoes` e cia) foi mantida como
fallback legado, tentada só se o RSC não render nada.

Achado importante ao migrar: **o mesmo código de cupom pode valer pra
várias categorias ao mesmo tempo** (ex.: "TECH5" com um card por
categoria). Por isso o dedupe usa a chave composta
`(codigo, categoria_id)`, não só `codigo` — ver `dedupe.py`.
"""

from __future__ import annotations

import logging
import random
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import rsc
from .alerts.telegram import TelegramNotifier
from .dedupe import Store
from .sources.olx import OlxSource

logger = logging.getLogger(__name__)

URL_CUPONS_PADRAO = "https://www.olx.com.br/cupons"
DEBUG_DUMP_PATH = Path("debug_cupons.html")

_BACKOFF_INICIAL_SEGUNDOS = 5
_BACKOFF_MAXIMO_SEGUNDOS = 600

# --- Formato legado (HTML renderizado, sem RSC) ------------------------
# Confirmados contra uma amostra real com um único cupom, antes de
# descobrirmos que a página também transmite via RSC. \w+ cobre o hash
# de build do CSS Modules.
_PADRAO_CARTAO = re.compile(r'class="container-outlined CouponCard_wrapper__')
_PADRAO_TITULO_LEGADO = re.compile(r'<h2 class="[^"]*CouponCard_title__\w+"[^>]*>([^<]+)</h2>')
_PADRAO_DESCRICAO_LEGADO = re.compile(r'</h2><p class="typo-caption undefined">([^<]+)</p>')
_PADRAO_CODIGO_LEGADO = re.compile(
    r'CouponCard_coupon__\w+[^>]*>\s*<p[^>]*uppercase"[^>]*>([^<]+)</p>'
)

# --- Formato RSC (primário) --------------------------------------------
_CHAVES_CODIGO = ("coupon", "code", "codigo")
_CHAVES_TITULO = ("title", "shorttitle", "titulo")
_CHAVES_DESCRICAO = ("description", "descricao")
_CHAVES_CATEGORIA_ID = ("categoryid", "categoria_id")
_CHAVES_EXPIRA_EM = ("expiresat", "expira_em")


@dataclass(frozen=True)
class Coupon:
    codigo: str
    titulo: str | None
    descricao: str | None
    categoria_id: str
    expira_em: datetime | None  # None = sem validade fixa (ex.: uso limitado por contagem)
    coletado_em: datetime


def _pega_por_chave(item: dict, candidatos: tuple[str, ...]):
    mapa = {k.lower(): v for k, v in item.items()}
    for candidato in candidatos:
        if candidato in mapa and mapa[candidato] not in (None, ""):
            return mapa[candidato]
    return None


def _parece_item_de_cupom(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    chaves = {k.lower() for k in item}
    return "coupon" in chaves and bool(chaves & {"title", "description", "shorttitle"})


def _encontrar_lista_de_cupons(node: object, vistos: set[int]) -> list[dict] | None:
    """Varre `node` recursivamente procurando uma lista cujos itens
    tenham cara de cupom. Não fixa caminho (`data`, `$L18`, etc. já
    mudaram de nome antes) — só a "cara" do item (tem `coupon` + título/
    descrição), mesmo princípio do scanner de anúncios em sources/olx.py.
    """
    node_id = id(node)
    if node_id in vistos:
        return None
    vistos.add(node_id)

    if isinstance(node, list):
        if node and all(_parece_item_de_cupom(item) for item in node):
            return node
        for item in node:
            encontrada = _encontrar_lista_de_cupons(item, vistos)
            if encontrada is not None:
                return encontrada
    elif isinstance(node, dict):
        for valor in node.values():
            encontrada = _encontrar_lista_de_cupons(valor, vistos)
            if encontrada is not None:
                return encontrada
    return None


def _extrair_cupons_brutos_rsc(html: str) -> list[dict]:
    for valor in rsc.iter_valores_rsc(html):
        # `vistos` novo por valor de nível superior — mesmo motivo já
        # documentado em sources/olx.py: reusar um set entre chunks
        # diferentes causou uma colisão real de id() no passado.
        lista = _encontrar_lista_de_cupons(valor, set())
        if lista is not None:
            return lista
    return []


def _parse_expira_em(valor: object) -> datetime | None:
    if not isinstance(valor, str) or not valor:
        return None
    texto = valor[:-1] + "+00:00" if valor.endswith("Z") else valor
    try:
        dt = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _montar_coupon_de_rsc(bruto: dict, agora: datetime) -> Coupon | None:
    codigo = _pega_por_chave(bruto, _CHAVES_CODIGO)
    if codigo is None:
        return None
    titulo = _pega_por_chave(bruto, _CHAVES_TITULO)
    descricao = _pega_por_chave(bruto, _CHAVES_DESCRICAO)
    categoria_id_bruta = _pega_por_chave(bruto, _CHAVES_CATEGORIA_ID)
    expira_em = _parse_expira_em(_pega_por_chave(bruto, _CHAVES_EXPIRA_EM))

    return Coupon(
        codigo=str(codigo).strip().upper(),
        titulo=str(titulo).strip() if titulo is not None else None,
        descricao=str(descricao).strip() if descricao is not None else None,
        categoria_id=str(categoria_id_bruta) if categoria_id_bruta is not None else "",
        expira_em=expira_em,
        coletado_em=agora,
    )


def _dividir_em_cartoes(html: str) -> list[str]:
    """[Formato legado] Corta o HTML em um bloco por cupom, usando o
    início de cada `CouponCard_wrapper__` como delimitador."""
    posicoes = [m.start() for m in _PADRAO_CARTAO.finditer(html)]
    blocos = []
    for i, inicio in enumerate(posicoes):
        fim = posicoes[i + 1] if i + 1 < len(posicoes) else len(html)
        blocos.append(html[inicio:fim])
    return blocos


def _extrair_cupons_legado(html: str, agora: datetime) -> list[Coupon]:
    """[Formato legado] Extração via HTML renderizado — tentada só se o
    RSC não devolver nada. Sem `categoria_id`/`expira_em` reais (não dá
    pra derivar de forma confiável do texto renderizado), então esses
    campos ficam com o default "sem categoria"/sem validade fixa."""
    cupons = []
    for bloco in _dividir_em_cartoes(html):
        match_codigo = _PADRAO_CODIGO_LEGADO.search(bloco)
        if not match_codigo:
            continue
        match_titulo = _PADRAO_TITULO_LEGADO.search(bloco)
        match_descricao = _PADRAO_DESCRICAO_LEGADO.search(bloco)
        cupons.append(
            Coupon(
                codigo=match_codigo.group(1).strip().upper(),
                titulo=match_titulo.group(1).strip() if match_titulo else None,
                descricao=match_descricao.group(1).strip() if match_descricao else None,
                categoria_id="",
                expira_em=None,
                coletado_em=agora,
            )
        )
    return cupons


def extract_coupons(html: str) -> list[Coupon]:
    """Extrai os cupons da página — tenta o formato RSC primeiro (dado
    estruturado, mais confiável), cai pro parser de HTML legado se o
    RSC não render nada (página bloqueada, ou a OLX removeu o RSC dessa
    página especificamente)."""
    agora = datetime.now(timezone.utc)

    cupons = []
    for bruto in _extrair_cupons_brutos_rsc(html):
        cupom = _montar_coupon_de_rsc(bruto, agora)
        if cupom is not None:
            cupons.append(cupom)
    if cupons:
        return cupons

    return _extrair_cupons_legado(html, agora)


def _extracao_parece_suspeita(cupons: list[Coupon]) -> bool:
    """True se dois cupons saíram com a mesma chave composta
    (codigo, categoria_id) — sinal de que a extração duplicou ou
    confundiu itens."""
    chaves = [(c.codigo, c.categoria_id) for c in cupons]
    return len(chaves) != len(set(chaves))


def _primeiro_valido(cupons: list[Coupon], agora: datetime) -> Coupon | None:
    """O primeiro cupom da lista (ordem de prioridade da própria OLX —
    não é garantia de "mais recente", só de "mais em destaque agora")
    que ainda não expirou. Cupons sem expira_em (uso limitado por
    contagem, não por data) são sempre considerados válidos."""
    for cupom in cupons:
        if cupom.expira_em is None or cupom.expira_em > agora:
            return cupom
    return None


class LatestCouponCache:
    """Guarda o cupom mais priorizado ainda visível na página de
    cupons — escrito pelo `CouponMonitor` a cada ciclo, lido pelo
    `TelegramNotifier` ao montar uma notificação de anúncio (injeção
    explícita, não import de estado global — ver `alerts/telegram.py`).

    Estado efêmero em memória, protegido por lock — se o processo
    reiniciar, começa vazio até o próximo ciclo do monitor de cupons
    rodar. Se o monitor de cupons estiver desativado, o cache nunca é
    escrito e fica vazio pra sempre — quem lê trata isso como "sem
    cupom disponível", sem erro."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cupom: Coupon | None = None

    def atualizar(self, cupom: Coupon | None) -> None:
        with self._lock:
            self._cupom = cupom

    def obter(self) -> Coupon | None:
        with self._lock:
            return self._cupom


class CouponMonitor:
    """Ciclo de polling da página de cupons — mesmo formato de loop dos
    monitores de produto (thread própria, backoff exponencial em falha,
    shutdown via `threading.Event`), sem nenhuma das etapas específicas
    de anúncio (filtro, prioridade, faixa de preço)."""

    def __init__(
        self,
        url: str,
        intervalo_segundos: int,
        jitter_segundos: int,
        source: OlxSource,
        notifier: TelegramNotifier,
        store: Store,
        stop_event: threading.Event,
        latest_coupon_cache: LatestCouponCache,
    ):
        self._url = url
        self._intervalo_segundos = intervalo_segundos
        self._jitter_segundos = jitter_segundos
        self._source = source
        self._notifier = notifier
        self._store = store
        self._stop_event = stop_event
        self._latest_coupon_cache = latest_coupon_cache
        self._backoff_segundos = _BACKOFF_INICIAL_SEGUNDOS
        self._dump_ja_salvo = False

    def run_forever(self) -> None:
        logger.info(
            "cupons: iniciado (intervalo=%ss, jitter=%ss, url=%s)",
            self._intervalo_segundos,
            self._jitter_segundos,
            self._url,
        )
        try:
            while not self._stop_event.is_set():
                try:
                    self._executar_ciclo()
                    self._backoff_segundos = _BACKOFF_INICIAL_SEGUNDOS
                    self._esperar_intervalo()
                except Exception:
                    logger.exception(
                        "cupons: falha no ciclo, aplicando backoff de %ss",
                        self._backoff_segundos,
                    )
                    self._esperar(self._backoff_segundos)
                    self._backoff_segundos = min(
                        self._backoff_segundos * 2, _BACKOFF_MAXIMO_SEGUNDOS
                    )
        finally:
            try:
                self._source.close()
            except Exception:
                logger.warning("cupons: erro ao fechar a fonte", exc_info=True)

    def _executar_ciclo(self) -> None:
        html = self._source.fetch_html(self._url)
        cupons = extract_coupons(html)

        if not cupons or _extracao_parece_suspeita(cupons):
            self._salvar_debug_dump(html, len(cupons))

        agora = datetime.now(timezone.utc)
        self._latest_coupon_cache.atualizar(_primeiro_valido(cupons, agora))

        primeira_execucao = self._store.eh_primeira_execucao_cupons()
        chaves = [(c.codigo, c.categoria_id) for c in cupons]
        chaves_novas = set(self._store.cupons_novos(chaves))
        novos = [c for c in cupons if (c.codigo, c.categoria_id) in chaves_novas]
        self._store.marcar_cupons_vistos(chaves)

        notificados = 0
        if primeira_execucao:
            logger.info(
                "cupons: primeira execução — %d cupom(ns) registrado(s) sem notificar",
                len(cupons),
            )
        else:
            for cupom in novos:
                try:
                    self._notifier.send_coupon(cupom)
                    notificados += 1
                except Exception:
                    logger.exception("cupons: falha ao notificar cupom '%s'", cupom.codigo)

        logger.info(
            "cupons: encontrados=%d novos=%d notificados=%d",
            len(cupons),
            len(novos),
            notificados,
        )

    def _salvar_debug_dump(self, html: str, n_cupons: int) -> None:
        if self._dump_ja_salvo:
            return
        try:
            DEBUG_DUMP_PATH.write_text(html, encoding="utf-8")
            self._dump_ja_salvo = True
            logger.warning(
                "cupons: HTML salvo em %s pra investigação (extração retornou 0 "
                "cupons ou contagem suspeita — %d cupom(ns) extraídos)",
                DEBUG_DUMP_PATH.resolve(),
                n_cupons,
            )
        except OSError:
            logger.debug("cupons: não deu pra salvar %s", DEBUG_DUMP_PATH, exc_info=True)

    def _esperar_intervalo(self) -> None:
        jitter = random.uniform(0, self._jitter_segundos) if self._jitter_segundos else 0
        self._esperar(self._intervalo_segundos + jitter)

    def _esperar(self, segundos: float) -> None:
        self._stop_event.wait(segundos)
