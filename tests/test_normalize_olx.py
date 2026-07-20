from __future__ import annotations

import json
from datetime import datetime, timezone

from olx_monitor.normalize import normalize_olx
from olx_monitor.sources.olx import (
    extract_ads_from_next_data,
    extract_ads_from_rsc,
    extract_next_data_json,
)

AGORA = datetime.now(timezone.utc)


def _html_com_next_data(payload: dict) -> str:
    return (
        "<html><head></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def _html_com_next_f_push(prefixo: str, valor: object, chamada_id: int = 1) -> str:
    """Monta um <script>self.__next_f.push([N, "..."])</script> igual ao
    que a OLX manda de verdade (formato RSC do App Router): o "valor" é
    serializado como "<prefixo>:<json>" e depois essa string inteira é
    re-serializada como um literal JSON (aspas/escapes), exatamente como
    o Next.js gera no HTML real."""
    decodificado = f"{prefixo}:{json.dumps(valor, ensure_ascii=False)}"
    literal = json.dumps(decodificado)
    return f"<script>self.__next_f.push([{chamada_id},{literal}])</script>"


def test_extract_next_data_json_extrai_e_faz_parse():
    payload = {"props": {"pageProps": {"ads": []}}}
    html = _html_com_next_data(payload)

    assert extract_next_data_json(html) == payload


def test_extract_next_data_json_retorna_none_se_ausente():
    assert extract_next_data_json("<html><body>sem script aqui</body></html>") is None


def test_extract_next_data_json_retorna_none_se_json_invalido():
    html = '<script id="__NEXT_DATA__" type="application/json">{invalido</script>'
    assert extract_next_data_json(html) is None


def test_scan_recursivo_encontra_lista_em_caminho_profundo_e_nao_documentado():
    # Simula o cenário do SPEC: a lista de anúncios pode estar em
    # qualquer nível, não em um caminho fixo como props.pageProps.ads.
    next_data = {
        "props": {
            "pageProps": {
                "algumaCoisaIrrelevante": {"foo": "bar"},
                "resultado": {
                    "secoes": [
                        {"tipo": "banner", "conteudo": None},
                        {
                            "tipo": "listagem",
                            "anuncios": [
                                {
                                    "id": "111",
                                    "subject": "PS5 lacrado",
                                    "price": "R$ 2.500,00",
                                    "url": "/item/ps5-lacrado-111",
                                    "location": {"city": "São Paulo"},
                                },
                                {
                                    "id": "222",
                                    "subject": "PS5 usado",
                                    "price": "R$ 2.000",
                                    "url": "/item/ps5-usado-222",
                                    "location": {"city": "Campinas"},
                                },
                            ],
                        },
                    ]
                },
            }
        }
    }

    encontrados = extract_ads_from_next_data(next_data)
    ids = {item["id"] for item in encontrados}

    assert ids == {"111", "222"}


def test_scan_recursivo_ignora_listas_que_nao_parecem_anuncio():
    next_data = {
        "menu": ["Games", "Eletrônicos", "Veículos"],
        "filtros": [{"nome": "preco"}, {"nome": "categoria"}],
        "ads": [{"id": "1", "subject": "PS5", "price": 2000}],
    }

    encontrados = extract_ads_from_next_data(next_data)

    assert len(encontrados) == 1
    assert encontrados[0]["id"] == "1"


def test_scan_recursivo_dedupe_por_id():
    item = {"id": "1", "subject": "PS5", "price": 2000}
    next_data = {"listaA": [item], "listaB": [item]}

    encontrados = extract_ads_from_next_data(next_data)

    assert len(encontrados) == 1


def test_normalize_olx_mapeia_campos_com_nomes_alternativos():
    raw = {
        "listId": "999",
        "title": "Nintendo Switch 2",
        "price": "R$ 3.199,90",
        "link": "consoles/switch-2-999",
        "location": {"city": "Rio de Janeiro", "state": "RJ"},
        "date": "2026-07-19T10:00:00Z",
    }

    anuncio = normalize_olx(raw, AGORA)

    assert anuncio is not None
    assert anuncio.id == "999"
    assert anuncio.titulo == "Nintendo Switch 2"
    assert anuncio.preco == 3199.90
    assert anuncio.url == "https://www.olx.com.br/consoles/switch-2-999"
    assert anuncio.local == "Rio de Janeiro, RJ"
    assert anuncio.fonte == "olx"
    assert anuncio.publicado_em == "2026-07-19T10:00:00Z"
    assert anuncio.coletado_em == AGORA


def test_normalize_olx_preserva_url_absoluta():
    raw = {"id": "1", "subject": "PS5", "price": 2000, "url": "https://www.olx.com.br/item/1"}

    anuncio = normalize_olx(raw, AGORA)

    assert anuncio.url == "https://www.olx.com.br/item/1"


def test_normalize_olx_descarta_item_sem_titulo():
    raw = {"id": "1", "price": 2000, "url": "/item/1"}

    assert normalize_olx(raw, AGORA) is None


def test_normalize_olx_preco_ausente_vira_none_sem_quebrar():
    raw = {"id": "1", "subject": "PS5 a combinar", "url": "/item/1"}

    anuncio = normalize_olx(raw, AGORA)

    assert anuncio is not None
    assert anuncio.preco is None


def test_normalize_olx_preco_sem_centavos_nao_e_dividido_por_mil():
    # Regressão: "R$ 2.000" (formato comum na OLX, sem vírgula decimal)
    # já foi interpretado como 2.0 em vez de 2000.0, porque o parser só
    # tratava "." como separador de milhar quando havia vírgula também.
    raw = {"listId": 1, "subject": "PS4", "priceValue": "R$ 2.000", "url": "/item/1"}

    anuncio = normalize_olx(raw, AGORA)

    assert anuncio.preco == 2000.0


def test_normalize_olx_data_unix_timestamp_vira_iso():
    raw = {
        "listId": 1,
        "subject": "PS4",
        "priceValue": "R$ 2.000",
        "url": "https://olx.com.br/item/1",
        "date": 1784561589,
    }

    anuncio = normalize_olx(raw, AGORA)

    assert anuncio.publicado_em == "2026-07-20T15:33:09+00:00"


# --- Formato RSC (App Router) --------------------------------------
#
# Em 2026-07 descobrimos que a OLX não usa mais __NEXT_DATA__: o
# conteúdo vem espalhado em vários <script>self.__next_f.push(...)
# </script>, sem id fixo. Os testes abaixo cobrem esse formato real
# (ver sources/olx.py) e as duas causas raiz que zeraram a extração
# durante o desenvolvimento: (1) itens de slot de banner publicitário
# intercalados na lista de anúncios, que faziam a checagem "todo mundo
# parece anúncio" falhar por inteiro; (2) reuso do mesmo `vistos`
# (set de id() de objeto) entre chunks diferentes, causando colisão de
# id() com objetos já coletados pelo GC.


def _anuncio_bruto_rsc(list_id: int, subject: str, price: str = "R$ 2.000") -> dict:
    return {
        "listId": list_id,
        "subject": subject,
        "priceValue": price,
        "price": price,
        "location": "São Paulo -  SP",
        "url": f"https://sp.olx.com.br/item-{list_id}",
        "date": 1784561589,
    }


def test_extract_ads_from_rsc_encontra_anuncio_em_chunk_unico():
    payload_pagina = [
        "$",
        "$L1a",
        None,
        {"ads": [_anuncio_bruto_rsc(111, "PS5 lacrado")]},
    ]
    html = "<html><body>" + _html_com_next_f_push("5", payload_pagina) + "</body></html>"

    encontrados = extract_ads_from_rsc(html)

    assert len(encontrados) == 1
    assert encontrados[0]["listId"] == 111


def test_extract_ads_from_rsc_ignora_entradas_que_nao_sao_json():
    # Reproduz um chunk real de verdade: várias linhas "id:valor"
    # separadas por \n, a maioria delas referências de módulo do React
    # (ex.: I[9766,[],""]) que não são JSON válido e devem ser puladas
    # sem quebrar o parsing das linhas que são JSON de verdade.
    decodificado = (
        '1:"$Sreact.fragment"\n'
        '3:I[9766,[],""]\n'
        '5:' + json.dumps(["$", "$L1a", None, {"ads": [_anuncio_bruto_rsc(222, "Switch 2")]}])
    )
    literal = json.dumps(decodificado)
    html = f"<html><body><script>self.__next_f.push([1,{literal}])</script></body></html>"

    encontrados = extract_ads_from_rsc(html)

    assert len(encontrados) == 1
    assert encontrados[0]["listId"] == 222


def test_extract_ads_from_rsc_tolera_slots_de_banner_intercalados():
    # Regressão: a lista real de "ads" da OLX intercala placeholders de
    # banner publicitário (só advertisingId/deviceType, sem id/título/
    # preço) a cada handful de posições. Exigir que TODOS os itens
    # pareçam anúncio zerava a lista inteira.
    ads = []
    for i in range(20):
        ads.append(_anuncio_bruto_rsc(1000 + i, f"PS5 usado {i}"))
        if i % 3 == 0:
            ads.append({"advertisingId": f"slot-{i}", "deviceType": "mobile"})

    payload_pagina = ["$", "$L1a", None, {"ads": ads}]
    html = "<html><body>" + _html_com_next_f_push("5", payload_pagina) + "</body></html>"

    encontrados = extract_ads_from_rsc(html)
    ids = {item["listId"] for item in encontrados}

    assert len(encontrados) == 20
    assert ids == {1000 + i for i in range(20)}


def test_extract_ads_from_rsc_combina_anuncios_de_chunks_diferentes():
    # Regressão: `vistos` não pode ser compartilhado entre chunks —
    # anúncios de um chunk anterior não podem fazer o scanner ignorar
    # anúncios de um chunk posterior.
    chunk_a = _html_com_next_f_push("5", ["$", "$L1a", None, {"ads": [_anuncio_bruto_rsc(1, "PS5")]}])
    chunk_b = _html_com_next_f_push("6", ["$", "$L1b", None, {"ads": [_anuncio_bruto_rsc(2, "Switch")]}])
    html = f"<html><body>{chunk_a}{chunk_b}</body></html>"

    encontrados = extract_ads_from_rsc(html)
    ids = {item["listId"] for item in encontrados}

    assert ids == {1, 2}


def test_extract_ads_from_rsc_dedupe_por_id_entre_chunks():
    anuncio = _anuncio_bruto_rsc(1, "PS5")
    chunk_a = _html_com_next_f_push("5", ["$", "$L1a", None, {"ads": [anuncio]}])
    chunk_b = _html_com_next_f_push("6", ["$", "$L1b", None, {"ads": [anuncio]}])
    html = f"<html><body>{chunk_a}{chunk_b}</body></html>"

    encontrados = extract_ads_from_rsc(html)

    assert len(encontrados) == 1


def test_extract_ads_from_rsc_retorna_vazio_sem_push_calls():
    assert extract_ads_from_rsc("<html><body>bloqueado, sem RSC nenhum</body></html>") == []


def test_normalize_olx_a_partir_de_item_rsc_real():
    bruto = _anuncio_bruto_rsc(1519484840, "Ps4 Pro Branco", price="R$ 2.000")

    anuncio = normalize_olx(bruto, AGORA)

    assert anuncio is not None
    assert anuncio.id == "1519484840"
    assert anuncio.titulo == "Ps4 Pro Branco"
    assert anuncio.preco == 2000.0
    assert anuncio.url == "https://sp.olx.com.br/item-1519484840"
    assert anuncio.local == "São Paulo - SP"
