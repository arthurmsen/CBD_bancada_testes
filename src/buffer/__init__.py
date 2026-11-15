"""
Módulo de Gerenciamento de Buffer Pool.
"""
from src.buffer.constants import (
    PageType,
    ReplacerType,
    DEFAULT_BUFFER_POOL_SIZE,
    PRIORITY_NORMAL,
    PRIORITY_INDEX_LEAF,
    PRIORITY_INDEX_INTERNAL,
    PRIORITY_CATALOG,
)
from src.buffer.frame import Frame
from src.buffer.replacer import Replacer, LRUReplacer, MRUReplacer
from src.buffer.buffer_metrics import BufferMetrics
from src.buffer.buffer_pool_manager import (
    BufferPoolManager,
    BufferPoolFullError,
    PinGuardHandle,
)

__all__ = [
    "PageType",
    "ReplacerType",
    "DEFAULT_BUFFER_POOL_SIZE",
    "PRIORITY_NORMAL",
    "PRIORITY_INDEX_LEAF",
    "PRIORITY_INDEX_INTERNAL",
    "PRIORITY_CATALOG",
    "Frame",
    "Replacer",
    "LRUReplacer",
    "MRUReplacer",
    "BufferMetrics",
    "BufferPoolManager",
    "BufferPoolFullError",
    "PinGuardHandle",
]
