# Documento de Implementação: Gerente de Arquivos (File Manager)
## Avaliação 2 – Indexação e Processamento de Consultas
**Disciplina:** Construção de Banco de Dados (CBD) – UFRJ 2026.2  
**Professor:** Milton R. Ramirez  

---

## 1. Visão Geral do Módulo

O **Gerente de Arquivos (`FileManager`)** constitui a camada de mais baixo nível do Sistema de Banco de Dados (Camada 4 - Base). Sua responsabilidade primordial é abstrair o armazenamento em disco secundário através de arquivos binários do sistema operacional divididos em blocos de tamanho fixo (**4096 bytes**), oferecendo operações primitivas de I/O de bloco e instrumentação física detalhada para a camada superior do **Gerente de Buffer Pool**.

Conforme estabelecido pela arquitetura formal da disciplina, **nenhuma camada acima do Gerente de Buffer Pool deve acessar arquivos de disco diretamente**. O `FileManager` foi projetado especificamente para ser consumido pelo `BufferPoolManager`.

---

## 2. Rastreamento e Conformidade com o PLANO_IMPLEMENTACAO.md

A tabela abaixo mapeia cada decisão arquitetural aprovada no `PLANO_IMPLEMENTACAO.md` (Módulo 1) para a sua respectiva implementação em código:

| Tópico do Plano | Decisão Homologada | Elemento em Código | Descrição da Solução |
| :--- | :--- | :--- | :--- |
| **1.1 Tamanho do Bloco** | Blocos fixos de **4 KB (4096 bytes)** | `constants.BLOCK_SIZE = 4096` | Alinhamento nativo com páginas de SO e setores físicos 4Kn; validação estrita em todas as operações de I/O. |
| **1.2 Técnica de Alocação** | **Offset Direto** com Extensões Lógicas | `FileManager.read_block`<br>`FileManager.write_block` | Acesso em tempo constante $O(1)$: `offset = block_id * BLOCK_SIZE`. Mapeamento sem indireções intermediárias. |
| **1.3 Descritor (Bloco 0)** | **Bloco 0 dedicado** com cabeçalho de 4096 bytes | `FileHeader`<br>`HEADER_FORMAT = "<10I4056s"` | Bloco binário com assinatura `0x0CBD2026`, tipos de arquivo, contagem de blocos e metadados lidos em exatamente 1 I/O físico. |
| **1.4 Gerência de Blocos Livres** | **Free List Encadeada LIFO** em $O(1)$ | `FileManager.allocate_block`<br>`FileManager.free_block` | Consumo de zero bytes adicionais (usa os primeiros 4 bytes do bloco livre para encadear); expansão sequencial no final do arquivo quando vazia. |
| **1.5 Contadores Físicos** | Matriz de contadores locais e globais (**Seq vs. Seek**) | `IOCounters`<br>`FileManager._record_physical_read`<br>`FileManager._record_physical_write` | Distingue acessos contíguos ($\Delta_{\text{seek}} = 0$) de saltos aleatórios ($\text{seek}++$ e distância), tanto por arquivo quanto no barramento global. |

---

## 3. Estrutura dos Arquivos Implementados

O código foi organizado no pacote `src/storage/`, com separação clara de responsabilidades:

```
src/
├── __init__.py
└── storage/
    ├── __init__.py          # Exportações da API pública de armazenamento
    ├── constants.py        # Constantes globais (BLOCK_SIZE, MAGIC_NUMBER, NO_BLOCK, tipos)
    ├── file_header.py      # Serialização/deserialização binária do Bloco 0 (FileHeader)
    ├── io_counters.py      # Dataclass de métricas e instrumentação de acessos físicos
    └── file_manager.py     # Núcleo do Gerente de Arquivos e controle de alças (FileHandle)
```

---

## 4. Detalhamento Técnico dos Componentes

### 4.1 Constantes Físicas (`src/storage/constants.py`)
- `BLOCK_SIZE = 4096`: Garante que todo frame do buffer pool e todo bloco de disco compartilhem a mesma unidade de transferência.
- `MAGIC_NUMBER = 0x0CBD2026`: Identificador binário de 32 bits presente nos primeiros 4 bytes do Bloco 0 para validação de integridade.
- `NO_BLOCK = 0xFFFFFFFF` ($2^{32}-1$): Sentinela para ponteiros nulos de blocos (ex: fim da Free List ou árvore B+ sem raiz inicial).
- Tipos de arquivo suportados: `FILE_TYPE_HEAP = 1`, `FILE_TYPE_BTREE = 2`, `FILE_TYPE_HASH = 3`.

### 4.2 Cabeçalho de Arquivo / Bloco 0 (`src/storage/file_header.py`)
Implementa a classe `FileHeader` com layout binário de exatamente 4096 bytes, formatado via `struct.pack` / `struct.unpack` com padrão little-endian (`<10I4056s`):

```
+-------------------------------------------------------------------------+
| Byte Offset | Campo            | Tipo    | Papel no SBD                 |
+-------------------------------------------------------------------------+
| 0x00 - 0x03 | magic_number     | uint32  | 0x0CBD2026 (Assinatura)      |
| 0x04 - 0x07 | file_type        | uint32  | 1=HEAP, 2=BTREE, 3=HASH      |
| 0x08 - 0x0B | block_size       | uint32  | 4096 bytes                   |
| 0x0C - 0x0F | total_blocks     | uint32  | Total de blocos alocados     |
| 0x10 - 0x13 | free_list_head   | uint32  | 1º bloco vago (ou NO_BLOCK)  |
| 0x14 - 0x17 | first_data_blk   | uint32  | 1º bloco de dados da relação |
| 0x18 - 0x1B | last_data_blk    | uint32  | Último bloco para append     |
| 0x1C - 0x1F | root_block_id    | uint32  | Raiz da B+ / Diretório Hash  |
| 0x20 - 0x23 | record_count     | uint32  | Total de tuplas ativas       |
| 0x24 - 0x27 | schema_version   | uint32  | Versão do dicionário/esquema |
| 0x28 - 0xFFF| reserved         | 4056 B  | Espaço reservado para futuro |
+-------------------------------------------------------------------------+
```

### 4.3 Contadores de I/O Físico (`src/storage/io_counters.py`)
A classe `IOCounters` armazena os indicadores fundamentais requeridos pelo edital da disciplina:
- `physical_reads_sequential`: Leituras contíguas sem movimentação de cabeça de disco.
- `physical_reads_random`: Leituras que exigiram reposicionamento (*seek*).
- `physical_writes_sequential`: Gravações contíguas em disco.
- `physical_writes_random`: Gravações com reposicionamento de cabeça.
- `total_seeks`: Quantidade absoluta de operações de reposicionamento.
- `seek_distance_sum`: Distância total percorrida pela cabeça virtual em número de blocos ($|\text{bloco\_atual} - \text{último\_bloco}|$).
- Propriedades calculadas: `total_reads`, `total_writes`, `total_blocks_transferred` e `total_bytes_transferred`.

### 4.4 O Gerente de Arquivos (`src/storage/file_manager.py`)
O `FileManager` centraliza toda a manipulação dos arquivos no sistema operacional:
1. **`create_file(path, file_type, ...)`**: Cria o arquivo físico `.heap`, `.bplus` ou `.hash`, grava o Bloco 0 inicializado (`total_blocks=1`) e força sincronização com disco via `fsync`.
2. **`open_file(path)`**: Abre o fluxo binário no modo `'r+b'`, lê e valida o Bloco 0 e instancia um `FileHandle`.
3. **`read_block(file_id, block_id)`**:
   - Valida limites ($0 \le block\_id < total\_blocks$).
   - Executa `stream.seek(block_id * BLOCK_SIZE)` e lê exatamente 4096 bytes.
   - Atualiza contadores locais e globais conforme a regra sequencial/seek.
4. **`write_block(file_id, block_id, data)`**:
   - Valida tamanho dos dados ($== 4096$) e limites de bloco.
   - Posiciona e grava no offset do bloco, descarregando com `flush()`.
5. **`allocate_block(file_id)`**:
   - **Caso A (Reuso via Free List):** Se `free_list_head != NO_BLOCK`, reaproveita o bloco em $O(1)$. Lê os primeiros 4 bytes do bloco reaproveitado para atualizar `free_list_head = next_free`, zera o bloco reaproveitado e persiste o Bloco 0.
   - **Caso B (Extensão Sequencial):** Se a lista estiver vazia, aloca no final do arquivo: `new_block_id = total_blocks++`, grava um bloco zerado de 4096 bytes e persiste o Bloco 0.
6. **`free_block(file_id, block_id)`**:
   - Impede desalocação do Bloco 0 (`block_id > 0`).
   - Empacota o antigo `free_list_head` nos primeiros 4 bytes do bloco liberado e o grava no disco.
   - Atualiza o Bloco 0 com `free_list_head = block_id` (inserção no topo da pilha em $O(1)$).
7. **Detecção de Acesso Sequencial vs. Seek**:
   - No nível de arquivo individual:
     $$\text{is\_seq} \iff \text{block\_id} == \text{handle.last\_accessed\_block\_id} + 1$$
   - No nível global:
     $$\text{is\_seq\_global} \iff (\text{file\_id} == \text{global\_last\_file\_id}) \land (\text{block\_id} == \text{global\_last\_block\_id} + 1)$$
   - Alternância entre tabelas durante uma junção é registrada como *seek* global, refletindo com precisão o custo da alternância de arquivos no disco físico.

---

## 5. Validação e Testes Automatizados

Foi desenvolvida a suíte de testes em `tests/test_file_manager.py`, contemplando 6 cenários de teste unitários e de estresse:

1. **`test_create_and_open_file`**: Criação de arquivo, validação de campos iniciais do Bloco 0 e reabertura consistente.
2. **`test_block_read_write`**: Gravação de payload completo de 4096 bytes e leitura fiel com integridade byte a byte.
3. **`test_bounds_checking`**: Verificação de disparo de `IndexError` em acessos além de `total_blocks` e `ValueError` para blocos de tamanho incorreto.
4. **`test_free_list_lifo_reuse_and_extension`**: Teste de ciclo de vida da Free List: alocação sequencial (blocos 1, 2, 3), liberação (blocos 2 e 1), reaproveitamento LIFO imediato (reuso do bloco 1, depois bloco 2) e retorno à extensão sequencial (bloco 4).
5. **`test_io_counters_sequential_vs_random`**: Validação matemática dos contadores: leitura contígua dos blocos 1, 2 e 3 gera 1 seek inicial e 2 leituras sequenciais; salto de volta para o bloco 1 incrementa o contador de seeks e leituras aleatórias.
6. **`test_persistence_across_close_reopen`**: Garantia de que metadados, contagem de blocos e ponteiros da Free List sobrevivem ao encerramento e reinicialização completa da aplicação.

### Resultado da Execução dos Testes:
```bash
python3 -m unittest discover -s tests -v

test_block_read_write (test_file_manager.TestFileManager.test_block_read_write) ... ok
test_bounds_checking (test_file_manager.TestFileManager.test_bounds_checking) ... ok
test_create_and_open_file (test_file_manager.TestFileManager.test_create_and_open_file) ... ok
test_free_list_lifo_reuse_and_extension (test_file_manager.TestFileManager.test_free_list_lifo_reuse_and_extension) ... ok
test_io_counters_sequential_vs_random (test_file_manager.TestFileManager.test_io_counters_sequential_vs_random) ... ok
test_persistence_across_close_reopen (test_file_manager.TestFileManager.test_persistence_across_close_reopen) ... ok

----------------------------------------------------------------------
Ran 6 tests in 0.010s

OK
```

---

## 6. Próximo Passo: Integração com o Gerente de Buffer Pool

Com o **Gerente de Arquivos** concluído, testado e em conformidade estrita com o plano arquitetural, o próximo passo é implementar a camada imediatamente superior: o **Gerente de Buffer Pool (`BufferPoolManager`)**, que:
- Alocará os frames em memória de 4096 bytes com quotas fixas (`CATALOG`, `INDEX`, `DATA`, `WORK`).
- Implementará os algoritmos de substituição **LRU** e **MRU**.
- Utilizará o `FileManager` para transferir blocos sob demanda via `pin_page` e persistir páginas sujas via `unpin_page` e `force_page`.
