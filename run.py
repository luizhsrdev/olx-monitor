from __future__ import annotations

import argparse
import logging
import signal
import threading
from types import FrameType

from dotenv import load_dotenv

from olx_monitor.alerts.telegram import TelegramNotifier
from olx_monitor.config import AppConfig, ConfigError, MonitorConfig, load_config
from olx_monitor.coupon_monitor import CouponMonitor, LatestCouponCache
from olx_monitor.dedupe import Store
from olx_monitor.enrichment import SellerEnricher
from olx_monitor.logging_setup import configurar_logging
from olx_monitor.scheduler import iniciar_monitores
from olx_monitor.sources.base import Source
from olx_monitor.sources.olx import OlxSource

logger = logging.getLogger(__name__)

_JOIN_TIMEOUT_SEGUNDOS = 35


def _fabricar_source(monitor: MonitorConfig) -> Source:
    if monitor.fonte == "olx":
        return OlxSource(modo=monitor.modo)
    raise ValueError(f"Fonte não suportada: {monitor.fonte}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor de anúncios de marketplace com alertas no Telegram."
    )
    parser.add_argument(
        "--config", default="monitores.yaml", help="Caminho do monitores.yaml (default: %(default)s)"
    )
    parser.add_argument(
        "--db", default="olx_monitor.db", help="Caminho do banco SQLite de dedupe (default: %(default)s)"
    )
    parser.add_argument("--log-level", default="INFO", help="Nível de log (default: %(default)s)")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = _parse_args()
    configurar_logging(args.log_level)

    try:
        config: AppConfig = load_config(args.config)
    except ConfigError as exc:
        logger.error("Erro de configuração: %s", exc)
        raise SystemExit(1) from exc

    store = Store(args.db)
    removidos = store.limpar_antigos()
    if removidos:
        logger.info("Limpeza do banco: %d registro(s) antigo(s) removido(s).", removidos)

    # Sempre criado, mesmo que o monitor de cupons esteja desativado —
    # é só um objeto em memória, custo zero. Se nunca for escrito (ver
    # abaixo), fica vazio pra sempre e a seção de cupom simplesmente
    # nunca aparece nas notificações — sem exigir que o monitor de
    # cupons exista.
    latest_coupon_cache = LatestCouponCache()
    notifier = TelegramNotifier(
        config.telegram.token, config.telegram.chat_id, latest_coupon_cache=latest_coupon_cache
    )

    # Worker global de enriquecimento (dados do vendedor) — uma fila +
    # uma thread + um browser Playwright dedicado, compartilhados entre
    # todos os monitores. Ver enrichment.py para o porquê de não ser
    # por-monitor nem na própria thread do monitor.
    enricher = SellerEnricher()
    enricher.start()

    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        logger.info("Sinal %s recebido, encerrando monitores...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    threads = iniciar_monitores(
        config.monitores,
        _fabricar_source,
        notifier,
        store,
        config.bloqueadas_globais,
        stop_event,
        enricher,
    )

    # Monitor de cupons: módulo separado dos monitores de produto (não
    # tem preço/filtro/faixa de valor), mas entra na mesma lista de
    # threads pra herdar o shutdown/join já existente abaixo de graça.
    if config.cupons is not None and config.cupons.ativo:
        fonte_cupons = OlxSource(modo="requests")
        cupom_monitor = CouponMonitor(
            url=config.cupons.url,
            intervalo_segundos=config.cupons.intervalo_segundos,
            jitter_segundos=config.padroes.jitter_segundos,
            source=fonte_cupons,
            notifier=notifier,
            store=store,
            stop_event=stop_event,
            latest_coupon_cache=latest_coupon_cache,
        )
        thread_cupons = threading.Thread(
            target=cupom_monitor.run_forever, name="monitor-cupons", daemon=True
        )
        thread_cupons.start()
        threads.append(thread_cupons)
    elif config.cupons is not None:
        logger.info("monitor de cupons: ativo=false, ignorando")

    if not threads:
        logger.warning("Nenhum monitor ativo em %s. Encerrando.", args.config)
        enricher.stop()
        store.close()
        return

    logger.info("%d monitor(es) ativo(s). Pressione Ctrl+C para encerrar.", len(threads))
    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        logger.info("Encerrando: aguardando monitores finalizarem o ciclo atual...")
        for thread in threads:
            thread.join(timeout=_JOIN_TIMEOUT_SEGUNDOS)
            if thread.is_alive():
                logger.warning(
                    "'%s' não finalizou em %ss, encerrando mesmo assim",
                    thread.name,
                    _JOIN_TIMEOUT_SEGUNDOS,
                )
        # Drena o que sobrou na fila de enriquecimento (até o timeout
        # dele) e fecha o browser dedicado do worker.
        enricher.stop()
        # só fecha o banco depois que (o quanto possível) nenhuma thread
        # de monitor ainda está usando a conexão — Store.close() por si
        # só serializa contra escritas em andamento, mas sem dar essa
        # chance primeiro corríamos o risco de fechar no meio de um ciclo.
        store.close()


if __name__ == "__main__":
    main()
