# Laboratório TTS do Raphael

Este fluxo prepara um conjunto experimental japonês para avaliar se um TTS dedicado consegue reproduzir a voz usada pela Aurora sem executar Edge TTS + RVC a cada resposta.

## O que o script faz

1. Usa Edge TTS (ja-JP-NanamiNeural) para sintetizar cada frase de phrases_ja.txt.
2. Converte o WAV da Nanami com o Raphael_Sensei.pth do Applio e o index correspondente, caso exista.
3. Salva os WAVs de entrada e convertidos em uma pasta fora do repositório.
4. Gera manifest.list no formato aceito pelo GPT-SoVITS:

**caminho_do_audio|speaker|ja|texto**

Isso prepara os dados; não instala nem treina o GPT-SoVITS automaticamente. Depois de ouvir e aprovar os WAVs, importe/revise o manifesto na interface de treinamento do GPT-SoVITS e faça o fine-tuning. O suporte e os passos podem variar conforme a versão que você instalar.

## Executar no Windows

No CMD, a partir da pasta do repositório Aurora:

```bat
git pull origin main
py -m py_compile tools\voice_tts_lab\build_raphael_dataset.py
py tools\voice_tts_lab\build_raphael_dataset.py --limit 8
```

O teste inicial gera apenas oito frases. Ouça os WAVs em:

**%USERPROFILE%\Documents\AuroraVoiceLab\Raphael_JA\converted_raphael**

Se a voz estiver consistente e sem artefatos graves, gere as frases restantes:

```bat
py tools\voice_tts_lab\build_raphael_dataset.py
```

O script mantém WAVs convertidos válidos já existentes; use --force para regenerá-los.

## Caminhos e opções

O padrão do Applio segue a instalação usada pela Aurora: **C:\Users\Administrator\Downloads\Applio-main**. Se a sua instalação estiver em outro local:

```bat
py tools\voice_tts_lab\build_raphael_dataset.py --applio-dir "D:\caminho\Applio"
```

Também é possível indicar o modelo e o index explicitamente:

```bat
py tools\voice_tts_lab\build_raphael_dataset.py --applio-dir "D:\Applio" --pth "D:\Applio\logs\Raphael_Sensei.pth" --index "D:\Applio\logs\Raphael_Sensei\added.index"
```

- Requer conexão com a internet para a síntese Edge TTS.
- A conversão RVC é executada localmente e pode levar algum tempo por frase.
- O index é opcional; o script procura os caminhos comuns usados pela Aurora.
- A saída padrão é %USERPROFILE%\Documents\AuroraVoiceLab\Raphael_JA, não a pasta versionada do projeto.
- Os áudios gerados são exemplos sintéticos do pipeline atual; o TTS novo pode herdar artefatos e prosódia da Nanami/RVC. É essencial ouvir e comparar antes de treinar.
- Use apenas modelos e gravações cujo uso você tem autorização para fazer.

Referência: https://github.com/RVC-Boss/GPT-SoVITS


## Integração do TTS treinado na Aurora

A interface `ui/launch.py` agora tenta primeiro a API local do GPT-SoVITS, usando os pesos treinados:

- GPT: `GPT_weights_v2Pro/xxx-e15.ckpt`
- SoVITS: `SoVITS_weights_v2Pro/xxx_e8_s816.pth`
- Áudio de referência: `logs/xxx/5-wav32k/0001_raphael.wav`
- Texto de referência: `こんにちは。ラファエルです。`

Se a API não estiver ativa, a Aurora tenta iniciá-la automaticamente usando `runtime/python.exe` e `api.py`. Os modelos permanecem carregados durante a sessão; as falas seguintes enviam somente o texto para sintetizar. Se a integração falhar, a Aurora mantém os fallbacks RVC, Voicevox e SAPI.

### Primeiro teste no Windows

Antes de iniciar a Aurora, feche a instância secundária de **Inferência TTS WebUI** do GPT-SoVITS para liberar VRAM; não é necessário excluir pesos nem logs. Depois:

```bat
cd /d C:\Tools\OpenCode\aurora-hud\app\aurora
git pull origin main
py -m py_compile ui\launch.py
py ui\launch.py
```

Na primeira fala, a Aurora inicia a API e carrega os modelos. Esse primeiro carregamento pode demorar; as falas seguintes reutilizam o servidor. O log da API fica em:

`%LOCALAPPDATA%\Aurora\logs\gpt_sovits_api.log`

### Configuração opcional

As opções podem ser substituídas por variáveis de ambiente:

- `AURORA_GPT_SOVITS_DIR`: pasta de instalação do GPT-SoVITS.
- `AURORA_GPT_SOVITS_API_PORT`: porta da API (padrão 9880).
- `AURORA_GPT_SOVITS_GPT_MODEL`: caminho do checkpoint GPT.
- `AURORA_GPT_SOVITS_SOVITS_MODEL`: caminho do peso SoVITS.
- `AURORA_GPT_SOVITS_REFERENCE`: WAV de referência.
- `AURORA_GPT_SOVITS_ENABLED=0`: desativa o caminho GPT-SoVITS e usa os fallbacks.

A integração é local; a tradução de respostas para japonês continua usando a configuração de tradução já existente na Aurora.
