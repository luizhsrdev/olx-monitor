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

import requests
from dotenv import load_dotenv

from .alerts.telegram import TelegramNotifier
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
        notifier.send(anuncio_fake, monitor_nome="Teste de credenciais", prioritario=True)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        detalhe = exc.response.text if exc.response is not None else str(exc)
        print(f"Falha ao enviar (HTTP {status}): {detalhe}")
        print(
            "Erros comuns: token errado -> 401 Unauthorized; "
            "chat_id errado -> 'Bad Request: chat not found'."
        )
        raise SystemExit(1) from exc
    except requests.RequestException as exc:
        print(f"Falha de rede ao enviar: {exc}")
        raise SystemExit(1) from exc

    print("Mensagem enviada. Confira o Telegram.")


if __name__ == "__main__":
    main()
