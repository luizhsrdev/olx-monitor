from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

from olx_monitor.alerts.telegram import (
    TelegramNotifier,
    TelegramSendError,
    _montar_bloco_vendedor,
    _montar_corpo,
)
from olx_monitor.models import Anuncio
from olx_monitor.seller_info import SellerInfo


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


def _resposta_ok(message_id: int = 555) -> MagicMock:
    resposta = MagicMock()
    resposta.raise_for_status.return_value = None
    resposta.json.return_value = {"ok": True, "result": {"message_id": message_id}}
    return resposta


# --- Formato do corpo (comum a send() e update()) ----------------------


def test_corpo_sem_termos_prioritarios_usa_marcador_padrao():
    texto = _montar_corpo(_anuncio(), "PS5 revenda", [])

    assert texto.startswith("🔔 Novo anúncio\n")
    assert "PRIORITÁRIO" not in texto


def test_corpo_com_termos_prioritarios_lista_os_termos():
    texto = _montar_corpo(_anuncio(), "PS5 revenda", ["lacrado", "1tb"])

    assert texto.startswith("🔥 <b>PRIORITÁRIO</b> (lacrado, 1tb)\n")


def test_corpo_contem_titulo_preco_local_monitor_e_link():
    texto = _montar_corpo(_anuncio(), "PS5 revenda", [])

    assert "<b>PS5 Digital Slim 1TB</b>" in texto
    assert "R$ 2.789,00" in texto
    assert "Jundiaí - SP" in texto
    assert "Monitor: PS5 revenda" in texto
    assert '🔗 <a href="https://olx.com.br/anuncio/1">Ver anúncio</a>' in texto


def test_corpo_escapa_html_do_titulo_e_do_monitor():
    anuncio = _anuncio(titulo="PS5 <script>alert(1)</script> & cia")
    texto = _montar_corpo(anuncio, "Monitor <x>", [])

    assert "<script>" not in texto
    assert "&lt;script&gt;" in texto
    assert "Monitor &lt;x&gt;" in texto


def test_corpo_escapa_termos_prioritarios():
    texto = _montar_corpo(_anuncio(), "PS5 revenda", ["<b>x</b>"])

    assert "&lt;b&gt;x&lt;/b&gt;" in texto


def test_corpo_preco_none_mostra_texto_alternativo():
    texto = _montar_corpo(_anuncio(preco=None), "PS5 revenda", [])

    assert "preço não informado" in texto


def test_corpo_local_none_mostra_texto_alternativo():
    texto = _montar_corpo(_anuncio(local=None), "PS5 revenda", [])

    assert "local não informado" in texto


# --- Bloco de dados do vendedor -----------------------------------------


def test_bloco_vendedor_com_dados_completos():
    info = SellerInfo(
        nome="Gabriel",
        membro_desde="maio de 2023",
        conta_verificada=True,
        verificacoes={"identidade": True, "telefone": True, "email": True},
        estrelas=None,
        tem_avaliacoes=False,
    )

    texto = _montar_bloco_vendedor(info)

    assert "👤 Vendedor: Gabriel" in texto
    assert "📅 Na OLX desde maio de 2023" in texto
    assert "✅ Conta verificada" in texto
    assert "✅ Identidade · Telefone · E-mail" in texto
    assert "⭐ Sem avaliações" in texto


def test_bloco_vendedor_separa_verificacoes_positivas_e_negativas():
    info = SellerInfo(verificacoes={"email": True, "facebook": False})

    texto = _montar_bloco_vendedor(info)

    assert "✅ E-mail" in texto
    assert "❌ Facebook" in texto


def test_bloco_vendedor_com_avaliacoes_mostra_estrelas():
    info = SellerInfo(estrelas=4.5, tem_avaliacoes=True)

    texto = _montar_bloco_vendedor(info)

    assert "⭐ 4.5" in texto


def test_bloco_vendedor_sem_nenhum_dado_usa_texto_alternativo():
    texto = _montar_bloco_vendedor(SellerInfo())

    assert texto == "👤 Dados do vendedor indisponíveis"


# --- send(): mensagem inicial com "buscando vendedor" + message_id -----


def test_send_inclui_loading_de_vendedor_e_retorna_message_id():
    notifier = TelegramNotifier(token="123:ABC", chat_id="1")

    with patch("olx_monitor.alerts.telegram.requests.post") as mock_post:
        mock_post.return_value = _resposta_ok(message_id=777)

        message_id = notifier.send(_anuncio(), "PS5 revenda", ["lacrado"])

    assert message_id == "777"
    texto_enviado = mock_post.call_args.kwargs["json"]["text"]
    assert "⏳ Buscando dados do vendedor..." in texto_enviado
    assert mock_post.call_args.args[0] == notifier._url_send


def test_send_sem_message_id_na_resposta_retorna_none():
    notifier = TelegramNotifier(token="123:ABC", chat_id="1")

    with patch("olx_monitor.alerts.telegram.requests.post") as mock_post:
        resposta = MagicMock()
        resposta.raise_for_status.return_value = None
        resposta.json.return_value = {"ok": True, "result": {}}
        mock_post.return_value = resposta

        assert notifier.send(_anuncio(), "PS5 revenda", []) is None


# --- update(): edita a mensagem, nunca propaga exceção ------------------


def test_update_com_seller_info_monta_bloco_do_vendedor():
    notifier = TelegramNotifier(token="123:ABC", chat_id="1")
    info = SellerInfo(nome="Gabriel", conta_verificada=True)

    with patch("olx_monitor.alerts.telegram.requests.post") as mock_post:
        mock_post.return_value = _resposta_ok()

        notifier.update("777", _anuncio(), "PS5 revenda", [], info)

    texto_editado = mock_post.call_args.kwargs["json"]["text"]
    assert "👤 Vendedor: Gabriel" in texto_editado
    assert "⏳ Buscando" not in texto_editado
    assert mock_post.call_args.args[0] == notifier._url_edit
    assert mock_post.call_args.kwargs["json"]["message_id"] == 777


def test_update_sem_seller_info_usa_texto_alternativo():
    notifier = TelegramNotifier(token="123:ABC", chat_id="1")

    with patch("olx_monitor.alerts.telegram.requests.post") as mock_post:
        mock_post.return_value = _resposta_ok()

        notifier.update("777", _anuncio(), "PS5 revenda", [], None)

    texto_editado = mock_post.call_args.kwargs["json"]["text"]
    assert "👤 Dados do vendedor indisponíveis" in texto_editado


def test_update_nao_propaga_excecao_se_editmessage_falhar():
    notifier = TelegramNotifier(token="123:ABC", chat_id="1")

    with patch("olx_monitor.alerts.telegram.requests.post", side_effect=requests.ConnectionError("boom")):
        # não deve levantar nada — mensagem original já é válida sozinha
        notifier.update("777", _anuncio(), "PS5 revenda", [], None)


def test_update_com_message_id_invalido_nao_chama_api():
    notifier = TelegramNotifier(token="123:ABC", chat_id="1")

    with patch("olx_monitor.alerts.telegram.requests.post") as mock_post:
        notifier.update("não-é-um-id", _anuncio(), "PS5 revenda", [], None)

    mock_post.assert_not_called()


# --- Regressão: token nunca pode vazar numa exceção --------------------


def test_send_falha_http_nao_vaza_token_na_excecao():
    notifier = TelegramNotifier(token="123456:SEGREDO-FALSO-ABC", chat_id="1")

    resposta_falsa = MagicMock()
    resposta_falsa.status_code = 401
    resposta_falsa.text = '{"ok":false,"error_code":401,"description":"Unauthorized"}'
    erro_http = requests.HTTPError(
        f"401 Client Error: Unauthorized for url: {notifier._url_send}",
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
    erro_conexao = requests.ConnectionError(f"falha ao conectar em {notifier._url_send}")

    with patch("olx_monitor.alerts.telegram.requests.post", side_effect=erro_conexao):
        try:
            notifier.send(_anuncio(), "Monitor", [])
            raise AssertionError("deveria ter levantado TelegramSendError")
        except TelegramSendError as exc:
            assert "SEGREDO-FALSO-ABC" not in str(exc)
            assert exc.__cause__ is None
