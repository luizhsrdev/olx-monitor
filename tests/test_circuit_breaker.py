from __future__ import annotations

from unittest.mock import MagicMock, patch

from olx_monitor.sources.olx import OlxSource, _CircuitBreaker


# --- _CircuitBreaker (lógica pura, sem rede) ----------------------------


def test_fechado_antes_do_limiar_de_falhas():
    cb = _CircuitBreaker(limiar=3, reset_segundos=100)

    cb.registrar_falha(agora=0)
    cb.registrar_falha(agora=1)

    assert cb.esta_aberto(agora=1) is False


def test_abre_depois_de_n_falhas_consecutivas():
    cb = _CircuitBreaker(limiar=3, reset_segundos=100)

    cb.registrar_falha(agora=0)
    cb.registrar_falha(agora=1)
    cb.registrar_falha(agora=2)

    assert cb.esta_aberto(agora=2) is True


def test_sucesso_reseta_contagem_de_falhas():
    cb = _CircuitBreaker(limiar=3, reset_segundos=100)

    cb.registrar_falha(agora=0)
    cb.registrar_falha(agora=1)
    cb.registrar_sucesso()
    cb.registrar_falha(agora=2)

    assert cb.esta_aberto(agora=2) is False  # só 1 falha desde o sucesso


def test_permanece_aberto_dentro_da_janela_de_reset():
    cb = _CircuitBreaker(limiar=3, reset_segundos=100)

    for t in range(3):
        cb.registrar_falha(agora=t)

    assert cb.esta_aberto(agora=50) is True


def test_fecha_sozinho_apos_janela_de_reset_expirar():
    cb = _CircuitBreaker(limiar=3, reset_segundos=100)

    for t in range(3):
        cb.registrar_falha(agora=t)
    assert cb.esta_aberto(agora=2) is True

    assert cb.esta_aberto(agora=103) is False  # janela de 100s expirou


def test_reset_por_tempo_zera_contagem_de_falhas():
    cb = _CircuitBreaker(limiar=3, reset_segundos=100)
    for t in range(3):
        cb.registrar_falha(agora=t)
    cb.esta_aberto(agora=200)  # dispara o reset por tempo

    cb.registrar_falha(agora=201)
    cb.registrar_falha(agora=202)

    assert cb.esta_aberto(agora=202) is False  # só 2 falhas desde o reset


# --- Integração com OlxSource: circuito aberto pula o requests ---------


def _resposta_bloqueada(status_code: int = 403) -> MagicMock:
    resposta = MagicMock()
    resposta.status_code = status_code
    resposta.text = "<html>Attention Required! Cloudflare</html>"
    return resposta


# HTML mínimo, mas com conteúdo RSC "utilizável" (pra não disparar
# OlxCollectionError) — sem nenhum anúncio de verdade dentro.
_HTML_RSC_SEM_ANUNCIOS = '<html><body><script>self.__next_f.push([1,"5:[1,2,3]"])</script></body></html>'


def test_apos_n_falhas_pula_requests_e_vai_direto_pro_playwright():
    source = OlxSource(modo="requests", timeout_segundos=1)
    url = "https://www.olx.com.br/busca?q=ps5"

    html_ok = (
        '<html><body><script>self.__next_f.push([1,"5:[[\\"$\\",\\"$L1a\\",null,'
        '{\\"ads\\":[{\\"id\\":\\"1\\",\\"subject\\":\\"PS5\\",\\"price\\":2000,'
        '\\"url\\":\\"https://x/1\\"}]}]]"])</script></body></html>'
    )

    with patch("olx_monitor.sources.olx.requests.get", return_value=_resposta_bloqueada()) as mock_get, \
         patch.object(OlxSource, "_fetch_playwright", return_value=html_ok) as mock_playwright:

        # 3 falhas consecutivas (limiar padrão) — cada uma ainda tenta requests
        for _ in range(3):
            source.collect(url)

        assert mock_get.call_count == 3
        assert mock_playwright.call_count == 3  # fallback em toda falha

        # a partir daqui, o circuito deve estar aberto: nem tenta requests
        source.collect(url)

        assert mock_get.call_count == 3  # não incrementou
        assert mock_playwright.call_count == 4


def test_circuito_e_por_dominio():
    source = OlxSource(modo="requests", timeout_segundos=1)

    with patch("olx_monitor.sources.olx.requests.get", return_value=_resposta_bloqueada()), \
         patch.object(OlxSource, "_fetch_playwright", return_value=_HTML_RSC_SEM_ANUNCIOS):
        for _ in range(3):
            source.collect("https://www.olx.com.br/busca?q=ps5")

    circuito_olx = source._circuito_de("https://www.olx.com.br/qualquer-coisa")
    circuito_outro = source._circuito_de("https://outro-dominio.com.br/x")

    assert circuito_olx.esta_aberto() is True
    assert circuito_outro.esta_aberto() is False
