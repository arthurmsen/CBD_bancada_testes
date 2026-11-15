"""
Testes unitários e de estresse para o Gerente de Arquivos (FileManager).
Verifica:
- Criação e integridade de arquivos binários com Bloco 0 (FileHeader).
- Leitura e escrita direta de blocos com checagem de limites.
- Contadores de I/O físico (distinção entre leituras/escritas sequenciais e seeks aleatórios).
- Gerência da Free List (reaproveitamento LIFO em O(1) e extensão sequencial).
- Persistência atômica entre fechamento e reabertura de arquivos.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from src.storage.constants import (
    BLOCK_SIZE,
    MAGIC_NUMBER,
    NO_BLOCK,
    FILE_TYPE_HEAP,
    FILE_TYPE_BTREE,
)
from src.storage.file_header import FileHeader
from src.storage.file_manager import FileManager


class TestFileManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="cbd_test_storage_")
        self.fm = FileManager(base_directory=self.test_dir)

    def tearDown(self):
        self.fm.close_all()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_create_and_open_file(self):
        """Testa criação de arquivo e validação do cabeçalho inicial."""
        file_path = "test_table.heap"
        header = self.fm.create_file(file_path, file_type=FILE_TYPE_HEAP)

        self.assertEqual(header.magic_number, MAGIC_NUMBER)
        self.assertEqual(header.block_size, BLOCK_SIZE)
        self.assertEqual(header.total_blocks, 1)  # Apenas o Bloco 0
        self.assertEqual(header.free_list_head, NO_BLOCK)
        self.assertEqual(header.record_count, 0)

        # Reabre e valida
        handle = self.fm.open_file(file_path)
        self.assertEqual(handle.header.magic_number, MAGIC_NUMBER)
        self.assertEqual(handle.header.file_type, FILE_TYPE_HEAP)
        self.assertEqual(handle.header.total_blocks, 1)

    def test_block_read_write(self):
        """Testa gravação e leitura exata de blocos de 4096 bytes."""
        file_path = "rw_table.heap"
        self.fm.create_file(file_path)

        # Aloca 1 novo bloco (bloco 1)
        blk_id = self.fm.allocate_block(file_path)
        self.assertEqual(blk_id, 1)

        # Cria dados de teste para preencher 4096 bytes
        test_payload = bytearray(b"A" * 100 + b"B" * 200 + b"C" * (BLOCK_SIZE - 300))
        self.fm.write_block(file_path, blk_id, test_payload)

        # Lê de volta e confere bytes
        read_payload = self.fm.read_block(file_path, blk_id)
        self.assertEqual(test_payload, read_payload)
        self.assertEqual(len(read_payload), BLOCK_SIZE)

    def test_bounds_checking(self):
        """Garante que leitura ou escrita fora dos limites lance IndexError."""
        file_path = "bounds.heap"
        self.fm.create_file(file_path)

        # total_blocks é 1 (apenas Bloco 0). Acessar bloco 1 deve falhar
        with self.assertRaises(IndexError):
            self.fm.read_block(file_path, 1)

        with self.assertRaises(IndexError):
            self.fm.write_block(file_path, 1, b"X" * BLOCK_SIZE)

        # Tentar escrever dados de tamanho diferente de 4096 deve falhar
        with self.assertRaises(ValueError):
            self.fm.write_block(file_path, 0, b"curto")

    def test_free_list_lifo_reuse_and_extension(self):
        """Testa o ciclo completo da Free List: extensão sequencial e reuso LIFO."""
        file_path = "freelist.heap"
        self.fm.create_file(file_path)

        # Aloca blocos 1, 2 e 3 (extensão sequencial)
        b1 = self.fm.allocate_block(file_path)
        b2 = self.fm.allocate_block(file_path)
        b3 = self.fm.allocate_block(file_path)
        self.assertEqual((b1, b2, b3), (1, 2, 3))

        header = self.fm.get_header(file_path)
        self.assertEqual(header.total_blocks, 4)  # 0, 1, 2, 3
        self.assertEqual(header.free_list_head, NO_BLOCK)

        # Libera o bloco 2 e depois o bloco 1
        self.fm.free_block(file_path, b2)
        header = self.fm.get_header(file_path)
        self.assertEqual(header.free_list_head, 2)

        self.fm.free_block(file_path, b1)
        header = self.fm.get_header(file_path)
        self.assertEqual(header.free_list_head, 1)

        # Próxima alocação deve reaproveitar o bloco 1 (LIFO)
        reused_1 = self.fm.allocate_block(file_path)
        self.assertEqual(reused_1, 1)
        header = self.fm.get_header(file_path)
        self.assertEqual(header.free_list_head, 2)

        # Próxima alocação deve reaproveitar o bloco 2
        reused_2 = self.fm.allocate_block(file_path)
        self.assertEqual(reused_2, 2)
        header = self.fm.get_header(file_path)
        self.assertEqual(header.free_list_head, NO_BLOCK)

        # Próxima alocação com lista vazia deve estender sequencialmente (bloco 4)
        b4 = self.fm.allocate_block(file_path)
        self.assertEqual(b4, 4)
        header = self.fm.get_header(file_path)
        self.assertEqual(header.total_blocks, 5)

    def test_io_counters_sequential_vs_random(self):
        """Testa a precisão dos contadores de acessos sequenciais vs seeks."""
        file_path = "counters.heap"
        self.fm.create_file(file_path)

        # Aloca blocos 1, 2, 3, 4
        for _ in range(4):
            self.fm.allocate_block(file_path)

        # Reseta os contadores para iniciar medição limpa
        self.fm.reset_counters()
        counters = self.fm.get_file_counters(file_path)
        self.assertEqual(counters.total_blocks_transferred, 0)

        # Leitura sequencial: bloco 1, depois bloco 2, depois bloco 3
        self.fm.read_block(file_path, 1)  # primeiro acesso local = seek para pos 1
        self.fm.read_block(file_path, 2)  # sequencial (last + 1)
        self.fm.read_block(file_path, 3)  # sequencial (last + 1)

        c = self.fm.get_file_counters(file_path)
        self.assertEqual(c.physical_reads_sequential, 2)
        self.assertEqual(c.physical_reads_random, 1)
        self.assertEqual(c.total_seeks, 1)

        # Agora faz um salto aleatório (seek) lendo o bloco 1 novamente
        self.fm.read_block(file_path, 1)
        c = self.fm.get_file_counters(file_path)
        self.assertEqual(c.physical_reads_sequential, 2)
        self.assertEqual(c.physical_reads_random, 2)
        self.assertEqual(c.total_seeks, 2)

    def test_persistence_across_close_reopen(self):
        """Garante que metadados, total de blocos e free list persistem após fechar e reabrir."""
        file_path = "persist.heap"
        self.fm.create_file(file_path, file_type=FILE_TYPE_BTREE)

        b1 = self.fm.allocate_block(file_path)
        b2 = self.fm.allocate_block(file_path)
        self.fm.free_block(file_path, b1)

        self.fm.update_header_metadata(
            file_path,
            root_block_id=b2,
            record_count=150,
            first_data_blk=b2,
        )

        # Fecha o arquivo e cria uma nova instância de FileManager
        self.fm.close_file(file_path)

        new_fm = FileManager(base_directory=self.test_dir)
        handle = new_fm.open_file(file_path)

        self.assertEqual(handle.header.file_type, FILE_TYPE_BTREE)
        self.assertEqual(handle.header.root_block_id, b2)
        self.assertEqual(handle.header.record_count, 150)
        self.assertEqual(handle.header.free_list_head, b1)
        self.assertEqual(handle.header.total_blocks, 3)

        new_fm.close_all()


if __name__ == "__main__":
    unittest.main()
