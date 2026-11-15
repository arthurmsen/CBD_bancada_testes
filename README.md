# Bancada de Testes para Avaliação de Desempenho de SBD
## Avaliação 2 – Indexação e Processamento de Consultas
**Universidade Federal do Rio de Janeiro (UFRJ)**  
**Disciplina:** Construção de Banco de Dados (CBD) – 2026.2  
**Professor:** Milton R. Ramirez  

---

## 1. Resumo do Projeto

Este projeto consiste na construção de uma **bancada de testes completa e instrumentada** para avaliação comparativa do desempenho no processamento de operações relacionais em Sistemas de Banco de Dados (SBD). O objetivo primordial é fornecer medidas empíricas rigorosas (principalmente o volume de blocos transferidos entre a memória secundária e o buffer pool) para responder à questão central:

> **"Qual configuração do SBD é mais adequada para realizar o processamento eficiente de uma dada carga de trabalho para uma determinada base de dados?"**

O sistema foi concebido em **quatro camadas estritamente desacopladas**, onde cada camada consome unicamente os serviços da camada imediatamente inferior, garantindo fidelidade de baixo nível à arquitetura real de sistemas gerenciadores de banco de dados:

```
+-------------------------------------------------------------------------+
| Camada 1: Bancada de Teste (Orquestrador & Benchmark)                   |
| - Gerador da Base de Dados (Esquema Universitário UFRJ)                 |
| - Executor de Experimentos, Variação de Fatores e Coleta de Métricas   |
| - Fórmulas de Custo Analítico vs. Custo Medido Real                     |
| - Exportador CSV, Gráficos e Validação Cruzada contra SQLite            |
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
| Camada 2: Mini Monitor de Execução (Primitivas Relacionais)             |
| - Seleções: Pontual (Equal) e Faixa (Inequality) a 1% e 50% de sel.     |
| - Junções: Block Nested-Loop Join, Sort-Merge Join, Partition-Hash Join |
| - Algoritmos operam estritamente sobre blocos fixados (pinned pages)    |
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
| Camada 3: Gerente de Armazenamento & Mini Catálogo                      |
| - Mini Catálogo (Dicionário de Dados, Schemas, Metadados e Estatísticas)|
| - Organizações Primárias: Heap, Árvore B+ e Hash Estático               |
| - Índices Secundários: Hash (UNIQUE) e B+ com Posting List (NOT UNIQUE) |
| - Slotted Page, Serialização de Registros, Null Bitmap e Desfragmentação|
+-------------------------------------------------------------------------+
                                    |
                                    v
+-------------------------------------------------------------------------+
| Camada 4: Gerente de Buffer Pool & Gerente de Arquivos                  |
| - BufferPoolManager: Frames de 4KB, Pin/Unpin, Dirty Bit, Eviction      |
|   (LRU e MRU), Toss-Immediate, Particionamento em Cotas Fixas          |
| - FileManager: I/O em bloco binário, Contadores Físicos (Seq vs. Seek)  |
|   Simulação de Disco com blocos de tamanho fixo (4096 bytes)            |
+-------------------------------------------------------------------------+
```

---

## 2. O Que Já Foi Implementado (Fase 1 Concluída)

Até o momento, a infraestrutura fundamental de **I/O físico e gerenciamento de memória volátil (Camada 4)** foi 100% implementada, documentada e auditada com 16 testes unitários e de estresse automatizados:

### 2.1 Gerente de Arquivos (`src/storage/`)
* **Blocos de 4 KB (4096 bytes)**: Alinhamento nativo com setores físicos 4Kn e páginas de memória virtual do SO.
* **Alocação por Offset Direto em $O(1)$**: `offset = block_id * BLOCK_SIZE`, viabilizando acesso instantâneo a nós e baldes de índices.
* **Descritor no Bloco 0 (`FileHeader`)**: Layout binário fixo de 4096 bytes contendo assinatura (`0x0CBD2026`), tipo de arquivo (`HEAP`, `BTREE`, `HASH`), total de blocos, cabeça da Free List e metadados lidos em exatamente 1 I/O físico.
* **Gerência de Blocos Livres via Free List LIFO em $O(1)$**: Consome zero bytes adicionais (usa os primeiros 4 bytes do bloco vago para encadear) e realiza extensão sequencial dinâmica no final do arquivo quando a lista estiver vazia.
* **Instrumentação de I/O Físico (`IOCounters`)**: Rastreamento de leituras e gravações físicas distinguindo acessos sequenciais ($\Delta_{\text{seek}} = 0$) de acessos aleatórios (*seeks*), acumulando distâncias de salto tanto por arquivo quanto no barramento global de disco.
* 📄 **Documentação detalhada:** [`IMPLEMENTACAO_GERENTE_ARQUIVOS.md`](file:///Users/arthurmonteiro/Desktop/trabalho2_CBD/IMPLEMENTACAO_GERENTE_ARQUIVOS.md)

### 2.2 Gerente de Buffer Pool (`src/buffer/`)
* **Relação 1:1 entre Bloco de Disco e Frame**: Frames de 4096 bytes gerenciados em memória volátil.
* **Particionamento em Cotas Fixas Rígidas**:
  - `CATALOG` (5% - 5 frames): Bloco 0 das relações e esquemas de catálogo.
  - `INDEX` (25% - 25 frames): Nós de Árvore B+ e baldes de diretório de Hash.
  - `DATA` (50% - 50 frames): Blocos de dados de arquivos Heap e registros.
  - `WORK` (20% - 20 frames): Área de trabalho reservada exclusivamente para junções e ordenação externa ($B=20$).
  - *Isolamento de Recursos:* Impede o fenômeno destrutivo de **Buffer Pollution** (varreduras de dados não expulsam nós de índice ou catálogo).
* **Políticas de Substituição**:
  - **`LRUReplacer`**: Expulsa o frame menos recentemente usado da respectiva cota.
  - **`MRUReplacer`**: Expulsa o frame mais recentemente usado (ótimo para o laço interno de *Block Nested-Loop Joins*).
* **Mecanismos Avançados**:
  - **Toss-Immediate**: Flag `toss_immediate=True` no desfixar páginas de varredura única, priorizando sua expulsão imediata no próximo *miss*.
  - **PinGuard (Context Manager)**: Prevenção estrita contra vazamento de pins (`with buffer_pool.pin_guard(...) as guard:`) garantindo desfixação atômica mesmo sob exceções e persistência de dirty bits.
  - **Retenção Preferencial**: Preservação hierárquica de nós internos de índices frente a nós folha.
  - **Gravação Forçada (`force_page`)** e **Prefetching Sequencial em Lote (`prefetch_blocks`)**.
  - **Coleta de Métricas (`BufferMetrics`)**: Rastreamento de requisições, hits, misses, taxa de acerto (`hit_ratio`), expulsões sujas e gravações forçadas.
* 📄 **Documentação detalhada:** [`IMPLEMENTACAO_GERENTE_BUFFER.md`](file:///Users/arthurmonteiro/Desktop/trabalho2_CBD/IMPLEMENTACAO_GERENTE_BUFFER.md)

---

## 3. O Que Pretende Ser Implementado (Roadmap Completo)

As próximas etapas cobrirão as Camadas 3, 2 e 1, além da orquestração dos benchmarks e entrega dos relatórios formais da disciplina:

```
[CONCLUÍDO] Camada 4: FileManager + BufferPoolManager (Testes 100% OK)
     │
     ▼
[PRÓXIMO]   Camada 3A: Formato de Registros e Slotted Page
     │                 - Registro binário, campos fixos/variáveis, null bitmap, tombstone
     ▼
            Camada 3B: Organizações Primárias & Métodos de Acesso (Matriz Seção 4.2)
     │                 - Heap File (scan, diretório de espaço livre FSM)
     │                 - Árvore B+ Primária e Secundária (posting list de RIDs sem repetição)
     │                 - Hash Primário e Secundário (MurmurHash3, encadeamento de overflow)
     ▼
            Camada 2:  Mini Monitor de Execução (Primitivas Relacionais)
     │                 - Seleções (pontual UNIQUE e faixa NOT UNIQUE a 1% e 50% de seletividade)
     │                 - Junções: Block Nested-Loop, Sort-Merge Join e Partition-Hash Join
     ▼
            Camada 1:  Bancada de Testes, Benchmark & Automação
     │                 - Gerador da base acadêmica UFRJ (Curso, Disciplina, Turma, Aluno, etc.)
     │                 - Espelho de validação cruzada em SQLite
     │                 - Automação de experimentos (3 repetições, variância < 5%)
     │                 - Fórmulas teóricas analíticas vs. Custos medidos reais
     ▼
            Entregáveis Acadêmicos & Análise de IA
                       - Arquivo POST_MORTEM.md na raiz (Code Review de IA e refatorações)
                       - Relatório Técnico final em PDF/Markdown e slides de apresentação
```

### Detalhamento das Próximas Entregas:

### 3.1 Camada 3: Formato de Registros e Slotted Page
- Estrutura de **Slotted Page** em blocos de 4 KB: cabeçalho de página, array de slots `(offset, length)` e corpo de tuplas crescendo do fim para o meio.
- Identificador universal de tupla **$\text{RID} = (\text{block\_id}, \text{slot\_num})$**.
- Tratamento de campos fixos (`INT`, `FLOAT`, `CHAR`), variáveis (`VARCHAR`), campos nulos com **Null Bitmap** e campos multivalorados com prefixo de tamanho.
- Deleção lógica com *tombstones* e desfragmentação interna sob demanda (*lazy defragmentation*).

### 3.2 Camada 3: Organizações Primárias e Métodos de Acesso (Matriz Seção 4.2)
- **Heap File**:
  - Varredura de arquivo direta (*File Scan*).
  - Índice secundário Hash para campo UNIQUE ($\text{Chave} \to \text{RID}$).
  - Índice secundário Árvore B+ para campo NOT UNIQUE ($\text{Chave} \to [\text{RID}_1, \text{RID}_2, \dots]$ sem repetição de chave nas folhas).
- **Árvore B+ Primária**:
  - Acesso direto ordenado via índice primário clusterizado (para campo UNIQUE e NOT UNIQUE).
  - Índice secundário clustering Árvore B+ para campo NOT UNIQUE.
- **Hash Primário**:
  - Acesso direto via índice primário UNIQUE e NOT UNIQUE.
  - Função de dispersão **MurmurHash3 (32-bit)** com encadeamento de baldes de overflow.
  - Índice secundário clustering Árvore B+ para campo NOT UNIQUE.

### 3.3 Camada 2: Mini Monitor de Execução (Primitivas Relacionais)
- **Operações de Seleção**:
  1. Seleção pontual por igualdade (`=`) em campo UNIQUE.
  2. Seleção por faixa de valores (`<`, `<=`, `>`, `>=`) em campo NOT UNIQUE.
  3. Avaliação nas seletividades de **1% (baixa)** e **50% (alta)**.
- **Operações de Junção (Join)**:
  1. **Block Nested-Loop Join (BNLJ)**: Carregamento da tabela externa em chunks de $B - 2 = 18$ blocos no buffer de trabalho.
  2. **Sort-Merge Join (SMJ)**: Fase de ordenação externa baseada em runs no buffer pool + fase de mescla sequencial.
  3. **Partition-Hash Join (PHJ)**: Fase de particionamento hash com $B - 1 = 19$ partições temporárias + fase de junção com hash table in-memory da menor partição.
  4. Combinações de tamanho de relação: Menor $\times$ Menor, Menor $\times$ Maior e Maior $\times$ Maior.

### 3.4 Camada 1: Bancada de Testes, Base Acadêmica e Benchmark
- **Esquema Universitário UFRJ**:
  - Tabelas pequenas: `Curso` (10 a 20 registros, cabe inteiramente na cota do buffer).
  - Tabelas médias: `Disciplina` (50 a 100 registros) e `Turma` (200 a 500 registros).
  - Tabelas grandes: `Aluno` (5.000 a 20.000 registros) e `Inscricao_Aluno_Turma` (25.000 a 100.000 registros).
  - Tabela 1:1 para Equijoin UNIQUE: `Detalhes_do_Aluno`.
- **Validação Cruzada contra SQLite**: Execução simultânea das mesmas consultas sobre uma base SQLite com os mesmos dados sintéticos gerados, garantindo 100% de precisão de resultados.
- **Automação Estatística**: Repetição de cada medição no mínimo 3 vezes até atingir coeficiente de variação $< 5\%$.
- **Fórmulas de Custo Analítico vs. Medido**: Confronto direto das contagens físicas contra os modelos analíticos da literatura clássica (Elmasri & Navathe; Silberschatz, Korth & Sudarshan).

### 3.5 Entregáveis Acadêmicos e Post-Mortem de IA (Edital Seção 7)
- **`POST_MORTEM.md`**: Relatório de auditoria de IA contendo log de prompts, code review do código ingênuo vs. refatorado, testes de estresse e comprovação de superação de armadilhas comuns (vazamento de pins, leitura fora do buffer, memory leak em joins).
- **Relatório Técnico Final**: Análise comparativa profunda justificando todas as escolhas frente às opções preteridas, tabelas, gráficos de desempenho e recomendações práticas de configuração de SBD conforme a carga de trabalho.

---

## 4. Estrutura de Diretórios do Projeto

```
trabalho2_CBD/
├── README.md                              # Este documento (Visão geral e roadmap)
├── PLANO_IMPLEMENTACAO.md                 # Especificação formal e decisões homologadas
├── IMPLEMENTACAO_GERENTE_ARQUIVOS.md      # Detalhamento técnico da Camada 4 (Storage)
├── IMPLEMENTACAO_GERENTE_BUFFER.md        # Detalhamento técnico da Camada 4 (Buffer)
├── Avaliacao_2_v2_...pdf                  # Edital oficial da disciplina (UFRJ 2026.2)
│
├── src/                                   # Código-fonte modular da bancada
│   ├── __init__.py
│   ├── storage/                           # Camada 4: Gerente de Arquivos
│   │   ├── __init__.py
│   │   ├── constants.py                   # Constantes físicas (BLOCK_SIZE=4096, MAGIC_NUMBER)
│   │   ├── file_header.py                 # Descritor binário do Bloco 0 (FileHeader)
│   │   ├── io_counters.py                 # Contadores de I/O físico (Seq vs. Seeks)
│   │   └── file_manager.py                # Núcleo de I/O em disco e Free List O(1)
│   │
│   ├── buffer/                            # Camada 4: Gerente de Buffer Pool
│   │   ├── __init__.py
│   │   ├── constants.py                   # Enumerações de PageType, ReplacerType, Cotas
│   │   ├── frame.py                       # Abstração de Frame em memória volátil
│   │   ├── replacer.py                    # Políticas de substituição (LRU e MRU)
│   │   ├── buffer_metrics.py              # Coleta de métricas analíticas (Hits, Misses)
│   │   └── buffer_pool_manager.py         # Gerente central do Buffer Pool e PinGuard
│   │
│   ├── record/                            # [A implementar] Slotted Page e Registros
│   ├── catalog/                           # [A implementar] Mini Catálogo e Esquemas
│   ├── execution/                         # [A implementar] Mini Monitor de Execução
│   └── benchmark/                         # [A implementar] Gerador de Base e Orquestrador
│
└── tests/                                 # Baterias de testes automatizados
    ├── __init__.py
    ├── test_file_manager.py               # 6 testes unitários do Gerente de Arquivos
    └── test_buffer_pool.py                # 10 testes unitários do Gerente de Buffer Pool
```

---

## 5. Como Executar os Testes Automatizados

O ambiente requer **Python 3.10+** (desenvolvido e validado em Python 3.14). Não são necessárias dependências externas para a execução dos módulos base de armazenamento e buffer pool.

Para rodar toda a suíte de testes automatizados:

```bash
python3 -m unittest discover -s tests -v
```

### Exemplo de Saída Esperada:
```text
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
