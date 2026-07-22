from __future__ import annotations

from datetime import datetime, timezone

from olx_monitor.config import MonitorConfig
from olx_monitor.filters import aplicar_filtros
from olx_monitor.models import Anuncio


def _monitor(**overrides) -> MonitorConfig:
    base = dict(
        nome="teste",
        ativo=True,
        fonte="olx",
        modo="requests",
        intervalo_segundos=60,
        jitter_segundos=0,
        urls=["https://exemplo.com"],
        preco_max=None,
        bloqueadas=[],
        obrigatorias_ou=[],
        prioritarias=[],
    )
    base.update(overrides)
    return MonitorConfig(**base)


def _anuncio(titulo: str, preco: float | None = 1000.0, anuncio_id: str = "1") -> Anuncio:
    return Anuncio(
        id=anuncio_id,
        titulo=titulo,
        preco=preco,
        url="https://olx.com.br/anuncio/1",
        local="São Paulo, SP",
        fonte="olx",
        publicado_em=None,
        coletado_em=datetime.now(timezone.utc),
    )


def test_bloqueada_local_descarta_anuncio():
    monitor = _monitor(bloqueadas=["controle"])
    resultados = aplicar_filtros([_anuncio("Controle PS5 novo")], monitor, bloqueadas_globais=[])

    assert len(resultados) == 1
    assert resultados[0].aceito is False
    assert "controle" in resultados[0].motivo


def test_bloqueada_global_descarta_mesmo_sem_bloqueada_local():
    monitor = _monitor(bloqueadas=[])
    resultados = aplicar_filtros(
        [_anuncio("PS5 com defeito")], monitor, bloqueadas_globais=["defeito"]
    )

    assert resultados[0].aceito is False
    assert "defeito" in resultados[0].motivo


def test_bloqueada_e_insensivel_a_acento_e_caixa():
    monitor = _monitor(bloqueadas=["PEÇAS"])
    resultados = aplicar_filtros([_anuncio("Vendo pecas de PS5")], monitor, bloqueadas_globais=[])

    assert resultados[0].aceito is False


def test_obrigatorias_ou_exige_pelo_menos_um_termo():
    monitor = _monitor(obrigatorias_ou=["ps5", "playstation 5"])

    aceito = aplicar_filtros([_anuncio("PlayStation 5 lacrado")], monitor, bloqueadas_globais=[])
    rejeitado = aplicar_filtros([_anuncio("Suporte para console")], monitor, bloqueadas_globais=[])

    assert aceito[0].aceito is True
    assert rejeitado[0].aceito is False
    assert rejeitado[0].motivo == "nenhuma obrigatoria_ou presente"


def test_obrigatorias_ou_vazio_nao_filtra_nada():
    monitor = _monitor(obrigatorias_ou=[])
    resultados = aplicar_filtros([_anuncio("Qualquer coisa")], monitor, bloqueadas_globais=[])

    assert resultados[0].aceito is True


def test_preco_max_descarta_acima_do_limite():
    monitor = _monitor(preco_max=2000)
    resultados = aplicar_filtros([_anuncio("PS5", preco=2500.0)], monitor, bloqueadas_globais=[])

    assert resultados[0].aceito is False
    assert "preco_max" in resultados[0].motivo


def test_preco_max_aceita_no_limite_ou_abaixo():
    monitor = _monitor(preco_max=2000)
    resultados = aplicar_filtros([_anuncio("PS5", preco=2000.0)], monitor, bloqueadas_globais=[])

    assert resultados[0].aceito is True


def test_preco_ausente_nao_e_descartado_por_preco_max():
    monitor = _monitor(preco_max=2000)
    resultados = aplicar_filtros([_anuncio("PS5", preco=None)], monitor, bloqueadas_globais=[])

    assert resultados[0].aceito is True


def test_prioritarias_marca_flag_sem_filtrar():
    monitor = _monitor(prioritarias=["lacrado"])

    prioritario = aplicar_filtros([_anuncio("PS5 lacrado")], monitor, bloqueadas_globais=[])
    normal = aplicar_filtros([_anuncio("PS5 usado")], monitor, bloqueadas_globais=[])

    assert prioritario[0].aceito is True and prioritario[0].prioritario is True
    assert normal[0].aceito is True and normal[0].prioritario is False


def test_prioritarias_registra_termo_que_bateu():
    monitor = _monitor(prioritarias=["lacrado", "novo"])
    resultados = aplicar_filtros([_anuncio("PS5 lacrado")], monitor, bloqueadas_globais=[])

    assert resultados[0].termos_prioritarios == ["lacrado"]


def test_prioritarias_sem_match_tem_lista_vazia():
    monitor = _monitor(prioritarias=["lacrado"])
    resultados = aplicar_filtros([_anuncio("PS5 usado")], monitor, bloqueadas_globais=[])

    assert resultados[0].termos_prioritarios == []


def test_prioritarias_registra_todos_os_termos_que_bateram_juntos():
    monitor = _monitor(prioritarias=["lacrado", "1tb", "novo"])
    resultados = aplicar_filtros(
        [_anuncio("PS5 Digital Slim 1TB lacrado")], monitor, bloqueadas_globais=[]
    )

    assert resultados[0].aceito is True
    assert resultados[0].prioritario is True
    assert resultados[0].termos_prioritarios == ["lacrado", "1tb"]


def test_ordem_das_regras_bloqueada_tem_prioridade_sobre_obrigatoria():
    monitor = _monitor(bloqueadas=["capa"], obrigatorias_ou=["ps5"])
    resultados = aplicar_filtros([_anuncio("Capa para PS5")], monitor, bloqueadas_globais=[])

    assert resultados[0].aceito is False
    assert "capa" in resultados[0].motivo
