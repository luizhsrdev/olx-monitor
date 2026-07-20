from __future__ import annotations

from typing import Protocol

from ..models import Anuncio


class Notifier(Protocol):
    """Interface de canal de alerta. Telegram é a única implementação
    hoje; Discord/e-mail/webhook plugam aqui depois sem tocar em
    filtro, dedupe ou scheduler."""

    def send(self, anuncio: Anuncio, monitor_nome: str, prioritario: bool) -> None:
        """Envia uma notificação para um anúncio já filtrado e
        deduplicado. Deve levantar exceção em caso de falha — quem
        chama decide como tratar (log + segue, sem derrubar o monitor)."""
        ...
