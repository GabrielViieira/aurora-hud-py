"""Níveis de risco e política de caminhos permitidos."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path


class Risk(IntEnum):
    """Risco crescente de uma ação.

    L0 — leitura/observação sem efeito colateral.
    L1 — escrita/execução contida no workspace autorizado.
    L2 — efeito externo ou difícil de desfazer (abrir URL, mover arquivos).
    L3 — destrutivo/elevado (apagar, instalar, privilégios, credenciais).
    """

    L0 = 0
    L1 = 1
    L2 = 2
    L3 = 3

    @classmethod
    def parse(cls, value: object, default: "Risk" = None) -> "Risk":  # type: ignore[assignment]
        if isinstance(value, Risk):
            return value
        if isinstance(value, int):
            return cls(max(0, min(3, value)))
        text = str(value or "").strip().upper()
        if text in cls.__members__:
            return cls[text]
        if default is not None:
            return default
        raise ValueError(f"nível de risco inválido: {value!r}")

    @property
    def label(self) -> str:
        return self.name


class PathDenied(PermissionError):
    """Caminho fora das raízes autorizadas ou marcado como sensível."""


DEFAULT_DENY_PATTERNS: tuple[str, ...] = (
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    "credentials*",
    "secrets*",
    "*.kdbx",
    ".env",
    ".env.*",
    "*.env",
)


@dataclass(frozen=True)
class PermissionPolicy:
    allowed_roots: tuple[Path, ...]
    deny_patterns: tuple[str, ...] = DEFAULT_DENY_PATTERNS
    allow_sensitive: bool = False

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise ValueError("pelo menos uma raiz autorizada é obrigatória")

    # ------------------------------------------------------------------ path
    def is_sensitive(self, path: Path) -> bool:
        name = path.name.lower()
        if name == ".env.example":
            return False
        for pattern in self.deny_patterns:
            if fnmatch.fnmatch(name, pattern.lower()):
                return True
        return False

    def _inside(self, candidate: Path) -> bool:
        candidate_norm = os.path.normcase(str(candidate))
        for root in self.allowed_roots:
            root_norm = os.path.normcase(str(root.resolve()))
            try:
                if os.path.commonpath([candidate_norm, root_norm]) == root_norm:
                    return True
            except ValueError:
                continue
        return False

    def resolve(
        self,
        raw: str | Path,
        *,
        must_exist: bool = False,
        for_write: bool = False,
    ) -> Path:
        """Resolve e valida um caminho contra as raízes autorizadas.

        - impede fuga com `..`, symlink ou drive diferente;
        - para escrita, valida o pai mais próximo que já existe;
        - recusa arquivos sensíveis, salvo `allow_sensitive=True`.
        """
        if raw is None or str(raw).strip() == "":
            raise PathDenied("caminho vazio")

        expanded = os.path.expandvars(str(raw)).strip()
        candidate = Path(os.path.expanduser(expanded))
        if not candidate.is_absolute():
            raise PathDenied(
                f"caminho precisa ser absoluto ou relativo ao workspace: {raw!r}"
            )

        resolved = candidate.resolve(strict=False)
        anchor = resolved
        if for_write or not resolved.exists():
            anchor = resolved if resolved.exists() else resolved.parent
            while not anchor.exists() and anchor != anchor.parent:
                anchor = anchor.parent
            anchor = anchor.resolve(strict=False)

        if not self._inside(anchor) or not self._inside(resolved):
            roots = "; ".join(str(root) for root in self.allowed_roots)
            raise PathDenied(
                f"acesso negado a {resolved}: fora das raízes autorizadas ({roots})"
            )
        if self.is_sensitive(resolved) and not self.allow_sensitive:
            raise PathDenied(f"arquivo sensível protegido: {resolved.name}")
        if must_exist and not resolved.exists():
            raise FileNotFoundError(f"arquivo não encontrado: {resolved}")
        if resolved.exists() and resolved.is_dir() and not for_write:
            # diretório é válido para listagem; leitura de conteúdo falha depois
            return resolved
        return resolved

    def describe(self) -> str:
        roots = ", ".join(str(root) for root in self.allowed_roots)
        return f"raízes autorizadas: {roots}"
