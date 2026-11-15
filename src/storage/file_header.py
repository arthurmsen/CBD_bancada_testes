"""
Estrutura do Cabeçalho do Arquivo (Descritor / Bloco 0).
Conforme especificado em PLANO_IMPLEMENTACAO.md (Seção 1.3).
"""
from __future__ import annotations
import struct
from dataclasses import dataclass
from src.storage.constants import (
    BLOCK_SIZE,
    MAGIC_NUMBER,
    NO_BLOCK,
    FILE_TYPE_HEAP,
    FILE_TYPE_NAMES,
)

# Estrutura binária: 10 inteiros sem sinal de 32 bits (little-endian) + 4056 bytes reservados
HEADER_FORMAT: str = "<10I4056s"
HEADER_SIZE: int = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == BLOCK_SIZE, f"Tamanho do cabeçalho ({HEADER_SIZE}) deve ser exatamente igual a BLOCK_SIZE ({BLOCK_SIZE})"


@dataclass
class FileHeader:
    """
    Representação em memória dos metadados persistidos no Bloco 0 de qualquer arquivo de dados/índice.
    """
    magic_number: int = MAGIC_NUMBER
    file_type: int = FILE_TYPE_HEAP
    block_size: int = BLOCK_SIZE
    total_blocks: int = 1         # Inicialmente apenas o Bloco 0 está alocado
    free_list_head: int = NO_BLOCK
    first_data_blk: int = NO_BLOCK
    last_data_blk: int = NO_BLOCK
    root_block_id: int = NO_BLOCK
    record_count: int = 0
    schema_version: int = 1
    reserved: bytes = b"\x00" * 4056

    def to_bytes(self) -> bytes:
        """Serializa o cabeçalho para exatamente BLOCK_SIZE (4096) bytes."""
        reserved_bytes = self.reserved
        if len(reserved_bytes) < 4056:
            reserved_bytes = reserved_bytes.ljust(4056, b"\x00")
        elif len(reserved_bytes) > 4056:
            reserved_bytes = reserved_bytes[:4056]

        return struct.pack(
            HEADER_FORMAT,
            self.magic_number,
            self.file_type,
            self.block_size,
            self.total_blocks,
            self.free_list_head,
            self.first_data_blk,
            self.last_data_blk,
            self.root_block_id,
            self.record_count,
            self.schema_version,
            reserved_bytes,
        )

    @classmethod
    def from_bytes(cls, data: bytes | bytearray) -> FileHeader:
        """Deserializa um bloco de 4096 bytes em uma instância de FileHeader."""
        if len(data) < BLOCK_SIZE:
            raise ValueError(
                f"Dados insuficientes para ler cabeçalho: esperado {BLOCK_SIZE} bytes, recebido {len(data)}"
            )

        unpacked = struct.unpack(HEADER_FORMAT, bytes(data[:BLOCK_SIZE]))
        (
            magic_number,
            file_type,
            block_size,
            total_blocks,
            free_list_head,
            first_data_blk,
            last_data_blk,
            root_block_id,
            record_count,
            schema_version,
            reserved,
        ) = unpacked

        if magic_number != MAGIC_NUMBER:
            raise ValueError(
                f"Assinatura de arquivo inválida: esperado 0x{MAGIC_NUMBER:08X}, encontrado 0x{magic_number:08X}"
            )

        if block_size != BLOCK_SIZE:
            raise ValueError(
                f"Tamanho de bloco incompatível: esperado {BLOCK_SIZE}, encontrado {block_size}"
            )

        return cls(
            magic_number=magic_number,
            file_type=file_type,
            block_size=block_size,
            total_blocks=total_blocks,
            free_list_head=free_list_head,
            first_data_blk=first_data_blk,
            last_data_blk=last_data_blk,
            root_block_id=root_block_id,
            record_count=record_count,
            schema_version=schema_version,
            reserved=reserved,
        )

    def describe(self) -> str:
        """Retorna uma representação textual detalhada para inspeção."""
        type_str = FILE_TYPE_NAMES.get(self.file_type, f"UNKNOWN({self.file_type})")
        return (
            f"FileHeader(\n"
            f"  magic_number=0x{self.magic_number:08X},\n"
            f"  file_type={type_str},\n"
            f"  block_size={self.block_size} bytes,\n"
            f"  total_blocks={self.total_blocks},\n"
            f"  free_list_head={self.free_list_head if self.free_list_head != NO_BLOCK else 'None'},\n"
            f"  first_data_blk={self.first_data_blk if self.first_data_blk != NO_BLOCK else 'None'},\n"
            f"  last_data_blk={self.last_data_blk if self.last_data_blk != NO_BLOCK else 'None'},\n"
            f"  root_block_id={self.root_block_id if self.root_block_id != NO_BLOCK else 'None'},\n"
            f"  record_count={self.record_count},\n"
            f"  schema_version={self.schema_version}\n"
            f")"
        )
