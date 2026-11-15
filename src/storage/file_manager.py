"""
Gerente de Arquivos (FileManager).
Conforme especificado em PLANO_IMPLEMENTACAO.md (Módulo 1).

Responsabilidades:
- Abstrair o disco através de arquivos binários do sistema operacional.
- Alocação e gerenciamento de blocos de tamanho fixo (4096 bytes) via offset direto.
- Gerência de blocos livres por Lista Encadeada (Free List) em O(1).
- Rastreamento minucioso de I/O físico (leituras, gravações, sequencial vs. aleatório/seeks),
  tanto em nível de arquivo individual quanto no barramento global.
"""
from __future__ import annotations
import os
import struct
from pathlib import Path
from typing import BinaryIO, Dict, Optional, Union
from src.storage.constants import (
    BLOCK_SIZE,
    MAGIC_NUMBER,
    NO_BLOCK,
    FILE_TYPE_HEAP,
)
from src.storage.file_header import FileHeader
from src.storage.io_counters import IOCounters


class FileHandle:
    """
    Representa a alça de um arquivo binário aberto pelo FileManager.
    """
    def __init__(self, file_id: str, path: Path, stream: BinaryIO, header: FileHeader):
        self.file_id: str = file_id
        self.path: Path = path
        self.stream: BinaryIO = stream
        self.header: FileHeader = header
        self.counters: IOCounters = IOCounters()
        self.last_accessed_block_id: Optional[int] = None
        self.header_dirty: bool = False

    def is_closed(self) -> bool:
        return self.stream.closed

    def flush(self) -> None:
        """Garante que buffers do processo sejam descarregados para o SO."""
        if not self.stream.closed:
            self.stream.flush()

    def sync(self) -> None:
        """Força a sincronização física com o disco (fsync)."""
        if not self.stream.closed:
            self.stream.flush()
            os.fsync(self.stream.fileno())

    def __enter__(self) -> FileHandle:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.flush()


class FileManager:
    """
    Gerente central de arquivos binários e I/O de blocos de disco.
    """
    def __init__(self, base_directory: Optional[Union[str, Path]] = None):
        self.base_directory: Path = Path(base_directory).resolve() if base_directory else Path.cwd()
        self.base_directory.mkdir(parents=True, exist_ok=True)

        self._open_files: Dict[str, FileHandle] = {}
        self.global_counters: IOCounters = IOCounters()
        self._global_last_file_id: Optional[str] = None
        self._global_last_block_id: Optional[int] = None

    def _resolve_path(self, path: Union[str, Path]) -> Path:
        """Resolve o caminho do arquivo em relação ao diretório base."""
        p = Path(path)
        if not p.is_absolute():
            p = self.base_directory / p
        return p.resolve()

    def create_file(
        self,
        path: Union[str, Path],
        file_type: int = FILE_TYPE_HEAP,
        schema_version: int = 1,
    ) -> FileHeader:
        """
        Cria um novo arquivo binário de dados/índice com Bloco 0 inicializado.
        Se o arquivo já existir, sobrescreve.
        """
        resolved_path = self._resolve_path(path)
        file_id = str(resolved_path)

        if file_id in self._open_files:
            raise IOError(f"Não é possível criar arquivo já aberto: {resolved_path}")

        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        # Inicializa o cabeçalho no Bloco 0
        header = FileHeader(
            magic_number=MAGIC_NUMBER,
            file_type=file_type,
            block_size=BLOCK_SIZE,
            total_blocks=1,  # Apenas o Bloco 0 alocado inicialmente
            free_list_head=NO_BLOCK,
            first_data_blk=NO_BLOCK,
            last_data_blk=NO_BLOCK,
            root_block_id=NO_BLOCK,
            record_count=0,
            schema_version=schema_version,
        )

        # Grava o cabeçalho no Bloco 0 (offset 0)
        with open(resolved_path, "w+b") as f:
            header_bytes = header.to_bytes()
            f.write(header_bytes)
            f.flush()
            os.fsync(f.fileno())

        # Registra a escrita física inicial do Bloco 0
        self._record_physical_write(file_id, 0, is_create=True)

        return header

    def open_file(self, path: Union[str, Path]) -> FileHandle:
        """
        Abre um arquivo binário existente no modo leitura/escrita ('r+b').
        Lê e valida o cabeçalho do Bloco 0.
        """
        resolved_path = self._resolve_path(path)
        file_id = str(resolved_path)

        if file_id in self._open_files:
            handle = self._open_files[file_id]
            if not handle.is_closed():
                return handle

        if not resolved_path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado: {resolved_path}")

        stream = open(resolved_path, "r+b")

        # Lê exatamente o Bloco 0 para carregar o cabeçalho
        stream.seek(0)
        header_data = stream.read(BLOCK_SIZE)
        if len(header_data) < BLOCK_SIZE:
            stream.close()
            raise IOError(f"Arquivo corrompido ou menor que BLOCK_SIZE: {resolved_path}")

        header = FileHeader.from_bytes(header_data)

        handle = FileHandle(
            file_id=file_id,
            path=resolved_path,
            stream=stream,
            header=header,
        )
        self._open_files[file_id] = handle

        # Registra a leitura física do Bloco 0
        self._record_physical_read(handle, 0)

        return handle

    def close_file(self, file_id_or_path: Union[str, Path]) -> None:
        """
        Salva o cabeçalho se modificado, dá flush e fecha o arquivo.
        """
        file_id = str(self._resolve_path(file_id_or_path))
        if file_id not in self._open_files:
            return

        handle = self._open_files[file_id]
        if not handle.is_closed():
            if handle.header_dirty:
                self._persist_header(handle)
            handle.flush()
            handle.stream.close()

        del self._open_files[file_id]

    def close_all(self) -> None:
        """Fecha todos os arquivos abertos."""
        open_ids = list(self._open_files.keys())
        for fid in open_ids:
            self.close_file(fid)

    def _persist_header(self, handle: FileHandle) -> None:
        """Grava os metadados do cabeçalho de volta no Bloco 0."""
        handle.stream.seek(0)
        header_bytes = handle.header.to_bytes()
        handle.stream.write(header_bytes)
        handle.flush()
        handle.header_dirty = False
        self._record_physical_write(handle.file_id, 0)

    # -------------------------------------------------------------------------
    # Operações Básicas de Bloco (Read / Write)
    # -------------------------------------------------------------------------

    def read_block(self, file_id_or_path: Union[str, Path], block_id: int) -> bytearray:
        """
        Lê exatamente BLOCK_SIZE (4096) bytes do bloco especificado.
        Atualiza contadores locais e globais (sequencial vs. seek).
        """
        handle = self._get_handle(file_id_or_path)

        if block_id < 0 or block_id >= handle.header.total_blocks:
            raise IndexError(
                f"Tentativa de leitura fora dos limites: block_id={block_id}, total_blocks={handle.header.total_blocks} em {handle.path.name}"
            )

        offset = block_id * BLOCK_SIZE
        handle.stream.seek(offset)
        data = handle.stream.read(BLOCK_SIZE)

        if len(data) != BLOCK_SIZE:
            raise IOError(
                f"Leitura incompleta no bloco {block_id} de {handle.path.name}: obtidos {len(data)}/{BLOCK_SIZE} bytes"
            )

        self._record_physical_read(handle, block_id)
        return bytearray(data)

    def write_block(
        self,
        file_id_or_path: Union[str, Path],
        block_id: int,
        data: Union[bytes, bytearray],
    ) -> None:
        """
        Grava exatamente BLOCK_SIZE (4096) bytes no bloco especificado.
        Atualiza contadores locais e globais (sequencial vs. seek).
        """
        handle = self._get_handle(file_id_or_path)

        if len(data) != BLOCK_SIZE:
            raise ValueError(
                f"Tamanho de dados inválido para escrita: esperado {BLOCK_SIZE} bytes, recebido {len(data)}"
            )

        if block_id < 0 or block_id >= handle.header.total_blocks:
            raise IndexError(
                f"Tentativa de escrita fora dos limites: block_id={block_id}, total_blocks={handle.header.total_blocks} em {handle.path.name}"
            )

        offset = block_id * BLOCK_SIZE
        handle.stream.seek(offset)
        handle.stream.write(data)
        handle.flush()

        self._record_physical_write(handle.file_id, block_id)

    # -------------------------------------------------------------------------
    # Gerência de Blocos Livres (Alocação e Desalocação via Free List em O(1))
    # -------------------------------------------------------------------------

    def allocate_block(self, file_id_or_path: Union[str, Path]) -> int:
        """
        Aloca um novo bloco de 4 KB no arquivo:
        1. Se houver blocos na Free List, reaproveita a cabeça da lista em O(1).
        2. Se a Free List estiver vazia, estende o arquivo no final (total_blocks++).
        Atualiza e persiste o cabeçalho no Bloco 0.
        Retorna o block_id alocado.
        """
        handle = self._get_handle(file_id_or_path)

        if handle.header.free_list_head != NO_BLOCK:
            # Reaproveita bloco livre existente
            reused_block_id = handle.header.free_list_head

            # Lê os primeiros 4 bytes do bloco livre para descobrir o próximo bloco livre
            offset = reused_block_id * BLOCK_SIZE
            handle.stream.seek(offset)
            free_node_bytes = handle.stream.read(4)

            next_free = NO_BLOCK
            if len(free_node_bytes) == 4:
                next_free = struct.unpack("<I", free_node_bytes)[0]

            # Atualiza a cabeça da Free List no cabeçalho
            handle.header.free_list_head = next_free
            handle.header_dirty = True

            # Inicializa o bloco reaproveitado com zeros
            empty_block = b"\x00" * BLOCK_SIZE
            handle.stream.seek(offset)
            handle.stream.write(empty_block)
            handle.flush()

            self._record_physical_write(handle.file_id, reused_block_id)
            self._persist_header(handle)
            return reused_block_id

        # Free list vazia: extensão sequencial dinâmica no final do arquivo
        new_block_id = handle.header.total_blocks
        handle.header.total_blocks += 1
        handle.header_dirty = True

        offset = new_block_id * BLOCK_SIZE
        handle.stream.seek(offset)
        handle.stream.write(b"\x00" * BLOCK_SIZE)
        handle.flush()

        self._record_physical_write(handle.file_id, new_block_id)
        self._persist_header(handle)
        return new_block_id

    def free_block(self, file_id_or_path: Union[str, Path], block_id: int) -> None:
        """
        Libera um bloco e o insere na cabeça da Free List em O(1):
        - Grava o antigo free_list_head nos primeiros 4 bytes do bloco liberado.
        - O Bloco 0 passa a ter free_list_head = block_id.
        """
        handle = self._get_handle(file_id_or_path)

        if block_id <= 0 or block_id >= handle.header.total_blocks:
            raise ValueError(
                f"Não é permitido desalocar bloco inválido ou reservado (block_id={block_id})"
            )

        # Prepara o nó livre: primeiros 4 bytes contêm o ponteiro para o próximo livre
        old_head = handle.header.free_list_head
        free_node = struct.pack("<I", old_head) + (b"\x00" * (BLOCK_SIZE - 4))

        offset = block_id * BLOCK_SIZE
        handle.stream.seek(offset)
        handle.stream.write(free_node)
        handle.flush()

        self._record_physical_write(handle.file_id, block_id)

        # Atualiza a cabeça da lista no cabeçalho
        handle.header.free_list_head = block_id
        handle.header_dirty = True
        self._persist_header(handle)

    # -------------------------------------------------------------------------
    # Atualização de Metadados do Arquivo
    # -------------------------------------------------------------------------

    def update_header_metadata(
        self,
        file_id_or_path: Union[str, Path],
        first_data_blk: Optional[int] = None,
        last_data_blk: Optional[int] = None,
        root_block_id: Optional[int] = None,
        record_count: Optional[int] = None,
        schema_version: Optional[int] = None,
    ) -> None:
        """Permite atualizar campos dinâmicos do cabeçalho e persiste no Bloco 0."""
        handle = self._get_handle(file_id_or_path)

        if first_data_blk is not None:
            handle.header.first_data_blk = first_data_blk
        if last_data_blk is not None:
            handle.header.last_data_blk = last_data_blk
        if root_block_id is not None:
            handle.header.root_block_id = root_block_id
        if record_count is not None:
            handle.header.record_count = record_count
        if schema_version is not None:
            handle.header.schema_version = schema_version

        handle.header_dirty = True
        self._persist_header(handle)

    def get_header(self, file_id_or_path: Union[str, Path]) -> FileHeader:
        """Retorna uma cópia do cabeçalho atual do arquivo."""
        handle = self._get_handle(file_id_or_path)
        return handle.header

    def get_file_counters(self, file_id_or_path: Union[str, Path]) -> IOCounters:
        """Retorna uma cópia dos contadores físicos do arquivo especificado."""
        handle = self._get_handle(file_id_or_path)
        return handle.counters.copy()

    def reset_counters(self) -> None:
        """Zera todos os contadores globais e por arquivo aberto."""
        self.global_counters.reset()
        self._global_last_file_id = None
        self._global_last_block_id = None
        for handle in self._open_files.values():
            handle.counters.reset()
            handle.last_accessed_block_id = None

    # -------------------------------------------------------------------------
    # Mecanismo Interno de Rastreamento de I/O Físico (Sequencial vs. Seek)
    # -------------------------------------------------------------------------

    def _get_handle(self, file_id_or_path: Union[str, Path]) -> FileHandle:
        file_id = str(self._resolve_path(file_id_or_path))
        if file_id not in self._open_files or self._open_files[file_id].is_closed():
            return self.open_file(file_id)
        return self._open_files[file_id]

    def _record_physical_read(self, handle: FileHandle, block_id: int) -> None:
        # 1. Contabilização em nível de arquivo individual
        local_is_seq = False
        local_seek_dist = 0
        if handle.last_accessed_block_id is not None:
            if block_id == handle.last_accessed_block_id + 1:
                local_is_seq = True
            else:
                local_seek_dist = abs(block_id - handle.last_accessed_block_id)
        else:
            # Primeiro acesso ao arquivo conta como seek inicial
            local_seek_dist = block_id

        handle.last_accessed_block_id = block_id
        handle.counters.record_read(local_is_seq, local_seek_dist)

        # 2. Contabilização em nível global (barramento geral de disco)
        global_is_seq = False
        global_seek_dist = 0
        if self._global_last_file_id == handle.file_id and self._global_last_block_id is not None:
            if block_id == self._global_last_block_id + 1:
                global_is_seq = True
            else:
                global_seek_dist = abs(block_id - self._global_last_block_id)
        else:
            # Mudança de arquivo ou primeiro acesso global
            global_seek_dist = block_id

        self._global_last_file_id = handle.file_id
        self._global_last_block_id = block_id
        self.global_counters.record_read(global_is_seq, global_seek_dist)

    def _record_physical_write(
        self,
        file_id: str,
        block_id: int,
        is_create: bool = False,
    ) -> None:
        handle = self._open_files.get(file_id)

        # 1. Contabilização em nível de arquivo individual (se handle existir)
        local_is_seq = False
        local_seek_dist = 0
        if handle:
            if handle.last_accessed_block_id is not None:
                if block_id == handle.last_accessed_block_id + 1:
                    local_is_seq = True
                else:
                    local_seek_dist = abs(block_id - handle.last_accessed_block_id)
            else:
                local_seek_dist = block_id
            handle.last_accessed_block_id = block_id
            handle.counters.record_write(local_is_seq, local_seek_dist)

        # 2. Contabilização em nível global
        global_is_seq = False
        global_seek_dist = 0
        if self._global_last_file_id == file_id and self._global_last_block_id is not None:
            if block_id == self._global_last_block_id + 1:
                global_is_seq = True
            else:
                global_seek_dist = abs(block_id - self._global_last_block_id)
        else:
            global_seek_dist = block_id

        self._global_last_file_id = file_id
        self._global_last_block_id = block_id
        self.global_counters.record_write(global_is_seq, global_seek_dist)
