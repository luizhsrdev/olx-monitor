from __future__ import annotations

import html
import logging

import requests

from ..models import Anuncio

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Envia alertas via API HTTP do Bot do Telegram (sendMessage)."""

    def __init__(self, token: str, chat_id: str, timeout_segundos: int = 10):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._timeout_segundos = timeout_segundos

    def send(self, anuncio: Anuncio, monitor_nome: str, prioritario: bool) -> None:
        texto = self._montar_mensagem(anuncio, monitor_nome, prioritario)
        resposta = requests.post(
            self._url,
            json={
                "chat_id": self._chat_id,
                "text": texto,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=self._timeout_segundos,
        )
        resposta.raise_for_status()

    @staticmethod
    def _montar_mensagem(anuncio: Anuncio, monitor_nome: str, prioritario: bool) -> str:
        marcador = "🔥 <b>PRIORITÁRIO</b>" if prioritario else "🔔 Novo anúncio"
        titulo = html.escape(anuncio.titulo)
        preco = _formatar_preco_brl(anuncio.preco)
        local = html.escape(anuncio.local) if anuncio.local else "local não informado"
        monitor_escapado = html.escape(monitor_nome)

        return (
            f"{marcador}\n"
            f"<b>{titulo}</b>\n"
            f"💰 {preco}\n"
            f"📍 {local}\n"
            f"🔎 Monitor: {monitor_escapado}\n"
            f'<a href="{anuncio.url}">Ver anúncio</a>'
        )


def _formatar_preco_brl(preco: float | None) -> str:
    if preco is None:
        return "preço não informado"
    texto = f"{preco:,.2f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"
