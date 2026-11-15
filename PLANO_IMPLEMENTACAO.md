# PLANO DE IMPLEMENTAÇÃO E DECISÕES ARQUITETURAIS
## Avaliação 2 – Indexação e Processamento de Consultas
**Disciplina:** Construção de Banco de Dados - UFRJ 2026.2  
**Professor:** Milton R. Ramirez  

---

## 1. Módulo: Gerente de Arquivos (File Manager)

Este módulo é responsável por abstrair a memória secundária (disco) utilizando arquivos binários do sistema operacional divididos em blocos de tamanho fixo, oferecendo a interface estrita de I/O para o Buffer Pool e contabilizando acessos físicos.

### 1.1 Tamanho dos Blocos e Fator de Blocagem
* **Decisão Adotada:** **Blocos de 4 KB (4096 bytes)** por página.
* **Justificativas:**
  1. **Alinhamento com Arquitetura de SO e Hardware:** 4 KB é o tamanho de página de memória virtual em sistemas operacionais modernos (Linux, macOS, Windows em x86_64 e ARM64) e coincide com o tamanho de setor físico dos discos rígidos e SSDs modernos (*Advanced Format* 4Kn). Isso elimina o overhead de sobreposição de páginas de I/O em nível de kernel.
  2. **Fan-out Adequado para Árvores B+:** Em páginas de 4096 bytes, considerando chaves típicas (8 a 16 bytes) e ponteiros de bloco (4 a 8 bytes), um nó de Árvore B+ obtém um *fan-out* entre 150 e 250 entradas. Isso permite indexar mais de 1 milhão de tuplas com altura $h \le 3$, minimizando I/O de navegação.
  3. **Granularidade e Concorrência no Buffer Pool:** Em um buffer pool com capacidade configurável (ex: de 256 KB a 2 MB), o bloco de 4 KB oferece o dobro de frames de memória em relação a 8 KB. Isso viabiliza algoritmos como *Block Nested-Loop Join* com chunks maiores e particionamento em múltiplos baldes no *Partition-Hash Join*.
* **Comparação com as Opções Preteridas:**
  * **2 KB (Preterida):** Apresenta baixo fator de blocagem ($B_f$), desperdício percentual excessivo de cabeçalho da *slotted page* e exige o dobro de transferências de I/O para varreduras de tabelas.
  * **8 KB (Preterida como padrão):** Embora favoreça varreduras puramente sequenciais em tabelas grandes, reduz pela metade a quantidade de páginas simultâneas que cabem no buffer pool reservado para junções, gerando contenção artificial e expulsões prematuras durante testes de algoritmos relacionais em memória controlada.

---

### 1.2 Técnica de Alocação de Blocos no Arquivo
* **Decisão Adotada:** **Alocação por Offset Direto com Extensões Lógicas (Direct-Mapped Extent-based)**.
* **Mecanismo:** Cada relação ou índice possui seu próprio arquivo físico no SO (`<tabela>.heap`, `<indice>.bplus`, etc.). Cada bloco possui um identificador sequencial `block_id` ($0, 1, 2, \dots, N-1$). O cálculo da posição física no arquivo é $O(1)$:
  $$\text{byte\_offset} = \text{block\_id} \times \text{BLOCK\_SIZE}$$
* **Justificativas:**
  1. **Acesso Aleatório em Tempo Constante $O(1)$:** Essencial para índices B+ Tree e Hash, onde ponteiros de nós e baldes apontam diretamente para um `block_id`.
  2. **Compatibilidade com Buffer Pool:** O par `(file_id, block_id)` atua como chave unívoca para hash de busca de frames no buffer pool.
* **Comparação com as Opções Preteridas:**
  * **Alocação Encadeada (Preterida):** Exigiria que cada bloco guardasse um ponteiro para o próximo. Para ler o bloco $k$, seriam necessários $k$ acessos a disco. Torna a busca por índice ou busca binária em blocos inviável.
  * **Alocação Contígua Pré-fixada (Preterida):** Exigiria definir o tamanho máximo exato do arquivo antecipadamente. Ao esgotar o espaço, exigiria realocação e cópia de todo o arquivo no disco, inviabilizando inserções dinâmicas.

---

### 1.3 Estrutura do Cabeçalho do Arquivo (Descritor / Bloco 0)
* **Decisão Adotada:** **Bloco 0 reservado exclusivamente para o Cabeçalho do Arquivo (File Descriptor)**.
* **Layout Binário Fixo (4096 bytes):**

| Offset (Bytes) | Campo | Tipo | Descrição |
| :--- | :--- | :--- | :--- |
| `0x00 - 0x03` | `magic_number` | `uint32` | Assinatura de validação (ex: `0xCBD2026`) |
| `0x04 - 0x07` | `file_type` | `uint32` | Tipo do arquivo: `1`=Heap, `2`=B+ Tree, `3`=Hash |
| `0x08 - 0x0B` | `block_size` | `uint32` | Tamanho do bloco em bytes (4096) |
| `0x0C - 0x0F` | `total_blocks` | `uint32` | Total de blocos alocados no arquivo |
| `0x10 - 0x13` | `free_list_head`| `uint32` | ID do 1º bloco livre na lista (`0xFFFFFFFF` se vazia) |
| `0x14 - 0x17` | `first_data_blk`| `uint32` | ID do primeiro bloco de dados da relação |
| `0x18 - 0x1B` | `last_data_blk` | `uint32` | ID do último bloco de dados (para append rápido) |
| `0x1C - 0x1F` | `root_block_id` | `uint32` | Bloco raiz (em árvores B+) ou diretório (em Hash) |
| `0x20 - 0x23` | `record_count`  | `uint32` | Quantidade total de tuplas ativas |
| `0x24 - 0x27` | `schema_version`| `uint32` | Versão do esquema correspondente no catálogo |
| `0x28 - 0xFFF` | `reserved`      | `bytes`  | Metadados estendidos / preenchimento |

* **Justificativa:** Permite que qualquer operação `Open` carregue integralmente o estado físico do arquivo com exatamente 1 leitura de bloco (4 KB), garantindo persistência atômica e recuperação simples.

---

### 1.4 Gerência de Blocos Livres
* **Decisão Adotada:** **Lista Encadeada de Blocos Livres (*Free List*) com Expansão Sequencial Dinâmica**.
* **Mecanismo:**
  1. O Bloco 0 contém o ponteiro `free_list_head`.
  2. Quando um bloco fica completamente vago (ex: por deleções em massa ou reorganização), os primeiros 4 bytes do bloco recebem o antigo `free_list_head`, e o Bloco 0 passa a apontar para ele (inserção na cabeça em $O(1)$).
  3. Ao solicitar um novo bloco:
     - Se `free_list_head != 0xFFFFFFFF`, retira-se o bloco da cabeça da lista, reaproveitando o espaço existente em $O(1)$.
     - Se a lista estiver vazia, estende-se o arquivo fisicamente no final (`block_id = total_blocks++`), mantendo a contiguidade física inicial.
* **Justificativas e Comparação com Opção Preterida (Free Space Bitmap):**
  * O custo em disco da *Free List* é **zero bytes extras**, pois utiliza o próprio corpo do bloco vazio que estaria ocioso.
  * Preteriu-se o *Bitmap de Blocos Livres* nesta camada para evitar a complexidade de alocar e encadear novos blocos de bitmap conforme o arquivo cresce, mantendo o Gerente de Arquivos enxuto e previsível.

---

### 1.5 Contadores de Leituras e Gravações Físicas (Sequencial vs. Seeks)
* **Decisão Adotada:** **Rastreamento de Estado da Cabeça de Leitura/Gravação com Matriz de Contadores Globais e Individuais por Arquivo**.
* **Regra de Detecção de Acesso Sequencial vs. Aleatório (*Seek*):**
  O Gerente de Arquivos monitora `last_accessed_block_id` por arquivo e no barramento global:
  * **Acesso Sequencial:** Ocorre quando o bloco solicitado é exatamente o sucessor do último bloco acessado no mesmo arquivo:
    $$\text{current\_block\_id} == \text{last\_accessed\_block\_id} + 1$$
    - Não gera *seek* ($\Delta_{\text{seek}} = 0$).
    - Incrementa `seq_reads` ou `seq_writes`.
  * **Acesso Aleatório (*Seek*):** Ocorre se $\text{current\_block\_id} \ne \text{last\_accessed\_block\_id} + 1$ ou se houver alternância entre arquivos diferentes:
    - Incrementa o contador de seeks (`seek_count++`).
    - Incrementa `random_reads` ou `random_writes`.
    - Registra a distância percorrida pela cabeça virtual: $|\text{current} - \text{last}|$.
* **Métricas Exportadas pelo Gerente de Arquivos:**
  1. `physical_reads_sequential` e `physical_reads_random`
  2. `physical_writes_sequential` e `physical_writes_random`
  3. `total_seeks` e `seek_distance_sum`
  4. `total_blocks_transferred` = `total_reads` + `total_writes`
  5. Método `reset_counters()` para isolamento de medições por bateria de teste.

---

## 2. Módulo: Gerente de Buffer Pool (Buffer Pool Manager)

Este módulo atua como o mediador estrito entre a memória volátil (frames) e a memória secundária (disco gerenciado pelo `FileManager`). Nenhuma camada superior acessa blocos de disco diretamente.

### 2.1 Número Total de Páginas e Proporção Blocos por Página
* **Decisão Adotada:** **1 bloco de disco por página de buffer pool (1:1)**; tamanho de frame de **4096 bytes** (4 KB).
* **Capacidade Padrão:** **$N = 100$ páginas** (400 KB gerenciados na configuração de referência).
* **Justificativas:**
  1. **Alinhamento Atômico:** Cada frame acomoda exatamente 1 bloco de disco, eliminando custos de fragmentação interna e overheads de paginação.
  2. **Escala de Avaliação de Cargas:** Com $N=100$, tabelas pequenas (10 a 50 blocos) cabem integralmente no buffer (simulando cache quente com taxa de acerto próxima a 100%), enquanto tabelas médias (150 a 300 blocos) e grandes (1.000 a 5.000 blocos) provocam paginação frequente e múltiplas passadas de I/O em algoritmos de junção, permitindo aferir os custos reais de I/O.

---

### 2.2 Repartição do Buffer Pool (Áreas Funcionais com Quotas Fixas Estritas)
* **Decisão Adotada:** **Repartição por Áreas Funcionais com Quotas Fixas e Rígidas** (sem modo compartilhado e sem realocação dinâmica de frames entre áreas).
* **Quotas Fixas Definidas (para $N = 100$ páginas):**
  1. **Área de Catálogo e Metadados (`CATALOG`):** $5\%$ ($5$ frames). Armazena o Bloco 0 das tabelas e esquemas.
  2. **Área de Índices (`INDEX`):** $25\%$ ($25$ frames). Armazena nós de Árvores B+ e baldes/diretórios de Hash.
  3. **Área de Dados Primários (`DATA`):** $50\%$ ($50$ frames). Armazena blocos de registros dos arquivos Heap e páginas de dados primárias.
  4. **Área de Trabalho Relacional (`WORK`):** $20\%$ ($20$ frames). Reservada exclusivamente para algoritmos de execução relacional ($B=20$ blocos dedicados para *Block Nested-Loop Join*, buffers de runs do *Sort-Merge Join* e partições do *Partition-Hash Join*).
* **Justificativas:**
  1. **Isolamento Total contra Poluição de Buffer (*Buffer Pollution*):** Impede que varreduras sequenciais completas em tabelas grandes expulsem páginas críticas de índices ou de catálogo.
  2. **Determinismo Analítico Estrito:** Fórmulas teóricas de custo (Elmasri & Navathe; Silberschatz) para junções assumem que a operação dispõe de exatamente $B$ blocos fixos na memória. A fixação estrita das quotas garante conformidade exata entre o modelo matemático e a execução medida.

---

### 2.3 Políticas de Substituição a serem Comparadas
* **Decisão Adotada:** **Apenas LRU (*Least Recently Used*) e MRU (*Most Recently Used*)**.
* **Mecanismos e Justificativas de Comparação:**
  1. **LRU (*Least Recently Used*):**
     - *Mecanismo:* Expulsa o frame cujo último acesso foi o mais distante no tempo (fila duplamente encadeada com mapeamento hash).
     - *Adequação:* Política padrão ótima para consultas com forte localidade temporal e buscas pontuais indexadas. Contudo, apresenta desempenho deficiente em varreduras repetidas em laço (*loop*).
  2. **MRU (*Most Recently Used*):**
     - *Mecanismo:* Expulsa o frame que acabou de ser referenciado mais recentemente.
     - *Adequação:* Política ideal para o laço interno de junções por *Block Nested-Loop*, pois o último bloco lido no final da varredura interna não será reutilizado até a próxima iteração do bloco externo.
* **Opções Preteridas:**
  * **FIFO:** Preterido por ignorar a recência e frequência de acesso, sofrendo de anomalias de paginação (Anomalia de Belady) sem trazer valor prático comparativo adicional frente a LRU.
  * **Clock:** Preterido na bancada por ser essencialmente uma aproximação de LRU; como nosso ambiente laboratorial é instrumentado em nível de aplicação, o LRU exato oferece maior fidelidade analítica.

---

### 2.4 Emprego de *Toss-Immediate*
* **Decisão Adotada:** **SIM, com suporte a flag explícita `toss_immediate=True` no método `unpin`**.
* **Funcionamento:** Quando uma varredura sequencial lê um bloco e sabe que ele não será reutilizado na mesma operação, ela invoca `unpin(block_id, toss_immediate=True)`. O frame é posicionado diretamente no início da fila de substituição, sendo o primeiro candidato a descarte no próximo *miss*.
* **Justificativa:** Reduz o tempo de retenção inútil de blocos de varredura única, preservando espaço para páginas de maior reutilização.

---

### 2.5 Fixação de Blocos (*Pinned Blocks*) e Prevenção de Defeitos
* **Decisão Adotada:** **Contador de Fixação Atômico (`pin_count`) associado a *Context Manager* (`with`)**.
* **Mecanismo:**
  1. Todo frame possui um `pin_count` inicializado em 0.
  2. `pin_page(block_id)`: incrementa `pin_count`. Frames com `pin_count > 0` são imunes a expulsão.
  3. `unpin_page(block_id, is_dirty)`: decrementa `pin_count` e, se `is_dirty=True`, liga o bit de modificação (`dirty = True`).
  4. **Proteção Estrutural contra Falhas (Edital 7.1):** Suporte nativo ao idioma Python:
     ```python
     with buffer_pool.pin_guard(block_id, is_dirty=...) as page:
         # manipulação do bloco
     # unpin garantido automaticamente, mesmo sob exceções
     ```
     Elimina riscos de vazamento de pins (*pinned blocks* órfãos).

---

### 2.6 Gravação Forçada (*Forced Output*)
* **Decisão Adotada:** **SIM, com métodos `force_page(block_id)` e `flush_all()`**.
* **Funcionamento:** Gravação síncrona imediata da página no arquivo em disco, independentemente do replacer. Mantém contador específico `forced_writes`.
* **Justificativa:** Garante persistência imediata do Bloco 0 (Cabeçalho de arquivo) na criação de esquemas, em checkpoints e ao término de cargas em massa (*bulk loading*).

---

### 2.7 Substituição com Prioridade / Retenção Preferencial
* **Decisão Adotada:** **SIM, Retenção Preferencial Hierárquica por Tipo de Página dentro de cada cota**.
* **Níveis de Retenção:**
  - Nível 3 (Máxima): Páginas de Catálogo e Cabeçalho (Bloco 0).
  - Nível 2 (Alta): Nós internos e raiz de Árvores B+ e diretórios de Hash.
  - Nível 1 (Média): Folhas de índices.
  - Nível 0 (Normal): Páginas de dados Heap e buffers de trabalho temporários.
* **Justificativa:** Garante que nós internos da árvore B+ não sejam descartados precocemente, mantendo a navegação do índice com custo físico próximo de zero após o aquecimento.

---

### 2.8 Leitura Antecipada (*Prefetching*) de Blocos
* **Decisão Adotada:** **SIM, com método `prefetch_blocks(file_id, start_block_id, count)`**.
* **Funcionamento:** Em varreduras sequenciais e no carregamento de chunks no *Block Nested-Loop Join*, o buffer pool requisita ao `FileManager` a leitura contígua de um lote de blocos (ex: 4 ou 8 blocos) de uma só vez.
* **Justificativa:** Converte múltiplos acessos com potenciais seeks em uma única transferência contígua no disco, permitindo demonstrar a redução de seeks no relatório final.

---

### 2.9 Métricas Auditadas do Buffer Pool
A cada operação executada, o Buffer Pool exporta:
1. `requests`: total de chamadas a `pin_page()`.
2. `hits`: requisições atendidas com página em memória.
3. `misses`: requisições que exigiram transferência física de disco.
4. `hit_ratio`: taxa de acerto ($\frac{\text{hits}}{\text{requests}} \times 100\%$).
5. `dirty_page_evictions`: gravações físicas disparadas por expulsão de páginas alteradas.
6. `forced_writes`: gravações físicas diretas síncronas.
7. `pinned_count_current`: gauge para auditoria de ausência de vazamento de pins.

---

## 3. Módulo: Organizações Primárias e Métodos de Acesso (Índices)

A arquitetura das organizações primárias e métodos de acesso segue estritamente a matriz de homologação da **Seção 4.2 do edital**, operando exclusivamente através do Gerente de Buffer Pool.

### 3.1 Matriz de Métodos de Acesso e Organizações a Implementar

| Organização Primária | Métodos de Acesso e Índices Homologados |
| :--- | :--- |
| **1. Heap (Arquivo Não Ordenado)** | • **1.1 Direto:** Varredura de arquivo (*File Scan*) para campo sem índice.<br>• **1.2 Secundário Hash (UNIQUE):** Índice hash secundário direto para campo chave ($\text{Chave} \to \text{RID}$).<br>• **1.3 Secundário Árvore B+ (NOT UNIQUE):** Árvore B+ sem repetição de chave nas folhas ($\text{Chave} \to [\text{RID}_1, \text{RID}_2, \dots]$). |
| **2. Árvore B+** | • **2.1 Direto (Índice Primário):** Acesso direto via índice primário ordenado (para campo UNIQUE ou NOT UNIQUE).<br>• **2.2 Secundário Clustering (NOT UNIQUE):** Árvore B+ sem repetição de chave nas folhas apontando para as tuplas da árvore primária. |
| **3. Hash Externo** | • **3.1 Direto (Primário UNIQUE):** Acesso direto via índice primário para campo UNIQUE.<br>• **3.2 Direto (Primário NOT UNIQUE):** Acesso direto via índice primário para campo não chave.<br>• **3.3 Secundário Clustering (NOT UNIQUE):** Árvore B+ sem repetição de chave nas folhas apontando para os registros do arquivo Hash. |

---

### 3.2 Layout de Registro e Estrutura da Slotted Page (Comum a Todas as Organizações)
* **Tamanho do Bloco:** 4096 bytes (4 KB).
* **Técnica de Blocagem:** **Não Estendida (*Unspanned*)** — nenhum registro cruza a fronteira física de um bloco. Todas as tuplas cabem integralmente no bloco de 4 KB.
* **Layout Binário da Slotted Page:**
  ```
  +------------------------------------------------------------------------------------+
  | Cabeçalho da Página (16 bytes):                                                    |
  | [page_type: uint16] [num_slots: uint16] [free_space_offset: uint16]                 |
  | [free_space_bytes: uint16] [next_block_id: uint32] [prev_block_id: uint32]         |
  +------------------------------------------------------------------------------------+
  | Array de Slots (cresce para a frente):                                             |
  | Slot 0: (offset: uint16, length: uint16)                                            |
  | Slot 1: (offset: uint16, length: uint16) ...                                       |
  +------------------------------------------------------------------------------------+
  |                       <--- ESPAÇO LIVRE CONTÍGUO --->                              |
  +------------------------------------------------------------------------------------+
  | Corpo de Registros (cresce do final do bloco para o meio):                         |
  | ... [Registro 1] [Registro 0]                                                      |
  +------------------------------------------------------------------------------------+
  ```
* **Identificador Universal de Tupla:** **$\text{RID} = (\text{block\_id}, \text{slot\_num})$**.
* **Formato Interno do Registro de Dados:**
  - `Null Bitmap` (1 a 2 bytes no cabeçalho do registro): 1 bit por campo anulável.
  - `Campos Fixos`: Alocados em sequência com deslocamento constante (`INT`: 4B, `FLOAT`: 8B, `CHAR(N)`: $N$B).
  - `Campos Variáveis (VARCHAR)`: Tabela de `(offset, length)` de 2 bytes cada no cabeçalho do registro, apontando para a área de bytes UTF-8 ao final do registro.
  - `Campos Multivalorados`: Serializados com prefixo de contagem `[count: uint16] [tam_1] [val_1] ...`.
  - `Campos Derivados`: Não armazenados fisicamente (computados em tempo de execução).
* **Tratamento de Deleção e Compactação:**
  - Deleção lógica com marcação de túmulo (*tombstone*): o slot correspondente recebe `offset = 0xFFFF`. O RID original permanece reservado, garantindo estabilidade aos ponteiros de índices secundários.
  - **Compactação (*Lazy Defragmentation*):** Executada sob demanda apenas quando uma nova tupla precisa de espaço e a soma dos espaços fragmentados no bloco comporta o registro, reorganizando os bytes contíguos do fim da página.

---

### 3.3 Organização 1: Arquivo Heap
* **Estrutura:** Arquivo de blocos de dados slotted pages não ordenados.
* **Gerenciamento de Espaço Livre:** Diretório de páginas no Bloco 0 / Bloco 1 mapeando categorias de espaço livre por bloco, viabilizando inserção rápida em $O(1)$ sem varrer blocos cheios.
* **Operações de Arquivo Implementadas:** `Open`, `Close`, `Reset`, `Scan`, `Find`, `FindNext`, `Read`, `Insert`, `Delete`, `Reorganize`.

---

### 3.4 Organização 2: Árvore B+ Primária e Secundária
* **Nó Interno (4 KB):** Fan-out $M \approx 170$ a $200$ entradas `[P_0, K_1, P_1, \dots, K_m, P_m]`.
* **Nó Folha Primário (4 KB):** Contém os próprios registros de dados completos (índice clusterizado), duplamente encadeado (`prev_leaf_block` e `next_leaf_block`) para varreduras sequenciais e por faixa com custo de navegação $O(1)$ entre folhas.
* **Nó Folha Secundário (NOT UNIQUE - sem repetição de chaves):**
  - Cada entrada consiste em: $\text{Chave} \to [\text{RID}_1, \text{RID}_2, \dots, \text{RID}_k]$ (*posting list* compacta).
  - Se a lista de RIDs exceder o espaço da folha, utiliza blocos de transbordamento de RIDs encadeados.
* **Invariantes e Algoritmos:**
  - Ocupação mínima estrita de 50% ($\lceil M/2 \rceil$).
  - Splits propagados recursivamente até a raiz.
  - Redistribuição e fusão (*borrow/merge*) em remoções que violem a ocupação mínima.
  - Suporte a Carga em Massa (*Bulk Loading* bottom-up) para inicialização limpa da base de testes.

---

### 3.5 Organização 3: Hash Primário e Secundário
* **Modelo:** Hashing Estático Baseado em Baldes (*Static Bucket Hashing*).
* **Tamanho do Balde:** 1 bloco físico de 4096 bytes.
* **Função Hash:** **MurmurHash3 (32-bit)** com distribuição avalanche uniforme:
  $$h(k) = \text{MurmurHash3}(k) \pmod{M_{\text{buckets}}}$$
* **Tratamento de Colisão:** Encadeamento de blocos de overflow (*Overflow Chaining*). Cada balde tem no cabeçalho o ponteiro `overflow_block_id` apontando para o próximo bloco alocado via `FileManager`.
* **Variantes:**
  - *Primário UNIQUE:* 1 tupla por chave primária dentro do balde.
  - *Primário NOT UNIQUE:* Múltiplas tuplas com a mesma chave agrupadas no mesmo balde e sua cadeia de overflow.
  - *Secundário UNIQUE (sobre Heap):* Baldes indexando pares `(Chave, RID)`.

---

## 4. Módulo: Mini Monitor de Execução (Primitivas Relacionais)

Biblioteca de operadores que recebem explicitamente o caminho de acesso a ser utilizado pelo experimento e operam respeitando estritamente os $B=20$ frames da área `WORK` do Buffer Pool.

### 4.1 Primitivas de Seleção
1. **Seleção Pontual por Igualdade (`=`) em Campo UNIQUE:**
   - Varredura de arquivo completa no Heap (Custo: $b$ transferências).
   - Busca em Hash Primário (Custo: $1$ a $1 + \text{overflow}$).
   - Busca em Árvore B+ Primária (Custo: $h$).
   - Busca via Índice Secundário Hash + Leitura Heap (Custo: $1 + 1 = 2$).
2. **Seleção por Faixa de Valores (`<`, `<=`, `>`, `>=`) em Campo NOT UNIQUE:**
   - Varredura de arquivo completa no Heap (Custo: $b$).
   - Busca em Árvore B+ Primária: navegação da raiz até a 1ª folha ($h$) + varredura sequencial das folhas encadeadas ($b_{\text{faixa}}$).
   - Busca via Índice Secundário Árvore B+: busca dos RIDs nas folhas + acessos aleatórios aos blocos do Heap para cada RID qualificado.
3. **Níveis de Seletividade Avaliados:**
   - Baixa seletividade: **1% dos registros** qualificados.
   - Alta seletividade: **50% dos registros** qualificados.

---

### 4.2 Primitivas de Junção (*Join*)
Implementação dos três algoritmos fundamentais operando estritamente dentro dos $B=20$ frames da partição `WORK`:

1. **Block Nested-Loop Join (BNLJ):**
   - A relação externa $R$ é carregada em blocos de $B - 2 = 18$ frames na memória.
   - A relação interna $S$ é percorrida bloco a bloco utilizando 1 frame de entrada.
   - 1 frame reservado para a saída do resultado.
   - Custo estimado: $b_R + \lceil b_R / (B - 2) \rceil \times b_S$.
2. **Sort-Merge Join (SMJ):**
   - **Fase 1 (Ordenação Externa de 2 Passadas):** Divide a relação em *runs* de $B$ blocos ordenados em memória e mescla os *runs* via algoritmo multi-way merge sort gravando runs temporários no buffer pool.
   - **Fase 2 (Mescla / Merge):** Percorre simultaneamente as duas relações ordenadas com 1 frame para cada relação.
   - Custo estimado: $3(b_R + b_S)$.
3. **Partition-Hash Join (PHJ):**
   - **Fase 1 (Particionamento):** Aplica função hash $h_1$ dividindo $R$ e $S$ em $B - 1 = 19$ partições gravadas em blocos temporários gerenciados no buffer pool.
   - **Fase 2 (Junção das Partições):** Para cada par de partições $(R_i, S_i)$, carrega a menor na área de trabalho, cria hash table in-memory com $h_2$ e sonda com a maior.
   - Custo estimado: $3(b_R + b_S)$.

### 4.3 Combinações de Tamanho das Relações
- **Menor $\times$ Menor:** Ambas as tabelas cabem nos frames de trabalho do buffer.
- **Menor $\times$ Maior:** A menor cabe nos frames de trabalho, mas a maior não (avaliação de relação interna vs. externa ótima).
- **Maior $\times$ Maior:** Nenhuma das duas cabe no buffer de trabalho (forçando paginação em disco estrita).

---

## 5. Módulo: Bancada de Testes, Benchmark e Orquestrador

### 5.1 Base de Dados Universitária (UFRJ)
* **Tabela Pequena:** `Curso` (10 a 20 registros, $\le 2$ blocos).
* **Tabelas Médias:** `Disciplina` (50 a 100 registros) e `Turma` (200 a 500 registros).
* **Tabelas Grandes:** `Aluno` (5.000 a 20.000 registros, $\approx 200$ a 800 blocos) e `Inscricao_Aluno_Turma` (25.000 a 100.000 registros, $\approx 1.000$ a 4.000 blocos).
* **Tabela 1:1 para Equijoin UNIQUE:** `Detalhes_do_Aluno` com chave primária compartilhada (`CPF`).

### 5.2 Metodologia de Execução de Experimentos
1. **Isolamento de Fatores:** Cada bateria varia exatamente um fator por vez (ex: tipo de organização, tamanho do buffer, algoritmo de join, política LRU vs MRU, seletividade).
2. **Critério de Estabilidade e Repetições:** Cada medição é executada no mínimo **3 vezes**. O experimento só é validado se o coeficiente de variação entre repetições for inferior a **5%**.
3. **Exportação de Dados:** Registro tabular automático em arquivos `.csv` e geração de gráficos comparativos.
4. **Validação Cruzada de Integridade:** As mesmas consultas são executadas em paralelo sobre uma base SQLite de espelho contendo exatamente os mesmos dados sintéticos gerados, garantindo que nenhum registro foi omitido ou duplicado incorretamente.

---

## 6. Fórmulas de Custo Analítico vs. Custo Medido

Para cada operação relacional avaliada, a bancada calculará simultaneamente o custo teórico da literatura e o custo físico real medido:

| Operação e Configuração | Fórmula de Custo Teórico (I/Os em Blocos) | Observações para Confronto no Relatório |
| :--- | :--- | :--- |
| **Varredura Completa (Heap Scan)** | $C = b$ | Leituras sequenciais; se prefetch ativo, $\text{seeks} \to 1$. |
| **Busca Pontual Hash Primário** | $C = 1 + \text{avg\_overflow}$ | 1 I/O para bucket primário + saltos na cadeia de transbordamento. |
| **Busca Pontual B+ Primário** | $C = h$ | Altura da árvore até a folha. Nós internos quentes reduzem I/O real no buffer. |
| **Busca Pontual Hash Secundário** | $C = (1 + \text{overflow}_{\text{idx}}) + 1_{\text{heap}}$ | 1 leitura no balde de índice + 1 leitura aleatória do RID no Heap. |
| **Busca por Faixa B+ Primário** | $C = h + \lceil \text{sel} \times b \rceil$ | Navegação até a 1ª folha + varredura contígua entre folhas encadeadas. |
| **Busca por Faixa B+ Secundário** | $C = h + b_{\text{folhas\_idx}} + \text{sel} \times N_{\text{tuplas}}$ | Custo de I/O aleatório explosivo no Heap para alta seletividade (50%). |
| **BNLJ (Block Nested-Loop)** | $C = b_R + \lceil b_R / (B - 2) \rceil \times b_S$ | $B=20$. Tabela menor obrigatoriamente como externa ($R$). |
| **Sort-Merge Join (SMJ)** | $C = 3(b_R + b_S)$ | $2(b_R + b_S)$ para ordenar em 2 passadas + $(b_R + b_S)$ para mesclar. |
| **Partition-Hash Join (PHJ)** | $C = 3(b_R + b_S)$ | $2(b_R + b_S)$ para particionar + $(b_R + b_S)$ para sondar as partições. |

---

## 7. Entregáveis e Prevenção de Defeitos de IA (Edital Seção 7)

### 7.1 Checklist de Auditoria e Pontos de Atenção (Item 7.1 do Edital)
- [x] **Zero Acesso Fora do Buffer Pool:** Todo I/O de blocos passa exclusivamente pelo `BufferPoolManager`. Nenhuma estrutura acessa arquivos físicos diretamente.
- [x] **Proteção de Pins:** Utilização de `pin_guard` (*context manager*) prevenindo vazamentos de pins e frames órfãos.
- [x] **Integridade do Dirty Bit:** Toda modificação em página seta o dirty bit no unpin ou força gravação antes do descarte.
- [x] **Busca Binária em Nível de Bloco:** A busca interna em slotted page opera estritamente nos slots e chaves da página em memória, sem gerar leituras artificiais de blocos adicionais.
- [x] **Árvores B+ Rigorosas:** Ocupação mínima de 50%, encadeamento bidirecional de folhas e propagação correta de divisões até a raiz.
- [x] **Cadeias de Overflow no Hash:** Verificação de que buscas e remoções percorrem integralmente a lista encadeada de transbordamento.
- [x] **Orçamento de Memória em Junções:** Nenhuma operação relacional utiliza estruturas nativas da linguagem (listas Python gigantes) fora da quota de 20 frames da área `WORK`.
- [x] **Limites de Slotted Page:** Checagem rigorosa de offsets para evitar acessos fora do limite físico de 4096 bytes do bloco.

### 7.2 Arquivos de Entrega Exigidos
1. **Código-fonte Completo da Bancada:** Organizado nas 4 camadas estritas (`storage`, `buffer`, `catalog`, `record`, `execution`, `benchmark`).
2. **`POST_MORTEM.md` na Raiz:** Documento de Análise Crítica e Post-Mortem de IA contendo log dos prompts, code review do código ingênuo vs refatorado, justificativas de correção e testes de estresse.
3. **`relatorio_tecnico.md` / PDF:** Relatório com todas as escolhas justificadas contra as opções preteridas, gráficos e tabelas com Custo Estimado $\times$ Custo Medido, e recomendações práticas de configuração de SBD conforme a carga de trabalho.


