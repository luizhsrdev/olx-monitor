"""Worker em background que busca dados do vendedor (Frente 3 do
enriquecimento assíncrono) e atualiza a notificação já enviada, sem
atrasar o ciclo de coleta do monitor que a disparou.

Uma fila + uma thread única, **compartilhadas entre todos os
monitores** — não um browser Playwright por monitor, nem a mesma
thread do monitor. Isso é deliberado: o browser persistente de cada
`OlxSource` (ver `sources/olx.py`) só pode ser usado pela thread que o
criou (API síncrona do Playwright, sem thread-safety entre threads
diferentes). Enriquecer na própria thread do monitor, logo após
enviar, atrasaria o próximo ciclo dele em dezenas de segundos toda vez
que aparecessem vários anúncios novos juntos — exatamente o tipo de
latência que essa frente inteira existe pra eliminar. Um worker global
com seu próprio browser dedicado resolve os dois problemas ao mesmo
tempo: nenhum ciclo de monitor é atrasado, e só um browser extra
existe no total (não um por monitor).
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path

from .alerts.base import Notifier
from .models import Anuncio
from .seller_info import extract_seller_info
from .sources.olx import OlxSource

logger = logging.getLogger(__name__)

# Timeout curto e independente do timeout das buscas de listagem — se
# a página do anúncio não responder rápido, desiste e a notificação
# fica sem dados do vendedor (ela já é válida e completa sem isso).
_TIMEOUT_BUSCA_VENDEDOR_SEGUNDOS = 10

DEBUG_SELLER_DUMP_PATH = Path("debug_seller.html")


@dataclass
class _TarefaEnriquecimento:
    anuncio: Anuncio
    monitor_nome: str
    termos_prioritarios: list[str]
    message_id: str
    notifier: Notifier


class SellerEnricher:
    """Fila + thread de background únicas para todo o processo. Uso:
    `start()` no início do processo, `enqueue()` toda vez que um `send()`
    retornar um `message_id`, `stop()` no shutdown."""

    def __init__(self) -> None:
        self._fila: queue.Queue[_TarefaEnriquecimento | None] = queue.Queue()
        self._source = OlxSource(modo="requests", timeout_segundos=_TIMEOUT_BUSCA_VENDEDOR_SEGUNDOS)
        self._thread: threading.Thread | None = None
        self._dump_ja_salvo = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="seller-enricher", daemon=True)
        self._thread.start()

    def enqueue(
        self,
        anuncio: Anuncio,
        monitor_nome: str,
        termos_prioritarios: list[str],
        message_id: str,
        notifier: Notifier,
    ) -> None:
        self._fila.put(
            _TarefaEnriquecimento(anuncio, monitor_nome, termos_prioritarios, message_id, notifier)
        )

    def stop(self, timeout: float = 15) -> None:
        self._fila.put(None)  # sentinela de parada
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning(
                    "seller-enricher: não encerrou em %ss, seguindo mesmo assim", timeout
                )
        self._source.close()

    def _run(self) -> None:
        logger.info("seller-enricher: iniciado")
        while True:
            tarefa = self._fila.get()
            if tarefa is None:
                break
            self._processar(tarefa)
        logger.info("seller-enricher: encerrado")

    def _processar(self, tarefa: _TarefaEnriquecimento) -> None:
        seller_info = None
        try:
            html = self._source.fetch_html(tarefa.anuncio.url)
            seller_info = extract_seller_info(html)
            if seller_info is None:
                logger.warning(
                    "seller-enricher: não achou dados do vendedor em %s", tarefa.anuncio.url
                )
                self._salvar_debug_dump(html)
        except Exception:
            logger.warning(
                "seller-enricher: falha buscando vendedor de %s",
                tarefa.anuncio.url,
                exc_info=True,
            )

        try:
            tarefa.notifier.update(
                tarefa.message_id,
                tarefa.anuncio,
                tarefa.monitor_nome,
                tarefa.termos_prioritarios,
                seller_info,
            )
        except Exception:
            # notifier.update() já não deveria levantar (é parte do
            # contrato do Notifier), mas essa é a última linha de
            # defesa — enriquecimento nunca pode derrubar o worker.
            logger.warning("seller-enricher: notifier.update falhou", exc_info=True)

    def _salvar_debug_dump(self, html: str) -> None:
        if self._dump_ja_salvo:
            return
        try:
            DEBUG_SELLER_DUMP_PATH.write_text(html, encoding="utf-8")
            self._dump_ja_salvo = True
            logger.warning(
                "seller-enricher: HTML da página do anúncio salvo em %s pra investigação "
                "(a extração dos dados do vendedor não achou nada com confiança suficiente)",
                DEBUG_SELLER_DUMP_PATH.resolve(),
            )
        except OSError:
            logger.debug("seller-enricher: não deu pra salvar %s", DEBUG_SELLER_DUMP_PATH, exc_info=True)
