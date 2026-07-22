from __future__ import annotations

import unicodedata


def normalize_text(texto: str) -> str:
    """Lowercase e remove acentos, para comparação robusta de texto.

    Necessário porque o mesmo anúncio pode ter "peças" ou "pecas" no
    título, e a comparação de filtro precisa ser insensível a isso.
    """
    sem_acento = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return sem_acento.lower()


def contains_any(texto_normalizado: str, termos: list[str]) -> str | None:
    """Retorna o primeiro termo de `termos` presente em `texto_normalizado`.

    `texto_normalizado` já deve ter passado por `normalize_text`. Os
    termos de busca são normalizados aqui, um a um.
    """
    for termo in termos:
        if normalize_text(termo) in texto_normalizado:
            return termo
    return None


def contains_all(texto_normalizado: str, termos: list[str]) -> list[str]:
    """Retorna todos os termos de `termos` presentes em `texto_normalizado`,
    na ordem em que aparecem em `termos` (não na ordem em que aparecem no
    texto). Lista vazia = nenhum termo bateu.
    """
    return [termo for termo in termos if normalize_text(termo) in texto_normalizado]
