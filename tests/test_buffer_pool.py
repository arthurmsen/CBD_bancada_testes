"""
Testes unitários e de validação arquitetural para o Gerente de Buffer Pool.
Cobre:
- Particionamento rígido em cotas fixas (CATALOG, INDEX, DATA, WORK).
- Rastreamento exato de métricas (requests, hits, misses, hit ratio).
- Políticas de substituição LRU e MRU.
- Toss-Immediate e Retenção Preferencial por Prioridade.
- Fixação de páginas (pin/unpin) e Context Manager (PinGuard).
- Gravação de páginas sujas (dirty eviction) e Gravação Forçada (forced output).
- Leitura antecipada (prefetching).
"""
import shutil
import tempfile
import unittest

from src.storage.constants import BLOCK_SIZE
from src.storage.file_manager import FileManager
from src.buffer.constants import (
    PageType,
    ReplacerType,
    PRIORITY_INDEX_LEAF,
    PRIORITY_INDEX_INTERNAL,
)
from src.buffer.buffer_pool_manager import BufferPoolManager, BufferPoolFullError


class TestBufferPoolManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="cbd_test_buffer_")
        self.fm = FileManager(base_directory=self.test_dir)
        # Pool com 20 páginas para facilitar testes de preenchimento de cotas
        # Quotas em N=20: CATALOG=1 (5%), INDEX=5 (25%), WORK=4 (20%), DATA=10 (50%)
        self.bp = BufferPoolManager(self.fm, pool_size=20, replacer_type=ReplacerType.LRU)

        # Cria arquivos de teste
        self.data_file = "test_data.heap"
        self.index_file = "test_idx.bplus"
        self.fm.create_file(self.data_file)
        self.fm.create_file(self.index_file)

        # Aloca vários blocos no arquivo de dados para testes
        for _ in range(15):
            self.fm.allocate_block(self.data_file)

        # Aloca blocos no arquivo de índice
        for _ in range(10):
            self.fm.allocate_block(self.index_file)

    def tearDown(self):
        self.fm.close_all()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_partition_quotas(self):
        """Valida que as cotas fixas respeitam exatamente as porcentagens definidas."""
        partitions = self.bp._partitions
        self.assertEqual(len(partitions[PageType.CATALOG]), 1)
        self.assertEqual(len(partitions[PageType.INDEX]), 5)
        self.assertEqual(len(partitions[PageType.WORK]), 4)
        self.assertEqual(len(partitions[PageType.DATA]), 10)
        self.assertEqual(self.bp.pool_size, 20)

    def test_hit_miss_and_metrics(self):
        """Valida o ciclo de MISS inicial e subsequente HIT com métricas correspondentes."""
        self.assertEqual(self.bp.get_metrics().requests, 0)

        # 1ª Leitura: Bloco 1 (MISS)
        data1 = self.bp.pin_page(self.data_file, 1, PageType.DATA)
        self.assertEqual(len(data1), BLOCK_SIZE)
        m = self.bp.get_metrics()
        self.assertEqual(m.requests, 1)
        self.assertEqual(m.misses, 1)
        self.assertEqual(m.hits, 0)
        self.assertEqual(m.hit_ratio, 0.0)

        # 2ª Leitura: Bloco 1 novamente (HIT)
        data2 = self.bp.pin_page(self.data_file, 1, PageType.DATA)
        self.assertEqual(data1, data2)
        m = self.bp.get_metrics()
        self.assertEqual(m.requests, 2)
        self.assertEqual(m.hits, 1)
        self.assertEqual(m.misses, 1)
        self.assertEqual(m.hit_ratio, 50.0)

        # Desfixa as duas referências
        self.bp.unpin_page(self.data_file, 1)
        self.bp.unpin_page(self.data_file, 1)

    def test_pinned_pages_immunity_and_full_error(self):
        """Garante que páginas com pin_count > 0 não são expulsas e estouram a cota com erro."""
        # A partição CATALOG tem exatamente 1 frame
        self.bp.pin_page(self.data_file, 0, PageType.CATALOG)

        # Tentar carregar outro bloco na partição CATALOG com o frame fixado deve falhar
        with self.assertRaises(BufferPoolFullError):
            self.bp.pin_page(self.index_file, 0, PageType.CATALOG)

        # Ao desfixar, o frame se torna elegível para expulsão
        self.bp.unpin_page(self.data_file, 0)
        self.bp.pin_page(self.index_file, 0, PageType.CATALOG)
        self.bp.unpin_page(self.index_file, 0)

    def test_pin_guard_context_manager(self):
        """Testa o context manager pin_guard garantindo unpin automático e proteção de dirty bit."""
        # Modifica dados dentro do guard
        with self.bp.pin_guard(self.data_file, 2, PageType.DATA) as guard:
            guard.data[:4] = b"TEST"
            guard.set_dirty(True)

        # Verifica que o pin_count voltou a zero (gauge)
        self.assertEqual(self.bp.get_metrics().pinned_count_current, 0)

        # Valida que o dirty bit foi ativado no frame
        key = (str(self.fm._resolve_path(self.data_file)), 2)
        frame_id = self.bp._page_table[key]
        self.assertTrue(self.bp.frames[frame_id].is_dirty)

        # Força gravação em disco
        self.bp.force_page(self.data_file, 2)
        self.assertFalse(self.bp.frames[frame_id].is_dirty)
        self.assertEqual(self.bp.get_metrics().forced_writes, 1)

        # Lê do disco diretamente via FileManager e valida
        disk_data = self.fm.read_block(self.data_file, 2)
        self.assertEqual(disk_data[:4], b"TEST")

    def test_lru_eviction_policy(self):
        """Testa política LRU: expulsa a página acessada há mais tempo."""
        # Partição WORK tem 4 frames
        bp_small = BufferPoolManager(self.fm, pool_size=10, replacer_type=ReplacerType.LRU)
        # WORK em pool_size=10 tem 2 frames
        work_frames_count = len(bp_small._partitions[PageType.WORK])
        self.assertEqual(work_frames_count, 2)

        # Carrega Bloco 1 e Bloco 2
        bp_small.pin_page(self.data_file, 1, PageType.WORK)
        bp_small.unpin_page(self.data_file, 1)

        bp_small.pin_page(self.data_file, 2, PageType.WORK)
        bp_small.unpin_page(self.data_file, 2)

        # Acessa Bloco 1 novamente (torna Bloco 1 o mais recente; Bloco 2 vira LRU)
        bp_small.pin_page(self.data_file, 1, PageType.WORK)
        bp_small.unpin_page(self.data_file, 1)

        # Carrega Bloco 3: deve expulsar Bloco 2 (LRU)
        bp_small.pin_page(self.data_file, 3, PageType.WORK)
        bp_small.unpin_page(self.data_file, 3)

        self.assertTrue(bp_small.is_page_resident(self.data_file, 1))
        self.assertFalse(bp_small.is_page_resident(self.data_file, 2))
        self.assertTrue(bp_small.is_page_resident(self.data_file, 3))

    def test_mru_eviction_policy(self):
        """Testa política MRU: expulsa a página mais recentemente acessada."""
        bp_mru = BufferPoolManager(self.fm, pool_size=10, replacer_type=ReplacerType.MRU)

        # Carrega Bloco 1 e Bloco 2 na partição WORK (2 frames)
        bp_mru.pin_page(self.data_file, 1, PageType.WORK)
        bp_mru.unpin_page(self.data_file, 1)

        bp_mru.pin_page(self.data_file, 2, PageType.WORK)
        bp_mru.unpin_page(self.data_file, 2)

        # O mais recente é o Bloco 2. Carregar Bloco 3 em MRU deve expulsar Bloco 2!
        bp_mru.pin_page(self.data_file, 3, PageType.WORK)
        bp_mru.unpin_page(self.data_file, 3)

        self.assertTrue(bp_mru.is_page_resident(self.data_file, 1))
        self.assertFalse(bp_mru.is_page_resident(self.data_file, 2))
        self.assertTrue(bp_mru.is_page_resident(self.data_file, 3))

    def test_toss_immediate(self):
        """Testa que unpin com toss_immediate=True força expulsão imediata no próximo miss."""
        bp_lru = BufferPoolManager(self.fm, pool_size=10, replacer_type=ReplacerType.LRU)

        # Carrega Bloco 1 com toss_immediate=True
        bp_lru.pin_page(self.data_file, 1, PageType.WORK)
        bp_lru.unpin_page(self.data_file, 1, toss_immediate=True)

        # Carrega Bloco 2 normalmente
        bp_lru.pin_page(self.data_file, 2, PageType.WORK)
        bp_lru.unpin_page(self.data_file, 2, toss_immediate=False)

        # Em LRU normal, Bloco 1 seria o LRU de qualquer forma, mas vamos acessar Bloco 1 novamente:
        bp_lru.pin_page(self.data_file, 1, PageType.WORK)
        bp_lru.unpin_page(self.data_file, 1, toss_immediate=True)
        # Agora Bloco 1 foi acessado mais recentemente que Bloco 2, mas foi marcado toss_immediate!

        # Carrega Bloco 3: deve expulsar Bloco 1 devido ao toss_immediate, mesmo Bloco 2 sendo mais antigo
        bp_lru.pin_page(self.data_file, 3, PageType.WORK)
        bp_lru.unpin_page(self.data_file, 3)

        self.assertFalse(bp_lru.is_page_resident(self.data_file, 1))
        self.assertTrue(bp_lru.is_page_resident(self.data_file, 2))
        self.assertTrue(bp_lru.is_page_resident(self.data_file, 3))

    def test_priority_retention(self):
        """Garante que páginas de prioridade mais alta (ex: nós internos) são retidas sobre folhas."""
        bp_idx = BufferPoolManager(self.fm, pool_size=10, replacer_type=ReplacerType.LRU)
        # Partição INDEX em pool_size=10 tem 2 frames
        self.assertEqual(len(bp_idx._partitions[PageType.INDEX]), 2)

        # Bloco 1 é nó folha (prioridade 1)
        bp_idx.pin_page(self.index_file, 1, PageType.INDEX, priority=PRIORITY_INDEX_LEAF)
        bp_idx.unpin_page(self.index_file, 1)

        # Bloco 2 é nó interno (prioridade 2 - maior retenção)
        bp_idx.pin_page(self.index_file, 2, PageType.INDEX, priority=PRIORITY_INDEX_INTERNAL)
        bp_idx.unpin_page(self.index_file, 2)

        # Bloco 1 foi acessado antes de Bloco 2. Agora acessamos Bloco 1 para torná-lo mais recente:
        bp_idx.pin_page(self.index_file, 1, PageType.INDEX, priority=PRIORITY_INDEX_LEAF)
        bp_idx.unpin_page(self.index_file, 1)

        # Em LRU puro, Bloco 2 seria expulso por ser o mais antigo.
        # Porém, Bloco 2 tem PRIORIDADE 2 e Bloco 1 tem PRIORIDADE 1.
        # Ao carregar Bloco 3, o replacer deve proteger Bloco 2 e expulsar Bloco 1!
        bp_idx.pin_page(self.index_file, 3, PageType.INDEX, priority=PRIORITY_INDEX_LEAF)
        bp_idx.unpin_page(self.index_file, 3)

        self.assertFalse(bp_idx.is_page_resident(self.index_file, 1))
        self.assertTrue(bp_idx.is_page_resident(self.index_file, 2))
        self.assertTrue(bp_idx.is_page_resident(self.index_file, 3))

    def test_prefetching(self):
        """Testa o prefetching sequencial em lote e verificação de hits."""
        # Pré-carrega blocos 5, 6, 7 e 8
        prefetched = self.bp.prefetch_blocks(self.data_file, 5, 4, PageType.DATA)
        self.assertEqual(prefetched, 4)

        # Zera contadores para medir o impacto do cache aquecido
        self.bp.reset_metrics()

        # Acessos subsequentes a esses blocos devem ser 100% HITs
        for b in range(5, 9):
            self.bp.pin_page(self.data_file, b, PageType.DATA)
            self.bp.unpin_page(self.data_file, b)

        m = self.bp.get_metrics()
        self.assertEqual(m.requests, 4)
        self.assertEqual(m.hits, 4)
        self.assertEqual(m.misses, 0)
        self.assertEqual(m.hit_ratio, 100.0)

    def test_dirty_eviction_persists_to_disk(self):
        """Valida que quando uma página dirty é expulsa, ela é gravada em disco automaticamente."""
        bp_small = BufferPoolManager(self.fm, pool_size=10, replacer_type=ReplacerType.LRU)
        # DATA em pool_size=10 tem 5 frames

        # Preenche os 5 frames de DATA
        for b in range(1, 6):
            with bp_small.pin_guard(self.data_file, b, PageType.DATA) as g:
                if b == 1:
                    g.data[:8] = b"DIRTY_01"
                    g.set_dirty(True)

        self.assertEqual(bp_small.get_metrics().dirty_page_evictions, 0)

        # Carrega bloco 6 em DATA (força expulsão de um frame; no caso, bloco 1 em LRU)
        with bp_small.pin_guard(self.data_file, 6, PageType.DATA) as g:
            pass

        # Verifica se dirty_page_evictions incrementou
        self.assertEqual(bp_small.get_metrics().dirty_page_evictions, 1)

        # Lê bloco 1 diretamente do disco pelo FileManager e confere a persistência
        disk_bytes = self.fm.read_block(self.data_file, 1)
        self.assertEqual(disk_bytes[:8], b"DIRTY_01")


if __name__ == "__main__":
    unittest.main()
