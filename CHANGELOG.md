# Changelog

Histórico de versões do JoaKApple. Formato baseado no
[Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/); as versões
seguem [SemVer](https://semver.org/lang/pt-BR/).

## [Não lançado]

### Adicionado
- Suporte ao **Google** (retorno do LERS), além da Apple. Na GUI, o seletor
  "Provedor" (Apple | Google) troca a origem dos arquivos, as etapas e o
  Termo; na CLI, `--provider google` (com `--source` como apelido de `--csv`).
  - A origem é a pasta com o que foi baixado do LERS: os ZIPs de produção
    e o PDF "Valores de Hash".
  - Nada é extraído nem descompactado em disco: os dados ficam nos ZIPs
    originais, como o Google entregou.
  - **Baixar** copia os ZIPs de produção e os PDFs para o destino, byte a
    byte (se o destino for a própria pasta do LERS, não copia nada).
  - **Verificar** calcula o SHA-512 de cada ZIP de dados lendo-o direto de
    dentro do ZIP de produção e confere contra o PDF de hashes do Google
    (lido com `pypdf`, nova dependência).
  - Não há etapa 3 para o Google; o card some da GUI. "Excluir" remove só a
    cópia no destino, nunca o original do LERS.
- Termo de Recebimento com provedor, algoritmo de hash e formato dos
  pacotes de acordo com o provedor (Google LLC, SHA-512, ZIP).

### Alterado
- O que é específico de cada nuvem saiu do `core.py` para `providers/`
  (`apple.py`, `google.py`); o motor do pipeline ficou genérico. O
  comportamento com a Apple não muda.

## [1.9.0] - 2026-09-18

### Adicionado
- GUI: a tela do Termo de Recebimento ganhou o checkbox "Incluir senha
  do GPG no termo", na Seção 3 (Especificações Técnicas). Nasce marcado
  automaticamente quando o campo de senha GPG da tela principal já tem
  valor; se marcada com o campo vazio, mostra aviso e desmarca de
  volta. Quando ativa, publica a linha "Senha de Descriptografia
  (GPG): {senha}" logo após "Algoritmo utilizado: SHA-256", nos três
  formatos de saída (markdown, HTML e .docx).

## [1.8.0] - 2026-09-18

### Corrigido
- GUI: a tela do Termo de Recebimento ficava travada em "Calculando
  hashes…" sem nenhum feedback, recalculando do zero — em série e num
  único thread — o hash de todo arquivo selecionado, mesmo os que o
  pipeline principal já tinha acabado de conferir (com barra de
  progresso) segundos antes. Para lotes com arquivos grandes/numerosos
  isso dava a impressão de travamento, mesmo sem nenhum erro.

### Adicionado
- `core.FileEntry` agora guarda o hash calculado na etapa de verificação
  do pipeline (junto com tamanho/mtime do arquivo naquele momento).
  `core.cached_file_hash()` reaproveita esse valor enquanto o arquivo em
  disco não mudar.
- `report.compute_hash_rows_cached()`: usa esse cache pro que já foi
  conferido e calcula em paralelo (mesmo `ThreadPoolExecutor` do
  pipeline) só o que falta, com progresso por arquivo. A tela do Termo
  de Recebimento passou a usar essa função — o texto agora atualiza ao
  vivo (bytes, velocidade, ETA) em vez de ficar parado até tudo
  terminar.

## [1.7.3] - 2026-09-18

### Corrigido
- CLI (`report`): o subcomando não tinha noção de "seleção" e calculava
  hash de todos os arquivos do CSV, mesmo com `--only`/`--exclude`/
  `--pattern` disponíveis no `run`. Agora `report` aceita as mesmas
  flags de seleção, e o menu interativo (`--menu`/`-i`) ganhou uma etapa
  de escolha de arquivos (igual ao `run`) antes de calcular os hashes —
  mesmo bug do Termo de Recebimento da GUI (v1.7.1), agora corrigido
  também no modo texto.

## [1.7.2] - 2026-09-18

### Adicionado
- Progresso ao vivo (percentual, bytes, velocidade e ETA) também durante a
  verificação de hash, na mesma linha/barra por arquivo que já mostrava o
  download — antes essa etapa só mostrava "Conferindo hash..." parado,
  sem nenhum número. Vale pra GUI e pro modo `--no-gui` (Linux). Não
  entra no agregado do topo/lote (só download conta pra aquele total).

## [1.7.1] - 2026-09-18

### Corrigido
- Termo de Recebimento: o gerador usava **todos** os arquivos do CSV pra
  listar e calcular hash, ignorando os checkboxes marcados na lista
  principal. Agora usa só o que está selecionado (mesmo filtro que o
  botão "Iniciar" já aplicava). Se nada estiver marcado, mostra um aviso
  em vez de gerar um termo vazio silenciosamente.

## [1.7.0] - 2026-09-18

### Adicionado
- Porcentagem numérica na barra de progresso geral (agregada, no topo da
  tela), além da cor enchendo — mostra `0%` a `100%` conforme os arquivos
  vão sendo concluídos.
- Modo texto `--no-gui` (só no Linux/AppImage — o `.exe` do Windows
  continua sem CLI, ver nota técnica abaixo), cobrindo todas as
  funcionalidades da GUI via subcomandos `run`/`list`/`delete`/`report`:
  - `run` baixa/verifica/descriptografa, com seleção de arquivos
    (`--only`/`--exclude`/`--pattern`), status ao vivo no terminal
    (percentual, velocidade, ETA por arquivo e agregado) e log colorido.
  - `list` lista os arquivos do CSV sem baixar nada.
  - `delete` remove do disco os arquivos já obtidos de uma seleção
    explícita (nunca "tudo" por engano).
  - `report` gera o Termo de Recebimento em `md`/`html`/`docx`.
  - Um menu interativo simples (`input()`, sem biblioteca nova) aparece
    automaticamente quando falta informação obrigatória, ou com
    `--menu`/`-i`.
  - Senha do GPG nunca aceita em texto puro numa flag: prompt interativo,
    `--passphrase-env` ou `--passphrase-stdin`.

### Técnico
- `format_eta`, `ProgressTracker` (velocidade/ETA por download) e
  `aggregate_progress` (agregado do lote) foram extraídos de `gui.py`
  para `core.py`, eliminando a duplicação entre a barra por arquivo, a
  barra agregada e o novo modo texto — os três agora reusam a mesma
  implementação, coberta por testes novos em `test_core.py`.
- Novo `apple_toolkit/app.py`: ponto de entrada único (`--no-gui` decide
  entre `cli.py` e `gui.py`), usado pelo build Linux do PyInstaller no
  lugar de `gui.py` direto. O build Windows não muda — continua
  compilando `gui.py` direto, sem modo texto.

## [1.6.0] - 2026-09-18

### Adicionado
- Velocidade de download e ETA por arquivo: a linha de status de cada
  download em andamento agora mostra, além de percentual e tamanho,
  a velocidade atual e o tempo restante estimado (ex.: "Baixando 62%
  · 15.2 MB/24.6 MB · 3.4 MB/s · ETA 00:03"). Aparece assim que a
  conexão é aberta (quando o tamanho do arquivo é conhecido) e some
  quando o download termina.
- Velocidade e ETA agregados do lote: o espaço que hoje fica vazio na
  barra de ação enquanto o pipeline roda passa a mostrar quanto já foi
  baixado do total do lote, a velocidade agregada e o tempo restante
  estimado (ex.: "42.1 MB / 128.4 MB · ↓ 6.4 MB/s total · restante
  ~01:02"). Ao terminar, volta a mostrar o resumo final como já
  funcionava.

## [1.5.0] - 2026-09-18

### Adicionado
- Barra de progresso individual por arquivo na lista de downloads, mostrando
  percentual, bytes baixados/total e velocidade em tempo real (antes só
  existia a barra agregada do lote inteiro).
- Lista de arquivos agora tem barra de rolagem própria e uma checkbox por
  arquivo para selecionar quais processar antes de clicar em "Iniciar" —
  arquivos desmarcados são ignorados no lote.
- Botão "Excluir" em cada linha, para apagar do disco (com confirmação) o
  que já foi baixado/descriptografado daquele arquivo específico, sem sair
  do programa.
- Log reformulado com cara de log técnico profissional: timestamp com
  milissegundos, nível (`DEBUG`/`INFO`/`OK`/`WARN`/`ERROR`) colorido, nome
  do componente e campos chave=valor (bytes, duração, velocidade, tentativa,
  thread, exit code do gpg, hash calculado, etc.), em vez de frases soltas.

### Corrigido
- Geração do Termo de Recebimento podia preencher hashes/volume/quantidade
  com placeholders mesmo tendo arquivos já baixados em disco: o botão
  "Gerar termo" ficava clicável antes do cálculo de hash (assíncrono)
  terminar, e não recarregava a lista de arquivos se o pipeline não tivesse
  rodado nesta sessão. Agora o botão fica desabilitado ("Calculando
  hashes…") até o cálculo terminar, e a janela carrega o CSV sozinha
  quando necessário.
- Texto padrão do Termo de Recebimento dizia que os hashes eram dos
  arquivos "contidos nos pacotes compactados (formato ZIP)" — os arquivos
  que a Apple envia são criptografados em GPG, não compactados em ZIP.
  Corrigido para "pacotes criptografados (formato GPG)".

## [1.4.1] - 2026-09-17

### Corrigido
- Janela principal usava um tamanho fixo em pixels (`1040x820`) que não
  se adaptava a fontes/DPI diferentes, cortando o botão "Gerar Termo de
  Recebimento…" no rodapé até o usuário esticar a janela manualmente.
  A janela agora calcula o próprio tamanho a partir do conteúdo, então
  nada fica escondido independente da tela.

## [1.4.0] - 2026-09-17

### Adicionado
- Verificação no workflow de release: a tag `vX.Y.Z` só publica se
  `APP_VERSION` (em `gui.py`) e a primeira entrada deste CHANGELOG
  baterem com a versão da tag. Evita repetir o bug de versão exibida
  ficando desatualizada em relação à tag publicada.
- Botão "Gerar Termo de Recebimento…" na GUI, que abre uma janela para
  preencher os dados manuais do Termo de Recebimento e Identificação de
  Evidência Telemática (processo, PIC/inquérito, responsável, etc.) e
  gera o texto já com quantidade de arquivos, volume total e a tabela de
  hashes SHA-256 calculados automaticamente a partir dos arquivos
  recebidos na pasta de destino.
- Duas formas de exportar o termo gerado: botão "Copiar" (texto simples
  + HTML formatado na área de transferência, para colar com Arial 12 e
  espaçamento 1,5 direto no Word/LibreOffice) e botão "Salvar .docx"
  (gera um arquivo Word já pronto, com títulos, negrito, tabela nativa
  e a mesma formatação Arial 12/espaçamento 1,5).

## [1.3.0] - 2026-09-17

### Adicionado
- Ícone próprio do app (paleta da GUI: verde-azulado + creme), embutido no
  `.exe` do Windows, no AppImage do Linux e carregado também na janela em
  tempo de execução.
- Badges de build/testes/release no README.
- Suíte de testes automatizados para o motor do pipeline (`core.py`) e
  workflow de CI rodando a cada push/PR.
- Este CHANGELOG.

## [1.2.1] - 2026-09-17

### Corrigido
- Versão exibida na tela ficava presa em "1.1.0" mesmo depois da tag
  v1.2.0, porque `APP_VERSION` é uma constante que precisa ser atualizada
  manualmente a cada release.
- Canto do painel de log com raio de borda diferente do resto do card
  (parecia um círculo flutuando solto no rodapé).

### Alterado
- Crédito do autor movido do rodapé para o cabeçalho da janela.

## [1.2.0] - 2026-09-17

### Adicionado
- GnuPG portátil embutido no build do Windows — não é mais necessário
  instalar nada à parte para descriptografar.

### Alterado
- GUI reconstruída do zero com CustomTkinter para bater com o mockup
  aprovado (a primeira versão em Tkinter/ttk puro foi rejeitada por não
  bater visualmente com o design aprovado).

### Corrigido
- Instalador do Gpg4win travando indefinidamente no CI do Windows —
  resolvido extraindo o instalador diretamente com 7-Zip em vez de
  executá-lo.

## [1.1.0] - 2026-09-17

### Adicionado
- Pipeline dividido em três etapas independentes (Baixar / Verificar /
  Descriptografar), que podem ser ativadas sozinhas ou em qualquer
  combinação — por exemplo, só conferir arquivos já baixados antes.

### Alterado
- README reescrito para o usuário final, não mais para quem só mexe no
  código.

## [1.0.0] - 2026-09-17

Primeira versão do toolkit unificado, substituindo os três scripts
originais (`baixar.py`, `conferir.py`, `decriptar.py`).

### Adicionado
- Motor de pipeline único (`apple_toolkit/core.py`) com download
  retomável, verificação de hash SHA256 e descriptografia GPG, com senha
  passada via stdin (nunca como argumento de linha de comando).
- Interface gráfica (`apple_toolkit/gui.py`).
- Build automatizado via GitHub Actions: executável Windows (PyInstaller)
  e AppImage Linux, publicados juntos numa release por tag `vX.Y.Z`.
- Crédito do autor nas notas de release do GitHub.
- Rebrand do projeto para **JoaKApple**.
