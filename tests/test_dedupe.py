from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from olx_monitor.dedupe import Store
from olx_monitor.models import Anuncio


def _anuncio(anuncio_id: str, fonte: str = "olx") -> Anuncio:
    return Anuncio(
        id=anuncio_id,
        titulo=f"anuncio {anuncio_id}",
        preco=100.0,
        url=f"https://olx.com.br/{anuncio_id}",
        local="São Paulo, SP",
        fonte=fonte,
        publicado_em=None,
        coletado_em=datetime.now(timezone.utc),
    )


@pytest.fixture
def store(tmp_path: Path):
    s = Store(tmp_path / "teste.db")
    yield s
    s.close()


def test_primeira_execucao_true_quando_banco_vazio(store):
    assert store.eh_primeira_execucao("Monitor A") is True


def test_primeira_execucao_false_apos_marcar_vistos(store):
    store.marcar_vistos("Monitor A", [_anuncio("1")])

    assert store.eh_primeira_execucao("Monitor A") is False


def test_primeira_execucao_e_por_monitor_nao_global(store):
    # Requisito explícito do SPEC: um monitor novo adicionado a um banco
    # já populado por outros monitores ainda deve ser tratado como
    # "primeira execução" — o escopo é por monitor_nome, não global.
    store.marcar_vistos("Monitor A", [_anuncio("1")])

    assert store.eh_primeira_execucao("Monitor B") is True


def test_filtrar_novos_exclui_ja_vistos(store):
    anuncios = [_anuncio("1"), _anuncio("2")]
    store.marcar_vistos("Monitor A", [anuncios[0]])

    novos = store.filtrar_novos("Monitor A", anuncios)

    assert [a.id for a in novos] == ["2"]


def test_filtrar_novos_nao_marca_nada_sozinho(store):
    anuncios = [_anuncio("1")]
    store.filtrar_novos("Monitor A", anuncios)

    # filtrar_novos não deve ter efeito colateral de marcar como visto —
    # rodar de novo com o mesmo anúncio ainda o considera novo.
    novos_de_novo = store.filtrar_novos("Monitor A", anuncios)
    assert [a.id for a in novos_de_novo] == ["1"]


def test_filtrar_novos_escopo_por_monitor(store):
    anuncio = _anuncio("1")
    store.marcar_vistos("Monitor A", [anuncio])

    novos_b = store.filtrar_novos("Monitor B", [anuncio])

    assert [a.id for a in novos_b] == ["1"]


def test_filtrar_novos_preserva_ordem(store):
    anuncios = [_anuncio("1"), _anuncio("2"), _anuncio("3")]
    store.marcar_vistos("Monitor A", [anuncios[1]])

    novos = store.filtrar_novos("Monitor A", anuncios)

    assert [a.id for a in novos] == ["1", "3"]


def test_marcar_vistos_e_idempotente(store):
    anuncio = _anuncio("1")
    store.marcar_vistos("Monitor A", [anuncio])
    store.marcar_vistos("Monitor A", [anuncio])  # não deve levantar erro

    assert store.filtrar_novos("Monitor A", [anuncio]) == []


def test_limpar_antigos_remove_registros_expirados(store):
    store.marcar_vistos("Monitor A", [_anuncio("1")])

    # Não há API pública para "voltar no tempo" um registro — força
    # diretamente no banco pra testar a fronteira da limpeza.
    antigo = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    with store._lock:
        store._conexao.execute(
            "UPDATE anuncios_vistos SET visto_em = ? WHERE anuncio_id = '1'",
            (antigo,),
        )
        store._conexao.commit()

    removidos = store.limpar_antigos(dias=90)

    assert removidos == 1
    assert store.eh_primeira_execucao("Monitor A") is True


def test_limpar_antigos_preserva_registros_recentes(store):
    store.marcar_vistos("Monitor A", [_anuncio("1")])

    removidos = store.limpar_antigos(dias=90)

    assert removidos == 0
    assert store.eh_primeira_execucao("Monitor A") is False


# --- Dedupe de cupons — tabela própria (cupons_vistos), desacoplada de
# Anuncio/Coupon: Store só lida com strings de código, quem monta o
# Coupon é coupon_monitor.py. ------------------------------------------


def test_cupons_primeira_execucao_true_quando_banco_vazio(store):
    assert store.eh_primeira_execucao_cupons() is True


def test_cupons_primeira_execucao_false_apos_marcar_vistos(store):
    store.marcar_codigos_vistos(["OFF30"])

    assert store.eh_primeira_execucao_cupons() is False


def test_cupons_codigos_novos_exclui_ja_vistos(store):
    store.marcar_codigos_vistos(["OFF30"])

    novos = store.codigos_novos(["OFF30", "PROMO5"])

    assert novos == ["PROMO5"]


def test_cupons_codigos_novos_nao_marca_nada_sozinho(store):
    store.codigos_novos(["OFF30"])

    assert store.codigos_novos(["OFF30"]) == ["OFF30"]


def test_cupons_codigos_novos_preserva_ordem(store):
    store.marcar_codigos_vistos(["B"])

    novos = store.codigos_novos(["A", "B", "C"])

    assert novos == ["A", "C"]


def test_cupons_marcar_codigos_vistos_e_idempotente(store):
    store.marcar_codigos_vistos(["OFF30"])
    store.marcar_codigos_vistos(["OFF30"])  # não deve levantar erro

    assert store.codigos_novos(["OFF30"]) == []


def test_cupons_dedupe_e_independente_do_dedupe_de_anuncios(store):
    # Um anúncio com id igual a um código de cupom não deve interferir
    # um no outro — tabelas completamente separadas.
    store.marcar_vistos("Monitor A", [_anuncio("OFF30")])

    assert store.codigos_novos(["OFF30"]) == ["OFF30"]


def test_limpar_antigos_tambem_remove_cupons_expirados(store):
    store.marcar_codigos_vistos(["OFF30"])

    antigo = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    with store._lock:
        store._conexao.execute(
            "UPDATE cupons_vistos SET visto_em = ? WHERE codigo = 'OFF30'",
            (antigo,),
        )
        store._conexao.commit()

    removidos = store.limpar_antigos(dias=90)

    assert removidos == 1
    assert store.eh_primeira_execucao_cupons() is True
