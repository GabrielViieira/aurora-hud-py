# aurora-hud-py

## Visão local

A Aurora pode encaminhar chamadas com imagens para um modelo visual local servido pelo Ollama, mantendo o provedor principal para conversa. Consulte [o guia de visão local](docs/vision-local-ollama.md) para instalar o modelo, configurar o `.env` e executar `py tools/test_local_vision.py` antes de testar a integração completa.

## Chave do FreeLLM

A chave é resolvida **a cada requisição**, entre `os.environ`, o `.env` e o valor lido no início:

- ambiente do processo (`FREELLMAPI_API_KEY` ou `AURORA_LLM_FALLBACK_KEY_ENV`);
- `.env` relido a cada chamada (permite colar uma chave nova sem reiniciar o HUD);
- valor capturado em `Settings.load` como último recurso.

Se o gateway responder `401`/`403` e a chave tiver mudado, o cliente recarrega e repete a requisição uma vez. Sem chave, a falha é `auth` com a mensagem indicando qual variável definir — e não uma tentativa HTTP com `Bearer` vazio.

## Visão local fora do ar

Se o endpoint de visão local recusar conexão (Ollama parado, porta fechada), o agente remove as imagens do contexto e continua apenas com os controles de UI Automation, orientando o modelo a usar `capturar_tela(somente_acessibilidade=true)`. O turno não é mais abortado por causa disso.

## Painel da HUD

Clique no botão ⚙ ou use **Ctrl+H** para abrir o painel da Aurora. Ele reúne o histórico local, o diagnóstico de conectividade e preferências da interface. No histórico é possível consultar, excluir uma sessão ou limpar as conversas salvas; essa limpeza não apaga memórias semânticas nem a auditoria.

As preferências da HUD ficam em `data/hud_preferences.json`, sem credenciais. Elas controlam a fala, o streaming, a abertura automática do painel para respostas longas, o volume e a velocidade de SAPI. O volume é aplicado ao áudio sintetizado. **Escape** continua cancelando a tarefa/fala; por voz, diga **“Aurora, para”** ou **“Aurora, pare de falar”**.


## HUD sobre jogos

A HUD reafirma a prioridade de janela no Windows sem ativar a Aurora nem roubar o foco do jogo. Se ela ainda ficar atrás do Unturned, confira o modo de exibição do jogo e prefira **Windowed Borderless / Janela sem bordas**. A prioridade de janela não consegue garantir que uma janela comum apareça sobre jogos que estejam usando um modo exclusivo de tela cheia.
