from __future__ import annotations

import json

from olx_monitor.seller_info import SellerInfo, extract_seller_info


def _html_com_next_f_push(prefixo: str, valor: object, chamada_id: int = 1) -> str:
    """Mesmo helper usado em test_normalize_olx.py — monta um
    <script>self.__next_f.push(...)</script> no formato real da OLX."""
    decodificado = f"{prefixo}:{json.dumps(valor, ensure_ascii=False)}"
    literal = json.dumps(decodificado)
    return f"<script>self.__next_f.push([{chamada_id},{literal}])</script>"


def _pagina_com_vendedor(card_vendedor: dict) -> str:
    payload = ["$", "$L1a", None, {"seller": card_vendedor, "outraCoisa": {"irrelevante": True}}]
    return "<html><body>" + _html_com_next_f_push("9", payload) + "</body></html>"


def test_extrai_nome_e_membro_desde():
    html = _pagina_com_vendedor(
        {
            "sellerName": "Gabriel",
            "memberSince": "desde maio de 2023",
            "isVerified": True,
        }
    )

    info = extract_seller_info(html)

    assert info is not None
    assert info.nome == "Gabriel"
    assert info.membro_desde == "maio de 2023"
    assert info.conta_verificada is True


def test_extrai_verificacoes_individuais():
    html = _pagina_com_vendedor(
        {
            "sellerName": "Gabriel",
            "memberSince": "desde maio de 2023",
            "email": True,
            "phone": True,
            "identity": True,
            "facebook": False,
        }
    )

    info = extract_seller_info(html)

    assert info.verificacoes == {
        "email": True,
        "telefone": True,
        "identidade": True,
        "facebook": False,
    }


def test_extrai_estrelas_e_avaliacoes():
    html = _pagina_com_vendedor(
        {"sellerName": "Gabriel", "memberSince": "desde maio de 2023", "rating": 4.5}
    )

    info = extract_seller_info(html)

    assert info.estrelas == 4.5
    assert info.tem_avaliacoes is True


def test_sem_avaliacoes_quando_estrelas_zero():
    html = _pagina_com_vendedor(
        {"sellerName": "Gabriel", "memberSince": "desde maio de 2023", "rating": 0}
    )

    info = extract_seller_info(html)

    assert info.tem_avaliacoes is False


def test_retorna_none_quando_nao_ha_card_de_vendedor():
    # Nada no HTML se parece com dados de vendedor (sem "desde", sem
    # verificação, sem nome) — não deve inventar um SellerInfo vazio.
    html = "<html><body>" + _html_com_next_f_push("5", {"algumaCoisa": [1, 2, 3]}) + "</body></html>"

    assert extract_seller_info(html) is None


def test_retorna_none_sem_nenhum_push_rsc():
    assert extract_seller_info("<html><body>bloqueado, sem RSC</body></html>") is None


def test_nome_sozinho_nao_e_confianca_suficiente():
    # Um dict só com um campo "name" (sem "desde"/verificação/estrelas)
    # não deveria ser confundido com o card do vendedor — nome é um
    # campo comum demais por si só.
    html = "<html><body>" + _html_com_next_f_push(
        "5", {"algumFiltro": {"name": "categoria-x"}}
    ) + "</body></html>"

    assert extract_seller_info(html) is None


def test_membro_desde_sem_match_do_padrao_preserva_texto_cru():
    # Se o valor não bater com o regex "desde <mês> de <ano>" mas ainda
    # assim vier de uma chave candidata, preserva o texto como veio —
    # melhor que descartar silenciosamente.
    html = _pagina_com_vendedor(
        {"sellerName": "Gabriel", "memberSince": "há mais de 1 ano", "isVerified": True}
    )

    info = extract_seller_info(html)

    assert info.membro_desde == "há mais de 1 ano"


def test_dataclass_seller_info_tem_defaults_none():
    info = SellerInfo()

    assert info.nome is None
    assert info.membro_desde is None
    assert info.conta_verificada is None
    assert info.verificacoes == {}
    assert info.estrelas is None
    assert info.tem_avaliacoes is None
