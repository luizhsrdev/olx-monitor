"""Comando temporário para testar as credenciais do Telegram sem subir o
monitor de verdade. Envia um anúncio fake pelo TelegramNotifier já
implementado, usando TELEGRAM_TOKEN/TELEGRAM_CHAT_ID do .env.

Uso:
    python -m olx_monitor.test_telegram

Não faz parte do pipeline — é só uma ferramenta de depuração manual do
setup inicial. Pode ser removido quando não for mais necessário.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from dotenv import load_dotenv

from .alerts.telegram import TelegramNotifier, TelegramSendError
from .models import Anuncio


def main() -> None:
    load_dotenv()

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print(
            "TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID não encontrados no ambiente. "
            "Confira seu .env (veja .env.example)."
        )
        raise SystemExit(1)

    anuncio_fake = Anuncio(
        id="teste-123",
        titulo="[TESTE] PlayStation 5 lacrado, 1TB",
        preco=2500.0,
        url="https://www.olx.com.br/item/teste-123",
        local="São Paulo, SP",
        fonte="olx",
        publicado_em=None,
        coletado_em=datetime.now(timezone.utc),
    )

    notifier = TelegramNotifier(token, chat_id)

    print(f"Enviando mensagem de teste para chat_id={chat_id}...")
    try:
        message_id = notifier.send(
            anuncio_fake, monitor_nome="Teste de credenciais", termos_prioritarios=["teste"]
        )
    except TelegramSendError as exc:
        print(f"Falha ao enviar: {exc}")
        print(
            "Erros comuns: token errado -> HTTP 401 Unauthorized; "
            "chat_id errado -> 'Bad Request: chat not found'."
        )
        raise SystemExit(1) from exc

    print(f"Mensagem enviada (message_id={message_id}). Confira o Telegram.")
    print(
        "Ela vai ficar mostrando '⏳ Buscando dados do vendedor...' pra sempre — "
        "esse teste não passa pelo worker de enriquecimento real (rode `python run.py` "
        "pra ver o fluxo completo de notificação + atualização)."
    )


if __name__ == "__main__":
    main()
