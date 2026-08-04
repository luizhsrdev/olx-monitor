from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Anuncio

_SCHEMA = """
CREATE TABLE IF NOT EXISTS anuncios_vistos (
    monitor_nome TEXT NOT NULL,
    fonte TEXT NOT NULL,
    anuncio_id TEXT NOT NULL,
    visto_em TEXT NOT NULL,
    PRIMARY KEY (monitor_nome, fonte, anuncio_id)
);
"""

# Tabela própria para o dedupe de cupons — deliberadamente separada de
# anuncios_vistos (cupom não é anúncio, não tem monitor_nome/fonte).
# Mesma conexão/lock/arquivo .db, infraestrutura reutilizada sem forçar
# o cupom na forma do Anuncio.
#
# Chave composta (codigo, categoria_id): descobrimos com dado real que
# o mesmo código de cupom (ex. "TECH5") pode valer pra várias categorias
# diferentes ao mesmo tempo, cada uma com seu próprio card na página.
# Com só "codigo" como chave, a primeira categoria vista "consumia" o
# dedupe e categorias novas com o mesmo código nunca mais notificavam.
_SCHEMA_CUPONS = """
CREATE TABLE IF NOT EXISTS cupons_vistos (
    codigo TEXT NOT NULL,
    categoria_id TEXT NOT NULL,
    visto_em TEXT NOT NULL,
    PRIMARY KEY (codigo, categoria_id)
);
"""


class Store:
    """Camada de dedupe em SQLite. Lembra quais anúncios já foram
    vistos por monitor, entre reinicializações do processo.

    Uma única conexão é compartilhada entre as threads dos monitores,
    serializada por um lock — o volume de escrita aqui é baixo o
    suficiente para isso não ser gargalo.
    """

    def __init__(self, caminho_db: str | Path):
        self._conexao = sqlite3.connect(str(caminho_db), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conexao.execute(_SCHEMA)
            self._migrar_schema_cupons_se_necessario()
            self._conexao.execute(_SCHEMA_CUPONS)
            self._conexao.commit()

    def _migrar_schema_cupons_se_necessario(self) -> None:
        """cupons_vistos mudou de chave simples (codigo) pra composta
        (codigo, categoria_id). Um banco criado antes dessa mudança tem
        a tabela antiga, sem a coluna categoria_id — INSERT/SELECT
        contra o schema novo quebrariam nela. Dedupe de cupom é dado
        efêmero e de baixo risco perder (pior caso: um cupom já visto
        notifica de novo uma vez após o upgrade), então é mais simples
        recriar a tabela do que migrar linha a linha. Deve ser chamado
        dentro do lock, antes de CREATE TABLE IF NOT EXISTS."""
        cursor = self._conexao.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cupons_vistos'"
        )
        if cursor.fetchone() is None:
            return  # tabela não existe ainda, nada pra migrar
        colunas = {
            row[1] for row in self._conexao.execute("PRAGMA table_info(cupons_vistos)")
        }
        if "categoria_id" not in colunas:
            self._conexao.execute("DROP TABLE cupons_vistos")

    def eh_primeira_execucao(self, monitor_nome: str) -> bool:
        """True se este monitor nunca teve nenhum anúncio registrado —
        usado para não disparar uma enxurrada de alertas na primeira
        rodada (ou quando um monitor novo é adicionado a um banco já
        existente)."""
        with self._lock:
            cursor = self._conexao.execute(
                "SELECT 1 FROM anuncios_vistos WHERE monitor_nome = ? LIMIT 1",
                (monitor_nome,),
            )
            return cursor.fetchone() is None

    def filtrar_novos(self, monitor_nome: str, anuncios: list[Anuncio]) -> list[Anuncio]:
        """Retorna, preservando a ordem, os anúncios que ainda não
        estão registrados para este monitor. Não marca nada como visto."""
        if not anuncios:
            return []
        ids = [a.id for a in anuncios]
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            cursor = self._conexao.execute(
                "SELECT anuncio_id FROM anuncios_vistos "
                f"WHERE monitor_nome = ? AND fonte = ? AND anuncio_id IN ({placeholders})",
                (monitor_nome, anuncios[0].fonte, *ids),
            )
            vistos = {row[0] for row in cursor.fetchall()}
        return [a for a in anuncios if a.id not in vistos]

    def marcar_vistos(self, monitor_nome: str, anuncios: list[Anuncio]) -> None:
        if not anuncios:
            return
        agora = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conexao.executemany(
                "INSERT OR IGNORE INTO anuncios_vistos "
                "(monitor_nome, fonte, anuncio_id, visto_em) VALUES (?, ?, ?, ?)",
                [(monitor_nome, a.fonte, a.id, agora) for a in anuncios],
            )
            self._conexao.commit()

    def eh_primeira_execucao_cupons(self) -> bool:
        """True se nunca registramos nenhum cupom — mesma regra do
        eh_primeira_execucao() de anúncios, aplicada à tabela própria
        de cupons: só popula na primeira rodada, sem notificar."""
        with self._lock:
            cursor = self._conexao.execute("SELECT 1 FROM cupons_vistos LIMIT 1")
            return cursor.fetchone() is None

    def cupons_novos(self, chaves: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """`chaves` é uma lista de (codigo, categoria_id). Retorna,
        preservando a ordem, as que ainda não estão registradas. Não
        marca nada como visto.

        Consulta uma chave por vez em vez de um IN batelado: o volume
        de cupons por ciclo é sempre pequeno (unidades), e SQLite não
        tem uma forma simples/portável de IN com tuplas — não vale a
        complexidade de montar isso na mão."""
        if not chaves:
            return []
        with self._lock:
            vistas = set()
            for codigo, categoria_id in chaves:
                cursor = self._conexao.execute(
                    "SELECT 1 FROM cupons_vistos WHERE codigo = ? AND categoria_id = ?",
                    (codigo, categoria_id),
                )
                if cursor.fetchone() is not None:
                    vistas.add((codigo, categoria_id))
        return [c for c in chaves if c not in vistas]

    def marcar_cupons_vistos(self, chaves: list[tuple[str, str]]) -> None:
        """`chaves` é uma lista de (codigo, categoria_id)."""
        if not chaves:
            return
        agora = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conexao.executemany(
                "INSERT OR IGNORE INTO cupons_vistos (codigo, categoria_id, visto_em) "
                "VALUES (?, ?, ?)",
                [(codigo, categoria_id, agora) for codigo, categoria_id in chaves],
            )
            self._conexao.commit()

    def limpar_antigos(self, dias: int = 90) -> int:
        """Remove registros de anúncios e cupons vistos há mais de
        `dias` dias.

        Sem isso, as tabelas de dedupe crescem para sempre num deploy
        24/7 de longo prazo — algo visto uma vez nunca mais precisa ser
        lembrado depois que já saiu de circulação há muito tempo.
        Retorna quantas linhas foram removidas ao todo (as duas
        tabelas)."""
        limite = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
        with self._lock:
            cursor_anuncios = self._conexao.execute(
                "DELETE FROM anuncios_vistos WHERE visto_em < ?", (limite,)
            )
            cursor_cupons = self._conexao.execute(
                "DELETE FROM cupons_vistos WHERE visto_em < ?", (limite,)
            )
            self._conexao.commit()
            return cursor_anuncios.rowcount + cursor_cupons.rowcount

    def close(self) -> None:
        # Adquire o lock antes de fechar: se uma thread de monitor
        # estiver no meio de execute()/commit() nesse instante, fechar
        # a conexão por baixo dela é acesso concorrente indefinido no
        # nível do SQLite (pode corromper o banco, não só levantar
        # exceção). O chamador ainda precisa garantir que as threads
        # já pararam de gerar trabalho novo antes de chamar close() —
        # ver thread.join() em run.py.
        with self._lock:
            self._conexao.close()
