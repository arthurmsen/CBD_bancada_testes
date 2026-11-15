"""
Representação de Frame de Memória no Buffer Pool.
Conforme especificado em PLANO_IMPLEMENTACAO.md (Módulo 2.1).
"""
from __future__ import annotations
import time
from typing import Optional
from src.storage.constants import BLOCK_SIZE
from src.buffer.constants import PageType, PRIORITY_NORMAL


class Frame:
    """
    Representa um frame individual de 4096 bytes na memória volátil do Buffer Pool.
    """
    def __init__(self, frame_id: int):
        self.frame_id: int = frame_id
        self.file_id: Optional[str] = None
        self.block_id: Optional[int] = None
        self.page_type: Optional[PageType] = None
        self.data: bytearray = bytearray(BLOCK_SIZE)
        self.pin_count: int = 0
        self.is_dirty: bool = False
        self.priority: int = PRIORITY_NORMAL
        self.last_accessed_time: float = 0.0

    @property
    def is_pinned(self) -> bool:
        """Indica se o frame está fixado na memória por alguma operação ativa."""
        return self.pin_count > 0

    @property
    def is_free(self) -> bool:
        """Indica se o frame está livre (não alocado para nenhuma página)."""
        return self.file_id is None or self.block_id is None

    def load_page(
        self,
        file_id: str,
        block_id: int,
        page_type: PageType,
        data: bytes | bytearray,
        priority: int = PRIORITY_NORMAL,
    ) -> None:
        """Carrega uma nova página no frame e inicializa os metadados."""
        self.file_id = file_id
        self.block_id = block_id
        self.page_type = page_type
        self.data[:] = data[:BLOCK_SIZE]
        self.pin_count = 1
        self.is_dirty = False
        self.priority = priority
        self.last_accessed_time = time.monotonic()

    def touch(self) -> None:
        """Atualiza o timestamp de acesso do frame."""
        self.last_accessed_time = time.monotonic()

    def reset(self) -> None:
        """Esvazia o frame, liberando-o para novo uso."""
        self.file_id = None
        self.block_id = None
        self.page_type = None
        self.data[:] = b"\x00" * BLOCK_SIZE
        self.pin_count = 0
        self.is_dirty = False
        self.priority = PRIORITY_NORMAL
        self.last_accessed_time = 0.0
