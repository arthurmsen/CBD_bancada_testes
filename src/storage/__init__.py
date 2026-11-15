"""
Módulo de Armazenamento Físico e Gerente de Arquivos.
"""
from src.storage.constants import (
    BLOCK_SIZE,
    MAGIC_NUMBER,
    NO_BLOCK,
    FILE_TYPE_HEAP,
    FILE_TYPE_BTREE,
    FILE_TYPE_HASH,
)
from src.storage.file_header import FileHeader
from src.storage.io_counters import IOCounters
from src.storage.file_manager import FileManager, FileHandle

__all__ = [
    "BLOCK_SIZE",
    "MAGIC_NUMBER",
    "NO_BLOCK",
    "FILE_TYPE_HEAP",
    "FILE_TYPE_BTREE",
    "FILE_TYPE_HASH",
    "FileHeader",
    "IOCounters",
    "FileManager",
    "FileHandle",
]
