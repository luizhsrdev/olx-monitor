from __future__ import annotations

from typing import Protocol

from ..models import Anuncio
from ..seller_info import SellerInfo


class Notifier(Protocol):
    """Interface de canal de alerta. Telegram é a única implementação
    hoje; Discord/e-mail/webhook plugam aqui depois sem tocar em
    filtro, dedupe ou scheduler.

    Fluxo em duas etapas, pra não atrasar a notificação esperando dados
    do vendedor: `send()` notifica imediatamente (rápido, sem dados do
    vendedor) e retorna um identificador de mensagem; `update()`
    enriquece essa mesma mensagem depois, quando/se os dados do
    vendedor chegarem. Um canal que não suporte edição pode retornar
    None em `send()` — nesse caso `update()` nunca é chamado pra essa
    notificação.
    """

    def send(self, anuncio: Anuncio, monitor_nome: str, termos_prioritarios: list[str]) -> str | None:
        """Envia uma notificação para um anúncio já filtrado e
        deduplicado. Retorna um identificador de mensagem (usado por
        `update()` depois) ou None se o canal não suportar edição.
        Deve levantar exceção em caso de falha — quem chama decide
        como tratar (log + segue, sem derrubar o monitor)."""
        ...

    def update(
        self,
        message_id: str,
        anuncio: Anuncio,
        monitor_nome: str,
        termos_prioritarios: list[str],
        seller_info: SellerInfo | None,
    ) -> None:
        """Atualiza uma mensagem já enviada com dados do vendedor
        (ou None, se a busca falhou). Nunca deve levantar exceção —
        falha silenciosamente (log + segue): a mensagem original já é
        válida e completa o suficiente sem esse enriquecimento."""
        ...
