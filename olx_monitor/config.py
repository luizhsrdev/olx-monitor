from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

INTERVALO_MINIMO_SEGUNDOS = 30

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

FONTES_SUPORTADAS = {"olx"}
MODOS_SUPORTADOS = {"requests", "playwright"}


class ConfigError(Exception):
    """Erro de configuração inválida em monitores.yaml."""


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str


@dataclass(frozen=True)
class PadroesConfig:
    intervalo_segundos: int
    jitter_segundos: int
    bloqueadas_globais: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MonitorConfig:
    nome: str
    ativo: bool
    fonte: str
    modo: str
    intervalo_segundos: int
    jitter_segundos: int
    urls: list[str]
    preco_max: float | None
    bloqueadas: list[str]
    obrigatorias_ou: list[str]
    prioritarias: list[str]


@dataclass(frozen=True)
class AppConfig:
    telegram: TelegramConfig
    padroes: PadroesConfig
    monitores: list[MonitorConfig]
    bloqueadas_globais: list[str]


def _substituir_env_vars(valor):
    if isinstance(valor, str):

        def _replace(match: re.Match) -> str:
            nome_var = match.group(1)
            if nome_var not in os.environ:
                raise ConfigError(
                    f"Variável de ambiente '{nome_var}' referenciada em monitores.yaml "
                    "não está definida. Confira seu arquivo .env."
                )
            return os.environ[nome_var]

        return _ENV_VAR_PATTERN.sub(_replace, valor)
    if isinstance(valor, dict):
        return {chave: _substituir_env_vars(v) for chave, v in valor.items()}
    if isinstance(valor, list):
        return [_substituir_env_vars(v) for v in valor]
    return valor


def _validar_intervalo(contexto: str, intervalo_segundos: int) -> None:
    if intervalo_segundos < INTERVALO_MINIMO_SEGUNDOS:
        raise ConfigError(
            f"'{contexto}': intervalo_segundos={intervalo_segundos} é menor que o mínimo "
            f"permitido ({INTERVALO_MINIMO_SEGUNDOS}s). Isso existe para não tomar rate "
            "limit/bloqueio da OLX — não reduza sem necessidade."
        )


def load_config(caminho: str | Path) -> AppConfig:
    caminho = Path(caminho)
    if not caminho.exists():
        raise ConfigError(
            f"Arquivo de configuração não encontrado: {caminho}. "
            "Copie monitores.yaml.example para monitores.yaml e ajuste."
        )

    try:
        bruto = yaml.safe_load(caminho.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"monitores.yaml inválido: {exc}") from exc

    if not bruto:
        raise ConfigError("monitores.yaml está vazio.")

    bruto = _substituir_env_vars(bruto)

    telegram_bruto = bruto.get("telegram") or {}
    token = telegram_bruto.get("token")
    chat_id = telegram_bruto.get("chat_id")
    if not token or not chat_id:
        raise ConfigError(
            "Seção 'telegram' precisa de 'token' e 'chat_id' preenchidos "
            "(via variáveis de ambiente ${TELEGRAM_TOKEN} / ${TELEGRAM_CHAT_ID})."
        )
    telegram = TelegramConfig(token=str(token), chat_id=str(chat_id))

    padroes_bruto = bruto.get("padroes") or {}
    intervalo_padrao = int(padroes_bruto.get("intervalo_segundos", 60))
    jitter_padrao = int(padroes_bruto.get("jitter_segundos", 0))
    bloqueadas_globais = [str(t) for t in (padroes_bruto.get("bloqueadas_globais") or [])]

    _validar_intervalo("padroes.intervalo_segundos", intervalo_padrao)

    monitores_brutos = bruto.get("monitores") or []
    if not monitores_brutos:
        raise ConfigError("Nenhum monitor definido em 'monitores'.")

    monitores: list[MonitorConfig] = []
    nomes_vistos: set[str] = set()

    for item in monitores_brutos:
        nome = item.get("nome")
        if not nome:
            raise ConfigError("Todo monitor precisa de um campo 'nome'.")
        if nome in nomes_vistos:
            raise ConfigError(f"Nome de monitor duplicado: '{nome}'.")
        nomes_vistos.add(nome)

        fonte = item.get("fonte", "olx")
        if fonte not in FONTES_SUPORTADAS:
            raise ConfigError(
                f"Monitor '{nome}': fonte '{fonte}' não suportada "
                f"(disponíveis: {sorted(FONTES_SUPORTADAS)})."
            )

        urls = item.get("urls") or []
        if not urls:
            raise ConfigError(f"Monitor '{nome}' não tem nenhuma URL em 'urls'.")

        intervalo = int(item.get("intervalo_segundos", intervalo_padrao))
        _validar_intervalo(f"monitores.{nome}.intervalo_segundos", intervalo)

        modo = item.get("modo", "requests")
        if modo not in MODOS_SUPORTADOS:
            raise ConfigError(
                f"Monitor '{nome}': modo '{modo}' inválido (use 'requests' ou 'playwright')."
            )

        preco_max = item.get("preco_max")

        monitores.append(
            MonitorConfig(
                nome=str(nome),
                ativo=bool(item.get("ativo", True)),
                fonte=fonte,
                modo=modo,
                intervalo_segundos=intervalo,
                jitter_segundos=int(item.get("jitter_segundos", jitter_padrao)),
                urls=[str(u) for u in urls],
                preco_max=float(preco_max) if preco_max is not None else None,
                bloqueadas=[str(t) for t in (item.get("bloqueadas") or [])],
                obrigatorias_ou=[str(t) for t in (item.get("obrigatorias_ou") or [])],
                prioritarias=[str(t) for t in (item.get("prioritarias") or [])],
            )
        )

    return AppConfig(
        telegram=telegram,
        padroes=PadroesConfig(intervalo_padrao, jitter_padrao, bloqueadas_globais),
        monitores=monitores,
        bloqueadas_globais=bloqueadas_globais,
    )
