"""
Estrutura de Contadores de I/O Físico.
Conforme especificado em PLANO_IMPLEMENTACAO.md (Seção 1.5).
Distinção precisa entre acessos sequenciais e aleatórios (seeks),
tanto por arquivo individual quanto no acumulador global.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from src.storage.constants import BLOCK_SIZE


@dataclass
class IOCounters:
    """
    Rastreia contadores físicos de I/O de disco para arquivos binários.
    """
    physical_reads_sequential: int = 0
    physical_reads_random: int = 0
    physical_writes_sequential: int = 0
    physical_writes_random: int = 0
    total_seeks: int = 0
    seek_distance_sum: int = 0

    @property
    def total_reads(self) -> int:
        """Total consolidado de leituras físicas."""
        return self.physical_reads_sequential + self.physical_reads_random

    @property
    def total_writes(self) -> int:
        """Total consolidado de gravações físicas."""
        return self.physical_writes_sequential + self.physical_writes_random

    @property
    def total_blocks_transferred(self) -> int:
        """Total de blocos transferidos entre disco e memória."""
        return self.total_reads + self.total_writes

    @property
    def total_bytes_transferred(self) -> int:
        """Volume total de dados transferidos em bytes."""
        return self.total_blocks_transferred * BLOCK_SIZE

    def reset(self) -> None:
        """Zera todos os contadores."""
        self.physical_reads_sequential = 0
        self.physical_reads_random = 0
        self.physical_writes_sequential = 0
        self.physical_writes_random = 0
        self.total_seeks = 0
        self.seek_distance_sum = 0

    def record_read(self, is_sequential: bool, seek_distance: int = 0) -> None:
        """Registra uma operação de leitura física."""
        if is_sequential:
            self.physical_reads_sequential += 1
        else:
            self.physical_reads_random += 1
            self.total_seeks += 1
            self.seek_distance_sum += seek_distance

    def record_write(self, is_sequential: bool, seek_distance: int = 0) -> None:
        """Registra uma operação de gravação física."""
        if is_sequential:
            self.physical_writes_sequential += 1
        else:
            self.physical_writes_random += 1
            self.total_seeks += 1
            self.seek_distance_sum += seek_distance

    def add(self, other: IOCounters) -> None:
        """Soma contadores de outra instância."""
        self.physical_reads_sequential += other.physical_reads_sequential
        self.physical_reads_random += other.physical_reads_random
        self.physical_writes_sequential += other.physical_writes_sequential
        self.physical_writes_random += other.physical_writes_random
        self.total_seeks += other.total_seeks
        self.seek_distance_sum += other.seek_distance_sum

    def copy(self) -> IOCounters:
        """Retorna uma cópia snapshot dos contadores."""
        return IOCounters(
            physical_reads_sequential=self.physical_reads_sequential,
            physical_reads_random=self.physical_reads_random,
            physical_writes_sequential=self.physical_writes_sequential,
            physical_writes_random=self.physical_writes_random,
            total_seeks=self.total_seeks,
            seek_distance_sum=self.seek_distance_sum,
        )

    def to_dict(self) -> dict:
        """Exporta um dicionário com todos os contadores e métricas derivadas."""
        d = asdict(self)
        d["total_reads"] = self.total_reads
        d["total_writes"] = self.total_writes
        d["total_blocks_transferred"] = self.total_blocks_transferred
        d["total_bytes_transferred"] = self.total_bytes_transferred
        return d
