from __future__ import annotations

from olx_monitor.seller_info import SellerInfo, extract_seller_info

# Trecho real de um debug_seller.html capturado em produção (2026-07) —
# não é sintético. Só os valores longos de coordenadas dos <path> de SVG
# foram encurtados para "... (omitido)" por legibilidade; tags, classes,
# atributos (fill/aria-label) e todo o texto visível são verbatim do
# HTML de verdade. A página individual de anúncio da OLX não usa RSC
# (zero ocorrências de self.__next_f.push nela) — os dados do vendedor
# vêm como HTML/texto já renderizado.
_FRAGMENTO_REAL_COM_VENDEDOR = (
    '<img src="https://static.olx.com.br/cd/vi/images/avatar.svg" '
    'alt="Foto de Jozelio Frutuozo" class="ad__sc-ypp2u2-7 aVsyu">'
    '<div class="ad__sc-ypp2u2-0 dQmvrk"><div class="ad__sc-1x77mz3-0 XphN">'
    '<div class="ad__sc-ypp2u2-1 gncofu"><div class="ad__sc-ypp2u2-11 cNRFtu">'
    '<span class="typo-body-large ad__sc-ypp2u2-4 TTTuh">Jozelio Frutuozo</span>'
    '<div class="ad__sc-ypp2u2-8 kfquG"><div class="ad__sc-kflvf0-0 GhDma">'
    '<span class="typo-caption text-neutral-120">Último acesso há 2 horas</span>'
    '</div></div></div></div></div></div></div></div>'
    '<div class="ad__sc-1bqzobc-1 fjBMYU"><div class="flex gap-1">'
    '<div class="flex flex-row w-full gap-1">'
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" '
    'aria-hidden="true" color="var(--color-neutral-130)">'
    '<path fill-rule="evenodd" d="M20.25,9.25 L20.25,6 ... (omitido)" '
    'fill="var(--color-neutral-130)"></path></svg>'
    '<span class="typo-body-small text-neutral-120 font-regular">'
    "Na OLX desde novembro de 2024</span></div></div>"
    '<div class="ad__sc-7hykp4-4 kzXthp">'
    '<span class="typo-caption font-semibold text-neutral-100">'
    "Este anunciante ainda não possui avaliações</span></div>"
    '<hr class="olx-divider" data-ds-component="DS-Divider" style="width:100%">'
    '<div class="profile-step-4 ad__sc-14u8c5l-0 jZKXOI">'
    '<span class="typo-body-medium font-semibold">Informações verificadas</span>'
    '<div class="ad__sc-jmgac1-0 uWNKP">'
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none">'
    '<path d="M8 16C12.4183 16 16 ... (omitido)" fill="#24A148"></path>'
    '<path d="M4.5 8L6.64645 10.1464 ... (omitido)" stroke="white" stroke-width="2" '
    'stroke-linecap="round"></path></svg>'
    '<p class="typo-body-small font-semibold text-neutral-130 block ad__sc-jmgac1-1 gonrhP" '
    'aria-label="E-mail">E-mail</p></div>'
    '<div class="ad__sc-jmgac1-0 uWNKP">'
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none">'
    '<path d="M8 16C12.4183 16 16 ... (omitido)" fill="#24A148"></path>'
    '<path d="M4.5 8L6.64645 10.1464 ... (omitido)" stroke="white" stroke-width="2" '
    'stroke-linecap="round"></path></svg>'
    '<p class="typo-body-small font-semibold text-neutral-130 block ad__sc-jmgac1-1 gonrhP" '
    'aria-label="Telefone">Telefone</p></div>'
    '<div class="ad__sc-jmgac1-0 uWNKP">'
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none">'
    '<path d="M8 16C12.4183 16 16 ... (omitido)" fill="#8994A9"></path>'
    '<path d="M5.5 5.5L10.5 10.5" stroke="white" stroke-width="2" stroke-linecap="round"></path>'
    '<path d="M5.5 10.5L10.5 5.5" stroke="white" stroke-width="2" stroke-linecap="round"></path>'
    "</svg>"
    '<p class="typo-body-small font-semibold text-neutral-130 block ad__sc-jmgac1-1 gonrhP" '
    'aria-label="Facebook não">Facebook</p></div></div></div>'
)


def _html(corpo: str) -> str:
    return f"<html><body>{corpo}</body></html>"


# --- Extração contra o fragmento real ------------------------------


def test_extrai_nome_do_alt_da_foto():
    info = extract_seller_info(_html(_FRAGMENTO_REAL_COM_VENDEDOR))

    assert info is not None
    assert info.nome == "Jozelio Frutuozo"


def test_extrai_membro_desde():
    info = extract_seller_info(_html(_FRAGMENTO_REAL_COM_VENDEDOR))

    assert info.membro_desde == "novembro de 2024"


def test_extrai_verificacoes_com_cor_do_icone():
    info = extract_seller_info(_html(_FRAGMENTO_REAL_COM_VENDEDOR))

    # E-mail e Telefone com fill verde (#24A148) = verificados;
    # Facebook com fill cinza (#8994A9) = não verificado.
    assert info.verificacoes == {"email": True, "telefone": True, "facebook": False}


def test_extrai_ausencia_de_avaliacoes():
    info = extract_seller_info(_html(_FRAGMENTO_REAL_COM_VENDEDOR))

    assert info.tem_avaliacoes is False
    assert info.estrelas is None


def test_conta_verificada_e_none_quando_selo_nao_aparece():
    # A amostra real inspecionada não tinha o selo "conta verificada"
    # em lugar nenhum do HTML — não é o mesmo que "sabemos que não é
    # verificada": é "não temos essa informação".
    info = extract_seller_info(_html(_FRAGMENTO_REAL_COM_VENDEDOR))

    assert info.conta_verificada is None


# --- Casos sem dados / degradação parcial ---------------------------


def test_retorna_none_quando_nao_ha_nenhum_dado_de_vendedor():
    assert extract_seller_info(_html("<p>bloqueado, sem nada aqui</p>")) is None


def test_retorna_none_para_html_vazio():
    assert extract_seller_info("") is None


def test_campo_faltando_nao_invalida_os_outros():
    # Só o nome, sem o resto — ainda deve retornar um SellerInfo
    # parcial, não None.
    html = _html('<img alt="Foto de Maria Silva" src="x">')

    info = extract_seller_info(html)

    assert info is not None
    assert info.nome == "Maria Silva"
    assert info.membro_desde is None
    assert info.verificacoes == {}
    assert info.tem_avaliacoes is None


def test_conta_verificada_reconhece_texto_literal_se_aparecer():
    # Não confirmado contra amostra real (ver seller_info.py) — mas o
    # reconhecimento do texto literal, se algum dia aparecer, deve
    # funcionar.
    html = _html('<img alt="Foto de Ana">Conta verificada')

    info = extract_seller_info(html)

    assert info.conta_verificada is True


def test_com_avaliacoes_extrai_nota_quando_padrao_bate():
    # Formato de "há avaliações" não confirmado contra amostra real
    # (a única inspecionada não tinha nenhuma) — best-effort.
    html = _html('<img alt="Foto de Ana"><span>4.8</span> avaliações')

    info = extract_seller_info(html)

    assert info.estrelas == 4.8
    assert info.tem_avaliacoes is True


def test_dataclass_seller_info_tem_defaults_none():
    info = SellerInfo()

    assert info.nome is None
    assert info.membro_desde is None
    assert info.conta_verificada is None
    assert info.verificacoes == {}
    assert info.estrelas is None
    assert info.tem_avaliacoes is None
