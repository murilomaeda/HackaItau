"""KeywordKnowledgeRetriever: chunking por seção `##` + score por palavras-chave (sem embeddings, sem vector DB).

O score é a fração do peso da pergunta encontrada no trecho. Palavra rara pesa mais que palavra comum, e palavra que
não existe na base pesa o máximo: "preço" sozinho não basta para uma pergunta sobre algo que a base não tem.
"""

import math
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.governance.bootstrap_resolver import normalize

# sem acento, porque são comparadas depois de `normalize`
_STOPWORDS = frozenset(
    "a o as os de da do das dos e em um uma para por com no na nos nas que se ao aos ou como sobre sao "
    "qual quais quanto quanta quantos quantas quando onde quem esta estao tem ter ser sera foi "
    "possivel possiveis existe existem posso pode podem preciso precisa hoje agora aqui isso isto esse essa "
    "meu minha voce tipo tipos".split()
)


def _stem(token: str) -> str:
    return token[:-1] if len(token) > 4 and token.endswith("s") else token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", normalize(text)) if len(t) > 2 and t not in _STOPWORDS]


class KeywordKnowledgeRetriever:
    def __init__(self, corpus_dir: Path | None = None) -> None:
        self._dir = corpus_dir or get_settings().knowledge_corpus_dir
        self._chunks: list[dict[str, Any]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        if self._chunks is None:
            chunks = []
            for path in sorted(self._dir.glob("*.md")):
                chunks.extend(self._chunk(path.stem, path.read_text(encoding="utf-8")))
            self._chunks = chunks
        return self._chunks

    @staticmethod
    def _chunk(doc_id: str, text: str) -> list[dict[str, Any]]:
        doc_title = ""
        sections: list[tuple[str, list[str]]] = []
        preamble: list[str] = []
        for line in text.splitlines():
            if line.startswith("# ") and not doc_title:
                doc_title = line[2:].strip()
            elif line.startswith("## "):
                sections.append((line[3:].strip(), []))
            elif sections:
                sections[-1][1].append(line)
            elif doc_title:
                preamble.append(line)
        if not sections:  # documento sem seções não pode sumir da busca
            sections = [(doc_title, preamble)]
        out = []
        for i, (title, body_lines) in enumerate(sections, start=1):
            body = "\n".join(body_lines).strip()
            if not body:
                continue
            out.append(
                {
                    "doc_id": doc_id,
                    "chunk_index": i,
                    "title": f"{doc_title} — {title}" if doc_title else title,
                    "text": body,
                    "_tokens": set(tokenize(title + " " + body)),
                    "_title_tokens": set(tokenize(title)),
                }
            )
        return out

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        q = set(tokenize(query))
        if not q:
            return []
        chunks = self._load()
        n = len(chunks)
        weight = {t: math.log(1 + n / max(1, sum(t in c["_tokens"] for c in chunks))) for t in q}
        total = sum(weight.values())
        scored = []
        for c in chunks:
            hits = q & c["_tokens"]
            if hits:
                # empate: a seção cujo título fala do assunto vem antes da que só o menciona
                scored.append((sum(weight[t] for t in hits) / total, len(q & c["_title_tokens"]), c))
        scored.sort(key=lambda s: (-round(s[0], 6), -s[1], s[2]["doc_id"], s[2]["chunk_index"]))
        return [
            {k: v for k, v in c.items() if not k.startswith("_")} | {"score": round(score, 3)}
            for score, _, c in scored[:top_k]
        ]
