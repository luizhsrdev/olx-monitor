from __future__ import annotations

import html
import logging

import requests

from ..models import Anuncio
from ..seller_info import SellerInfo

logger = logging.getLogger(__name__)


class TelegramSendError(Exception):
    """Erro ao enviar mensagem via Telegram (sendMessage).

    Mensagem sempre sanitizada (status HTTP + corpo da resposta do
    Telegram) — nunca a exceção crua do `requests`, que inclui a URL
    completa da requisição. Como o token do bot fica embutido nessa URL
    (`.../bot<token>/sendMessage`), deixar a exceção original vazar até
    um `logger.exception()` gravaria o token em texto claro no log.
    """


_ROTULOS_VERIFICACAO = {
    "email": "E-mail",
    "telefone": "Telefone",
    "identidade": "Identidade",
    "facebook": "Facebook",
}


class TelegramNotifier:
    """Envia alertas via API HTTP do Bot do Telegram.

    Fluxo em duas etapas: `send()` notifica imediatamente, sem dados do
    vendedor (a busca deles pode levar segundos — não faz sentido
    segurar a notificação por causa disso, é exatamente a janela em
    que se perde o anúncio pra outro comprador). `update()` edita essa
    mesma mensagem depois via `editMessageText`, quando/se os dados do
    vendedor chegarem (ver `enrichment.py`).
    """

    def __init__(self, token: str, chat_id: str, timeout_segundos: int = 10):
        self._url_send = f"https://api.telegram.org/bot{token}/sendMessage"
        self._url_edit = f"https://api.telegram.org/bot{token}/editMessageText"
        self._chat_id = chat_id
        self._timeout_segundos = timeout_segundos

    def send(self, anuncio: Anuncio, monitor_nome: str, termos_prioritarios: list[str]) -> str | None:
        texto = _montar_corpo(anuncio, monitor_nome, termos_prioritarios) + (
            "\n\n⏳ Buscando dados do vendedor..."
        )
        resposta = self._chamar_api(self._url_send, {"text": texto})

        try:
            return str(resposta.json()["result"]["message_id"])
        except (ValueError, KeyError):
            logger.warning(
                "olx: resposta do Telegram sem message_id — não será possível "
                "atualizar essa mensagem com os dados do vendedor depois"
            )
            return None

    def update(
        self,
        message_id: str,
        anuncio: Anuncio,
        monitor_nome: str,
        termos_prioritarios: list[str],
        seller_info: SellerInfo | None,
    ) -> None:
        corpo = _montar_corpo(anuncio, monitor_nome, termos_prioritarios)
        bloco_vendedor = (
            _montar_bloco_vendedor(seller_info)
            if seller_info is not None
            else "👤 Dados do vendedor indisponíveis"
        )
        texto = f"{corpo}\n\n{bloco_vendedor}"

        try:
            message_id_int = int(message_id)
        except ValueError:
            logger.warning("olx: message_id '%s' inválido, não dá pra editar", message_id)
            return

        try:
            self._chamar_api(
                self._url_edit, {"message_id": message_id_int, "text": texto}
            )
        except TelegramSendError as exc:
            # update() nunca propaga: a mensagem original (enviada em
            # send()) já é válida e completa sem o enriquecimento —
            # só loga e segue, sem retry.
            logger.warning("olx: falha ao editar mensagem %s: %s", message_id, exc)

    def _chamar_api(self, url: str, campos: dict) -> requests.Response:
        try:
            resposta = requests.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": False,
                    **campos,
                },
                timeout=self._timeout_segundos,
            )
            resposta.raise_for_status()
        except requests.RequestException as exc:
            # `from None` suprime o encadeamento: a exceção original do
            # `requests` (que contém a URL/token em str(exc)) nunca deve
            # aparecer num traceback logado.
            raise TelegramSendError(_mensagem_erro_sanitizada(exc)) from None
        return resposta


def _montar_corpo(anuncio: Anuncio, monitor_nome: str, termos_prioritarios: list[str]) -> str:
    if termos_prioritarios:
        termos_escapados = ", ".join(html.escape(t) for t in termos_prioritarios)
        marcador = f"🔥 <b>PRIORITÁRIO</b> ({termos_escapados})"
    else:
        marcador = "🔔 Novo anúncio"
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
        f'🔗 <a href="{anuncio.url}">Ver anúncio</a>'
    )


def _montar_bloco_vendedor(info: SellerInfo) -> str:
    linhas = []
    if info.nome:
        linhas.append(f"👤 Vendedor: {html.escape(info.nome)}")
    if info.membro_desde:
        linhas.append(f"📅 Na OLX desde {html.escape(info.membro_desde)}")
    if info.conta_verificada is True:
        linhas.append("✅ Conta verificada")
    elif info.conta_verificada is False:
        linhas.append("❌ Conta não verificada")

    verificados = [_ROTULOS_VERIFICACAO.get(k, k) for k, v in info.verificacoes.items() if v]
    nao_verificados = [_ROTULOS_VERIFICACAO.get(k, k) for k, v in info.verificacoes.items() if not v]
    if verificados:
        linhas.append(f"✅ {' · '.join(verificados)}")
    if nao_verificados:
        linhas.append(f"❌ {' · '.join(nao_verificados)}")

    if info.tem_avaliacoes is True and info.estrelas is not None:
        linhas.append(f"⭐ {info.estrelas:.1f}")
    elif info.tem_avaliacoes is False:
        linhas.append("⭐ Sem avaliações")

    if not linhas:
        return "👤 Dados do vendedor indisponíveis"

    return "\n".join(linhas)


def _mensagem_erro_sanitizada(exc: requests.RequestException) -> str:
    """Constrói uma mensagem de erro segura a partir de uma exceção do
    `requests` — nunca inclui a URL da requisição (que carrega o token
    do bot). Usa o corpo da resposta do Telegram quando disponível, que
    é só JSON de erro (`{"ok":false,"description":...}`), nunca o token
    de volta."""
    resposta = getattr(exc, "response", None)
    if resposta is not None:
        return f"HTTP {resposta.status_code}: {resposta.text}"
    return f"{type(exc).__name__}: sem resposta do servidor (falha de rede/timeout)"


def _formatar_preco_brl(preco: float | None) -> str:
    if preco is None:
        return "preço não informado"
    texto = f"{preco:,.2f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"
