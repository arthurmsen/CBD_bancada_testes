"""
Constantes e enumerações do Gerente de Buffer Pool.
Conforme especificado em PLANO_IMPLEMENTACAO.md (Módulo 2).
"""
from enum import Enum


class PageType(str, Enum):
    """Tipos funcionais de página no Buffer Pool para particionamento de cotas."""
    CATALOG = "CATALOG"  # Bloco 0, metadados de catálogo e esquemas
    INDEX = "INDEX"      # Nós de Árvore B+ e baldes/diretórios de Hash
    DATA = "DATA"        # Páginas de dados de arquivos Heap e tuplas
    WORK = "WORK"        # Área de trabalho reservada para algoritmos de junção e ordenação


class ReplacerType(str, Enum):
    """Políticas de substituição homologadas para a bancada."""
    LRU = "LRU"  # Least Recently Used
    MRU = "MRU"  # Most Recently Used


# Níveis de retenção preferencial hierárquica por tipo de página
PRIORITY_NORMAL: int = 0          # Páginas de dados Heap e trabalho temporário
PRIORITY_INDEX_LEAF: int = 1      # Páginas folha de índices
PRIORITY_INDEX_INTERNAL: int = 2  # Nós internos e raiz de Árvore B+, cabeçalho de Hash
PRIORITY_CATALOG: int = 3         # Bloco 0 de relações e descritores do dicionário

# Capacidade padrão da bancada de testes (100 páginas de 4096 bytes = 400 KB)
DEFAULT_BUFFER_POOL_SIZE: int = 100

# Proporções de cotas fixas estritas
QUOTA_CATALOG_PERCENT: float = 0.05  # 5%
QUOTA_INDEX_PERCENT: float = 0.25    # 25%
QUOTA_DATA_PERCENT: float = 0.50     # 50%
QUOTA_WORK_PERCENT: float = 0.20     # 20%
