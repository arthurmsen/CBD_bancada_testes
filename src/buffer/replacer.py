"""
Políticas de Substituição de Páginas (Replacers).
Conforme especificado em PLANO_IMPLEMENTACAO.md (Módulos 2.3, 2.4 e 2.7).
Políticas homologadas: LRU (Least Recently Used) e MRU (Most Recently Used),
com suporte a Toss-Immediate e Retenção Preferencial por Prioridade.
"""
from __future__ import annotations
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional, Set
from src.buffer.constants import PRIORITY_NORMAL


class Replacer(ABC):
    """
    Interface abstrata para algoritmos de substituição de páginas do buffer pool.
    """
    @abstractmethod
    def pin(self, frame_id: int) -> None:
        """Remove o frame dos candidatos a expulsão (em uso)."""
        pass

    @abstractmethod
    def unpin(
        self,
        frame_id: int,
        toss_immediate: bool = False,
        priority: int = PRIORITY_NORMAL,
    ) -> None:
        """Torna o frame elegível para expulsão quando seu pin_count atinge 0."""
        pass

    @abstractmethod
    def victim(self) -> Optional[int]:
        """Seleciona e remove um frame vítima para expulsão. Retorna None se todos estiverem fixados."""
        pass

    @abstractmethod
    def size(self) -> int:
        """Quantidade de frames atualmente elegíveis para expulsão."""
        pass

    @abstractmethod
    def remove(self, frame_id: int) -> None:
        """Remove o frame dos candidatos sem necessariamente expeli-lo como vítima."""
        pass


class BasePriorityReplacer(Replacer):
    """
    Implementação base que gerencia timestamps de acesso, prioridades de retenção
    e fila preferencial de toss-immediate.
    """
    def __init__(self):
        # Mapeia frame_id -> timestamp de acesso
        self._timestamps: Dict[int, float] = {}
        # Mapeia frame_id -> prioridade hierárquica
        self._priorities: Dict[int, int] = {}
        # Conjunto de frames desfixados com sinalização toss_immediate=True
        self._toss_immediate_frames: Set[int] = set()

    def pin(self, frame_id: int) -> None:
        self._timestamps.pop(frame_id, None)
        self._priorities.pop(frame_id, None)
        self._toss_immediate_frames.discard(frame_id)

    def unpin(
        self,
        frame_id: int,
        toss_immediate: bool = False,
        priority: int = PRIORITY_NORMAL,
    ) -> None:
        self._timestamps[frame_id] = time.monotonic()
        self._priorities[frame_id] = priority
        if toss_immediate:
            self._toss_immediate_frames.add(frame_id)
        else:
            self._toss_immediate_frames.discard(frame_id)

    def remove(self, frame_id: int) -> None:
        self.pin(frame_id)

    def size(self) -> int:
        return len(self._timestamps)


class LRUReplacer(BasePriorityReplacer):
    """
    Least Recently Used (LRU):
    1. Se houver frames desfixados com toss_immediate, expulsa um deles imediatamente.
    2. Caso contrário, identifica o menor nível de prioridade entre os candidatos
       (preservando nós de catálogo e índices de prioridade mais alta).
    3. Dentre os candidatos de menor prioridade, escolhe o de acesso mais antigo (menor timestamp).
    """
    def victim(self) -> Optional[int]:
        if not self._timestamps:
            return None

        # 1. Prioridade máxima de descarte: toss-immediate
        if self._toss_immediate_frames:
            # Dentre os toss-immediate, pega o de menor prioridade e mais antigo
            candidate_id = min(
                self._toss_immediate_frames,
                key=lambda fid: (self._priorities.get(fid, PRIORITY_NORMAL), self._timestamps[fid]),
            )
            self.pin(candidate_id)
            return candidate_id

        # 2. Retenção preferencial: menor nível de prioridade primeiro
        min_priority = min(self._priorities.values())
        candidates = [
            fid for fid, prio in self._priorities.items() if prio == min_priority
        ]

        # 3. Menor timestamp = menos recentemente usado
        victim_id = min(candidates, key=lambda fid: self._timestamps[fid])
        self.pin(victim_id)
        return victim_id


class MRUReplacer(BasePriorityReplacer):
    """
    Most Recently Used (MRU):
    1. Se houver frames com toss_immediate, expulsa um deles primeiro.
    2. Caso contrário, dentre os frames de menor prioridade, escolhe o de acesso
       mais recente (maior timestamp). Ideal para varreduras repetidas em laço (nested loops).
    """
    def victim(self) -> Optional[int]:
        if not self._timestamps:
            return None

        # 1. Toss-immediate prioritário
        if self._toss_immediate_frames:
            candidate_id = min(
                self._toss_immediate_frames,
                key=lambda fid: (self._priorities.get(fid, PRIORITY_NORMAL), -self._timestamps[fid]),
            )
            self.pin(candidate_id)
            return candidate_id

        # 2. Retenção preferencial: menor nível de prioridade primeiro
        min_priority = min(self._priorities.values())
        candidates = [
            fid for fid, prio in self._priorities.items() if prio == min_priority
        ]

        # 3. Maior timestamp = mais recentemente usado
        victim_id = max(candidates, key=lambda fid: self._timestamps[fid])
        self.pin(victim_id)
        return victim_id
