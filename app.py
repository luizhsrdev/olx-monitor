"""Painel local (Streamlit) para editar monitores.yaml sem mexer em YAML na
mão. Ferramenta de conveniência para gerenciar CRUD de monitores — não faz
parte do pipeline de monitoramento em si, que continua sendo `python run.py`
rodando como processo separado. Não integra, não inicia nem para o monitor.

Uso:
    streamlit run app.py

Reescreve monitores.yaml inteiro a cada salvamento (não preserva comentários
inline do arquivo original — ver README). O bloco `telegram:` nunca é lido
com substituição de variável de ambiente, então os placeholders
${TELEGRAM_TOKEN} / ${TELEGRAM_CHAT_ID} sempre voltam intactos: o painel
simplesmente nunca resolve essas strings, então não existe valor real para
vazar de volta ao arquivo.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from olx_monitor.config import INTERVALO_MINIMO_SEGUNDOS

CAMINHO_YAML = Path("monitores.yaml")


# --- Funções puras (sem dependência do runtime do Streamlit) -------------
# Mantidas fora de main() para poderem ser importadas/testadas isoladamente
# sem disparar a renderização da página inteira.


def carregar_dados(caminho: Path = CAMINHO_YAML) -> dict:
    """Lê o YAML cru, sem substituir ${VAR} — é isso que garante que os
    placeholders do bloco telegram nunca sejam resolvidos pelo painel."""
    texto = caminho.read_text(encoding="utf-8")
    dados = yaml.safe_load(texto)
    if not dados:
        raise ValueError(f"'{caminho}' está vazio ou inválido.")
    dados.setdefault("monitores", [])
    return dados


def salvar_dados(dados: dict, caminho: Path = CAMINHO_YAML) -> None:
    with caminho.open("w", encoding="utf-8") as f:
        yaml.safe_dump(
            dados,
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=1_000_000,  # evita quebrar URLs longas em múltiplas linhas
        )


def parse_lista_virgula(texto: str) -> list[str]:
    return [item.strip() for item in texto.split(",") if item.strip()]


def parse_urls(texto: str) -> list[str]:
    return [linha.strip() for linha in texto.splitlines() if linha.strip()]


def resumo_filtros(monitor: dict) -> str:
    n_obrig = len(monitor.get("obrigatorias_ou") or [])
    n_bloq = len(monitor.get("bloqueadas") or [])
    n_prior = len(monitor.get("prioritarias") or [])
    return f"{n_obrig} obrigatórias, {n_bloq} bloqueadas, {n_prior} prioritárias"


def validar_monitor(
    nome: str, preco_max: float | None, urls: list[str], intervalo_segundos: int
) -> list[str]:
    erros = []
    if not nome.strip():
        erros.append("Nome não pode ser vazio.")
    if preco_max is None or preco_max <= 0:
        erros.append("Preço máximo precisa ser um número positivo.")
    if not urls:
        erros.append("Precisa de pelo menos uma URL de busca.")
    if intervalo_segundos < INTERVALO_MINIMO_SEGUNDOS:
        erros.append(
            f"Intervalo não pode ser menor que {INTERVALO_MINIMO_SEGUNDOS}s — "
            "abaixo disso o risco de rate limit/bloqueio da OLX supera o "
            "tempo que se economiza (mesma regra que o run.py aplica)."
        )
    return erros


def montar_monitor(
    nome: str,
    ativo: bool,
    urls: list[str],
    preco_max: float,
    intervalo_segundos: int,
    obrigatorias_ou: list[str],
    bloqueadas: list[str],
    prioritarias: list[str],
    fonte: str = "olx",
    modo: str | None = None,
) -> dict:
    monitor = {
        "nome": nome.strip(),
        "ativo": ativo,
        "fonte": fonte,
        "intervalo_segundos": int(intervalo_segundos),
        "urls": urls,
        "preco_max": float(preco_max),
        "bloqueadas": bloqueadas,
        "obrigatorias_ou": obrigatorias_ou,
        "prioritarias": prioritarias,
    }
    if modo:
        monitor["modo"] = modo
    return monitor


# --- UI (Streamlit) --------------------------------------------------------
# Só é executada quando o arquivo roda como script principal (é o caso tanto
# de `streamlit run app.py` quanto de `python app.py`) — importar este
# módulo para reusar as funções acima não dispara a renderização da página.


def _renderizar_formulario(st, dados: dict, monitor_existente: dict | None) -> None:
    """monitor_existente=None -> modo criação; senão, modo edição."""
    eh_edicao = monitor_existente is not None
    chave_form = f"form_{monitor_existente['nome']}" if eh_edicao else "form_novo_monitor"
    base = monitor_existente or {}

    with st.form(chave_form, clear_on_submit=not eh_edicao):
        nome = st.text_input("Nome", value=base.get("nome", ""))
        ativo = st.checkbox("Ativo", value=base.get("ativo", True))
        urls_texto = st.text_area(
            "URLs de busca (uma por linha)",
            value="\n".join(base.get("urls", [])),
            height=100,
        )
        preco_max = st.number_input(
            "Preço máximo (R$)",
            min_value=0.0,
            value=float(base.get("preco_max") or 0.0),
            step=50.0,
        )
        intervalo_segundos = st.number_input(
            "Intervalo entre consultas (segundos)",
            min_value=0,
            value=int(base.get("intervalo_segundos", 60)),
            step=10,
            help=f"Nunca pode ser menor que {INTERVALO_MINIMO_SEGUNDOS}s.",
        )
        obrigatorias_texto = st.text_input(
            "Obrigatórias — pelo menos uma precisa estar no título (separadas por vírgula)",
            value=", ".join(base.get("obrigatorias_ou", [])),
        )
        bloqueadas_texto = st.text_input(
            "Bloqueadas — descarta se qualquer uma aparecer no título (separadas por vírgula)",
            value=", ".join(base.get("bloqueadas", [])),
        )
        prioritarias_texto = st.text_input(
            "Prioritárias — só marca o alerta, não filtra (separadas por vírgula)",
            value=", ".join(base.get("prioritarias", [])),
        )

        rotulo_botao = "Salvar alterações" if eh_edicao else "Criar monitor"
        enviado = st.form_submit_button(rotulo_botao, type="primary")

    if not enviado:
        return

    urls = parse_urls(urls_texto)
    erros = validar_monitor(nome, preco_max, urls, intervalo_segundos)

    nome_normalizado = nome.strip()
    outros_nomes = {
        m["nome"] for m in dados["monitores"] if m is not monitor_existente
    }
    if nome_normalizado in outros_nomes:
        erros.append(f"Já existe um monitor chamado '{nome_normalizado}'.")

    if erros:
        for erro in erros:
            st.error(erro)
        return

    novo_monitor = montar_monitor(
        nome=nome_normalizado,
        ativo=ativo,
        urls=urls,
        preco_max=preco_max,
        intervalo_segundos=intervalo_segundos,
        obrigatorias_ou=parse_lista_virgula(obrigatorias_texto),
        bloqueadas=parse_lista_virgula(bloqueadas_texto),
        prioritarias=parse_lista_virgula(prioritarias_texto),
        fonte=base.get("fonte", "olx"),
        modo=base.get("modo"),
    )

    if eh_edicao:
        indice = next(
            i for i, m in enumerate(dados["monitores"]) if m is monitor_existente
        )
        dados["monitores"][indice] = novo_monitor
    else:
        dados["monitores"].append(novo_monitor)

    salvar_dados(dados)
    st.session_state["mensagem_sucesso"] = (
        f"Monitor '{novo_monitor['nome']}' "
        f"{'atualizado' if eh_edicao else 'criado'} com sucesso."
    )
    if eh_edicao:
        st.session_state.pop("editando", None)
    st.rerun()


def main() -> None:
    import streamlit as st

    st.set_page_config(page_title="OLX Monitor — Painel", page_icon="🛒", layout="centered")
    st.title("🛒 OLX Monitor — Painel de configuração")
    st.caption(
        "Edita `monitores.yaml` direto. Ferramenta de conveniência local — não roda o "
        "monitoramento; isso continua sendo `python run.py`, em outro processo."
    )

    if not CAMINHO_YAML.exists():
        st.error(
            f"'{CAMINHO_YAML}' não encontrado. Copie monitores.yaml.example para "
            "monitores.yaml antes de usar o painel."
        )
        st.stop()

    try:
        dados = carregar_dados()
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    if "mensagem_sucesso" in st.session_state:
        st.success(st.session_state.pop("mensagem_sucesso"))

    st.header("Monitores")

    if not dados["monitores"]:
        st.info("Nenhum monitor configurado ainda. Crie um no formulário abaixo.")

    for monitor in list(dados["monitores"]):
        nome = monitor["nome"]
        with st.container(border=True):
            col_toggle, col_info, col_editar, col_remover = st.columns([1, 3, 1, 1])

            with col_toggle:
                novo_ativo = st.toggle(
                    "Ativo", value=monitor.get("ativo", True), key=f"ativo_{nome}"
                )
            with col_info:
                preco_max = monitor.get("preco_max")
                preco_fmt = f"R$ {preco_max:.2f}" if preco_max is not None else "sem limite"
                st.markdown(f"**{nome}** — {preco_fmt}")
                st.caption(resumo_filtros(monitor))
            with col_editar:
                if st.button("Editar", key=f"editar_{nome}"):
                    st.session_state["editando"] = nome
                    st.rerun()
            with col_remover:
                if st.button("Remover", key=f"remover_{nome}"):
                    st.session_state[f"confirmar_remocao_{nome}"] = True
                    st.rerun()

            if novo_ativo != monitor.get("ativo", True):
                monitor["ativo"] = novo_ativo
                salvar_dados(dados)
                st.session_state["mensagem_sucesso"] = (
                    f"Monitor '{nome}' {'ativado' if novo_ativo else 'desativado'}."
                )
                st.rerun()

            if st.session_state.get(f"confirmar_remocao_{nome}"):
                st.warning(f"Remover o monitor **{nome}**? Essa ação não pode ser desfeita.")
                col_sim, col_nao = st.columns(2)
                with col_sim:
                    if st.button("Sim, remover", key=f"confirma_sim_{nome}", type="primary"):
                        dados["monitores"] = [
                            m for m in dados["monitores"] if m["nome"] != nome
                        ]
                        salvar_dados(dados)
                        st.session_state.pop(f"confirmar_remocao_{nome}", None)
                        if st.session_state.get("editando") == nome:
                            st.session_state.pop("editando", None)
                        st.session_state["mensagem_sucesso"] = f"Monitor '{nome}' removido."
                        st.rerun()
                with col_nao:
                    if st.button("Cancelar", key=f"confirma_nao_{nome}"):
                        st.session_state.pop(f"confirmar_remocao_{nome}", None)
                        st.rerun()

    nome_editando = st.session_state.get("editando")
    if nome_editando:
        monitor_existente = next(
            (m for m in dados["monitores"] if m["nome"] == nome_editando), None
        )
        if monitor_existente is None:
            st.session_state.pop("editando", None)
        else:
            st.header(f"Editar monitor: {nome_editando}")
            if st.button("Cancelar edição"):
                st.session_state.pop("editando", None)
                st.rerun()
            _renderizar_formulario(st, dados, monitor_existente)

    st.header("Criar novo monitor")
    _renderizar_formulario(st, dados, None)


if __name__ == "__main__":
    main()
