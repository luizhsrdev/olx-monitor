from __future__ import annotations

import logging
import sys


def configurar_logging(nivel: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, nivel.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(threadName)-24s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
