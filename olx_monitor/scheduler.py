from __future__ import annotations

import logging
import random
import threading
from datetime import datetime, timezone
from typing import Callable

from .alerts.base import Notifier
from .config import MonitorConfig
from .dedupe import Store
from .filters import aplicar_filtros
from .models import Anuncio
from .normalize import normalize
from .sources.base import Source

logger = logging.getLogger(__name__)

_PAUSA_MIN_ENTRE_URLS_SEGUNDOS = 1.0
_PAUSA_MAX_ENTRE_URLS_SEGUNDOS = 3.0
_BACKOFF_INICIAL_SEGUNDOS = 5
_BACKOFF_MAXIMO_SEGUNDOS = 600

FabricarSource = Callable[[MonitorConfig], Source]


class MonitorRunner:
    """Executa o loop coleta -> normaliza -> filtra -> dedupe -> alerta
    de um único monitor. Cada instância roda na sua própria thread, com
    seu próprio intervalo — um monitor lento nunca atrasa um rápido, e
    uma falha aqui nunca derruba os outros monitores nem o processo."""

    def __init__(
        self,
        monitor: MonitorConfig,
        source: Source,
        notifier: Notifier,
        store: Store,
        bloqueadas_globais: list[str],
        stop_event: threading.Event,
    ):
        self._monitor = monitor
        self._source = source
        self._notifier = notifier
        self._store = store
        self._bloqueadas_globais = bloqueadas_globais
        self._stop_event = stop_event
        self._backoff_segundos = _BACKOFF_INICIAL_SEGUNDOS

    def run_forever(self) -> None:
        logger.info(
            "monitor '%s': iniciado (intervalo=%ss, jitter=%ss, urls=%d, modo=%s)",
            self._monitor.nome,
            self._monitor.intervalo_segundos,
            self._monitor.jitter_segundos,
            len(self._monitor.urls),
            self._monitor.modo,
        )
        while not self._stop_event.is_set():
            try:
                self._executar_ciclo()
                self._backoff_segundos = _BACKOFF_INICIAL_SEGUNDOS
            except Exception:
                logger.exception(
                    "monitor '%s': falha no ciclo, aplicando backoff de %ss",
                    self._monitor.nome,
                    self._backoff_segundos,
                )
                self._esperar(self._backoff_segundos)
                self._backoff_segundos = min(self._backoff_segundos * 2, _BACKOFF_MAXIMO_SEGUNDOS)
                continue

            self._esperar_intervalo()

    def _executar_ciclo(self) -> None:
        coletados: list[Anuncio] = []
        for indice, url in enumerate(self._monitor.urls):
            brutos = self._source.collect(url)
            agora = datetime.now(timezone.utc)
            for bruto in brutos:
                anuncio = normalize(self._monitor.fonte, bruto, agora)
                if anuncio is not None:
                    coletados.append(anuncio)
            if indice < len(self._monitor.urls) - 1:
                self._esperar(
                    random.uniform(_PAUSA_MIN_ENTRE_URLS_SEGUNDOS, _PAUSA_MAX_ENTRE_URLS_SEGUNDOS)
                )

        resultados = aplicar_filtros(coletados, self._monitor, self._bloqueadas_globais)
        aceitos = [r.anuncio for r in resultados if r.aceito]
        termos_prioritarios_por_id = {
            r.anuncio.id: r.termos_prioritarios for r in resultados if r.aceito
        }

        motivos_descarte: dict[str, int] = {}
        for r in resultados:
            if not r.aceito:
                motivos_descarte[r.motivo] = motivos_descarte.get(r.motivo, 0) + 1

        primeira_execucao = self._store.eh_primeira_execucao(self._monitor.nome)
        novos = self._store.filtrar_novos(self._monitor.nome, aceitos)
        self._store.marcar_vistos(self._monitor.nome, aceitos)

        notificados = 0
        if primeira_execucao:
            logger.info(
                "monitor '%s': primeira execução — %d anuncio(s) registrado(s) sem notificar",
                self._monitor.nome,
                len(aceitos),
            )
        else:
            for anuncio in novos:
                try:
                    self._notifier.send(
                        anuncio,
                        self._monitor.nome,
                        termos_prioritarios_por_id.get(anuncio.id, []),
                    )
                    notificados += 1
                except Exception:
                    logger.exception(
                        "monitor '%s': falha ao notificar anuncio id=%s",
                        self._monitor.nome,
                        anuncio.id,
                    )

        logger.info(
            "monitor '%s': coletados=%d aceitos=%d descartados=%d (%s) novos=%d notificados=%d",
            self._monitor.nome,
            len(coletados),
            len(aceitos),
            len(coletados) - len(aceitos),
            motivos_descarte,
            len(novos),
            notificados,
        )

    def _esperar_intervalo(self) -> None:
        jitter = random.uniform(0, self._monitor.jitter_segundos) if self._monitor.jitter_segundos else 0
        self._esperar(self._monitor.intervalo_segundos + jitter)

    def _esperar(self, segundos: float) -> None:
        self._stop_event.wait(segundos)


def iniciar_monitores(
    monitores: list[MonitorConfig],
    fabricar_source: FabricarSource,
    notifier: Notifier,
    store: Store,
    bloqueadas_globais: list[str],
    stop_event: threading.Event,
) -> list[threading.Thread]:
    """Sobe uma thread daemon por monitor ativo e retorna as threads
    iniciadas (monitores com ativo=false são pulados)."""
    threads: list[threading.Thread] = []
    for monitor in monitores:
        if not monitor.ativo:
            logger.info("monitor '%s': ativo=false, ignorando", monitor.nome)
            continue
        source = fabricar_source(monitor)
        runner = MonitorRunner(monitor, source, notifier, store, bloqueadas_globais, stop_event)
        thread = threading.Thread(
            target=runner.run_forever, name=f"monitor-{monitor.nome}", daemon=True
        )
        thread.start()
        threads.append(thread)
    return threads
