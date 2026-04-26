# Análise Completa: Sistema STT (Speech-to-Text)

## Visão Geral

Este projeto contém 3 arquivos Python para transcrição de áudio em tempo real usando a API do ChatGPT. O sistema captura áudio do microfone e transcreve automaticamente usando a API `/backend-api/transcribe` do ChatGPT.

**Arquivos analisados:**
1. `stt-config-ok.py` - Versão avançada com fila de transcrição paralela
2. `stt_auto_detect_FUNCIONAL.py` - Detector automático para falas curtas
3. `stt_auto_detect_v7.py` - **Idêntico ao FUNCIONAL** (mesmo código)

---

## 1. stt-config-ok.py (Versão Avançada)

### O que faz
Sistema de transcrição contínua com arquitetura de fila paralela:
- Microfone **sempre ouvindo** (não para entre falas)
- Detecta fim de fala por silêncio (padrão 800ms)
- Fecha turnos e coloca numa **fila de transcrição**
- **Worker thread** transcreve em paralelo sem pausar captura
- Imprime e salva em `transcricao.txt` na ordem correta

### Como funciona

#### Arquitetura
```
Microfone --> audio_callback --> audio_queue
                                    |
                                    v
                              run_capture()
                                /      \
                    (idle) pre_roll    recording frames
                                        |
                                    turno fechado
                                        |
                                        v
                                   turn_queue
                                        |
                                        v
                                transcription_worker (thread)
                                        |
                                        v
                                   TranscriptStore
                                        |
                                        v
                                   transcricao.txt
```

#### Componentes principais

**Detecção de fala:**
- `threshold` (padrão 260): Volume para iniciar gravação
- `continue_threshold` (padrão 220): Volume para continuar gravando
- `silence_ms` (padrão 800ms): Silêncio para fechar turno
- `start_required_ms` (80ms): Tempo acima do threshold para iniciar
- `continue_required_ms` (90ms): Frames consecutivos para confirmar fala (anti-clique)
- `min_speech_ms` (250ms): Tempo mínimo real de voz no turno

**Filtros anti-ruído:**
- `discard_cooldown_ms` (250ms): Após descartar ruído curto, ignora novos gatilhos
- `pre_roll_ms` (600ms): Mantém áudio antes do gatilho para não cortar início

**Modo adaptativo (opcional):**
- `--adaptive`: Ajusta threshold baseado no ruído ambiente
- Limitado por `--threshold-cap` (padrão 420)

#### Fluxo de transcrição
1. Áudio capturado via `sounddevice` (16kHz, mono, int16)
2. Detecta início de fala comparando RMS com threshold
3. Acumula frames em `frames[]` durante gravação
4. Fecha turno por silêncio ou `max_turn_seconds`
5. Cria objeto `Turn` com metadados e coloca na `turn_queue`
6. Worker thread: salva WAV temporário → envia para API → limpa texto → salva

### Como usar

```bash
# Instalar dependências
pip install sounddevice numpy curl_cffi

# Primeira execução (vai pedir headers do DevTools)
python stt-config-ok.py

# Com parâmetros ajustados
python stt-config-ok.py --threshold 360 --silence-ms 1000

# Configuração recomendada (validada)
python stt-config-ok.py --threshold 260 --continue-threshold 220 --start-required-ms 80 --continue-required-ms 90 --min-speech-ms 250 --silence-ms 800 --discard-cooldown-ms 250
```

### Parâmetros CLI
| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `--threshold` | 260 | Volume para iniciar fala |
| `--continue-threshold` | 220 | Volume para continuar fala |
| `--silence-ms` | 800 | Silêncio para fechar turno |
| `--pre-roll-ms` | 600 | Áudio antes do gatilho |
| `--start-required-ms` | 80 | Tempo mínimo para iniciar |
| `--min-ms` | 700 | Duração mínima do turno |
| `--min-speech-ms` | 250 | Tempo mínimo real de voz |
| `--max-turn-seconds` | 0 (desligado) | Fecha por tempo máximo |
| `--continue-required-ms` | 90 | Frames consecutivos para confirmar fala |
| `--discard-cooldown-ms` | 250 | Cooldown após descartar ruído |
| `--adaptive` | False | Usa threshold adaptativo |
| `--meter` | True | Mostra medidor de volume |

---

## 2. stt_auto_detect_v7.py / stt_auto_detect_FUNCIONAL.py

### O que faz
Detector automático otimizado para **falas curtas**:
- Detecta frases como "oi", "sim", "não", "tá"
- Envia automaticamente a cada **5 segundos** (para quem fala sem parar)
- Overlap de 1200ms entre chunks para não perder contexto
- Remove repetições entre segmentos (dedup)

### Como funciona

#### Diferenças do stt-config-ok.py
- **Sem fila paralela**: Transcreve em thread simples por segmento
- **Envio por tempo**: Força envio a cada 5s mesmo sem silêncio
- **Overlap**: Mantém 1200ms do final no próximo chunk
- **Dedup inteligente**: `remover_repeticao()` evita repetir texto já transcrito
- **Filtro de lixo**: Ignora alucinações da API como "bye.", "here we go"

#### Fluxo
1. Aguarda fala com `START_REQUIRED_MS` (180ms acima de 260)
2. Grava até silêncio (700ms) ou 5 segundos
3. Valida: duração mínima 500ms, pico > 350, média > 70
4. Thread transcreve e salva com dedup
5. Se foi por tempo: mantém overlap e continua gravando
6. Se foi por silêncio: reseta e aguarda nova fala

### Como usar

```bash
pip install sounddevice numpy curl_cffi
python stt_auto_detect_v7.py
# ou
python stt_auto_detect_FUNCIONAL.py
```

### Configurações principais
| Constante | Valor | Descrição |
|-----------|-------|-----------|
| `THRESHOLD` | 260 | Volume para detectar fala |
| `START_REQUIRED_MS` | 180 | Tempo para iniciar |
| `SILENCE_END_MS` | 700 | Silêncio para fechar |
| `PRE_ROLL_MS` | 500 | Áudio antes da fala |
| `SEND_EVERY_SECONDS` | 5 | Força envio periódico |
| `OVERLAP_MS` | 1200 | Sobreposição entre chunks |
| `MIN_SEGMENT_MS` | 500 | Duração mínima para enviar |
| `MIN_SPEECH_MS` | 180 | Tempo mínimo de voz |
| `MIN_PEAK_TO_SEND` | 350 | Pico mínimo de volume |
| `MIN_AVG_TO_SEND` | 70 | Média mínima de volume |

---

## 3. Comparação entre as Versões

| Característica | stt-config-ok.py | stt_auto_detect_v7.py |
|----------------|------------------|----------------------|
| Fila paralela | ✅ Sim | ❌ Não |
| Envio periódico | Opcional (max_turn) | ✅ Automático (5s) |
| Overlap | ❌ Não | ✅ 1200ms |
| Dedup de texto | ❌ Não | ✅ Sim |
| Filtro de lixo | ❌ Não | ✅ Sim |
| Threshold adaptativo | ✅ Sim | ❌ Fixo |
| Anti-clique | ✅ continue_required | ❌ Não |
| Cooldown pós-ruído | ✅ Sim | ❌ Não |
| CLI args | ✅ Extenso | ❌ Fixo no código |
| Ideal para | Frases longas, contínuo | Falas curtas, intermitente |

---

## 4. O que pode ser feito / Melhorias Possíveis

### Integração e Automação
1. **Hotkey para iniciar/parar**: Adicionar atalho global (ex: Ctrl+Shift+R)
2. **Integração com clipboard**: Auto-copiar transcrição para Ctrl+V
3. **Modo dictação**: Inserir texto direto na janela ativa (pyautogui)
4. **WebSocket**: Enviar transcrições em tempo real para outro sistema

### Melhorias Técnicas
1. **VAD (Voice Activity Detection) real**: Usar Silero VAD ou WebRTC VAD em vez de threshold RMS simples
2. **Múltiplos modelos**: Suporte para Whisper local (faster-whisper) como fallback
3. **Buffer circular**: Evitar perda de áudio se a fila encher
4. **Reconexão automática**: Se API falhar, tentar novamente com backoff
5. **Múltiplos idiomas**: Alternar dinamicamente entre pt-BR, en-US, etc.

### Interface e UX
1. **GUI**: Interface gráfica com Tkinter/PyQt para controle visual
2. **Notificações**: Toast notification quando transcrição completa
3. **Waveform visual**: Mostrar forma de onda em tempo real
4. **Histórico**: Navegar por transcrições anteriores

### Funcionalidades Avançadas
1. **Punctuation restoration**: Adicionar pontuação pós-transcrição (modelos BERT)
2. **Speaker diarization**: Identificar quem está falando (se múltiplas vozes)
3. **Comandos de voz**: Detectar "pare", "salve", "limpe" para controlar o sistema
4. **Export formats**: Salvar em SRT, VTT para legendas de vídeo
5. **Streaming mode**: Enviar áudio em chunks menores para transcrição incremental

### Robustez
1. **Refresh token automático**: Detectar 401 e pedir novo token
2. **Backup de transcrições**: Versionar `transcricao.txt`
3. **Log estruturado**: Salvar JSON com metadados (timestamp, duração, confiança)
4. **Testes automatizados**: Testar detecção com arquivos de áudio conhecidos

---

## 5. Configuração de Token (Comum a todos)

### Primeira execução
1. Acesse https://chatgpt.com
2. Abra DevTools (F12) → Network
3. Fale algo para criar uma transcrição
4. Procure requisição `transcribe`
5. Clique com botão direito → Copy → Copy request headers
6. Cole no terminal quando solicitado
7. Digite `FIM` para finalizar

### Formato do token.txt
```json
{
  "authorization": "Bearer eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0...",
  "cookie": "_cfuvid=...; __Host-nextauth...",
  "chatgpt-account-id": "uuid-aqui",
  "oai-device-id": "uuid-aqui",
  "oai-language": "pt-BR"
}
```

---

## 6. Dependências

```bash
pip install sounddevice numpy curl_cffi
```

- **sounddevice**: Captura áudio do microfone
- **numpy**: (usado indiretamente pelo sounddevice)
- **curl_cffi**: Requisições HTTP com impersonação de Chrome (bypass Cloudflare)

---

## 7. Estrutura do Projeto

```
stt/
├── stt-config-ok.py              # Versão avançada com fila
├── stt_auto_detect_v7.py         # Detector para falas curtas
├── stt_auto_detect_FUNCIONAL.py  # Cópia do v7 (arquivos idênticos)
├── token.txt                     # Credenciais (criado na primeira execução)
├── transcricao.txt               # Saída de texto acumulado
└── ANALISE_STT.md                # Este arquivo
```

---

## 8. Observações Importantes

⚠️ **Legal**: Este projeto usa a API interna do ChatGPT sem autorização explícita. Use por sua conta e risco.

⚠️ **Tokens**: O token expira. Se der erro 401, delete `token.txt` e gere um novo.

⚠️ **Rate limiting**: Muitas requisições podem bloquear sua conta temporariamente.

✅ **Recomendação**: Para uso sério, considere usar OpenAI Whisper API oficial ou modelos locais (faster-whisper).

---

## 9. Conclusão

O sistema oferece duas abordagens distintas:
- **stt-config-ok.py**: Mais robusto, ideal para transcrição longa e contínua
- **stt_auto_detect_v7.py**: Mais simples, ideal para comandos curtos e ditado rápido

Ambos usam a mesma API de transcrição do ChatGPT e compartilham a maior parte da lógica de captura e envio de áudio.
