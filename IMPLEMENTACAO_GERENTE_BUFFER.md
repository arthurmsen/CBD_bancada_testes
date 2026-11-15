# Documento de Implementação: Gerente de Buffer Pool (Buffer Pool Manager)
## Avaliação 2 – Indexação e Processamento de Consultas
**Disciplina:** Construção de Banco de Dados (CBD) – UFRJ 2026.2  
**Professor:** Milton R. Ramirez  

---

## 1. Visão Geral do Módulo

O **Gerente de Buffer Pool (`BufferPoolManager`)** constitui a camada intermediária fundamental (Camada 4 - Topo) do Sistema de Gerenciamento de Banco de Dados. Ele atua como o **mediador estrito e exclusivo** entre a memória volátil (frames de 4096 bytes) e a memória secundária (arquivos gerenciados pelo `FileManager`).

Conforme o edital da disciplina e as boas práticas de SBD:
> **"Recomenda-se que nenhuma camada acima do Gerente de Buffer Pool acesse blocos da memória diretamente."**

O `BufferPoolManager` implementado garante que todas as operações relacionais (scans, buscas pontuais, junções e manipulação de índices) ocorram única e exclusivamente através de blocos fixados (*pinned pages*) em frames de memória, contabilizando com precisão matemática as requisições, acertos (*hits*), faltas (*misses*), gravações sujas e gravações forçadas.

---

## 2. Rastreamento e Conformidade com o PLANO_IMPLEMENTACAO.md

A tabela a seguir demonstra a correspondência direta entre cada decisão aprovada no `PLANO_IMPLEMENTACAO.md` (Módulo 2) e o código implementado:

| Tópico do Plano | Decisão Homologada | Elemento em Código | Descrição da Solução |
| :--- | :--- | :--- | :--- |
| **2.1 Relação Blocos/Páginas** | **1 bloco por página (1:1)**; $N=100$ páginas base (400 KB) | `Frame.data = bytearray(4096)`<br>`DEFAULT_BUFFER_POOL_SIZE = 100` | Frames com tamanho atômico de 4096 bytes idêntico aos blocos do `FileManager`, eliminando fragmentação e overhead de mapeamento. |
| **2.2 Repartição do Buffer** | **Quotas Fixas Estritas** (sem modo compartilhado) | `BufferPoolManager._calculate_partitions`<br>`self._partitions[PageType]` | Divisão rígida dos frames em 4 áreas funcionais: `CATALOG` (5%), `INDEX` (25%), `DATA` (50%) e `WORK` (20%). Cada área possui frames e replacer próprios. |
| **2.3 Políticas de Substituição** | Apenas **LRU** e **MRU** | `LRUReplacer`<br>`MRUReplacer`<br>`ReplacerType` | `LRU` para cargas com localidade temporal e buscas pontuais; `MRU` para o laço interno de junções (*Nested Loop*). |
| **2.4 Toss-Immediate** | **SIM**, via flag `toss_immediate=True` | `Replacer.unpin(toss_immediate=True)`<br>`_toss_immediate_frames` | Páginas desfixadas com toss-immediate são colocadas no topo da fila de expulsão, sendo descartadas imediatamente no próximo miss. |
| **2.5 Fixação de Blocos** | `pin_count` atômico + **Context Manager (`pin_guard`)** | `BufferPoolManager.pin_page`<br>`BufferPoolManager.unpin_page`<br>`BufferPoolManager.pin_guard` | Frames com `pin_count > 0` são imunes a expulsão. O `pin_guard` erradica vazamento de pins e perda de dirty bit (Ponto de Atenção 7.1). |
| **2.6 Gravação Forçada** | **SIM**, método `force_page` e `flush_all` | `BufferPoolManager.force_page`<br>`BufferPoolManager.flush_all` | Gravação síncrona imediata sem desalojar o frame do buffer pool, com contador dedicado `forced_writes`. |
| **2.7 Retenção Preferencial** | **Hierarquia de Prioridades** (Níveis 0 a 3) | `BasePriorityReplacer._priorities`<br>`PRIORITY_CATALOG`, `PRIORITY_INDEX_*` | Nós internos de B+ e catálogo possuem prioridade superior; o replacer expulsa primeiro os frames de menor prioridade na cota. |
| **2.8 Leitura Antecipada** | **Prefetching Sequencial em Lote** | `BufferPoolManager.prefetch_blocks` | Pré-carrega lotes de blocos contíguos na cota apropriada, transformando acessos futuros em 100% de *hits* em memória. |
| **2.9 Métricas Auditadas** | Requests, Hits, Misses, Hit Ratio, Dirty Evictions, Forced Writes | `BufferMetrics`<br>`BufferPoolManager.get_metrics` | Coleta analítica em tempo real tanto no consolidado global quanto por partição funcional individual. |

---

## 3. Arquitetura e Estrutura de Arquivos

O módulo foi estruturado no pacote `src/buffer/`:

```
src/
└── buffer/
    ├── __init__.py               # API pública de exportação do módulo
    ├── constants.py              # Enumerações de PageType, ReplacerType, cotas e prioridades
    ├── frame.py                  # Abstração de Frame físico na memória volátil
    ├── replacer.py               # Algoritmos de substituição LRU e MRU com prioridade e toss-immediate
    ├── buffer_metrics.py         # Dataclass de coleta de métricas analíticas e cálculo de hit ratio
    └── buffer_pool_manager.py    # Gerente central do Buffer Pool e Context Manager PinGuard
```

---

## 4. Detalhamento Técnico das Funcionalidades

### 4.1 Particionamento Rígido com Cotas Fixas (`src/buffer/constants.py`)
Para atender à exigência de determinismo estrito nas fórmulas analíticas de custo (onde o parâmetro de memória de junção $B$ deve permanecer constante durante todo o teste), o buffer pool particiona os $N$ frames disponíveis nas quatro áreas:
- **`CATALOG` (5%)**: Armazena o Bloco 0 das relações e o esquema de tabelas. Apresenta taxa de acerto próxima a 100%.
- **`INDEX` (25%)**: Reservado para nós de Árvore B+ e baldes de diretório de tabelas Hash.
- **`DATA` (50%)**: Frames dedicados aos blocos de dados dos arquivos Heap e páginas primárias de tuplas.
- **`WORK` (20%)**: Área de trabalho exclusiva para os algoritmos relacionais ($B$ blocos dedicados para *Block Nested-Loop*, buffers de *Sort-Merge* e partições de *Partition-Hash*).

**Isolamento de Recursos:** Se uma partição atinge seu limite e todos os seus frames estão fixados, o sistema levanta `BufferPoolFullError` em vez de roubar frames de outra partição, impedindo o fenômeno destrutivo de **Buffer Pollution** (varredura de dados expulsando nós de índices ou catálogo).

### 4.2 Abstração de Frame (`src/buffer/frame.py`)
Cada frame gerencia:
- `data: bytearray`: Buffer mutável de exatamente 4096 bytes.
- `file_id` e `block_id`: Chave composta unívoca da página residente.
- `pin_count`: Contador de leitores/escritores concorrentes ativos.
- `is_dirty`: Flag indicando se houve alteração nos dados desde o carregamento.
- `priority`: Grau de retenção hierárquica (0 = Normal, 1 = Folha, 2 = Interno, 3 = Catálogo).
- `last_accessed_time`: Timestamp monotonicamente crescente para ordenação de recência.

### 4.3 Políticas de Substituição LRU e MRU (`src/buffer/replacer.py`)
Os replacers implementam a interface abstrata `Replacer`:
- **`LRUReplacer`**:
  1. Verifica se há páginas na lista `toss_immediate`. Se houver, desaloja uma delas prioritariamente.
  2. Caso contrário, identifica o menor nível de prioridade ativo entre os frames desfixados.
  3. Dentre os candidatos de menor prioridade, seleciona o frame com menor timestamp (acesso mais antigo).
- **`MRUReplacer`**:
  1. Verifica prioritariamente frames marcados com `toss_immediate`.
  2. Dentre os frames de menor prioridade, seleciona o frame com maior timestamp (acesso mais recente). Esta política é matematicamente ótima para o laço interno de *Block Nested-Loop Joins*.

### 4.4 Mecanismo de Proteção PinGuard (`src/buffer/buffer_pool_manager.py`)
Para erradicar o erro clássico de desenvolvimento apontado no item 7.1 do edital (*"pin sem unpin correspondente, ou página alterada sem ligar o dirty bit"*), foi criado o context manager `pin_guard`:

```python
with buffer_pool.pin_guard(file_id, block_id, PageType.DATA) as guard:
    # Modifica o buffer de bytes
    guard.data[0:4] = b"NOVO"
    guard.set_dirty(True)
    # guard.set_toss_immediate(True)  # se não precisar mais da página
# O unpin é executado atomicamente ao sair do bloco, mesmo sob exceções!
```

### 4.5 Gravação Forçada e Flush de Páginas Sujas
- **`dirty_page_evictions`**: Quando um frame é selecionado como vítima e possui `is_dirty == True`, o `BufferPoolManager` invoca `file_manager.write_block()` para sincronizar os dados com o disco antes de carregar o novo bloco, incrementando o contador de expulsões sujas.
- **`forced_writes` (`force_page` / `flush_all`)**: Permite a persistência síncrona imediata da página sem desalojá-la do buffer pool, essencial para gravações de Bloco 0 ao criar tabelas e checkpoints.

### 4.6 Prefetching Sequencial
O método `prefetch_blocks(file_id, start_block, count, page_type)` pré-carrega blocos contíguos do arquivo no buffer pool com custo de seek nulo via `FileManager`, deixando as páginas residentes no cache com `pin_count = 0`. Os acessos posteriores da aplicação encontram as páginas imediatamente na memória (100% *HIT*).

---

## 5. Validação e Testes Automatizados

A suíte de testes em `tests/test_buffer_pool.py` contempla 10 cenários de teste unitários e de estresse cobrindo todas as exigências do plano:

1. **`test_partition_quotas`**: Valida a precisão das proporções matemáticas de cotas fixas (`CATALOG`, `INDEX`, `DATA`, `WORK`).
2. **`test_hit_miss_and_metrics`**: Valida o registro exato de MISS no primeiro acesso e HIT no segundo, com cálculo correto de `hit_ratio`.
3. **`test_pinned_pages_immunity_and_full_error`**: Garante que frames com `pin_count > 0` não são expulsos e levantam `BufferPoolFullError` ao esgotar a cota.
4. **`test_pin_guard_context_manager`**: Valida a automação de unpin, integridade do `dirty_bit` e sincronização física de dados modificados.
5. **`test_lru_eviction_policy`**: Valida que o frame menos recentemente acessado é expulso em uma partição cheia.
6. **`test_mru_eviction_policy`**: Valida que o frame mais recentemente acessado é expulso sob a política MRU.
7. **`test_toss_immediate`**: Valida que uma página sinalizada com toss-immediate é desalojada prioritariamente no próximo miss, superando a ordem temporal normal.
8. **`test_priority_retention`**: Valida que nós de índice com prioridade superior (nó interno = 2) são preservados no buffer, enquanto nós folha (prioridade 1) são expulsos primeiro.
9. **`test_prefetching`**: Valida o pré-carregamento de blocos contíguos e a conversão de acessos subsequentes em 100% de hits.
10. **`test_dirty_eviction_persists_to_disk`**: Valida que páginas modificadas que sofrem expulsão natural são gravadas no disco físico de forma transparente e íntegra.

### Resultado dos Testes Automatizados:
```bash
python3 -m unittest discover -s tests -v

test_dirty_eviction_persists_to_disk (test_buffer_pool.TestBufferPoolManager.test_dirty_eviction_persists_to_disk) ... ok
test_hit_miss_and_metrics (test_buffer_pool.TestBufferPoolManager.test_hit_miss_and_metrics) ... ok
test_lru_eviction_policy (test_buffer_pool.TestBufferPoolManager.test_lru_eviction_policy) ... ok
test_mru_eviction_policy (test_buffer_pool.TestBufferPoolManager.test_mru_eviction_policy) ... ok
test_partition_quotas (test_buffer_pool.TestBufferPoolManager.test_partition_quotas) ... ok
test_pin_guard_context_manager (test_buffer_pool.TestBufferPoolManager.test_pin_guard_context_manager) ... ok
test_pinned_pages_immunity_and_full_error (test_buffer_pool.TestBufferPoolManager.test_pinned_pages_immunity_and_full_error) ... ok
test_prefetching (test_buffer_pool.TestBufferPoolManager.test_prefetching) ... ok
test_priority_retention (test_buffer_pool.TestBufferPoolManager.test_priority_retention) ... ok
test_toss_immediate (test_buffer_pool.TestBufferPoolManager.test_toss_immediate) ... ok
test_block_read_write (test_file_manager.TestFileManager.test_block_read_write) ... ok
test_bounds_checking (test_file_manager.TestFileManager.test_bounds_checking) ... ok
test_create_and_open_file (test_file_manager.TestFileManager.test_create_and_open_file) ... ok
test_free_list_lifo_reuse_and_extension (test_file_manager.TestFileManager.test_free_list_lifo_reuse_and_extension) ... ok
test_io_counters_sequential_vs_random (test_file_manager.TestFileManager.test_io_counters_sequential_vs_random) ... ok
test_persistence_across_close_reopen (test_file_manager.TestFileManager.test_persistence_across_close_reopen) ... ok

----------------------------------------------------------------------
Ran 16 tests in 0.049s

OK
```

---

## 6. Próximo Passo: Formato de Registros e Slotted Page

Com a Camada 4 integralmente concluída e auditada (`FileManager` + `BufferPoolManager`), a infraestrutura de I/O em bloco está 100% operacional. O próximo passo lógico é implementar a **Camada 3 (Layout de Registros e Slotted Page)**:
- Cabeçalho de página com array de slots `(offset, length)`.
- Suporte a registros com campos de tamanho fixo, campos de tamanho variável (`VARCHAR`), `Null Bitmap` e campos multivalorados.
- Identificador universal de tupla **$\text{RID} = (\text{block\_id}, \text{slot\_num})$**.
- Deleção lógica com *tombstones* e compactação sob demanda (*lazy defragmentation*).
