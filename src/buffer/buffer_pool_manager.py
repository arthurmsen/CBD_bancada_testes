"""
Gerente de Buffer Pool (BufferPoolManager).
Conforme especificado em PLANO_IMPLEMENTACAO.md (Módulo 2).

Responsabilidades:
- Mediador estrito entre a memória volátil (frames) e o disco (FileManager).
- Particionamento em 4 áreas funcionais com cotas fixas estritas (CATALOG, INDEX, DATA, WORK).
- Políticas de substituição intercambiáveis: LRU e MRU.
- Suporte a Toss-Immediate, Retenção Preferencial por Prioridade e Prefetching.
- Mecanismo seguro de Pin/Unpin via Context Manager (PinGuard) para erradicar vazamentos de pins.
- Rastreamento integral de métricas (requests, hits, misses, hit ratio, dirty evictions, forced writes).
"""
from __future__ import annotations
from contextlib import contextmanager
from typing import Dict, Generator, List, Optional, Tuple, Union
from pathlib import Path

from src.storage.constants import BLOCK_SIZE
from src.storage.file_manager import FileManager
from src.buffer.constants import (
    PageType,
    ReplacerType,
    DEFAULT_BUFFER_POOL_SIZE,
    QUOTA_CATALOG_PERCENT,
    QUOTA_INDEX_PERCENT,
    QUOTA_DATA_PERCENT,
    QUOTA_WORK_PERCENT,
    PRIORITY_NORMAL,
    PRIORITY_INDEX_LEAF,
    PRIORITY_INDEX_INTERNAL,
    PRIORITY_CATALOG,
)
from src.buffer.frame import Frame
from src.buffer.replacer import Replacer, LRUReplacer, MRUReplacer
from src.buffer.buffer_metrics import BufferMetrics


class BufferPoolFullError(RuntimeError):
    """Exceção levantada quando todos os frames de uma partição estão fixados (pinned)."""
    pass


class PinGuardHandle:
    """
    Handle exposto dentro do bloco 'with buffer_pool.pin_guard(...) as guard:'.
    Permite ler/modificar os bytes e sinalizar dirty bit ou toss-immediate de forma limpa.
    """
    def __init__(self, data: bytearray, state: dict):
        self.data: bytearray = data
        self._state: dict = state

    def set_dirty(self, is_dirty: bool = True) -> None:
        """Marca que a página foi alterada e precisará ser gravada em disco."""
        self._state["is_dirty"] = is_dirty

    def set_toss_immediate(self, toss: bool = True) -> None:
        """Sinaliza que a página não será mais necessária (prioridade para expulsão)."""
        self._state["toss_immediate"] = toss


class BufferPoolManager:
    """
    Gerenciador do pool de buffers com cotas fixas e medição analítica de I/O.
    """
    def __init__(
        self,
        file_manager: FileManager,
        pool_size: int = DEFAULT_BUFFER_POOL_SIZE,
        replacer_type: ReplacerType = ReplacerType.LRU,
    ):
        self.file_manager: FileManager = file_manager
        self.pool_size: int = pool_size
        self.replacer_type: ReplacerType = replacer_type

        # 1. Aloca os frames físicos em memória
        self.frames: List[Frame] = [Frame(i) for i in range(pool_size)]

        # 2. Page Table: mapeia (file_id, block_id) -> frame_id
        self._page_table: Dict[Tuple[str, int], int] = {}

        # 3. Calcula e define as partições com cotas fixas estritas
        self._partitions: Dict[PageType, List[int]] = self._calculate_partitions(pool_size)

        # 4. Cria os replacers independentes para cada partição
        self._replacers: Dict[PageType, Replacer] = {}
        for ptype in PageType:
            self._replacers[ptype] = self._create_replacer(replacer_type)

        # 5. Métricas globais e por partição
        self.global_metrics: BufferMetrics = BufferMetrics()
        self.partition_metrics: Dict[PageType, BufferMetrics] = {
            ptype: BufferMetrics() for ptype in PageType
        }

    def _normalize_file_id(self, file_id_or_path: Union[str, Path]) -> str:
        return str(self.file_manager._resolve_path(file_id_or_path))

    def _calculate_partitions(self, pool_size: int) -> Dict[PageType, List[int]]:
        """
        Divide os frame_ids em 4 partições com cotas fixas rígidas:
        - CATALOG: 5% (mínimo 1 frame)
        - INDEX: 25%
        - WORK: 20%
        - DATA: restante (50%)
        """
        catalog_count = max(1, int(pool_size * QUOTA_CATALOG_PERCENT))
        index_count = max(1, int(pool_size * QUOTA_INDEX_PERCENT))
        work_count = max(1, int(pool_size * QUOTA_WORK_PERCENT))
        data_count = pool_size - (catalog_count + index_count + work_count)
        if data_count <= 0:
            raise ValueError(f"Tamanho de Buffer Pool insuficiente ({pool_size}) para as 4 cotas.")

        partitions: Dict[PageType, List[int]] = {
            PageType.CATALOG: list(range(0, catalog_count)),
            PageType.INDEX: list(range(catalog_count, catalog_count + index_count)),
            PageType.WORK: list(
                range(catalog_count + index_count, catalog_count + index_count + work_count)
            ),
            PageType.DATA: list(
                range(catalog_count + index_count + work_count, pool_size)
            ),
        }
        return partitions

    def _create_replacer(self, replacer_type: ReplacerType) -> Replacer:
        if replacer_type == ReplacerType.LRU:
            return LRUReplacer()
        elif replacer_type == ReplacerType.MRU:
            return MRUReplacer()
        else:
            raise ValueError(f"Replacer desconhecido: {replacer_type}")

    def set_replacer_type(self, replacer_type: ReplacerType) -> None:
        """Permite alternar entre LRU e MRU em todas as partições para fins comparativos."""
        self.replacer_type = replacer_type
        for ptype in PageType:
            old_replacer = self._replacers[ptype]
            new_replacer = self._create_replacer(replacer_type)
            # Transfere frames desfixados elegíveis
            for fid in self._partitions[ptype]:
                frame = self.frames[fid]
                if not frame.is_free and frame.pin_count == 0:
                    new_replacer.unpin(fid, toss_immediate=False, priority=frame.priority)
            self._replacers[ptype] = new_replacer

    def _default_priority(self, page_type: PageType) -> int:
        if page_type == PageType.CATALOG:
            return PRIORITY_CATALOG
        elif page_type == PageType.INDEX:
            return PRIORITY_INDEX_INTERNAL
        return PRIORITY_NORMAL

    # -------------------------------------------------------------------------
    # Operações Principais: Pin e Unpin
    # -------------------------------------------------------------------------

    def pin_page(
        self,
        file_id_or_path: Union[str, Path],
        block_id: int,
        page_type: PageType = PageType.DATA,
        priority: Optional[int] = None,
    ) -> bytearray:
        """
        Obtém e fixa uma página na memória:
        - Se residente (HIT): incrementa pin_count e remove dos candidatos à expulsão.
        - Se não residente (MISS): seleciona frame na partição com cota fixa, realiza
          flush se sujo, lê do disco via FileManager e fixa a página.
        Retorna o buffer mutável de bytes da página (bytearray de 4096 bytes).
        """
        file_id = self._normalize_file_id(file_id_or_path)
        key = (file_id, block_id)
        replacer = self._replacers[page_type]
        prio = priority if priority is not None else self._default_priority(page_type)

        # 1. Caso HIT: Página já está presente em algum frame do buffer
        if key in self._page_table:
            frame_id = self._page_table[key]
            frame = self.frames[frame_id]

            frame.pin_count += 1
            frame.touch()
            # Remove o frame dos candidatos a vítima enquanto pin_count > 0
            replacer.pin(frame_id)

            self.global_metrics.record_request(hit=True)
            self.partition_metrics[page_type].record_request(hit=True)
            self._update_pinned_gauge()
            return frame.data

        # 2. Caso MISS: Necessário carregar do disco
        self.global_metrics.record_request(hit=False)
        self.partition_metrics[page_type].record_request(hit=False)

        # Procura um frame na partição reservada com cota fixa
        frame_id = self._find_or_evict_frame(page_type)
        frame = self.frames[frame_id]

        # Se o frame escolhido continha dados de outra página suja, grava no disco
        if not frame.is_free and frame.is_dirty:
            assert frame.file_id is not None and frame.block_id is not None
            self.file_manager.write_block(frame.file_id, frame.block_id, frame.data)
            self.global_metrics.record_dirty_eviction()
            self.partition_metrics[page_type].record_dirty_eviction()
            frame.is_dirty = False

        # Se havia uma página mapeada neste frame, remove da page_table
        if not frame.is_free:
            old_key = (frame.file_id, frame.block_id)
            self._page_table.pop(old_key, None)

        # Lê o bloco físico do disco através do FileManager
        block_bytes = self.file_manager.read_block(file_id, block_id)

        # Carrega os dados no frame e inicializa pin_count = 1
        frame.load_page(file_id, block_id, page_type, block_bytes, priority=prio)
        self._page_table[key] = frame_id
        replacer.pin(frame_id)

        self._update_pinned_gauge()
        return frame.data

    def unpin_page(
        self,
        file_id_or_path: Union[str, Path],
        block_id: int,
        is_dirty: bool = False,
        toss_immediate: bool = False,
    ) -> None:
        """
        Libera uma fixação da página:
        - Decrementa pin_count.
        - Se is_dirty=True, ativa o dirty_bit.
        - Se pin_count == 0, insere o frame no replacer da sua partição.
        """
        file_id = self._normalize_file_id(file_id_or_path)
        key = (file_id, block_id)

        if key not in self._page_table:
            raise KeyError(f"Página não encontrada no buffer pool: {file_id} bloco {block_id}")

        frame_id = self._page_table[key]
        frame = self.frames[frame_id]

        if frame.pin_count <= 0:
            raise ValueError(
                f"Tentativa de desfixar página com pin_count <= 0: {file_id} bloco {block_id}"
            )

        if is_dirty:
            frame.is_dirty = True

        frame.pin_count -= 1

        if frame.pin_count == 0:
            assert frame.page_type is not None
            replacer = self._replacers[frame.page_type]
            replacer.unpin(
                frame_id,
                toss_immediate=toss_immediate,
                priority=frame.priority,
            )

        self._update_pinned_gauge()

    @contextmanager
    def pin_guard(
        self,
        file_id_or_path: Union[str, Path],
        block_id: int,
        page_type: PageType = PageType.DATA,
        is_dirty: bool = False,
        toss_immediate: bool = False,
        priority: Optional[int] = None,
    ) -> Generator[PinGuardHandle, None, None]:
        """
        Context Manager para proteção estrita de Pins (prevenção contra vazamento de frames).
        Garante a invocação automática de unpin_page na saída do bloco, mesmo em caso de erro.
        """
        file_id = self._normalize_file_id(file_id_or_path)
        data = self.pin_page(file_id, block_id, page_type=page_type, priority=priority)
        state = {"is_dirty": is_dirty, "toss_immediate": toss_immediate}
        handle = PinGuardHandle(data, state)
        try:
            yield handle
        finally:
            self.unpin_page(
                file_id,
                block_id,
                is_dirty=state["is_dirty"],
                toss_immediate=state["toss_immediate"],
            )

    # -------------------------------------------------------------------------
    # Gravação Forçada (Forced Output) e Flush
    # -------------------------------------------------------------------------

    def force_page(self, file_id_or_path: Union[str, Path], block_id: int) -> None:
        """
        Gravação forçada e síncrona imediata da página no disco (sem desalojá-la do buffer).
        Incrementa o contador de forced_writes.
        """
        file_id = self._normalize_file_id(file_id_or_path)
        key = (file_id, block_id)

        if key in self._page_table:
            frame_id = self._page_table[key]
            frame = self.frames[frame_id]
            if frame.is_dirty:
                self.file_manager.write_block(file_id, block_id, frame.data)
                frame.is_dirty = False
                self.global_metrics.record_forced_write()
                if frame.page_type:
                    self.partition_metrics[frame.page_type].record_forced_write()

    def flush_all(self) -> None:
        """Grava todas as páginas marcadas como dirty no disco."""
        for frame in self.frames:
            if not frame.is_free and frame.is_dirty:
                assert frame.file_id is not None and frame.block_id is not None
                self.file_manager.write_block(frame.file_id, frame.block_id, frame.data)
                frame.is_dirty = False
                self.global_metrics.record_forced_write()
                if frame.page_type:
                    self.partition_metrics[frame.page_type].record_forced_write()

    # -------------------------------------------------------------------------
    # Leitura Antecipada (Prefetching)
    # -------------------------------------------------------------------------

    def prefetch_blocks(
        self,
        file_id_or_path: Union[str, Path],
        start_block_id: int,
        count: int,
        page_type: PageType = PageType.DATA,
    ) -> int:
        """
        Carrega antecipadamente um lote contíguo de blocos na partição especificada.
        Retorna a quantidade de blocos efetivamente pré-carregados (misses evitados).
        """
        file_id = self._normalize_file_id(file_id_or_path)
        prefetched_count = 0

        for b in range(start_block_id, start_block_id + count):
            key = (file_id, b)
            if key not in self._page_table:
                # Carrega o bloco no buffer e desfixa imediatamente para deixá-lo no cache
                try:
                    self.pin_page(file_id, b, page_type=page_type)
                    self.unpin_page(file_id, b, is_dirty=False, toss_immediate=False)
                    prefetched_count += 1
                except BufferPoolFullError:
                    break

        return prefetched_count

    # -------------------------------------------------------------------------
    # Auxiliares Internos
    # -------------------------------------------------------------------------

    def _find_or_evict_frame(self, page_type: PageType) -> int:
        """
        Localiza um frame livre na partição correspondente à cota.
        Se todos estiverem ocupados, invoca o replacer daquela cota para expulsar uma vítima.
        """
        partition_frames = self._partitions[page_type]

        # 1. Procura primeiro por um frame livre (ainda não utilizado)
        for fid in partition_frames:
            if self.frames[fid].is_free:
                return fid

        # 2. Se não houver frame livre, seleciona a vítima no replacer da cota
        victim_id = self._replacers[page_type].victim()
        if victim_id is not None:
            return victim_id

        # 3. Todos os frames da cota estão pinned
        raise BufferPoolFullError(
            f"Todos os {len(partition_frames)} frames da partição {page_type.value} estão fixados (pinned)."
        )

    def _update_pinned_gauge(self) -> None:
        total_pinned = sum(1 for f in self.frames if f.is_pinned)
        self.global_metrics.pinned_count_current = total_pinned
        for ptype, frame_ids in self._partitions.items():
            count = sum(1 for fid in frame_ids if self.frames[fid].is_pinned)
            self.partition_metrics[ptype].pinned_count_current = count

    def get_metrics(self) -> BufferMetrics:
        """Retorna snapshot das métricas globais consolidadas."""
        return self.global_metrics.copy()

    def get_partition_metrics(self, page_type: PageType) -> BufferMetrics:
        """Retorna snapshot das métricas da partição funcional especificada."""
        return self.partition_metrics[page_type].copy()

    def reset_metrics(self) -> None:
        """Zera todos os contadores de requisição do buffer pool."""
        self.global_metrics.reset()
        for m in self.partition_metrics.values():
            m.reset()
        self._update_pinned_gauge()

    def get_resident_pages_count(self) -> int:
        """Retorna quantas páginas estão carregadas no buffer pool."""
        return len(self._page_table)

    def is_page_resident(self, file_id_or_path: Union[str, Path], block_id: int) -> bool:
        """Verifica se uma determinada página física já se encontra carregada em memória."""
        file_id = self._normalize_file_id(file_id_or_path)
        return (file_id, block_id) in self._page_table
