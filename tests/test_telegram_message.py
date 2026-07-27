from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from olx_monitor.alerts.telegram import TelegramNotifier, TelegramSendError
from olx_monitor.models import Anuncio


def _anuncio(titulo="PS5 Digital Slim 1TB", preco=2789.0, local="Jundiaí - SP") -> Anuncio:
    return Anuncio(
        id="1",
        titulo=titulo,
        preco=preco,
        url="https://olx.com.br/anuncio/1",
        local=local,
        fonte="olx",
        publicado_em=None,
        coletado_em=datetime.now(timezone.utc),
    )


# --- Formato da mensagem ----------------------------------------------


def test_mensagem_sem_termos_prioritarios_usa_marcador_padrao():
    texto = TelegramNotifier._montar_mensagem(_anuncio(), "PS5 revenda", [])

    assert texto.startswith("🔔 Novo anúncio\n")
    assert "PRIORITÁRIO" not in texto


def test_mensagem_com_termos_prioritarios_lista_os_termos():
    texto = TelegramNotifier._montar_mensagem(_anuncio(), "PS5 revenda", ["lacrado", "1tb"])

    assert texto.startswith("🔥 <b>PRIORITÁRIO</b> (lacrado, 1tb)\n")


def test_mensagem_contem_titulo_preco_local_monitor_e_link():
    texto = TelegramNotifier._montar_mensagem(_anuncio(), "PS5 revenda", [])

    assert "<b>PS5 Digital Slim 1TB</b>" in texto
    assert "R$ 2.789,00" in texto
    assert "Jundiaí - SP" in texto
    assert "Monitor: PS5 revenda" in texto
    assert '<a href="https://olx.com.br/anuncio/1">Ver anúncio</a>' in texto


def test_mensagem_escapa_html_do_titulo_e_do_monitor():
    anuncio = _anuncio(titulo="PS5 <script>alert(1)</script> & cia")
    texto = TelegramNotifier._montar_mensagem(anuncio, "Monitor <x>", [])

    assert "<script>" not in texto
    assert "&lt;script&gt;" in texto
    assert "Monitor &lt;x&gt;" in texto


def test_mensagem_escapa_termos_prioritarios():
    texto = TelegramNotifier._montar_mensagem(_anuncio(), "PS5 revenda", ["<b>x</b>"])

    assert "&lt;b&gt;x&lt;/b&gt;" in texto


def test_mensagem_preco_none_mostra_texto_alternativo():
    texto = TelegramNotifier._montar_mensagem(_anuncio(preco=None), "PS5 revenda", [])

    assert "preço não informado" in texto


def test_mensagem_local_none_mostra_texto_alternativo():
    texto = TelegramNotifier._montar_mensagem(_anuncio(local=None), "PS5 revenda", [])

    assert "local não informado" in texto


# --- Regressão: token nunca pode vazar numa exceção --------------------


def test_send_falha_http_nao_vaza_token_na_excecao():
    notifier = TelegramNotifier(token="123456:SEGREDO-FALSO-ABC", chat_id="1")

    resposta_falsa = MagicMock()
    resposta_falsa.status_code = 401
    resposta_falsa.text = '{"ok":false,"error_code":401,"description":"Unauthorized"}'
    erro_http = requests.HTTPError(
        f"401 Client Error: Unauthorized for url: {notifier._url}",
        response=resposta_falsa,
    )

    with patch("olx_monitor.alerts.telegram.requests.post") as mock_post:
        mock_post.return_value.raise_for_status.side_effect = erro_http

        try:
            notifier.send(_anuncio(), "Monitor", [])
            raise AssertionError("deveria ter levantado TelegramSendError")
        except TelegramSendError as exc:
            assert "SEGREDO-FALSO-ABC" not in str(exc)
            assert "401" in str(exc)
            assert "Unauthorized" in str(exc)
            # garante que a exceção original (que carrega o token na URL)
            # não fica pendurada na cadeia — senão um logger.exception()
            # ainda a imprimiria inteira.
            assert exc.__cause__ is None
            assert exc.__suppress_context__ is True


def test_send_falha_de_rede_sem_resposta_nao_vaza_token():
    notifier = TelegramNotifier(token="123456:SEGREDO-FALSO-ABC", chat_id="1")
    erro_conexao = requests.ConnectionError(f"falha ao conectar em {notifier._url}")

    with patch("olx_monitor.alerts.telegram.requests.post", side_effect=erro_conexao):
        try:
            notifier.send(_anuncio(), "Monitor", [])
            raise AssertionError("deveria ter levantado TelegramSendError")
        except TelegramSendError as exc:
            assert "SEGREDO-FALSO-ABC" not in str(exc)
            assert exc.__cause__ is None
