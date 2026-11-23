# Lonely Wallet v4.2.33

**Gerenciador pessoal de investimentos | Docker / ZimaOS | Português (Brasil)**

Esta versão parte da base estável v4.2.32. Mantém a interface, as regras financeiras, a importação B3 e o fluxo de carregamento do dashboard, com otimizações específicas para reduzir chamadas repetidas às APIs e proteger as cotas de planos gratuitos.

As consultas da brapi passam a usar cache persistente em SQLite, orçamento local móvel de 30 dias, uma chamada por ticker, concorrência serializada e intervalo mínimo entre chamadas. As respostas recentes podem ser reutilizadas após reinicializações e, durante indisponibilidades temporárias, respostas anteriores dentro da janela de contingência podem ser reaproveitadas. A sincronização Pluggy para novas instalações passa a ter frequência padrão de quatro horas; preferências já salvas pelo usuário são preservadas.

## 1. Informações da aplicação

- **Versão:** 4.2.33 (`VERSION` é a fonte única da versão exibida no rodapé e enviada no User-Agent)
- **Backend:** Python 3.12, Flask e Gunicorn
- **Persistência:** SQLite, com bancos separados para ativos e tokens
- **Porta:** `8085` no host e `8080` no container
- **Dados persistentes:** `/DATA/AppData/lonely-wallet/data` no host, montado em `/data`
- **Container:** executa com usuário/grupo não privilegiado `app` (UID/GID `10001`) após preparar as permissões do volume

## 2. Instalação e atualização no ZimaOS

1. Faça backup dos dados antes de atualizar.
2. Extraia o ZIP em uma pasta de projeto.
3. Mantenha o volume existente no `docker-compose.yml`:

```yaml
volumes:
  - /DATA/AppData/lonely-wallet/data:/data
```

4. Na pasta do `docker-compose.yml`, execute:

```bash
docker compose up -d --build
```

5. Consulte a saúde e a versão:

```bash
curl http://IP_DO_SERVIDOR:8085/api/health
```

O retorno deve apresentar `ok: true` e `version: 4.2.33`.

**Não apague nem recrie a pasta `/DATA/AppData/lonely-wallet/data`.** A atualização substitui o código e a imagem, não os bancos, tokens, credenciais ou configurações persistidos nesse volume.

## 3. Bancos e arquivos persistentes

| Arquivo | Finalidade |
|---|---|
| `ativos.db` | Operações, posições, proventos e dados da carteira |
| `tokens.db` | PIN, autenticação, credenciais e configurações sensíveis |
| `carteira.db` | Banco legado, preservado para migração/compatibilidade quando existir |
| `quotes_last_valid.json` | Últimas cotações válidas salvas |
| `dashboard_cache.json` | Snapshot completo do dashboard, invalidado quando os bancos mudam |
| `.api-credentials.key` | Chave usada para proteger credenciais de integrações |
| `.lonely-wallet-session.key` | Segredo persistente das sessões, quando configurado |
| `backups/pre-exclusao-*` | Backups preventivos antes de exclusões em massa |

Não copie apenas os bancos de dados se também precisar recuperar credenciais criptografadas. Guarde as chaves em local protegido e separado.

## 4. Backup e restauração

O script `scripts/backup.py` cria uma pasta de backup e faz cópias consistentes dos bancos usando a API de backup do SQLite. Também valida a integridade das cópias e inclui o cache de cotações quando válido.

Exemplo:

```bash
python scripts/backup.py --destination /backup
```

Por padrão, as chaves de criptografia não são incluídas. Para incluí-las explicitamente:

```bash
python scripts/backup.py --destination /backup --include-keys
```

Use `--include-keys` somente se o destino for protegido e restrito. Sem a chave original, credenciais armazenadas criptografadas podem não ser recuperáveis, embora os registros financeiros dos bancos continuem preservados.

Antes de restaurar, pare o container, guarde uma cópia do diretório atual `/data`, restaure os arquivos para o mesmo volume e confira as permissões. Não faça testes de restauração sobre a única cópia de produção.

## 5. Importação de planilhas B3

- Aceita arquivos `.xlsx` de até **16 MB**.
- Arquivos acima do limite retornam HTTP **413** com mensagem específica.
- A planilha é aberta em modo somente leitura (`read_only=True`) para reduzir o uso de memória.
- O pacote XLSX é validado antes da leitura: estrutura ZIP, quantidade de entradas, tamanho total descompactado, tamanho máximo de componentes e taxa de compressão.
- Planilhas com macros não são aceitas.
- O parser XML protegido de `defusedxml` é obrigatório; a aplicação falha de forma segura se a proteção não estiver disponível.
- O limite é de 200.000 linhas e 64 colunas. Planilhas acima desses limites devem ser divididas antes da importação.

A prévia continua sendo a etapa recomendada antes de confirmar a importação. Compare os registros com o extrato oficial e não execute exclusões em massa sem backup.

## 6. Segurança e dependências

Dependências Python fixadas nesta versão:

- Flask `3.1.3`
- Gunicorn `27.0.1`
- cryptography `50.0.2`
- openpyxl `3.1.5`
- defusedxml `0.7.1`
- requests `2.34.2`

O Flask foi atualizado para uma versão que inclui a correção CVE-2026-27205. A aplicação define um limite de conteúdo para uploads e retorna uma resposta JSON própria para HTTP 413. As bibliotecas de segurança e autenticação continuam no banco `tokens.db` e no volume persistente.

A atualização das dependências deve ser validada com o container completo e com os fluxos de autenticação, Pluggy, B3, cotações, IRPF e DARF antes de publicar uma imagem de produção.

## 7. Gráficos e Chart.js

A aplicação prioriza o Chart.js `4.6.1` servido localmente. O script `scripts/vendor_chartjs.py` busca o pacote oficial npm e valida o artefato com o SRI SHA-512 fixado no código antes de extrair a distribuição UMD. O valor fixado foi conferido com os metadados `dist.integrity` do pacote npm.

O carregamento usa dois scripts independentes e sequenciais: primeiro o bundle Chart.js, quando existe e passa na verificação SHA-256 local; logo depois, `chart-fallback.js` é carregado sempre. O fallback se encerra imediatamente quando `window.Chart` já existe. Assim, se a primeira requisição responder 404 ou falhar durante o carregamento, o navegador ainda executa o segundo script, sem tentar reutilizar a tag que falhou.

Se o build estiver sem conexão com o registry npm e o bundle não estiver presente, a imagem pode ser construída com o renderer alternativo incluído no projeto. Essa condição é registrada no log e em `chartjs-build-status.json`. O endpoint `/api/health` expõe `charts.engine`, `charts.status`, `charts.chartjs_integrity_verified` e `charts.fallback_available`; quando somente o fallback estiver disponível, o endpoint continua respondendo HTTP 200, mas retorna `status: degraded` para deixar a limitação visível. Se os dois renderers estiverem ausentes, o healthcheck retorna HTTP 503.

O fallback contempla linha, barra, rosca e pizza, inclui tooltips por hover (respeitando callbacks `title`, `beforeBody`, `label` e `afterBody` mais usados pelo dashboard), callbacks de clique/hover e uma animação simples de entrada. Ele não substitui todos os recursos avançados, plugins, layouts, escalas ou animações do Chart.js completo. Com o bundle íntegro, o Chart.js completo é usado automaticamente.

Falha de rede/timeout é a única condição que permite continuar com o fallback. Se um pacote for recebido com SRI incorreto, estrutura inesperada, HTTP 404 ou outro erro de integridade, o build falha em vez de publicar silenciosamente uma dependência inválida. Um bundle local já existente também precisa corresponder ao SHA-256 do arquivo sidecar.

O build ainda precisa ter as dependências Python disponíveis no índice configurado ou no cache/wheelhouse do ambiente. A opção offline para o Chart.js não torna automaticamente o `pip install` independente de rede.

## 8. Testes e validação

Com Python 3.12 e as dependências instaladas:

```bash
python -m py_compile app/main.py scripts/backup.py scripts/vendor_chartjs.py
python -m unittest discover -s tests -v
```

Os testes de runtime JavaScript ficam em `tests/*runtime.js` e podem ser executados no ambiente Node compatível quando disponível. O teste `tests/test_vendor_chartjs.py` cobre build offline, falha de integridade e verificação do pin SRI.

A validação de release deve incluir build Docker, login/PIN, carga inicial do dashboard, manutenção do volume `/data`, importação B3 dentro e acima do limite, backup/restauração de ambos os bancos, movimentações, proventos, IRPF/DARF e conexões Pluggy.

## 9. Solução de problemas

### O container parece ter perdido os dados

Confira se o Compose ainda monta `/DATA/AppData/lonely-wallet/data:/data`. Um caminho diferente ou vazio faz a aplicação parecer recém-instalada. Não apague o diretório antigo.

### O healthcheck falha

Consulte:

```bash
docker compose logs --tail=200 lonely-wallet
curl http://IP_DO_SERVIDOR:8085/api/health
```

Verifique se a porta `8085` está livre e se o UID/GID `10001` possui acesso ao volume persistente.

### A importação B3 retorna HTTP 413

O arquivo excedeu o limite de 16 MB da requisição. Exporte novamente ou divida a planilha. O erro 413 é intencional e não deve aparecer como falha genérica de leitura.

### A importação B3 rejeita o XLSX

Confirme que o arquivo é `.xlsx` sem macros, que não foi corrompido ao baixar e que está dentro dos limites de linhas, colunas e tamanho descompactado.

### Os gráficos não aparecem

Confirme que `/static/vendor/chart.umd.min.js` está presente e íntegro. Sem o bundle, o frontend deve carregar `/static/vendor/chart-fallback.js`. Verifique os logs do build e a aba Network do navegador para identificar erro de arquivo estático.

### O PIN correto retorna bloqueio

Aguarde o período de bloqueio. Não apague `tokens.db`, pois ele contém dados de autenticação e credenciais de integrações.

### Uma API externa está indisponível

Consulte o estado da integração e os logs do container. A falta de cotação não deve ser interpretada como perda de dados da carteira; o sistema pode mostrar a última cotação válida salva, quando existir.

## 10. Limitações e cuidados

- A cobertura e a atualização de cotações/proventos dependem das fontes externas e dos limites de cada provedor.
- A aplicação é um gerenciador pessoal, não é corretora, custodiante, banco ou consultoria de investimentos.
- Os cálculos fiscais e resumos de IRPF/DARF são auxiliares e devem ser conferidos com documentos oficiais e regras vigentes.
- Não exponha diretamente a porta do container à internet. Prefira VPN ou proxy reverso com HTTPS, autenticação e regras de firewall.
- Não remova manualmente arquivos SQLite, WAL, chaves ou cache durante o funcionamento do container.

## 11. Histórico de versões

### v4.2.32

- Corrigido o carregamento offline: `chart-fallback.js` agora é um segundo script independente e sempre carregado após o Chart.js opcional.
- Adicionada verificação de integridade do bundle no backend; a aplicação não injeta Chart.js no navegador quando o arquivo local está ausente ou não corresponde ao SHA-256 sidecar.
- `/api/health` agora informa o mecanismo gráfico (`chartjs`, `fallback` ou `unavailable`) e marca o serviço como `degraded` quando somente o renderer alternativo está ativo.
- Builds sem conexão com o npm registram explicitamente o modo fallback; erro HTTP, arquivo inesperado ou divergência de SRI SHA-512 interrompe o build.
- Fallback aprimorado com tooltips por hover, suporte aos callbacks `title`, `beforeBody`, `label` e `afterBody`, e animação básica de entrada.
- SRI SHA-512 de Chart.js 4.6.1 conferido com os metadados npm; versões pinadas de Flask, Gunicorn, cryptography, defusedxml e openpyxl verificadas como existentes no PyPI.
- Requests atualizado de 2.32.5 para 2.34.2.
- Adicionados testes para ordem dos scripts, comportamento dos tooltips/animação, modo offline e falhas de integridade.

### v4.2.31

- Corrigida a preparação dos testes de regressão: tabelas financeiras são limpas no banco de ativos e os saldos importados são isolados entre testes.
- Upload B3 acima de 16 MB retorna HTTP 413 com mensagem específica.
- XLSX processado em modo somente leitura, com limite de linhas/colunas, validação do pacote ZIP e proteção XML via `defusedxml`.
- Rotação de backups preventivos tolera diretórios removidos durante a varredura e não segue links simbólicos.
- Migração de bancos e script de backup usam URI SQLite escapada para caminhos com `?`, `#`, `%` e espaços; conexões de verificação são fechadas explicitamente.
- Chart.js atualizado para 4.6.1 com verificação SRI SHA-512 do pacote npm; fallback local incluído para builds sem acesso à internet.
- Flask, Gunicorn e cryptography atualizados; versão do rodapé e User-Agent derivadas do arquivo `VERSION`.
- README reorganizado e atualizado.

### v4.2.30

- Tooltip dos controles de movimentação das categorias padronizado com o estilo do projeto.
- Altura do cabeçalho de Renda Fixa alinhada às demais categorias.
- Total confirmado de proventos passou a somar recebimentos dos últimos 12 meses e pagamentos anunciados com vencimento nos próximos 12 meses.

### v4.2.29

- Adicionado ao cabeçalho de Renda Fixa o indicador Valor total com descontos, calculado a partir do saldo líquido estimado de todas as aplicações.
- O total consolidado apresenta `—` quando alguma aplicação não possui estimativa líquida confiável.
- Preservadas as melhorias tipográficas dos cards de resumo e os espaçamentos de Últimos ativos lançados.

### v4.2.28

- Adicionada a coluna Valor total com descontos ao cabeçalho da categoria Renda Fixa, à direita de Valor total.
- O indicador soma os valores individuais de Saldo com descontos de todas as aplicações, independentemente da paginação.
- Evitada a reaplicação de impostos sobre valores já calculados por aplicação; totais incompletos são apresentados como `—`.
- Melhorias nos cards de destaques e últimos ativos, com ajustes de margens, espaçamentos e adaptação a telas menores.

### v4.2.27

- O card Últimos ativos lançados passou a mostrar os dois ativos com operações mais recentes.
- O Ranking de proventos passou a exibir os dois maiores pagadores com pagamentos confirmados.
- Cabeçalhos de cards auxiliares padronizados com os cards estatísticos.

### v4.2.26

- O arquivo-base v4.2.30 não continha notas recuperáveis desta versão. O histórico detalhado não foi reconstruído para evitar atribuir alterações sem comprovação.

### v4.2.25 e anteriores

Consulte as notas históricas preservadas nos pacotes anteriores. Este README prioriza o histórico confirmado a partir da v4.2.25 e as orientações atuais de instalação, segurança e manutenção.

## 12. Controle de consumo das APIs gratuitas

- A brapi usa cache persistente em SQLite, compartilhado entre threads e preservado após reinicializações do container.
- O cache padrão é de 30 minutos durante o pregão, 12 horas fora do pregão, 12 horas para proventos, 24 horas para histórico e 6 horas para a lista de ativos.
- O limite padrão é de um ticker por chamada, com apenas uma requisição brapi simultânea e intervalo mínimo de 350 ms entre chamadas.
- Os orçamentos locais padrão são de 10.000 chamadas brapi por janela móvel de 30 dias, 300 chamadas HG Brasil por 24 horas e 150 chamadas Dados B3 por 24 horas. Os limites reservam margem para outras aplicações que possam compartilhar os tokens; as contagens incluem apenas esta instalação.
- Quando o orçamento é atingido, a aplicação deixa de chamar a brapi e tenta reutilizar uma resposta em cache ainda dentro da janela de contingência.
- O painel de consumo aparece em **Integrações e APIs** e mostra os limites locais da brapi, HG Brasil e Dados B3, além das chamadas registradas de Pluggy e outras fontes. Consultá-lo não chama o provedor; lê apenas o SQLite local.
- A frequência padrão da sincronização Pluggy para novas instalações é de 4 horas. As chamadas Pluggy são espaçadas em pelo menos 1 segundo, com o intervalo persistido entre reinicializações, e a API Key é reutilizada por aproximadamente 115 minutos. Uma frequência já salva pelo usuário não é sobrescrita.

Variáveis configuráveis no Compose:

```yaml
BRAPI_LOCAL_30D_BUDGET: "10000"
BRAPI_MAX_TICKERS_PER_REQUEST: "1"
BRAPI_REQUEST_GAP_SECONDS: "0.35"
HGBRASIL_LOCAL_24H_BUDGET: "300"
DADOSB3_LOCAL_24H_BUDGET: "150"
PLUGGY_REQUEST_GAP_SECONDS: "1.0"
```

A HG Brasil usa limite local padrão de 300 chamadas por 24 horas, abaixo da cota gratuita publicada de 400 por dia; Dados B3 usa limite local padrão de 150 por 24 horas, abaixo da cota grátis publicada de 200 por dia. Os limites podem ser ajustados com `HGBRASIL_LOCAL_24H_BUDGET` e `DADOSB3_LOCAL_24H_BUDGET`; valor `0` desativa o limite local daquele provedor. Para a brapi, `BRAPI_LOCAL_30D_BUDGET=0` desativa o orçamento local, mas não é recomendado no plano gratuito. Mantenha `BRAPI_MAX_TICKERS_PER_REQUEST=1` no plano gratuito; aumente somente se o plano contratado permitir mais tickers por chamada.

O orçamento é uma proteção local, não uma leitura oficial da cota do provedor. Outras aplicações usando o mesmo token não são contabilizadas. Consulte o painel do próprio provedor para conferir o saldo global.

