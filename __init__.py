"""Aurora HUD — núcleo do assistente pessoal.

Fase 1: configuração, cliente FreeLLM, agente de texto com ferramentas,
memória SQLite, auditoria e voz japonesa opcional.

O pacote é deliberadamente sem dependências externas para rodar no Python
instalado nesta máquina (3.14). Camadas que precisem de bibliotecas extras
(por exemplo STT com faster-whisper) entram como opcionais nas fases seguintes.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
