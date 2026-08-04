from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from olx_monitor import coupon_monitor
from olx_monitor.coupon_monitor import (
    Coupon,
    CouponMonitor,
    LatestCouponCache,
    _extracao_parece_suspeita,
    _primeiro_valido,
    extract_coupons,
)
from olx_monitor.dedupe import Store


@pytest.fixture(autouse=True)
def _debug_dump_isolado(tmp_path: Path, monkeypatch):
    """DEBUG_DUMP_PATH é relativo ao cwd — sem isso, um teste que
    dispara _salvar_debug_dump (extração vazia/suspeita) escreve por
    cima do debug_cupons.html de verdade na raiz do repo, se o pytest
    rodar de lá (foi exatamente isso que aconteceu uma vez, sobrescrevendo
    um dump real com conteúdo sintético de teste — daí este fixture)."""
    monkeypatch.setattr(coupon_monitor, "DEBUG_DUMP_PATH", tmp_path / "debug_cupons_teste.html")


def _html_com_next_f_push(prefixo: str, valor: object, chamada_id: int = 1) -> str:
    """Mesmo helper usado em test_normalize_olx.py/test_seller_info.py."""
    decodificado = f"{prefixo}:{json.dumps(valor, ensure_ascii=False)}"
    literal = json.dumps(decodificado)
    return f"<script>self.__next_f.push([{chamada_id},{literal}])</script>"


def _bruto(coupon: str, category_id: str, title: str, expires_at: str | None = None) -> dict:
    return {
        "categoryId": category_id,
        "categoryName": f"Categoria {category_id}",
        "coupon": coupon,
        "description": f"Válido para compras usando {coupon}",
        "expiresAt": expires_at,
        "link": "https://www.olx.com.br/brasil?opst=1",
        "title": title,
        "shortTitle": title,
    }


def _html_rsc(*brutos: dict) -> str:
    payload = ["$", "$L18", None, {"data": list(brutos)}]
    return "<html><body>" + _html_com_next_f_push("5", payload) + "</body></html>"


# --- Extração via RSC (formato real, confirmado em 2026-08) -----------


def test_extrai_codigo_titulo_descricao_expira_em_via_rsc():
    html = _html_rsc(_bruto("OFF30", "-1", "R$30 de desconto", "2026-08-04T02:59:00.000Z"))

    cupons = extract_coupons(html)

    assert len(cupons) == 1
    c = cupons[0]
    assert c.codigo == "OFF30"
    assert c.titulo == "R$30 de desconto"
    assert c.descricao == "Válido para compras usando OFF30"
    assert c.categoria_id == "-1"
    assert c.expira_em == datetime(2026, 8, 4, 2, 59, tzinfo=timezone.utc)


def test_cupom_sem_expiresat_vira_expira_em_none():
    html = _html_rsc(_bruto("NOVO40", "-1", "R$40 de desconto", expires_at=None))

    cupons = extract_coupons(html)

    assert cupons[0].expira_em is None


def test_mesmo_codigo_em_categorias_diferentes_vira_cupons_distintos():
    # Reproduz o achado real: "TECH5" repetido, um cartão por categoria.
    html = _html_rsc(
        _bruto("TECH5", "3000", "5% OFF em Celulares", "2026-08-04T02:59:00.000Z"),
        _bruto("TECH5", "16000", "5% OFF em Games", "2026-08-04T02:59:00.000Z"),
    )

    cupons = extract_coupons(html)

    assert len(cupons) == 2
    assert [c.categoria_id for c in cupons] == ["3000", "16000"]
    assert [c.titulo for c in cupons] == ["5% OFF em Celulares", "5% OFF em Games"]
    assert _extracao_parece_suspeita(cupons) is False  # chave composta resolve a duplicata


def test_scanner_rsc_nao_fixa_caminho():
    # Simula o array de cupons enterrado fundo, sob nomes de chave que
    # podem mudar (não é "data"/"$L18" hardcoded) — só a "cara" do item
    # importa (tem "coupon" + título/descrição).
    payload = [
        "$",
        "div",
        None,
        {
            "outraCoisaIrrelevante": {"foo": "bar"},
            "secao": {"algumaChaveQualquer": [_bruto("ACHADO10", "-1", "10% off")]},
        },
    ]
    html = "<html><body>" + _html_com_next_f_push("9", payload) + "</body></html>"

    cupons = extract_coupons(html)

    assert len(cupons) == 1
    assert cupons[0].codigo == "ACHADO10"


def test_extracao_rsc_vazia_nao_e_suspeita_por_si_so():
    assert extract_coupons("<html><body>bloqueado, sem RSC</body></html>") == []


def test_extracao_suspeita_quando_mesma_chave_composta_repetida():
    cupons = [
        Coupon("X", None, None, "-1", None, coletado_em=None),
        Coupon("X", None, None, "-1", None, coletado_em=None),
    ]
    assert _extracao_parece_suspeita(cupons) is True


# --- Fallback legado (HTML renderizado, sem RSC) -----------------------
# Trecho real de um debug_cupons.html capturado antes de descobrirmos
# que a página também transmite via RSC (ver docstring de
# coupon_monitor.py) — mantido como fallback, exercitado aqui.

_CARTAO_LEGADO_REAL = (
    'class="container-outlined CouponCard_wrapper__4Iudh">'
    '<div class="flex flex-col gap-0-25 CouponCard_title__AzRC8">'
    '<h2 class="typo-body-large font-bold CouponCard_title__AzRC8">R$30 de desconto com Garantia da OLX</h2>'
    '<p class="typo-caption undefined">Válido para compras entre R$400 e R$20000 utilizando Garantia OLX</p>'
    "</div>"
    '<div class="flex CouponCard_content__mC_ED">'
    '<div class="CouponContent_wrapper__0jWx8 CouponCard_coupon__7fWGP p-1 container-outlined">'
    '<p class="typo-body-large font-regular text-secondary-100 uppercase">OFF30</p></div></div>'
    "</div>"
)


def _html_legado(*cartoes: str) -> str:
    corpo = "".join(f"<div {c}" for c in cartoes)
    return f'<html><body><div class="CouponsList_couponsGrid__x">{corpo}</div></body></html>'


def test_fallback_legado_usado_quando_rsc_nao_tem_cupom():
    cupons = extract_coupons(_html_legado(_CARTAO_LEGADO_REAL))

    assert len(cupons) == 1
    assert cupons[0].codigo == "OFF30"
    assert cupons[0].titulo == "R$30 de desconto com Garantia da OLX"
    assert cupons[0].categoria_id == ""
    assert cupons[0].expira_em is None  # não dá pra derivar do HTML legado com confiança


def test_rsc_tem_prioridade_sobre_legado_quando_ambos_presentes():
    html_rsc = _html_rsc(_bruto("DORSC", "-1", "Veio do RSC"))
    html_misto = html_rsc + _html_legado(_CARTAO_LEGADO_REAL)

    cupons = extract_coupons(html_misto)

    assert [c.codigo for c in cupons] == ["DORSC"]


# --- _primeiro_valido: seleção respeitando expira_em -------------------


def test_primeiro_valido_pula_expirados():
    agora = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    cupons = [
        Coupon("EXPIRADO", None, None, "-1", agora - timedelta(hours=1), coletado_em=agora),
        Coupon("VALIDO", None, None, "-1", agora + timedelta(hours=1), coletado_em=agora),
    ]

    assert _primeiro_valido(cupons, agora).codigo == "VALIDO"


def test_primeiro_valido_sem_expira_em_e_sempre_valido():
    agora = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    cupons = [Coupon("SEMPRE", None, None, "-1", None, coletado_em=agora)]

    assert _primeiro_valido(cupons, agora).codigo == "SEMPRE"


def test_primeiro_valido_lista_vazia_retorna_none():
    assert _primeiro_valido([], datetime.now(timezone.utc)) is None


def test_primeiro_valido_todos_expirados_retorna_none():
    agora = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    cupons = [Coupon("X", None, None, "-1", agora - timedelta(minutes=1), coletado_em=agora)]

    assert _primeiro_valido(cupons, agora) is None


# --- LatestCouponCache ---------------------------------------------------


def test_latest_coupon_cache_comeca_vazio():
    assert LatestCouponCache().obter() is None


def test_latest_coupon_cache_atualizar_e_obter():
    cache = LatestCouponCache()
    cupom = Coupon("X", None, None, "-1", None, coletado_em=datetime.now(timezone.utc))

    cache.atualizar(cupom)

    assert cache.obter() is cupom


def test_latest_coupon_cache_atualizar_none_limpa_cache():
    cache = LatestCouponCache()
    cache.atualizar(Coupon("X", None, None, "-1", None, coletado_em=datetime.now(timezone.utc)))

    cache.atualizar(None)

    assert cache.obter() is None


# --- Ciclo do CouponMonitor ---------------------------------------------


def _monitor_com_html(html: str, notifier=None, cache=None) -> tuple[CouponMonitor, Store]:
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
        latest_coupon_cache=cache or LatestCouponCache(),
    )
    return monitor, store


def test_primeira_execucao_nao_notifica():
    notifier = MagicMock()
    monitor, store = _monitor_com_html(_html_rsc(_bruto("OFF30", "-1", "t")), notifier)

    monitor._executar_ciclo()

    assert notifier.send_coupon.call_count == 0
    assert store.eh_primeira_execucao_cupons() is False
    store.close()


def test_cupom_ja_visto_nao_notifica_de_novo():
    notifier = MagicMock()
    monitor, store = _monitor_com_html(_html_rsc(_bruto("OFF30", "-1", "t")), notifier)

    monitor._executar_ciclo()
    monitor._executar_ciclo()

    assert notifier.send_coupon.call_count == 0
    store.close()


def test_cupom_novo_e_notificado():
    notifier = MagicMock()
    monitor, store = _monitor_com_html(_html_rsc(_bruto("OFF30", "-1", "t")), notifier)
    monitor._executar_ciclo()

    monitor._source.fetch_html.return_value = _html_rsc(_bruto("NOVO99", "-1", "Novo"))
    monitor._executar_ciclo()

    assert notifier.send_coupon.call_count == 1
    assert notifier.send_coupon.call_args.args[0].codigo == "NOVO99"
    store.close()


def test_mesmo_codigo_categoria_nova_e_notificado():
    # Regressão end-to-end do bug real: TECH5/3000 já visto não deve
    # suprimir TECH5/16000 aparecendo num ciclo seguinte.
    notifier = MagicMock()
    monitor, store = _monitor_com_html(
        _html_rsc(_bruto("TECH5", "3000", "Celulares")), notifier
    )
    monitor._executar_ciclo()  # primeira execução

    monitor._source.fetch_html.return_value = _html_rsc(
        _bruto("TECH5", "3000", "Celulares"), _bruto("TECH5", "16000", "Games")
    )
    monitor._executar_ciclo()

    assert notifier.send_coupon.call_count == 1
    assert notifier.send_coupon.call_args.args[0].categoria_id == "16000"
    store.close()


def test_falha_ao_notificar_um_cupom_nao_impede_os_outros():
    notifier = MagicMock()
    notifier.send_coupon.side_effect = [Exception("boom"), None]
    monitor, store = _monitor_com_html(_html_rsc(_bruto("X", "-1", "t")), notifier)
    monitor._executar_ciclo()  # primeira execução

    monitor._source.fetch_html.return_value = _html_rsc(
        _bruto("FALHA1", "-1", "t"), _bruto("OK2", "-1", "t")
    )
    monitor._executar_ciclo()  # não deve levantar, mesmo com a 1a falhando

    assert notifier.send_coupon.call_count == 2
    store.close()


def test_ciclo_atualiza_o_cache_com_o_primeiro_cupom_valido():
    cache = LatestCouponCache()
    monitor, store = _monitor_com_html(
        _html_rsc(
            _bruto("PRIMEIRO", "-1", "t1"),
            _bruto("SEGUNDO", "-1", "t2"),
        ),
        cache=cache,
    )

    monitor._executar_ciclo()

    assert cache.obter().codigo == "PRIMEIRO"
    store.close()


def test_ciclo_atualiza_cache_pra_none_quando_lista_vazia():
    cache = LatestCouponCache()
    cache.atualizar(Coupon("ANTIGO", None, None, "-1", None, coletado_em=datetime.now(timezone.utc)))
    monitor, store = _monitor_com_html("<html><body>sem cupom nenhum</body></html>", cache=cache)

    monitor._executar_ciclo()

    assert cache.obter() is None  # não deixa cupom fantasma grudado
    store.close()


def test_ciclo_nao_notifica_mas_ainda_atualiza_cache_na_primeira_execucao():
    # O cache reflete "o que está visível agora", independente de já
    # ter sido notificado ou não — mesmo na primeira execução (que não
    # notifica nada), o cache deve ser populado.
    cache = LatestCouponCache()
    monitor, store = _monitor_com_html(_html_rsc(_bruto("OFF30", "-1", "t")), cache=cache)

    monitor._executar_ciclo()

    assert cache.obter().codigo == "OFF30"
    store.close()


# Nota: um teste que lia debug_cupons.html direto do disco existiu aqui
# e foi removido — dependia de um arquivo mutável fora do controle do
# repo (o mesmo arquivo que o próprio monitor de cupons sobrescreve).
# A estrutura real que ele validava (7 cupons, "TECH5" repetido em 6
# categorias, "NOVO40" sem expira_em) está reproduzida fielmente nos
# testes sintéticos acima, construídos a partir dos dados reais
# capturados durante o desenvolvimento — sem a fragilidade de depender
# de um arquivo que pode não existir ou já ter sido sobrescrito.
