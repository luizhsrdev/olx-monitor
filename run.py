from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from types import FrameType

from dotenv import load_dotenv

from olx_monitor.alerts.telegram import TelegramNotifier
from olx_monitor.config import AppConfig, ConfigError, MonitorConfig, load_config
from olx_monitor.dedupe import Store
from olx_monitor.logging_setup import configurar_logging
from olx_monitor.scheduler import iniciar_monitores
from olx_monitor.sources.base import Source
from olx_monitor.sources.olx import OlxSource

logger = logging.getLogger(__name__)


# Toggle temporário de depuração: roda o Chromium do fallback playwright
# com janela visível, para checar manualmente se o desafio anti-bot da
# Cloudflare se comporta diferente com um navegador não-headless. Não é
# uma opção de config permanente (não existe em monitores.yaml de
# propósito) — requer um ambiente com display, então não use em deploy.
_PLAYWRIGHT_HEADLESS = os.environ.get("OLX_MONITOR_PLAYWRIGHT_HEADLESS", "true").strip().lower() not in (
    "0",
    "false",
    "no",
)


def _fabricar_source(monitor: MonitorConfig) -> Source:
    if monitor.fonte == "olx":
        return OlxSource(modo=monitor.modo, headless=_PLAYWRIGHT_HEADLESS)
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
    notifier = TelegramNotifier(config.telegram.token, config.telegram.chat_id)
    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        logger.info("Sinal %s recebido, encerrando monitores...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    threads = iniciar_monitores(
        config.monitores, _fabricar_source, notifier, store, config.bloqueadas_globais, stop_event
    )
    if not threads:
        logger.warning("Nenhum monitor ativo em %s. Encerrando.", args.config)
        store.close()
        return

    logger.info("%d monitor(es) ativo(s). Pressione Ctrl+C para encerrar.", len(threads))
    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
