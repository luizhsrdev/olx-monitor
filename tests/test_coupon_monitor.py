from __future__ import annotations

import threading
from unittest.mock import MagicMock

from olx_monitor.coupon_monitor import (
    Coupon,
    CouponMonitor,
    _dividir_em_cartoes,
    _extracao_parece_suspeita,
    extract_coupons,
)
from olx_monitor.dedupe import Store

# Trecho real de um debug_cupons.html capturado em produção (2026-07) —
# não é sintético. Só o valor de coordenadas do <path> do ícone de
# "expira em" foi encurtado por legibilidade; tags, classes e todo o
# texto visível são verbatim do HTML de verdade. A página de cupons não
# usa RSC (zero self.__next_f.push nela) — diferente da listagem de
# anúncios.
_CARTAO_REAL = (
    'class="container-outlined CouponCard_wrapper__4Iudh">'
    '<picture class="CouponCard_icon__Qa89B">'
    '<source srcSet="https://static.olx.com.br/recommendation/home/categories/default.webp" type="image/webp"/>'
    '<img src="https://static.olx.com.br/recommendation/home/categories/default.png" '
    'alt="ícone pequeno da categoria do cupom"/></picture>'
    '<div class="flex flex-col gap-0-25 CouponCard_title__AzRC8">'
    '<h2 class="typo-body-large font-bold CouponCard_title__AzRC8">R$30 de desconto com Garantia da OLX</h2>'
    '<p class="typo-caption undefined">Válido para compras entre R$400 e R$20000 utilizando Garantia OLX</p>'
    "</div>"
    '<div class="flex CouponCard_content__mC_ED">'
    '<div class="CouponContent_wrapper__0jWx8 CouponCard_coupon__7fWGP p-1 container-outlined">'
    '<p class="typo-body-large font-regular text-secondary-100 uppercase">OFF30</p></div>'
    '<div role="region" aria-label="Notifications" tabindex="-1" style="pointer-events:none">'
    '<ol tabindex="-1" class="ds-toast-viewport"></ol></div>'
    '<a href="https://www.olx.com.br/brasil?opst=1" target="_blank" rel="noopener noreferrer" '
    'class="olx-core-button olx-core-button--tertiary olx-core-button--small CouponCard_button__q8TYF">'
    "Ver produtos</a></div>"
    '<div class="flex gap-0-5 CouponCard_footer__UUDKW">'
    '<svg width="16" height="16" viewBox="0 0 25 25" fill="none" xmlns="http://www.w3.org/2000/svg" '
    'aria-hidden="true" color="var(--color-feedback-error-100)">'
    '<path d="M6.65636 9.8231 ... (omitido)" fill="var(--color-feedback-error-100)"></path></svg>'
    '<p class="typo-caption" color="--color-feedback-error-100">Expira em menos de 24 horas</p>'
    "</div></div>"
)


def _html(*cartoes: str) -> str:
    corpo = "".join(f"<div {c}" for c in cartoes)
    return f'<html><body><div class="CouponsList_couponsGrid__x">{corpo}</div></body></html>'


def _cartao_com(codigo: str, titulo: str) -> str:
    """Deriva um cartão sintético a partir do real, só trocando código
    e título — usado pra montar páginas com múltiplos cupons (a página
    real inspecionada só tinha um), pra exercitar o corte entre
    cartões. Não substitui validação contra um dump real com vários
    cupons de verdade."""
    return _CARTAO_REAL.replace("OFF30", codigo).replace(
        "R$30 de desconto com Garantia da OLX", titulo
    )


# --- Extração contra o fragmento real (um cupom) ------------------


def test_extrai_codigo_titulo_descricao_validade_do_fragmento_real():
    cupons = extract_coupons(_html(_CARTAO_REAL))

    assert len(cupons) == 1
    cupom = cupons[0]
    assert cupom.codigo == "OFF30"
    assert cupom.titulo == "R$30 de desconto com Garantia da OLX"
    assert cupom.descricao == "Válido para compras entre R$400 e R$20000 utilizando Garantia OLX"
    assert cupom.validade == "Expira em menos de 24 horas"


def test_extracao_de_fragmento_unico_nao_e_suspeita():
    html = _html(_CARTAO_REAL)
    n_cartoes = len(_dividir_em_cartoes(html))
    cupons = extract_coupons(html)

    assert _extracao_parece_suspeita(n_cartoes, cupons) is False


# --- Corte entre múltiplos cartões (sintético — ver aviso no módulo) --


def test_corte_separa_multiplos_cartoes_sem_vazar_conteudo():
    html = _html(
        _cartao_com("OFF30", "R$30 de desconto"),
        _cartao_com("PROMO5", "R$5 de desconto"),
        _cartao_com("OFERTA6", "R$6 de desconto"),
    )

    cupons = extract_coupons(html)

    assert [c.codigo for c in cupons] == ["OFF30", "PROMO5", "OFERTA6"]
    assert [c.titulo for c in cupons] == [
        "R$30 de desconto",
        "R$5 de desconto",
        "R$6 de desconto",
    ]
    # cada cupom carrega sua própria descrição/validade, não a de outro
    assert all(c.descricao == "Válido para compras entre R$400 e R$20000 utilizando Garantia OLX" for c in cupons)
    assert all(c.validade == "Expira em menos de 24 horas" for c in cupons)


def test_corte_multiplos_cartoes_nao_e_suspeito_quando_correto():
    html = _html(_cartao_com("A1", "t1"), _cartao_com("A2", "t2"))
    n_cartoes = len(_dividir_em_cartoes(html))
    cupons = extract_coupons(html)

    assert n_cartoes == 2
    assert _extracao_parece_suspeita(n_cartoes, cupons) is False


def test_extracao_suspeita_quando_codigos_duplicados():
    # Simula o bug que _extracao_parece_suspeita existe pra pegar: o
    # corte "vazou" e dois cartões viraram o mesmo código.
    assert _extracao_parece_suspeita(
        2,
        [
            Coupon("X", None, None, None, coletado_em=None),
            Coupon("X", None, None, None, coletado_em=None),
        ],
    ) is True


def test_extracao_suspeita_quando_cartao_nao_virou_cupom():
    # 2 cartões no HTML, só 1 Coupon válido saiu — sinal de que algum
    # cartão perdeu o código no meio do caminho.
    assert _extracao_parece_suspeita(2, [Coupon("X", None, None, None, coletado_em=None)]) is True


# --- Degradação parcial / ausência de dados -------------------------


def test_cartao_sem_codigo_e_descartado():
    html = _html('class="container-outlined CouponCard_wrapper__x"><p>sem código aqui</p>')

    assert extract_coupons(html) == []


def test_campo_faltando_nao_invalida_o_cupom():
    cartao_so_com_codigo = (
        'class="container-outlined CouponCard_wrapper__x">'
        '<div class="CouponCard_coupon__abc"><p class="uppercase">SOLO10</p></div>'
    )

    cupons = extract_coupons(_html(cartao_so_com_codigo))

    assert len(cupons) == 1
    assert cupons[0].codigo == "SOLO10"
    assert cupons[0].titulo is None
    assert cupons[0].descricao is None
    assert cupons[0].validade is None


def test_sem_nenhum_cartao_retorna_lista_vazia():
    assert extract_coupons("<html><body>bloqueado, sem cartão nenhum</body></html>") == []


# --- Ciclo do CouponMonitor -------------------------------------------


def _monitor_com_html(html: str, notifier=None) -> tuple[CouponMonitor, Store]:
    source = MagicMock()
    source.fetch_html.return_value = html
    store = Store(":memory:")
    monitor = CouponMonitor(
        url="https://www.olx.com.br/cupons",
        intervalo_segundos=60,
        jitter_segundos=0,
        source=source,
        notifier=notifier or MagicMock(),
        store=store,
        stop_event=threading.Event(),
    )
    return monitor, store


def test_primeira_execucao_nao_notifica():
    notifier = MagicMock()
    monitor, store = _monitor_com_html(_html(_CARTAO_REAL), notifier)

    monitor._executar_ciclo()

    assert notifier.send_coupon.call_count == 0
    assert store.eh_primeira_execucao_cupons() is False
    store.close()


def test_cupom_ja_visto_nao_notifica_de_novo():
    notifier = MagicMock()
    monitor, store = _monitor_com_html(_html(_CARTAO_REAL), notifier)

    monitor._executar_ciclo()  # primeira execução, só popula
    monitor._executar_ciclo()  # mesmo cupom

    assert notifier.send_coupon.call_count == 0
    store.close()


def test_cupom_novo_e_notificado():
    notifier = MagicMock()
    monitor, store = _monitor_com_html(_html(_CARTAO_REAL), notifier)
    monitor._executar_ciclo()  # primeira execução

    monitor._source.fetch_html.return_value = _html(_cartao_com("NOVO99", "Novo cupom"))
    monitor._executar_ciclo()

    assert notifier.send_coupon.call_count == 1
    cupom = notifier.send_coupon.call_args.args[0]
    assert cupom.codigo == "NOVO99"
    store.close()


def test_falha_ao_notificar_um_cupom_nao_impede_os_outros():
    notifier = MagicMock()
    notifier.send_coupon.side_effect = [Exception("boom"), None]
    monitor, store = _monitor_com_html(_html(_CARTAO_REAL), notifier)
    monitor._executar_ciclo()  # primeira execução

    monitor._source.fetch_html.return_value = _html(
        _cartao_com("FALHA1", "t"), _cartao_com("OK2", "t")
    )
    monitor._executar_ciclo()  # não deve levantar, mesmo com a 1a falhando

    assert notifier.send_coupon.call_count == 2
    store.close()
