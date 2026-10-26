# Testar visão local com Ollama

Este modo encaminha **somente as chamadas que contêm imagens** para um endpoint local compatível com a API OpenAI. As mensagens sem imagem continuam usando o provedor principal configurado na Aurora.

## 1. Instalar e baixar um modelo visual

Instale o Ollama para Windows: https://ollama.com/download

Abra um PowerShell e execute:

```powershell
ollama --version
ollama pull qwen2.5vl:3b
ollama run qwen2.5vl:3b
```

No prompt do modelo, experimente perguntar algo simples e depois saia com `/bye`. O download do modelo precisa de internet, mas depois de baixado a inferência local pode ser executada sem enviar a imagem a um serviço externo.

Confira se o modelo está instalado:

```powershell
(Invoke-RestMethod "http://127.0.0.1:11434/api/tags").models | Select-Object -ExpandProperty name
```

Deve aparecer `qwen2.5vl:3b` (ou uma etiqueta equivalente retornada pelo Ollama).

## 2. Configurar o .env da Aurora

No arquivo `.env` que o runtime da Aurora carrega, mantenha as demais configurações, incluindo a chave atual do FreeLLM, e ajuste/adicone estas linhas:

```dotenv
AURORA_LLM_VISION_BASE_URL=http://127.0.0.1:11434/v1
AURORA_LLM_VISION_MODEL=qwen2.5vl:3b
AURORA_LLM_VISION_TIMEOUT_S=120
AURORA_LLM_VISION_MAX_RETRIES=0
```

Se o arquivo já tiver `AURORA_LLM_VISION_MODEL=gemini-3.8-flash`, **substitua o valor existente** em vez de criar uma segunda linha com a mesma variável.

## 3. Reiniciar e validar

Feche todas as janelas da Aurora e inicie de novo:

```powershell
py -u ui/launch.py
```

Peça uma tarefa de desktop que precise de captura de tela, por exemplo, para identificar o aplicativo aberto. Após a Aurora obter uma imagem, procure no log uma linha como:

```text
modelo=qwen2.5vl:3b; imagem=True; provedor=visao-local
```

Essa linha confirma que a mensagem com imagem foi encaminhada ao endpoint local. A primeira chamada textual da tarefa ainda pode usar o provedor principal para decidir qual ferramenta executar; esta mudança localiza o processamento das chamadas visuais, não torna automaticamente todo o agente offline.

## Limitações do primeiro teste

- O modelo precisa estar baixado e o serviço Ollama em execução.
- A resposta visual pode ser mais lenta na primeira inferência, enquanto o modelo carrega para a memória. A Aurora permite até 150 segundos para uma chamada de visão local.
- O endpoint está configurado para o modelo Qwen. Não mantenha o nome de um modelo de nuvem nessa variável.
- Este teste valida o caminho de visão; a capacidade do modelo de emitir chamadas de ferramenta de maneira confiável precisa ser verificada na tarefa real.
