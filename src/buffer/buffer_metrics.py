"""
Métricas de desempenho e auditoria do Gerente de Buffer Pool.
Conforme especificado em PLANO_IMPLEMENTACAO.md (Módulo 2.9).
"""
from __future__ import annotations
from dataclasses import dataclass, asdict


@dataclass
class BufferMetrics:
    """
    Rastreia contadores de desempenho do Buffer Pool (global ou por partição).
    """
    requests: int = 0
    hits: int = 0
    misses: int = 0
    dirty_page_evictions: int = 0
    forced_writes: int = 0
    pinned_count_current: int = 0

    @property
    def hit_ratio(self) -> float:
        """Taxa de acerto do buffer pool em porcentagem (0.0 a 100.0)."""
        if self.requests == 0:
            return 0.0
        return (self.hits / self.requests) * 100.0

    def record_request(self, hit: bool) -> None:
        """Registra uma requisição de página (pin_page)."""
        self.requests += 1
        if hit:
            self.hits += 1
        else:
            self.misses += 1

    def record_dirty_eviction(self) -> None:
        """Registra gravação física de página dirty expulsa pelo replacer."""
        self.dirty_page_evictions += 1

    def record_forced_write(self) -> None:
        """Registra gravação forçada imediata (force_page)."""
        self.forced_writes += 1

    def reset(self) -> None:
        """Zera todos os contadores de requisição e I/O do buffer."""
        self.requests = 0
        self.hits = 0
        self.misses = 0
        self.dirty_page_evictions = 0
        self.forced_writes = 0
        # pinned_count_current não é zerado aqui pois reflete o estado atual dos frames

    def copy(self) -> BufferMetrics:
        """Retorna uma cópia snapshot das métricas."""
        return BufferMetrics(
            requests=self.requests,
            hits=self.hits,
            misses=self.misses,
            dirty_page_evictions=self.dirty_page_evictions,
            forced_writes=self.forced_writes,
            pinned_count_current=self.pinned_count_current,
        )

    def to_dict(self) -> dict:
        """Exporta métricas formatadas em dicionário para geração de relatórios."""
        d = asdict(self)
        d["hit_ratio"] = self.hit_ratio
        return d
