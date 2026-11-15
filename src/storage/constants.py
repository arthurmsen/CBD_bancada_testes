"""
Constantes físicas e parâmetros arquiteturais do Gerente de Arquivos.
Conforme especificado em PLANO_IMPLEMENTACAO.md (Módulo 1).
"""

# Tamanho físico do bloco em bytes (4 KB)
BLOCK_SIZE: int = 4096

# Assinatura de validação (magic number) do cabeçalho de arquivo (0xCBD2026)
MAGIC_NUMBER: int = 0x0CBD2026

# Sentinela de identificador de bloco inexistente / nulo (uint32 max)
NO_BLOCK: int = 0xFFFFFFFF

# Tipos de arquivos suportados
FILE_TYPE_HEAP: int = 1   # Arquivo Heap de registros não ordenados
FILE_TYPE_BTREE: int = 2  # Arquivo de Índice ou Tabela Árvore B+
FILE_TYPE_HASH: int = 3   # Arquivo de Diretório/Baldes de Hash Estático

# Mapa textual de tipos para depuração e exibição
FILE_TYPE_NAMES = {
    FILE_TYPE_HEAP: "HEAP",
    FILE_TYPE_BTREE: "BTREE",
    FILE_TYPE_HASH: "HASH",
}
