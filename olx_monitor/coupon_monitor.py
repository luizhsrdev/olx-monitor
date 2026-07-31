"""Monitor de cupons de desconto da OLX (https://www.olx.com.br/cupons).

Módulo deliberadamente separado dos monitores de produto: cupom não tem
preço, filtro de palavras nem faixa de valor, e o que se faz com ele é
copiar o código, não correr pra comprar. Reaproveita a infraestrutura já
existente — `OlxSource` pra buscar a página (requests + circuit breaker +
fallback playwright com browser persistente), `Store` pra dedupe (numa
tabela própria, `cupons_vistos`, não a de anúncios) e `TelegramNotifier`
pra avisar — mas com ciclo, extração e dedupe próprios; não força o
cupom dentro da abstração de `Anuncio`/`MonitorConfig`.

A página de cupons usa HTML renderizado puro, **não RSC** — confirmado
inspecionando um `debug_cupons.html` real (zero ocorrências de
`self.__next_f.push`; ver `sources/olx.py`/`seller_info.py` pro contraste
com a listagem, que É RSC). Cada cupom fica dentro de um
`<div class="... CouponCard_wrapper__<hash>">` (hash de build do CSS
Modules — só o prefixo estável antes do hash é usado pra delimitar
cartões e localizar campos, mesmo padrão de resiliência já usado em
`seller_info.py` pros ícones de verificação).

A extração foi validada contra uma amostra real com **um único cupom**
na página — o corte entre múltiplos `CouponCard_wrapper__` consecutivos
(`_dividir_em_cartoes`) não foi testado contra dado real, só contra um
fragmento sintético (ver `tests/test_coupon_monitor.py`). Por isso
`_extracao_parece_suspeita` salva `debug_cupons.html` automaticamente
sempre que a extração vier vazia ou a contagem parecer estranha — no dia
que a página tiver vários cupons de verdade, esse arquivo permite validar
(e corrigir, se precisar) o corte contra dado real.
"""

from __future__ import annotations

import logging
import random
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .alerts.telegram import TelegramNotifier
from .dedupe import Store
from .sources.olx import OlxSource

logger = logging.getLogger(__name__)

URL_CUPONS_PADRAO = "https://www.olx.com.br/cupons"
DEBUG_DUMP_PATH = Path("debug_cupons.html")

_BACKOFF_INICIAL_SEGUNDOS = 5
_BACKOFF_MAXIMO_SEGUNDOS = 600

# Confirmados contra uma amostra real (ver docstring do módulo). \w+
# cobre o hash de build do CSS Modules, que muda só quando a OLX
# reimplanta o frontend — o prefixo semântico (CouponCard_title__ etc.)
# é a parte estável.
_PADRAO_CARTAO = re.compile(r'class="container-outlined CouponCard_wrapper__')
_PADRAO_TITULO = re.compile(r'<h2 class="[^"]*CouponCard_title__\w+"[^>]*>([^<]+)</h2>')
_PADRAO_DESCRICAO = re.compile(r'</h2><p class="typo-caption undefined">([^<]+)</p>')
_PADRAO_CODIGO = re.compile(r'CouponCard_coupon__\w+[^>]*>\s*<p[^>]*uppercase"[^>]*>([^<]+)</p>')
_PADRAO_VALIDADE = re.compile(
    r'CouponCard_footer__\w+"[^>]*>.*?<p class="typo-caption"[^>]*>([^<]+)</p>', re.DOTALL
)


@dataclass(frozen=True)
class Coupon:
    codigo: str
    titulo: str | None
    descricao: str | None
    validade: str | None
    coletado_em: datetime


def _dividir_em_cartoes(html: str) -> list[str]:
    """Corta o HTML em um bloco por cupom, usando o início de cada
    `CouponCard_wrapper__` como delimitador — cada bloco vai do início
    de um cartão até o início do próximo (ou o fim do documento, pro
    último)."""
    posicoes = [m.start() for m in _PADRAO_CARTAO.finditer(html)]
    blocos = []
    for i, inicio in enumerate(posicoes):
        fim = posicoes[i + 1] if i + 1 < len(posicoes) else len(html)
        blocos.append(html[inicio:fim])
    return blocos


def _extrair_codigo(bloco: str) -> str | None:
    match = _PADRAO_CODIGO.search(bloco)
    return match.group(1).strip().upper() if match else None


def _extrair_titulo(bloco: str) -> str | None:
    match = _PADRAO_TITULO.search(bloco)
    return match.group(1).strip() if match else None


def _extrair_descricao(bloco: str) -> str | None:
    match = _PADRAO_DESCRICAO.search(bloco)
    return match.group(1).strip() if match else None


def _extrair_validade(bloco: str) -> str | None:
    match = _PADRAO_VALIDADE.search(bloco)
    return match.group(1).strip() if match else None


def extract_coupons(html: str) -> list[Coupon]:
    """Extrai os cupons do HTML renderizado da página de cupons.

    Cada campo é extraído independentemente — um campo faltando não
    invalida o cupom inteiro, só aquele campo fica `None`. Um cartão
    sem código identificável é descartado (sem código não dá pra
    deduplicar nem notificar de forma útil)."""
    agora = datetime.now(timezone.utc)
    cupons = []
    for bloco in _dividir_em_cartoes(html):
        codigo = _extrair_codigo(bloco)
        if codigo is None:
            continue
        cupons.append(
            Coupon(
                codigo=codigo,
                titulo=_extrair_titulo(bloco),
                descricao=_extrair_descricao(bloco),
                validade=_extrair_validade(bloco),
                coletado_em=agora,
            )
        )
    return cupons


def _extracao_parece_suspeita(n_cartoes: int, cupons: list[Coupon]) -> bool:
    """True se algo no resultado merece uma segunda olhada: um cartão
    encontrado não virou um `Coupon` válido (perdeu o código no
    caminho), ou dois cupons saíram com o mesmo código — sinal de que o
    corte entre cartões vazou conteúdo de um pro outro."""
    if len(cupons) != n_cartoes:
        return True
    codigos = [c.codigo for c in cupons]
    return len(codigos) != len(set(codigos))


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
    ):
        self._url = url
        self._intervalo_segundos = intervalo_segundos
        self._jitter_segundos = jitter_segundos
        self._source = source
        self._notifier = notifier
        self._store = store
        self._stop_event = stop_event
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
        n_cartoes = len(_dividir_em_cartoes(html))
        cupons = extract_coupons(html)

        if not cupons or _extracao_parece_suspeita(n_cartoes, cupons):
            self._salvar_debug_dump(html, n_cartoes)

        primeira_execucao = self._store.eh_primeira_execucao_cupons()
        codigos_novos = set(self._store.codigos_novos([c.codigo for c in cupons]))
        novos = [c for c in cupons if c.codigo in codigos_novos]
        self._store.marcar_codigos_vistos([c.codigo for c in cupons])

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

    def _salvar_debug_dump(self, html: str, n_cartoes: int) -> None:
        if self._dump_ja_salvo:
            return
        try:
            DEBUG_DUMP_PATH.write_text(html, encoding="utf-8")
            self._dump_ja_salvo = True
            logger.warning(
                "cupons: HTML salvo em %s pra investigação (extração retornou 0 "
                "cupons ou contagem suspeita — %d cartão(ões) encontrados no HTML)",
                DEBUG_DUMP_PATH.resolve(),
                n_cartoes,
            )
        except OSError:
            logger.debug("cupons: não deu pra salvar %s", DEBUG_DUMP_PATH, exc_info=True)

    def _esperar_intervalo(self) -> None:
        jitter = random.uniform(0, self._jitter_segundos) if self._jitter_segundos else 0
        self._esperar(self._intervalo_segundos + jitter)

    def _esperar(self, segundos: float) -> None:
        self._stop_event.wait(segundos)
