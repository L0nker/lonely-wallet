# Lonely Wallet v4.2.39

**Gerenciador pessoal de investimentos | Docker / ZimaOS | Português (Brasil)**

Esta versão remove os orçamentos artificiais diários e mensais anteriormente definidos pela aplicação para brapi, HG Brasil, Dados B3, Pluggy e FRED. As APIs deixam de ser bloqueadas por contadores internos. A aplicação mantém cache persistente, registro informativo de chamadas, reutilização de respostas e tratamento de HTTP 429/Retry-After conforme informado pelo próprio provedor.

Foram removidos também os intervalos fixos locais entre requisições da brapi e Pluggy. Permanecem proteções contra rajadas de solicitações idênticas simultâneas, autenticação reutilizável da Pluggy e backoff temporário para HTTP 429 e falhas transitórias de rede/servidor. O painel de APIs agora mostra apenas estatísticas de utilização, sem saldo ou teto local.

## 1. Informações da aplicação

- **Versão:** 4.2.39 (`VERSION` é a fonte única da versão exibida no rodapé e enviada no User-Agent)
- **Backend:** Python 3.12, Flask e Gunicorn
- **Persistência:** SQLite, com bancos separados para ativos, tokens e estado das APIs
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

O retorno deve apresentar `ok: true` e `version: 4.2.39`. O estado de cache e consumo das APIs é mantido em `/data/api_state.db`.

**Não apague nem recrie a pasta `/DATA/AppData/lonely-wallet/data`.** A atualização substitui o código e a imagem, não os bancos, tokens, credenciais ou configurações persistidos nesse volume.

## 3. Bancos e arquivos persistentes

| Arquivo | Finalidade |
|---|---|
| `ativos.db` | Operações, posições, proventos e dados da carteira |
| `tokens.db` | PIN, autenticação, credenciais e configurações sensíveis |
| `api_state.db` | Cache HTTP, estados temporários de erro e histórico informativo de consumo das APIs |
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

## 12. Uso das APIs sem orçamentos locais

- A Lonely Wallet não impõe limites diários ou mensais de chamadas para brapi, HG Brasil, Dados B3, Pluggy ou FRED.
- Os contadores do painel são somente informativos e não bloqueiam novas consultas.
- O cache persistente continua ativo: cotações, proventos, histórico e listas de ativos reutilizam respostas recentes para evitar chamadas duplicadas.
- A aplicação não aplica intervalo fixo de 350 ms na brapi nem de um segundo na Pluggy.
- Solicitações idênticas simultâneas usam um bloqueio por chave de cache, evitando trabalho duplicado; consultas diferentes aos demais provedores podem ocorrer em paralelo. A brapi mantém uma chamada simultânea no perfil padrão, compatível com o plano Gratuito.
- Respostas HTTP 429 respeitam o cabeçalho `Retry-After` do provedor. Falhas transitórias de rede e respostas 5xx recebem uma pausa curta para evitar repetição agressiva. Respostas 400, 401 e 403 não criam cotas artificiais persistentes; erros de autenticação são apresentados para correção.
- O endpoint `/api/integracoes/consumo` e o painel **Integrações e APIs** apresentam somente contagens locais, reutilização de cache e última chamada registrada. Consultar o painel não chama os provedores.
- `BRAPI_MAX_TICKERS_PER_REQUEST` controla apenas o tamanho do lote por chamada, não a quantidade de chamadas. O padrão permanece em 1 ticker para compatibilidade com o plano Gratuito; ajuste para 10 ou 20 somente se o plano contratado permitir.
- `PLUGGY_MAX_PAGES` é uma proteção estrutural de paginação contra respostas inconsistentes ou loops infinitos; não é um orçamento de chamadas.

Variáveis de ambiente restantes para formato de consulta e paginação (não são orçamentos de chamadas):

```yaml
BRAPI_MAX_TICKERS_PER_REQUEST: "1"
PLUGGY_MAX_PAGES: "100"
PLUGGY_PAGE_SIZE: "500"
```

Os limites comerciais, cotas e políticas de rate limit pertencem a cada provedor e ao plano contratado. Quando o provedor retorna HTTP 429, a Lonely Wallet respeita a orientação de espera recebida; não inventa uma contagem máxima de chamadas.

## 13. Correções e validação da v4.2.36

### Estabilidade da migração SQLite

- A migração do armazenamento de APIs inspeciona `PRAGMA table_info` antes de consultar tabelas legadas.
- Schemas antigos sem `endpoint`, `credential_hash`, `stale_hits` ou campos opcionais de estado recebem valores padrão seguros.
- O schema já existente de `api_state.db` também é atualizado com colunas ausentes antes da criação dos índices.
- O marcador `legacy_assets_migrated_v2` só é gravado após a cópia ser concluída; a execução pode ser repetida sem duplicar os registros migrados.
- O histórico antigo de cache, requisições, estatísticas e cooldowns é preservado quando as colunas de origem estão disponíveis.

### Inicialização do Gunicorn

- `HOME` agora aponta para `/home/app`, diretório pertencente ao usuário não privilegiado da aplicação.
- `XDG_RUNTIME_DIR` aponta para `/tmp/lonely-wallet-runtime`, criado com permissões restritas e proprietário `app` pelo entrypoint.
- O socket de controle do Gunicorn é definido explicitamente em `/tmp/lonely-wallet-runtime/gunicorn.ctl`, evitando tentativas de gravação em `/root/.gunicorn` após a redução de privilégios.

### Proteções de consumo das APIs na v4.2.36

- A v4.2.36 ainda utilizava orçamentos locais conservadores e intervalos fixos de requisição. Esses bloqueios artificiais foram removidos na v4.2.39.
- A validação estrita da paginação Pluggy e a sinalização de dados antigos reutilizados do cache permanecem.
- A rotina de migração do `api_state.db` continua separada do banco financeiro `ativos.db`.

### Validação da v4.2.36

- 4 testes do entrypoint e preparação das permissões aprovados, incluindo `HOME` e `XDG_RUNTIME_DIR`.
- 3 testes de backup SQLite aprovados.
- 5 testes do fornecedor/validação do Chart.js aprovados.
- Teste isolado da migração de schemas antigos sem `endpoint` aprovado, incluindo atualização de destino preexistente e idempotência.
- Compilação sintática Python aprovada para `app/main.py`, entrypoint, backup e testes alterados.
- Docker Compose YAML analisado e válido.
- Integridade do pacote ZIP conferida após a geração.
- A suíte integrada `tests/test_regressions.py` não foi executada porque Flask não está instalado neste ambiente.
- Build Docker real e validação final no ZimaOS não foram executados neste ambiente.

**Atualização:** mantenha o volume `/DATA/AppData/lonely-wallet/data:/data`. Não apague `ativos.db`, `tokens.db` ou `api_state.db`. A atualização corrige o schema e a migração automaticamente, preservando os dados financeiros e o histórico de consumo.


## Lonely Wallet v4.2.37

### Estabilidade SQLite e sincronizações

- A sincronização Pluggy confirma as gravações locais antes de executar novas chamadas HTTP e grava as movimentações em lotes curtos.
- A promoção de operações agendadas usa conexão SQLite isolada, verifica a existência de operações vencidas e evita `UPDATE` quando não há trabalho.
- A sincronização de proventos possui exclusão mútua por thread e lease persistente no `api_state.db`, evitando execuções duplicadas entre workers.
- As gravações de proventos são confirmadas em lotes curtos.
- Falhas complementares de proventos ou bloqueios transitórios na leitura final não derrubam o dashboard quando existe um retrato anterior válido.
- A interface limita as tentativas automáticas e libera o último estado local disponível após falhas repetidas.

### Regra de origem dos proventos

- **Pagos/recebidos:** contabilizados exclusivamente a partir de transações de proventos importadas pela Pluggy. Lançamentos manuais, importações B3 e respostas pagas de outras APIs não confirmam recebimento. A identificação considera a descrição da movimentação para classificar corretamente JCP, dividendos e rendimentos.
- **A receber:** eventos anunciados pelas fontes integradas que disponibilizam calendário de proventos são exibidos como pendentes até existir confirmação Pluggy. Nesta versão, o calendário externo implementado é o da BRAPI v2; a cotação BRAPI v1 também é usada como fallback quando retorna `cashDividends`. Importações B3 e lançamentos manuais futuros também permanecem pendentes.
- HG Brasil, Dados B3 (séries de índices), FRED, BCB, IBGE e Tesouro são usados para cotações ou indicadores e não são tratados como calendários de proventos quando não fornecem eventos de pagamento.
- Eventos anunciados por várias fontes são deduplicados por ativo, data de pagamento, tipo normalizado e valor. Correspondências já confirmadas pela Pluggy são removidas da lista pendente, com tolerância para pequenas diferenças de valor e data. Transações Pluggy com IDs diferentes permanecem distintas para preservar recebimentos legítimos em contas diferentes.
- Quando a Pluggy confirma um evento previamente anunciado por outra API, o registro existente é preservado sem duplicar o pagamento.

### Atualização

- Base: Lonely Wallet v4.2.36.
- Manter os bancos `ativos.db`, `tokens.db` e `api_state.db`, além dos arquivos WAL.
- Manter o volume `/DATA/AppData/lonely-wallet/data:/data`.
- Reconstruir a imagem Docker para aplicar as alterações.


## 14. Correções de proventos e gráfico (v4.2.38)

- Proventos recebidos são confirmados exclusivamente por transações de proventos importadas pela Pluggy; outras fontes não transformam um anúncio em pagamento recebido.
- Eventos “A receber” são considerados somente a partir do primeiro dia do mês corrente. Quando o mês de pagamento termina sem confirmação pela Pluggy, o evento expirado deixa de aparecer nas previsões, totais, histórico pendente e gráficos.
- Eventos do mês corrente permanecem visíveis até o fechamento do mês, mesmo que o dia previsto já tenha passado; eventos dos meses futuros também permanecem.
- O card Evolução de Proventos usa colunas empilhadas para recebidos e a receber. O hover trabalha por índice e destaca os dois segmentos da coluna inteira.
- O tooltip apresenta mês/ano, valor a receber, valor recebido e total combinado, seguindo o comportamento visual de referência fornecido pelo usuário.
- Clicar em qualquer parte da coluna abre a distribuição combinada daquele mês, com detalhamento por ativo ou por tipo de provento.
- O filtro “Últimos 12 meses” mantém os recebimentos do período histórico e inclui os pagamentos pendentes a partir do mês corrente.
- A atualização não altera o histórico bruto nem exclui registros das APIs; apenas deixa de projetar eventos pendentes cujo mês já terminou sem confirmação.

## 15. Remoção dos limites artificiais de APIs (v4.2.39)

- Removidos os tetos locais diários e mensais de brapi, HG Brasil, Dados B3, Pluggy e FRED.
- Removidos os intervalos fixos locais de 350 ms da brapi e 1 segundo da Pluggy.
- Contadores de chamadas permanecem apenas para diagnóstico e não bloqueiam requisições.
- Falhas ao registrar métricas no `api_state.db` não impedem a chamada externa.
- O cache persistente e a proteção contra solicitações idênticas simultâneas continuam ativos.
- HTTP 429 continua respeitando `Retry-After` do provedor; falhas transitórias 5xx/rede recebem backoff curto.
- HTTP 400, 401 e 403 deixam de criar bloqueios locais persistentes.
- Removidas as variáveis de orçamento e intervalo do `docker-compose.yml`; variáveis antigas ainda presentes em instalações existentes são ignoradas pelo código.
- Validação local: 63 testes Python independentes e 40 verificações de runtime JavaScript aprovados; os três scripts JavaScript inline da página também passaram em `node --check`.
- A suíte integrada que importa o backend não foi executada neste ambiente porque Flask não está instalado; build Docker real e teste final no ZimaOS permanecem pendentes.

