# Lonely Wallet v4.2.53

# Atualizações da versão 4.2.53

## Exclusão de proventos não recebidos após a data prevista
- Removida a categoria visual e lógica de proventos “Atrasado”.
- Quando a data de pagamento passa e não existe confirmação de recebimento, o evento deixa de aparecer em “Meus proventos”, filtros, histórico, gráficos, distribuições e totais.
- O evento continua preservado no banco SQLite, sem exclusão física ou alteração do registro importado. Se posteriormente houver uma transação de recebimento confirmada pela Pluggy, ela continuará podendo aparecer no histórico como recebida.
- Pagamentos previstos para hoje permanecem em “A receber” até o fim do dia; previsões com data futura continuam normalmente.
- A filtragem é aplicada no backend e reforçada no frontend para impedir que dados antigos em cache reapareçam.
- O gráfico Evolução de Proventos voltou a ter somente duas séries: “Proventos recebidos” e “A receber”. Legenda, filtros de período, tabela e totais foram ajustados para refletir essa regra.
- O payload mensal agora contém somente `recebido`, `a_receber` e `total`; não existe mais valor ou contador de proventos atrasados.
- Incrementado `DASHBOARD_CACHE_FORMAT` para 10 para forçar a reconstrução do snapshot após a atualização.

## Testes e validação da versão 4.2.53
- Adicionados testes para descartar proventos não recebidos com data anterior ao dia local, preservando o registro original em memória e no banco.
- Testados eventos com pagamento previsto para hoje, para o futuro, datas inválidas e amortizações.
- Adicionada validação para que o resumo mensal ignore eventos com data de pagamento expirada.
- Atualizado o teste de integração do `/api/dashboard` para verificar que um evento MXRF11 vencido não aparece como previsão, não é contabilizado e não cria campos de atraso.
- Testado o contrato frontend para garantir ausência da terceira série, da legenda e dos filtros de atraso.
- Executados 133 testes Python disponíveis, todos aprovados, excluindo `test_regressions.py` porque Flask/Werkzeug não estão instalados neste ambiente; a rede indisponível impediu instalar a dependência para rodar a suíte de integração.
- Executadas cinco suítes JavaScript de runtime, todas aprovadas.
- Compilação sintática dos arquivos Python aprovada. A validação de configuração Docker e o build real ainda precisam ser concluídos no ambiente Docker de destino.
- O pacote foi atualizado para a versão 4.2.53; bancos de dados, tokens, credenciais, snapshots e registros de proventos não foram incluídos nem modificados no ZIP.

# Atualizações da versão 4.2.52

## Correção crítica do endpoint `/api/dashboard` (v4.2.52)
- Corrigido `NameError: monthly_values is not defined`, que fazia a rota `/api/dashboard` retornar HTTP 500 ao montar a resposta de proventos.
- Reintroduzida a construção mensal antes da serialização do dashboard, agora com as três categorias separadas: `recebido`, `a_receber` e `atrasado`.
- Adicionado total mensal consolidado sem misturar pagamentos atrasados com recebimentos efetivos.
- Incrementado `DASHBOARD_CACHE_FORMAT` para 9, invalidando snapshots antigos que poderiam não conter a nova estrutura mensal.
- Adicionados testes unitários de agregação mensal, validação de status e teste de regressão da rota Flask `/api/dashboard` com um pagamento vencido de MXRF11.

## Correção da classificação de proventos com pagamento vencido (v4.2.52)
- Eventos cuja data de pagamento já passou e que ainda não possuem confirmação não permanecem como `A receber`.
- Esses eventos são apresentados separadamente como `Atrasado · aguardando confirmação`, com indicação de dias em atraso.
- Proventos atrasados não são tratados como recebidos, não entram no lucro realizado e não integram a previsão futura.
- Com Pluggy operacional, o evento só passa a `Pago` quando houver transação Pluggy conciliada; falhas temporárias da Pluggy não ativam o fallback.
- Sem Pluggy operacional, APIs alternativas podem estimar recebimentos somente depois que o mês de pagamento termina; pagamentos vencidos dentro do mês atual continuam como atrasados até o fechamento do mês.
- O gráfico Evolução de Proventos ganhou uma terceira série para pagamentos atrasados; filtros da tabela e período permitem consultar esses eventos separadamente.
- A versão do cache de dashboard foi incrementada e a data local foi incluída na assinatura do snapshot. Assim, snapshots antigos são invalidados após a atualização e os status/dias de atraso são recalculados na virada de cada dia, sem novas chamadas externas apenas por esse motivo. O primeiro carregamento recalcula os dados e grava um novo snapshot; bancos e registros financeiros não são apagados.

## Correção do Retry-After nas chamadas diretas da Pluggy
- Criada a função compartilhada `_pluggy_apply_retry_cooldown(response)` para aplicar cooldown persistente às chamadas diretas da Pluggy.
- A autenticação (`_pluggy_api_key`) e as consultas (`_pluggy_request`) utilizam o mesmo tratamento.
- Respostas HTTP 408 e 425 respeitam `Retry-After` em segundos ou HTTP-date; quando o cabeçalho está ausente ou inválido, aplicam espera padrão de 30 segundos.
- Respostas HTTP 429 mantêm espera mínima de 60 segundos e respeitam valores superiores informados pelo provedor.
- Respostas HTTP 5xx respeitam `Retry-After` ou aplicam espera padrão de 30 segundos.
- O cooldown é persistido em `api_provider_state`, evitando novas chamadas antecipadas de autenticação ou API.
- Falhas transitórias não ativam o fallback de recebimentos nem alteram as regras de confirmação financeira da Pluggy.

## Testes da versão 4.2.52
- Adicionados oito testes funcionais de cooldown Pluggy para HTTP 408, 425, 429, 500 e 503.
- Testados `Retry-After` em segundos, HTTP-date, ausente e inválido.
- Verificado que autenticação e consultas Pluggy utilizam o mecanismo compartilhado.
- Executados 136 testes Python disponíveis, todos aprovados, excluindo o módulo `test_regressions.py` porque Flask/Werkzeug não estão instalados no ambiente de validação.
- Incluído em `test_regressions.py` um teste de integração da rota `/api/dashboard` com um pagamento MXRF11 vencido; esse teste requer Flask e permanece pendente de execução no build com dependências.
- Executadas cinco suítes JavaScript de runtime.
- Validada a compilação Python, YAML Docker Compose e integridade do ZIP.
- Build Docker real e chamadas reais à API Pluggy não foram executados neste ambiente.
- O bundle Chart.js completo continua não validado nesta geração; o pacote permanece em fallback offline.

# Atualizações da versão 4.2.47

## Respeito completo ao cabeçalho Retry-After
- Implementada a interpretação de `Retry-After` nos dois formatos padronizados: segundos de espera e data HTTP.
- Adicionada a função compartilhada `_retry_after_seconds()` para BRAPI, HG Brasil e Pluggy, incluindo autenticação Pluggy.
- Datas HTTP são convertidas para segundos restantes em UTC; cabeçalhos inválidos usam o intervalo padrão conservador.
- Mantido limite de espera de no mínimo 60 segundos para respostas 429, evitando tentativas antecipadas.
- Removidos limites máximos curtos que poderiam reconsultar um provedor antes do horário indicado por um `Retry-After` longo.

## Obtenção resiliente do Chart.js completo
- O build tenta obter Chart.js 4.6.1 do registry npm e de dois mirrors de distribuição.
- Implementadas tentativas repetidas para falhas transitórias de rede, HTTP 429 e HTTP 5xx.
- Todos os mirrors precisam fornecer exatamente o tarball compatível com o SRI SHA-512 fixado; nenhum bundle é aceito apenas por tamanho ou nome.
- Erros de integridade, pacote inválido e erros HTTP permanentes interrompem o build, sem fallback silencioso para artefato não confiável.
- Quando todos os registries estão temporariamente inacessíveis, o build mantém o `chart-fallback.js` e registra o estado `degraded`.
- Adicionado argumento Docker `CHARTJS_DOWNLOAD_ATTEMPTS` para ajustar o número de tentativas por registry.
- O bundle completo não pôde ser baixado nesta geração porque o ambiente de criação estava sem resolução DNS/rede externa; o status do pacote permanece corretamente em `fallback`.
- Foram executados 104 testes Python disponíveis (exceto `test_regressions.py`, que requer Flask ausente no ambiente) e cinco suítes JavaScript.

## Testes da versão 4.2.47
- Adicionados testes para `Retry-After` em segundos inteiros e fracionários.
- Adicionados testes para `Retry-After` em formato de data HTTP, data expirada, cabeçalho inválido e limites de segurança.
- Adicionados testes para confirmar que autenticação e requisições Pluggy, além dos provedores comuns, usam o parser compartilhado.
- Incluído o tratamento de `Retry-After` também em respostas HTTP 503 quando o provedor fornece esse cabeçalho.
- Adicionado teste de failover de registry npm para mirror com validação SRI.
- Adicionado teste de nova tentativa após HTTP 503 transitório no download do Chart.js.
- Adicionado teste de nova tentativa após HTTP 503 transitório.
- Mantidos os testes de fallback offline e rejeição de pacote com integridade inválida.

# Atualizações da versão 4.2.46


## Dashboard de consumo das APIs
- Removida a métrica “Orçamento interno: Desativado” quando `budget_enabled` está ausente ou é falso.
- A métrica de orçamento somente é renderizada quando o backend informa explicitamente `budget_enabled=true`.
- Reorganizado o painel para apresentar chamadas BRAPI e reaproveitamento de cache em duas colunas quando não existe orçamento interno ativo.
- Mantida a apresentação condicional de três colunas somente quando um orçamento estiver efetivamente habilitado.

## Testes da versão 4.2.46
- Adicionados testes para garantir que orçamento desativado não apareça no painel.
- Adicionado teste para garantir que a métrica só apareça quando habilitada explicitamente pelo backend.
- Mantidos os testes de ausência de cotas artificiais e de funcionamento dos contadores informativos.

## Healthcheck e estado degradado dos gráficos
- O endpoint `/api/health` mantém HTTP 200 quando o renderer alternativo está disponível e informa `status=degraded` no JSON.
- O modo estrito `/api/health?strict=1` retorna HTTP 503 quando o Chart.js completo não está disponível ou validado.
- O healthcheck do Docker utiliza `scripts/healthcheck.py` e o modo estrito, de modo que um build offline com renderer alternativo seja identificado como degradado pelo Docker, sem impedir o acesso normal à aplicação.
- O comando de healthcheck registra o estado, o renderer e o motivo da degradação nos logs de saúde do container.
- Quando o Chart.js completo passa pela validação de integridade, o endpoint estrito retorna HTTP 200.

## Persistência e recuperação da fila de proventos
- Criada a tabela `proventos_sync_queue_state` no banco `ativos.db` para persistir a execução atual, a solicitação pendente, os parâmetros, o resultado e os erros.
- A solicitação atual é gravada no SQLite antes de iniciar a thread de sincronização.
- Uma nova solicitação com universo de ativos diferente fica persistida como pendente, mantendo apenas a atualização pendente mais recente.
- Ao reiniciar o container ou o processo Gunicorn, uma sincronização que estava `running` é recuperada automaticamente.
- A recuperação reexecuta a sincronização usando as chaves idempotentes `event_key` dos eventos BRAPI, evitando duplicações.
- A execução atual é concluída antes da execução pendente, preservando a serialização das chamadas aos provedores.
- Estados e resultados permanecem disponíveis depois da reinicialização.
- A configuração Docker suportada continua usando um worker Gunicorn para garantir um único proprietário da fila em memória.

## Testes da versão 4.2.45
- Testada a persistência da execução atual e da solicitação pendente.
- Testada a recuperação automática de uma execução interrompida após reinicialização simulada.
- Testada a retomada sequencial da execução atual e da execução pendente.
- Testado o modo normal e estrito do endpoint de saúde para renderer `degraded` e Chart.js `ready`.
- Build Docker real e validação de uma reinicialização efetiva do container ainda precisam ser realizados no ZimaOS.

# Atualizações da versão 4.2.44

## Proventos recebidos sem Pluggy configurada
- Quando não existem credenciais Pluggy válidas e item ativo conectado, os eventos de proventos registrados pelas APIs alternativas podem ser apresentados como recebimentos estimados.
- Os recebimentos estimados são identificados na interface como `Pago · API`, com indicação de que não possuem confirmação Pluggy.
- Para respeitar a regra temporal da carteira, somente eventos de meses já encerrados entram no total de recebidos; eventos do mês atual permanecem em `A receber` durante todo o mês, mesmo que o dia publicado de pagamento já tenha passado.
- Lançamentos manuais e importações B3, isoladamente, nunca são promovidos a recebimentos estimados.
- Quando a Pluggy possui credenciais válidas e um item ativo, somente transações Pluggy confirmadas são consideradas recebimentos. Falhas temporárias da Pluggy ou problemas ao descriptografar credenciais não ativam fallback estimado.
- Previsões alternativas conciliadas com uma transação Pluggy não são contabilizadas duas vezes.

## Compatibilidade do cache SQLite
- Adicionado suporte à coluna legada obrigatória `cached_at` na tabela `api_response_cache`.
- A gravação preenche `cached_at` e `fetched_at`, preservando compatibilidade com bancos implantados que exigem qualquer uma dessas colunas.
- A migração sincroniza os timestamps legados e reconhece valores numéricos e ISO-8601 ao ler o cache.
- Os registros existentes são preservados, sem recriação ou limpeza da tabela.

## Agendador Pluggy
- O HTTP 409 de uma sincronização já em andamento é tratado como concorrência esperada, sem marcar a execução como falha nem iniciar backoff indevido de cinco minutos.

## Testes
- Adicionado teste para eventos alternativos de meses anteriores quando a Pluggy não está configurada.
- Adicionado teste para manter eventos com data de pagamento passada dentro do mês atual como `A receber`.
- Adicionado teste para impedir recebimentos alternativos quando a Pluggy está operacionalmente configurada.
- Adicionados testes para credenciais Pluggy ausentes, inválidas ou sem item ativo.
- Adicionados testes para banco SQLite legado com `cached_at NOT NULL` e `payload_json NOT NULL`.
- Adicionados testes para preservar pagamentos Pluggy distintos e a idempotência de reprocessamento.
- Build Docker real e sincronização funcional com provedores externos ainda precisam ser validados no ambiente de produção.


## APIs sem cotas artificiais locais
- Removidas cotas acumuladas locais por dia ou mês para BRAPI, HG Brasil e Dados B3.
- O painel de consumo apresenta estatísticas de chamadas e cache, sem bloquear novas consultas por orçamento interno.
- São respeitados os limites efetivamente comunicados pelos provedores, incluindo HTTP 429, `Retry-After` e períodos de indisponibilidade.
- Mantidos intervalos de ritmo configuráveis entre chamadas; esses intervalos não representam cotas acumuladas.

## Sincronização de proventos em segundo plano
- A sincronização de calendários BRAPI não bloqueia mais a resposta do dashboard.
- O dashboard, importação B3 e sincronização Pluggy enfileiram ou reaproveitam uma tarefa em segundo plano.
- A rota `POST /api/proventos/sincronizar` inicia a tarefa e `GET /api/proventos/sincronizar/status` informa seu estado.
- Alterações no universo de tickers são processadas em uma execução pendente após a tarefa atual.

## Conciliação Pluggy de JCP e pagamentos líquidos
- A conciliação considera os valores `amount`, `grossAmount` e `netAmount` quando fornecidos no registro externo.
- O valor líquido (`netAmount`) é utilizado como valor efetivamente creditado quando disponível; os valores brutos continuam disponíveis para conciliar previsões anunciadas.
- Mantidos critérios de compatibilidade por ticker, data, categoria e data-com.
- Uma transação Pluggy atualiza somente a melhor previsão correspondente.

## Empacotamento e gráficos
- Removidos arquivos temporários, `.pytest_cache`, `__pycache__` e `.pyc` do pacote de distribuição.
- O build tenta obter Chart.js 4.6.1 e validar sua integridade SHA-512. Se o registry estiver indisponível, permanece em modo `fallback` e o estado `degraded` é informado; o bundle completo não é declarado como validado nessa condição.

## Testes da versão 4.2.44
- Adicionados testes para ausência de cotas artificiais locais.
- Adicionados testes para enfileiramento, consolidação e execução em segundo plano da sincronização de proventos.
- Adicionados testes para conciliação de JCP entre valores brutos e líquidos.
- Testadas as rotas de sincronização manual e consulta de status.
- Build Docker real e sincronização com provedores externos precisam ser validados no ambiente de produção.


# Atualizações da versão 4.2.41

## Compatibilidade do cache SQLite
- Corrigida a incompatibilidade com bancos persistentes que possuem `payload_json TEXT NOT NULL` na tabela `api_response_cache`.
- A migração preserva a coluna legada e adiciona/atualiza `payload` sem recriar a tabela nem apagar registros.
- As novas gravações sincronizam `payload` e `payload_json`, atendendo às restrições NOT NULL de instalações antigas.
- Leituras antigas que ainda não possuam `payload` utilizam o conteúdo válido de `payload_json` como fallback.
- A leitura do cache reconhece as duas representações e reaproveita respostas antigas válidas.

## Confirmação de proventos exclusivamente pela Pluggy
- O card de proventos recebidos e o lucro total consideram somente eventos vinculados a um ID externo de transação Pluggy importado e confirmado.
- Registros manuais, planilhas B3 e eventos de outras APIs não são considerados recebimentos apenas porque a data de pagamento passou.
- Eventos BRAPI permanecem como `A receber` até que uma transação Pluggy compatível seja conciliada.
- Na inicialização, registros antigos marcados automaticamente como `Pago` são rebaixados para `A receber` quando não existe correspondência Pluggy confirmada.
- Ao confirmar um pagamento pela Pluggy, a previsão correspondente da BRAPI é promovida para `Pago`, evitando que permaneça duplicada nas previsões.
- IDs Pluggy distintos são preservados mesmo quando ticker, data e valor coincidem.
- Quando um evento Pluggy é conciliado com um lançamento manual/B3, a data e o valor do recebimento são calculados a partir do evento externo, não do lançamento usado apenas como vínculo.
- A conciliação reutiliza um registro manual/B3 não vinculado apenas uma vez; outro ID externo distinto gera outro registro confirmado.
- Eventos antigos anteriormente marcados como duplicados podem ser reprocessados em sincronizações futuras.

## Persistência do cache CDI/IPCA
- O cache de séries SGS agora é persistido atomicamente em `/data/sgs_cache.json`.
- O fallback recente de até sete dias sobrevive a reinicializações do container.
- Os dados persistidos passam por validação de formato, datas e valores numéricos antes de serem reutilizados.
- O arquivo é limitado em tamanho, escrito com permissão restrita e substituído atomicamente.

## Documentação e pacote
- Atualizada a versão do README, dos exemplos de healthcheck e dos metadados Docker/CasaOS para `4.2.41`.
- Removidos arquivos `__pycache__` e `.pyc` do pacote de distribuição.

## Testes
- Adicionados testes para schema legado com `payload_json NOT NULL`, migração preservando valores e gravação posterior no cache.
- Adicionados testes para excluir pagamentos não confirmados pela Pluggy.
- Adicionados testes para preservar dois IDs externos Pluggy distintos com mesmo ticker/data/valor.
- Adicionado teste para garantir que data e valor confirmados venham do registro Pluggy, mesmo quando o lançamento conciliado tiver pequena diferença.
- Adicionados testes para conciliar uma linha manual/B3 uma única vez e garantir idempotência em reprocessamentos.
- Adicionado teste para persistência e recuperação do cache SGS após reinicialização simulada.
- Build Docker real, sincronização com a API Pluggy e validação no ZimaOS ainda precisam ser realizados no ambiente de produção.


# Atualizações da versão 4.2.40

## Lucro total
- Corrigida a composição do card **Lucro total** para somar o ganho de capital atual aos dividendos e proventos efetivamente recebidos.
- O cálculo utiliza a mesma métrica de ganho de capital exibida no detalhamento do card, evitando usar o valor de `ganho` do último ponto histórico, que pode divergir do resultado atual.
- Proventos futuros e anunciados não entram nessa soma.
- Os dividendos não são adicionados novamente ao patrimônio total nem ao ganho de capital.

## Migração do cache de APIs
- Adicionada migração automática da coluna `payload` em `api_response_cache` para bancos SQLite criados por versões antigas.
- A migração também garante as colunas de metadados esperadas pelo cache, sem recriar a tabela.
- Os registros existentes são preservados. Linhas antigas que recebem payload vazio são tratadas como cache miss e atualizadas quando o provedor responder novamente.

## Robustez da consulta CDI/IPCA pelo SGS
- Implementadas até três tentativas para respostas transitórias vazias, não JSON ou erros HTTP do Banco Central.
- Adicionada espera progressiva entre tentativas.
- Implementado fallback para dados SGS previamente consultados e armazenados em memória por até sete dias quando o endpoint estiver indisponível.
- Se não houver resposta válida nem cache recente, o cálculo informa indisponibilidade do indexador, sem inventar taxa ou rendimento.

## Testes
- Adicionados testes de runtime para soma do ganho de capital com proventos recebidos, incluindo cenários de ganho negativo e valores inválidos.
- Adicionados testes de migração de `api_response_cache` preservando registros antigos.
- Adicionados testes de retry do SGS após resposta vazia e de fallback para cache recente.
- Compilação Python e testes JavaScript de runtime executados neste ambiente.
- Testes Python que dependem do Flask não puderam ser executados porque a dependência não estava instalada e o ambiente estava sem acesso ao PyPI.
- Build Docker e verificação real da API do Banco Central permanecem pendentes no ZimaOS.


## Histórico de versões anteriores

## Correção das métricas do gráfico Evolução do Patrimônio

- Corrigida a janela de distribuição aberta ao clicar em uma coluna do gráfico **Evolução de Proventos**, que podia permanecer vazia devido a uma referência JavaScript a uma variável inexistente (`side`).
- Corrigida a renderização do gráfico de rosca para apresentar a distribuição dos valores por ativo e por tipo.
- O clique em qualquer segmento da coluna mensal agora abre a distribuição do mês inteiro, somando proventos recebidos e valores a receber.
- O título e o total da janela passam a representar o mês selecionado, sem limitar a distribuição ao segmento azul ou claro que recebeu o clique.
- O modo **Por Ativo** apresenta a participação de cada ticker; o modo **Por Tipo** consolida os valores por classe de ativo.
- Mantidos os filtros de tipo de ativo e ativo selecionado ao construir a distribuição mensal.
- Incluído teste de runtime para validar os valores consolidados, as duas formas de agrupamento e a criação do gráfico de rosca.

**Gerenciador pessoal de investimentos | Docker / ZimaOS | Português (Brasil)**

A aplicação preserva a interface, as regras financeiras, as integrações, os dados e as credenciais existentes. As consultas às APIs utilizam cache persistente, controle de concorrência e intervalos de ritmo configuráveis. Não são aplicadas cotas artificiais locais por dia ou mês; limites oficiais e respostas reais HTTP 429 dos provedores continuam sendo respeitados. As respostas recentes podem ser reutilizadas após reinicializações e, durante indisponibilidades temporárias, respostas anteriores dentro da janela de contingência podem ser reaproveitadas. A sincronização Pluggy para novas instalações utiliza frequência padrão de quatro horas; preferências já salvas pelo usuário são preservadas.

## Atualizações da versão 4.2.39

- Corrigida a metodologia de capital aplicado no gráfico **Evolução do Patrimônio** para ações, FIIs e demais ativos negociados em bolsa.
- Vendas passam a reduzir o capital aplicado pelo custo médio das unidades vendidas, em vez de subtrair o valor financeiro de venda. Isso evita tratar lucro ou prejuízo realizado como alteração do custo das posições restantes.
- Implementada reconstrução histórica do custo médio a partir do saldo-base importado, revertendo compras e vendas para meses anteriores e aplicando operações posteriores à fotografia.
- Mantida a metodologia anterior para renda fixa, cujo comportamento já estava correto.
- O ganho de capital do mês corrente passa a ser calculado pela diferença entre valor atual e valor investido, alinhado aos cards da carteira e sem incluir proventos.
- Adicionados cinco testes de regressão para compras, vendas parciais, reconstrução a partir do saldo-base, posições totalmente vendidas e exclusão da renda fixa da nova regra.
- Build Docker real e comparação funcional no ZimaOS precisam ser confirmados pelo usuário.

## Atualizações da versão 4.2.37

- Corrigida a referência JavaScript inexistente que interrompia a renderização da janela de distribuição mensal.
- A distribuição aberta pelo gráfico agora inclui recebidos e a receber do mês selecionado, independentemente do segmento da coluna clicado.
- Implementados os gráficos de rosca **Por Ativo** e **Por Tipo**, com total consolidado, participação percentual e tooltip com valor.
- Adicionado teste de runtime para confirmar o detalhamento mensal consolidado.

## Atualizações da versão 4.2.36

- Corrigido o ciclo de bloqueio SQLite entre gravações da sincronização Pluggy e o registro de chamadas de API em conexão separada.
- Gravações locais pendentes são confirmadas antes da chamada Pluggy seguinte, mantendo a contabilização obrigatória e o bloqueio seguro em caso de falha.
- Operações agendadas só são atualizadas quando existe ao menos um registro vencido; conflitos transitórios no processamento de rotina não provocam HTTP 500 no dashboard.
- Mantido o WAL configurado na inicialização, sem reexecutar `PRAGMA journal_mode=WAL` em toda conexão web.
- Adicionada migração da coluna `endpoint` para bancos persistentes antigos nas tabelas `api_response_cache` e `api_request_log`.
- Gunicorn usa `/tmp` como HOME para seu socket de controle, evitando erro de permissão em `/root/.gunicorn`.
- Incluídos testes de regressão para as correções de concorrência e operações agendadas.
- Build Docker e sincronização real com a Pluggy precisam ser confirmados no ZimaOS.

## Atualizações da versão 4.2.35

- Proventos pendentes são considerados somente do mês atual em diante; eventos vencidos sem confirmação de pagamento deixam de compor previsões e totais.
- O gráfico de evolução passa a exibir os meses atuais e futuros com previsões válidas.
- Hover do gráfico seleciona a coluna mensal completa e apresenta tooltip consolidado com mês/ano, proventos a receber, recebidos e total.
- O renderer alternativo offline também suporta tooltip combinado em modo por índice.


## 1. Informações da aplicação

- **Versão:** 4.2.47 (`VERSION` é a fonte única da versão exibida no rodapé e enviada no User-Agent)
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
curl -i http://IP_DO_SERVIDOR:8085/api/health?strict=1
```

O endpoint normal informa `status=healthy` ou `status=degraded`. O endpoint estrito retorna HTTP 503 quando o bundle Chart.js completo não está disponível/validado; isso faz o healthcheck Docker identificar a degradação dos gráficos. O renderer alternativo continua permitindo o acesso à aplicação.

**Não apague nem recrie a pasta `/DATA/AppData/lonely-wallet/data`.** A atualização substitui o código e a imagem, não os bancos, tokens, credenciais ou configurações persistidos nesse volume.

## 3. Bancos e arquivos persistentes

| Arquivo | Finalidade |
|---|---|
| `ativos.db` | Operações, posições, proventos e dados da carteira |
| `tokens.db` | PIN, autenticação, credenciais e configurações sensíveis |
| `carteira.db` | Banco legado, preservado para migração/compatibilidade quando existir |
| `quotes_last_valid.json` | Últimas cotações válidas salvas |
| `dashboard_cache.json` | Snapshot completo do dashboard, invalidado quando os bancos mudam |
| `sgs_cache.json` | Séries SGS CDI/IPCA persistidas para fallback após indisponibilidade do Banco Central |
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
curl -i http://IP_DO_SERVIDOR:8085/api/health?strict=1
```

Se o status for `degraded` e o renderer for `fallback`, a aplicação continua acessível, mas o healthcheck Docker marca o container como degradado porque o Chart.js completo não foi validado. Reconstrua com acesso ao registry npm para obter Chart.js 4.6.1. Se o status for `unhealthy`, verifique banco SQLite, logs e permissões do volume. O healthcheck é executado por `scripts/healthcheck.py` e registra o estado/renderer nos logs de saúde.

Verifique também se a porta `8085` está livre e se o UID/GID `10001` possui acesso ao volume persistente.

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

## 12. Controle de consumo das APIs

- As chamadas e reutilizações de cache são registradas localmente para diagnóstico e acompanhamento.
- Não existem cotas artificiais por dia ou mês que bloqueiem chamadas à BRAPI, HG Brasil ou Dados B3.
- Limites oficiais, respostas HTTP 429, `Retry-After` e indisponibilidades reais dos provedores continuam sendo respeitados.
- O cache persistente SQLite é compartilhado entre threads e preservado após reinicializações do container.
- O cache padrão é de 30 minutos durante o pregão, 12 horas fora do pregão, 12 horas para proventos, 24 horas para histórico e 6 horas para a lista de ativos.
- A BRAPI mantém uma chamada por ticker por requisição no perfil padrão e intervalo de 350 ms entre chamadas, como controle de ritmo compatível com o plano gratuito configurado.
- O painel em **Integrações e APIs** apresenta somente estatísticas de chamadas e cache desta instalação; consultar o painel não chama os provedores.
- A frequência padrão da sincronização Pluggy para novas instalações é de 4 horas. As chamadas Pluggy são espaçadas em pelo menos 1 segundo, com o intervalo persistido entre reinicializações, e a API Key é reutilizada por aproximadamente 115 minutos.

Variáveis configuráveis no Compose:

```yaml
BRAPI_MAX_TICKERS_PER_REQUEST: "1"
BRAPI_REQUEST_GAP_SECONDS: "0.35"
PLUGGY_REQUEST_GAP_SECONDS: "1.0"
```

Os intervalos locais controlam somente o ritmo de envio. Não substituem as regras dos provedores nem criam limites acumulados de utilização.


### Sincronização de proventos em segundo plano

- A sincronização BRAPI e de calendários de proventos não é executada dentro da resposta HTTP do dashboard.
- O dashboard, a importação B3 e a sincronização Pluggy apenas enfileiram ou reaproveitam uma execução em andamento.
- A rota `POST /api/proventos/sincronizar` inicia a tarefa e retorna sem aguardar as chamadas de rede.
- O andamento e o resultado podem ser consultados em `GET /api/proventos/sincronizar/status`.
- Execuções concorrentes para a mesma carteira são consolidadas; mudanças no universo de tickers geram uma execução pendente após a atual.
- Erros são registrados no estado da tarefa, sem bloquear o carregamento do dashboard.
- A execução atual e uma execução pendente são persistidas na tabela `proventos_sync_queue_state` do banco `ativos.db`.
- Ao reiniciar o container, uma execução interrompida é retomada automaticamente; eventos já gravados são reaplicados de forma idempotente pela chave `event_key`.
- A configuração Docker suportada utiliza um único worker Gunicorn para manter um único proprietário da fila em memória.

### Histórico patrimonial em segundo plano

- A resposta inicial do dashboard não aguarda a reconstrução completa das séries históricas de preços.
- O servidor executa a reconstrução em worker e a interface consulta o endpoint `/api/performance-history` até a conclusão.
- O resultado é incorporado ao cache do dashboard somente quando a assinatura dos dados ainda corresponde à carteira processada.
- No plano gratuito da brapi, mantenha `BRAPI_MAX_TICKERS_PER_REQUEST=1`. Aumente apenas de acordo com o plano contratado e as permissões do endpoint.
- Séries ausentes permanecem sinalizadas como histórico incompleto; não são substituídas por preços de venda ou por retornos presumidos.


## Correções da versão 4.2.43

- O histórico de proventos recebidos inclui ativos vendidos; cards de posição atual continuam limitados aos ativos mantidos.
- A sincronização de proventos considera tipos de ativos presentes em operações executadas e saldos-base históricos, sem fazer uma chamada de cotação ao vivo por ativo vendido.
- A conciliação Pluggy/agenda de proventos exige ticker, data com tolerância máxima de um dia, diferença de valor de até R$ 0,02 e compatibilidade de categoria quando conhecida.
- Uma transação Pluggy marca apenas a previsão mais próxima, evitando pagar vários eventos parecidos de uma só vez.
- Removida a lista de posições financeiras pessoais embutida no código; migrações continuam reconhecendo snapshots de usuário já armazenados no banco.
- O pacote entregue usa o renderer alternativo porque o registry npm estava inacessível durante a geração. O Dockerfile tenta baixar Chart.js 4.6.1 e valida o SRI/SHA-256 durante o build; quando não há rede, o status correto é `degraded` e o fallback permanece ativo.


## Correção do Retry-After nas chamadas diretas da Pluggy
- Criada a função compartilhada `_pluggy_apply_retry_cooldown(response)` para aplicar o cooldown persistente às chamadas diretas da Pluggy.
- Autenticação (`_pluggy_api_key`) e consultas (`_pluggy_request`) aplicam o mesmo tratamento às respostas do provedor.
- Respostas HTTP 408 e 425 passam a respeitar `Retry-After` em segundos ou HTTP-date, com cooldown padrão de 30 segundos quando o cabeçalho não existe ou é inválido.
- Respostas HTTP 429 mantêm cooldown mínimo conservador de 60 segundos e respeitam períodos superiores enviados pelo provedor.
- Respostas HTTP 5xx continuam aplicando cooldown com o período informado pelo provedor ou 30 segundos por padrão.
- O cooldown é persistido em `api_provider_state`, impedindo novas chamadas de autenticação ou API antes do prazo definido.
- Falhas transitórias não ativam fallback de recebimentos nem alteram a regra de confirmação financeira Pluggy.

## Testes da versão 4.2.52
- Adicionados testes funcionais para HTTP 408, 425, 429, 500 e 503 nas regras de cooldown Pluggy.
- Testados `Retry-After` em segundos, HTTP-date, ausente e inválido.
- Verificado que autenticação e consultas Pluggy chamam o mecanismo compartilhado.
- Mantidos os testes anteriores de provedores, cache, fila de proventos, cálculos financeiros e renderer alternativo.
- A suíte completa `test_regressions.py` continua dependendo de Flask/Werkzeug, que não estão disponíveis neste ambiente.
- Build Docker real e chamadas reais à API Pluggy não foram executados neste ambiente.

## v4.2.49

- As consultas oficiais do Banco Central (SGS) e do IBGE/SIDRA utilizam o controle comum de provedores, com persistência de cooldown e respeito ao cabeçalho `Retry-After` em segundos ou HTTP-date.
- Respostas HTTP 408, 425, 429 e 5xx aplicam cooldown por provedor; dados antigos válidos podem ser utilizados dentro da janela de cache de contingência.
- Respostas 200 com JSON inválido são removidas do cache e recebem tentativas curtas, sem repetir chamadas bloqueadas por `Retry-After`.
- O healthcheck do Docker considera saudável o serviço quando o renderer fallback está disponível, mas continua imprimindo `degraded`; `/api/health?strict=1` permanece disponível para monitoração estrita.
- O vendoring do Chart.js respeita `Retry-After` dos registries/CDNs: aguarda somente períodos curtos e passa para outro mirror quando a espera indicada é longa, sem consultar novamente a mesma origem antes do prazo.

### Disponibilidade dos gráficos e healthcheck

O healthcheck do container considera a aplicação operacional quando o renderer alternativo está disponível, mas imprime o estado `degraded` para diagnóstico. O endpoint `/api/health?strict=1` continua retornando HTTP 503 nesse cenário para monitoramento estrito. Assim, a limitação gráfica não faz o Docker marcar como indisponível uma carteira cujo dashboard continua acessível.
