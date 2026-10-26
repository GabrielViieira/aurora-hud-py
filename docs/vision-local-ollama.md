# Testar visão local com Ollama

Este modo encaminha **somente as chamadas que contêm imagens** para um endpoint local compatível com a API OpenAI. As mensagens sem imagem continuam usando o provedor principal configurado na Aurora.

O cliente local recebe a imagem **sem definições de ferramentas (`tools`)**, pois alguns modelos Ollama retornam HTTP 400 quando elas são enviadas. A resposta visual é tratada como uma observação, os payloads de imagem são removidos do histórico e essa observação volta ao provedor principal, que continua o ciclo de decisão e pode chamar as ferramentas de mouse/teclado.
Para respeitar o contexto padrão de 4096 tokens de alguns modelos Ollama, a chamada visual recebe somente uma instrução curta e a imagem, sem prompt de sistema nem histórico antigo. A saída visual é limitada a 300 tokens e o histórico completo permanece disponível para o provedor principal.

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

No arquivo `.env` que o runtime da Aurora carrega, mantenha as demais configurações, incluindo a chave atual do FreeLLM, e ajuste/adicione estas linhas:

```dotenv
AURORA_LLM_VISION_BASE_URL=http://127.0.0.1:11434/v1
AURORA_LLM_VISION_MODEL=qwen2.5vl:3b
AURORA_LLM_VISION_TIMEOUT_S=120
AURORA_LLM_VISION_MAX_RETRIES=0
```

Se o arquivo já tiver `AURORA_LLM_VISION_MODEL=gemini-3.8-flash`, **substitua o valor existente** em vez de criar uma segunda linha com a mesma variável. Variáveis definidas no ambiente do Windows têm prioridade sobre o `.env`. Se um teste ainda mostrar `gemini-3.8-flash`, confira no PowerShell:

```powershell
Get-Item Env:AURORA_LLM_VISION_MODEL -ErrorAction SilentlyContinue
```

Se houver um valor antigo, remova-o da sessão atual antes de iniciar o teste/aplicativo:

```powershell
Remove-Item Env:AURORA_LLM_VISION_MODEL -ErrorAction SilentlyContinue
```

A ferramenta `capturar_tela` usa 640 px por padrão para reduzir latência e tokens. O agente limita a imagem enviada ao modelo local a no máximo 640 px; se o backend responder com erro de limite de tokens/contexto, tenta novamente com imagens de até 448 px e depois 320 px. Se a análise visual local continuar indisponível e a captura tiver controles úteis de UI Automation, o agente remove a imagem e tenta continuar apenas com os dados estruturados, sem inventar o que estava visível. Para tarefas com campos e botões convencionais, prefira `capturar_tela(somente_acessibilidade=true)` antes de gastar tempo com visão. Se texto pequeno ficar difícil de ler, complemente com `interface_acessivel.elementos` antes de aumentar a resolução. O texto base64 da imagem também é excluído do histórico textual enviado ao provedor principal.

## 3. Testar a visão local isoladamente

Antes de abrir a Aurora, execute no diretório do projeto:

```powershell
py tools/test_local_vision.py
```

O script captura a tela, reduz a imagem e a envia diretamente ao endereço local configurado para obter uma descrição. Ele não usa o FreeLLM nem envia a captura à nuvem; por isso, é o teste mais simples para separar problemas do Ollama/modelo dos problemas do agente. Feche antes qualquer janela que não queira incluir na captura.

Se o Ollama responder com uma descrição coerente, o modelo visual local está funcionando.

## 4. Reiniciar e validar a integração

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
- A resposta visual pode ser mais lenta na primeira inferência, enquanto o modelo carrega para a memória. O exemplo configura 120 segundos para a chamada HTTP; o prazo externo da Aurora acrescenta uma pequena margem, com teto de segurança de 150 segundos.
- O endpoint está configurado para o modelo Qwen. Não mantenha o nome de um modelo de nuvem nessa variável.
- Este teste valida o caminho de visão; a capacidade do modelo de emitir chamadas de ferramenta de maneira confiável precisa ser verificada na tarefa real.
