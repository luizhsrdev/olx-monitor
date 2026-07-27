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
            self._conexao.commit()

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

    def limpar_antigos(self, dias: int = 90) -> int:
        """Remove registros de anúncios vistos há mais de `dias` dias.

        Sem isso, `anuncios_vistos` cresce para sempre num deploy 24/7 de
        longo prazo — um anúncio visto uma vez nunca mais precisa ser
        lembrado depois que ele já saiu de circulação há muito tempo.
        Retorna quantas linhas foram removidas."""
        limite = (datetime.now(timezone.utc) - timedelta(days=dias)).isoformat()
        with self._lock:
            cursor = self._conexao.execute(
                "DELETE FROM anuncios_vistos WHERE visto_em < ?", (limite,)
            )
            self._conexao.commit()
            return cursor.rowcount

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
