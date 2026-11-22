# Lonely Wallet

## Histórico de versões

### v4.2.11
- Ajustes visuais e melhorias no painel de gerenciamento Pluggy.

### v4.2.12
- Melhorias nas tentativas de carregamento inicial e no comportamento do painel Pluggy.

### v4.2.13
- Tooltip informativo na coluna Rentabilidade, com valor monetário correspondente ao retorno da posição.
- Prioridade ao retorno monetário consolidado da Pluggy, com alternativa baseada em preço e proventos recebidos quando não houver histórico monetário.
- Tooltips de Rentabilidade e Variação independentes, preservando o comportamento original da variação diária.

### v4.2.14
- Ajustes de tooltip e compatibilidade na coluna Rentabilidade.

### v4.2.15
- Versão intermediária. Este README não contém um resumo técnico confiável das alterações dessa versão.

### v4.2.16
- Migração SQLite por API de backup, validação de integridade e melhorias no entrypoint.

### v4.2.17
- Correções de segurança dos argumentos JavaScript, CSP e carregamento local do Chart.js durante a construção da imagem.
- Sincronização automática Pluggy movida para o servidor, com controle de concorrência e frequência persistida; a API exige `interval_minutes`.
- Validação cronológica das quantidades em operações de venda, rotação dos 10 backups preventivos mais recentes e sanitização de erros externos.
- Migração SQLite publicada por arquivo temporário somente após validação de integridade.
- Testes de migração, rotação e isolamento dos dados de teste alinhados com a implementação.
- Fórmulas existentes de rentabilidade, variação diária e proventos preservadas sem alterações.

# Lonely Wallet v4.2.17

**Carteira pessoal de investimentos | Aplicação web self-hosted | Docker / ZimaOS**

> Guia do usuário, instalação, atualização, segurança, backup, solução de problemas e perguntas frequentes.

- **Versão:** 4.2.17
- **Idioma da interface:** Português (Brasil)
- **Backend:** Python 3.12 + Flask + Gunicorn
- **Persistência:** SQLite e arquivo JSON de última cotação válida
- **Porta padrão:** `8085` no host, `8080` no container
- **Diretório persistente:** `/DATA/AppData/lonely-wallet/data` no host, montado em `/data` no container

---

## Atualização 4.2.7

- Os indicadores e valores financeiros ficam ocultos enquanto as cotações e os indexadores atuais são confirmados.
- A confirmação dos valores atuais é separada da sincronização complementar de proventos e do histórico de rentabilidade, que continuam em segundo plano.
- A lista de oportunidades de compra exige preço atual abaixo do preço médio e participação atual abaixo da participação ideal.
- O rodapé e os metadados do Compose identificam a versão 4.2.7.

## Atualização 4.2.9

- Reorganização do modal **Gerenciar API Pluggy** em duas áreas horizontais: frequência de sincronização e exclusão de dados importados.
- Modal ampliado para aproveitar a largura disponível, mantendo adaptação para telas menores.
- Salvamento da frequência com confirmação de leitura após a gravação no SQLite.
- Requisição de salvamento isolada da atualização geral do dashboard para evitar que a interface seja recriada durante a alteração.
- Configuração carregada do servidor na inicialização e ao abrir o modal, com cache HTTP desativado para essa preferência.
- O seletor retorna à última frequência salva quando ocorre erro no salvamento.

## Atualização 4.2.8

- O último dashboard totalmente processado é salvo em `/data/dashboard_cache.json` e reaproveitado no F5 quando bancos, credenciais e arquivo de cotações não mudaram.
- Na primeira abertura sem cache válido, os valores permanecem ocultos até terminar a geração completa de cotações, indexadores, proventos e histórico.
- O bloqueio de carregamento cobre toda a janela e impede rolagem enquanto não houver valores confirmados.
- Cada conexão MeuPluggy recebeu um botão individual de sincronização, além do botão de sincronização geral.
- O timeout das requisições de sincronização Pluggy foi ampliado e o Gunicorn passou a aceitar solicitações longas.

## Atualização 4.2.8

- O último dashboard totalmente processado é salvo em `/data/dashboard_cache.json` e reaproveitado no F5 quando bancos, credenciais e arquivo de cotações não mudaram.
- Na primeira abertura sem cache válido, os valores permanecem ocultos até terminar a geração completa de cotações, indexadores, proventos e histórico.
- O bloqueio de carregamento cobre toda a janela e impede rolagem enquanto não houver valores confirmados.
- Cada conexão MeuPluggy recebeu um botão individual de sincronização, além do botão de sincronização geral.
- O tempo limite de sincronização Pluggy foi ampliado no navegador e no servidor.

## 1. O que é o Lonely Wallet

O Lonely Wallet é uma aplicação pessoal para registrar e acompanhar investimentos, posições, operações, proventos e informações auxiliares da carteira. É hospedado pelo próprio usuário e acessado pelo navegador.

**O Lonely Wallet não é corretora, custodiante, banco, consultoria ou serviço de recomendação de investimentos.** Os valores dependem dos lançamentos cadastrados, dos saldos importados e da disponibilidade, cobertura, frequência e regras dos provedores externos. Confira extratos, notas de corretagem e documentos oficiais antes de tomar decisões ou preparar declarações fiscais.

## 2. Novidades da versão 4.2.5

Esta versão é uma atualização de manutenção baseada na 4.2.1.

- O backup manual agora inclui `quotes_last_valid.json`, além dos bancos SQLite.
- O cache de cotações é validado como JSON antes de ser incluído no backup.
- O arquivo de cache é copiado com permissões restritas no diretório de backup.
- Corrigidas as referências de versão no guia de atualização e no Compose para 4.2.5.
- Documentação reorganizada com procedimentos de instalação, atualização, restauração, segurança e perguntas frequentes.

### Como interpretar a última cotação salva

Quando os provedores não retornam um preço válido, o sistema pode usar a última cotação válida persistida em `/data/quotes_last_valid.json`. Esse valor é uma **referência histórica**, não uma cotação em tempo real. A propriedade `quoteLastUpdated` representa o momento em que a aplicação salvou/observou o preço no cache; não deve ser interpretada como confirmação do horário oficial de negociação informado pela bolsa ou pelo provedor.

## 3. Requisitos

- Servidor Linux compatível com Docker e Docker Compose (ZimaOS é suportado pelo Compose incluído).
- Arquitetura `amd64` conforme metadados do pacote.
- Porta TCP `8085` disponível no host.
- Espaço em disco para bancos, cache e backups.
- Acesso à rede para consultar provedores externos e, conforme a interface, carregar bibliotecas de terceiros.

## 4. Instalação e atualização no ZimaOS

### Instalação inicial

1. Extraia o ZIP em uma pasta de projeto no servidor.
2. Confira o `docker-compose.yml` e ajuste o endereço do ícone se necessário. O IP `192.168.31.79` presente no exemplo é específico de uma rede local e pode ser alterado.
3. Confirme que a pasta persistente existe: `/DATA/AppData/lonely-wallet/data`.
4. Na pasta que contém o Compose, execute:

```bash
docker compose up -d --build
```

5. Abra `http://IP_DO_SERVIDOR:8085` no navegador.
6. Confira a saúde e a versão:

```bash
curl http://IP_DO_SERVIDOR:8085/api/health
```

O retorno deve indicar `ok: true` e `version: 4.2.17`.

### Atualização de uma versão anterior

1. Faça um backup antes de substituir os arquivos.
2. Extraia o pacote 4.2.17 em uma pasta de projeto.
3. **Mantenha exatamente o volume de dados existente**. O Compose padrão utiliza:

```yaml
volumes:
  - /DATA/AppData/lonely-wallet/data:/data
```

4. Não apague, renomeie ou recrie a pasta de dados para atualizar o código.
5. Execute `docker compose up -d --build`.
6. Confira `/api/health`, faça login e valide a carteira, as cotações, as operações e os proventos.

> Não altere o caminho do volume sem planejar uma migração. Um caminho novo e vazio pode fazer a aplicação parecer recém-instalada, mesmo que os dados antigos continuem no diretório anterior.

## 5. Persistência: o que existe em `/data`

Os arquivos podem variar conforme o histórico de instalação e migrações. Os principais são:

| Arquivo | Finalidade |
|---|---|
| `ativos.db` | Banco principal de ativos/operações na configuração atual |
| `tokens.db` | PIN, autenticação e dados de credenciais conforme configuração |
| `carteira.db` | Banco legado, mantido para compatibilidade/migração quando existente |
| `quotes_last_valid.json` | Última cotação válida salva por ativo |
| `.api-credentials.key` | Chave usada para proteger credenciais de API, quando criada |
| `.lonely-wallet-session.key` | Segredo persistente usado para sessões, quando criado |
| `backups/` | Backups preventivos gerados antes de exclusões em massa |

**Não edite esses arquivos manualmente com a aplicação em execução.** Faça cópias consistentes e mantenha as chaves protegidas.

## 6. Backup manual

O script está disponível no pacote e é copiado para a imagem Docker em `/srv/scripts/backup.py`.

### Executar dentro do container

Monte um destino persistente em `/backup` (ou escolha outro destino) e execute:

```bash
docker exec lonely-wallet python /srv/scripts/backup.py --destination /backup
```

O script cria uma pasta `lonely-wallet-AAAAmmdd-HHMMSS` com os bancos SQLite existentes e, se presente, `quotes_last_valid.json`. Os bancos são copiados pela API de backup do SQLite e passam por `PRAGMA integrity_check`. O cache JSON é validado antes da cópia.

### Backup incluindo chaves

Por padrão, as chaves **não** são incluídas. Isso reduz o risco de um backup comum carregar junto os segredos que protegem as credenciais. Porém, sem a chave original, credenciais criptografadas podem não ser recuperáveis em outro servidor.

Se precisar migrar a instalação e recuperar credenciais criptografadas, use a opção abaixo somente para um destino criptografado, privado e com controle de acesso:

```bash
docker exec lonely-wallet python /srv/scripts/backup.py --destination /backup-seguro --include-keys
```

O script coloca as chaves em uma subpasta `secrets/` com permissões restritas. Proteja o conjunto inteiro do backup. Não envie esse diretório por e-mail, mensageiros ou armazenamento público.

### Restaurar um backup

1. Pare o container: `docker compose down`.
2. Faça uma cópia de segurança do diretório `/DATA/AppData/lonely-wallet/data` atual.
3. Restaure os bancos e `quotes_last_valid.json` para o diretório persistente correto.
4. Se a migração exigir recuperar credenciais criptografadas, restaure também as chaves correspondentes do backup protegido. Não substitua chaves por outras aleatórias.
5. Confira permissões dos arquivos e inicie: `docker compose up -d`.
6. Verifique `/api/health`, autenticação e dados da carteira.

A restauração pode exigir que os arquivos sejam copiados para o diretório do host que está montado em `/data`. Não restaure bancos de uma versão mais nova sobre uma versão antiga sem avaliar compatibilidade.

## 7. Cotações e provedores externos

- A aplicação tenta consultar os provedores configurados e disponíveis para o ativo.
- A cobertura varia por ticker, tipo de ativo, plano, token, limite de requisições e disponibilidade do provedor.
- Se não houver preço válido, a aplicação pode recorrer à última cotação válida armazenada.
- Uma cotação recuperada do cache é histórica e pode estar desatualizada.
- Se o ativo nunca teve uma cotação válida salva e os provedores não retornarem preço, a interface pode mostrar **Sem cotação**.
- Não trate cache histórico como preço de execução, preço oficial de fechamento ou valor garantido de liquidação.

### O que fazer quando aparece “Sem cotação”

1. Confirme se o ticker está correto e corresponde ao ativo esperado.
2. Verifique se o servidor tem acesso à internet e se os tokens/provedores configurados estão válidos.
3. Consulte os logs: `docker compose logs --tail=200 lonely-wallet`.
4. Tente novamente mais tarde, respeitando limites dos provedores.
5. Não cadastre um preço estimado como se fosse cotação oficial.

### Por que o preço pode estar diferente da corretora?

Provedores podem ter atraso, cobertura diferente, horário de atualização distinto, ajustes corporativos ou indisponibilidade. Compare ticker, mercado, moeda, data e horário. Para fins contábeis e fiscais, use os documentos oficiais pertinentes.

## 8. Importação B3 e operações

- Utilize planilhas exportadas pela B3 em formato compatível com a tela de importação.
- Revise a prévia antes de confirmar a importação.
- Mantenha os arquivos originais da B3 como comprovantes.
- A identificação de duplicidades combina fingerprint e critérios semânticos; ainda assim, confira o relatório de importação e os lançamentos após importar.
- Execuções parciais, operações no mesmo dia e aportes recorrentes devem ser conferidos individualmente.
- Antes de importar novamente uma planilha, verifique se ela já foi processada.

### Por que uma linha pode ser ignorada?

Pode ser reconhecida como duplicada, estar fora dos formatos reconhecidos ou não conter dados obrigatórios. Consulte o relatório da importação e compare com a planilha original. Não apague operações para “forçar” uma nova importação sem backup.

## 9. Saldos-base, Pluggy e vendas retroativas

Saldos-base e snapshots de integração representam posições existentes em uma data de referência. Eles podem ser necessários para validar vendas e reconstruir posições mesmo quando não há compras locais cadastradas.

Ao registrar uma venda retroativa, a validação considera a posição disponível na data da operação. Verifique se a data do saldo-base e a data da venda estão corretas. Sincronizações parciais ou snapshots incompletos podem afetar a reconstrução; compare sempre com extratos e notas.

## 10. Proventos, IRPF e DARF

- Confira ticker, tipo de evento, valor, data de competência e data de pagamento.
- Diferencie proventos recebidos de eventos futuros/anunciados.
- Evite importar o mesmo evento por mais de uma origem sem conferir a deduplicação.
- O resumo de IRPF é uma ferramenta auxiliar, não uma declaração oficial nem aconselhamento tributário.
- O custo fiscal pode considerar custos de compra e dedução proporcional em vendas, enquanto a posição operacional pode seguir regra distinta de preço médio. Consulte a documentação fiscal e um profissional habilitado antes de transmitir a declaração.
- Confira DARFs, códigos, competências, vencimentos e valores nos canais oficiais da Receita Federal.

## 11. PIN e segurança

- O PIN protege o acesso conforme a configuração habilitada.
- Mantenha o PIN privado e não o reutilize em outros serviços.
- O bloqueio de tentativas reduz tentativas automatizadas, mas não substitui proteção de rede.
- Evite expor a porta `8085` diretamente à internet. Para acesso remoto, prefira VPN ou proxy reverso com HTTPS e autenticação apropriada.
- Não compartilhe tokens de APIs, arquivos `.key`, bancos ou backups sem proteção.
- A configuração padrão do container mantém compatibilidade com volumes existentes e ainda executa como root. Restrinja o acesso ao host e ao diretório de dados.

## 12. Diagnóstico e solução de problemas

### A aplicação não abre

```bash
docker compose ps
docker compose logs --tail=200 lonely-wallet
```

Confirme a porta publicada, o estado do container e se o diretório persistente está acessível.

### A carteira parece vazia depois da atualização

- Confira se o volume continua apontando para `/DATA/AppData/lonely-wallet/data:/data`.
- Verifique se os arquivos de banco existem no host.
- Não crie um banco novo nem copie arquivos aleatórios por cima dos existentes.
- Pare o container antes de qualquer restauração.

### O PIN correto retorna bloqueio

Aguarde o período de bloqueio e tente novamente. Se houver outros dispositivos na rede, confirme que o endereço IP do cliente está sendo interpretado corretamente pelo ambiente. Não apague o banco de tokens como tentativa de correção.

### O healthcheck falha

Consulte `docker compose logs --tail=200 lonely-wallet` e acesse `/api/health`. Verifique se a aplicação iniciou, se os bancos estão acessíveis e se a versão informada é `4.2.5`.

### A importação B3 apresenta duplicatas ou ignora operações

Compare cada linha da prévia com a planilha original e com as operações já registradas. Guarde a planilha e o relatório de importação para análise. Não execute exclusão em massa sem backup.

### Os gráficos não carregam

O Chart.js é copiado para `app/static/vendor/chart.umd.min.js` durante a construção da imagem Docker pelo script `scripts/vendor_chartjs.py`. Se o arquivo não existir, verifique os logs de build e reconstrua a imagem. O navegador não precisa acessar um CDN para carregar os gráficos.

## 13. Perguntas frequentes

**Posso atualizar sem perder meus investimentos?**  
Sim, desde que mantenha o mesmo diretório persistente e faça backup antes da atualização. Não remova o volume `/DATA/AppData/lonely-wallet/data:/data`.

**O ZIP contém meus dados ou meu PIN?**  
Não deve conter o banco pessoal do servidor. Os dados persistentes ficam no volume `/data`. Nunca compartilhe um ZIP que tenha sido modificado para incluir bancos ou segredos.

**O backup inclui as últimas cotações?**  
Na versão 4.2.5, sim. O script inclui `quotes_last_valid.json` quando o arquivo existe e valida seu JSON antes de copiar.

**O backup inclui as chaves de criptografia?**  
Não por padrão. Use `--include-keys` somente quando necessário e guarde o backup em local criptografado e restrito.

**Se eu não incluir as chaves, perco os investimentos?**  
Os registros financeiros nos bancos são copiados normalmente. Porém, credenciais de APIs criptografadas podem não ser recuperáveis sem a chave original. Mantenha uma cópia segura das chaves para recuperação/migração.

**Por que um ativo mostra cotação desatualizada?**  
Porque o provedor não retornou uma cotação válida e a aplicação utilizou a última cotação salva. Esse preço não é em tempo real.

**Por que um ativo continua “Sem cotação”?**  
Porque não há cotação válida do provedor nem preço histórico válido salvo para aquele ticker.

**O Lonely Wallet envia ordens para a corretora?**  
Não. É um gerenciador de carteira e não executa ordens de compra ou venda.

**O resumo IRPF substitui o programa da Receita Federal?**  
Não. É um apoio de organização e conferência. Revise os documentos e as regras fiscais aplicáveis antes de declarar.

**Posso apagar os arquivos `.db` para resolver um erro?**  
Não. Isso pode causar perda de dados. Faça backup e investigue os logs antes de qualquer intervenção.

**Posso acessar pela internet?**  
Evite expor diretamente a porta. Prefira VPN ou proxy reverso com HTTPS, autenticação e regras de firewall.

**Onde encontro os logs?**  
Execute `docker compose logs --tail=200 lonely-wallet` na pasta do Compose.

## 14. Limitações conhecidas

- O container ainda executa como root para manter compatibilidade com bind mounts existentes do ZimaOS.
- O Chart.js é servido localmente pelo Flask após ser obtido no build da imagem; a construção exige acesso HTTPS ao jsDelivr. A execução normal do container não depende do CDN.
- Não há CSP estrita nesta versão porque o frontend ainda utiliza handlers inline.
- A cobertura de cotações depende dos provedores, tickers, planos e limites de API.
- Rentabilidade, benchmarks e reconciliação completa de snapshots exigem validação com dados reais representativos.
- Backups preventivos de exclusões ficam em `/data/backups/pre-exclusao-*`; monitore o espaço e defina retenção.

## 15. Testes e validação

Em ambiente Python 3.12 com dependências instaladas:

```bash
python -m py_compile app/main.py scripts/backup.py
python -m unittest discover -s tests -v
```

Para validar o backup em ambiente de teste, use uma cópia dos bancos e do cache, execute o script para um diretório de destino e confirme a presença de `quotes_last_valid.json`. Não use a primeira execução de teste para substituir dados de produção.

A validação completa exige ainda inicialização do container, login, compra/venda, venda retroativa, importação B3, proventos, IRPF/DARF, indisponibilidade de APIs, recuperação do cache e restauração de backup.

## 16. Estrutura do projeto

```text
.
├── Dockerfile
├── docker-compose.yml
├── README.md
├── VERSION
├── requirements.txt
├── scripts/
│   └── backup.py
├── tests/
│   └── test_regressions.py
└── app/
    ├── main.py
    ├── static/
    └── templates/
```

## 17. Suporte para diagnóstico

Ao pedir ajuda, informe:
- Versão exibida em `/api/health`.
- Mensagem de erro exata e horário aproximado.
- Nome/ticker do ativo, sem compartilhar dados pessoais desnecessários.
- Trecho relevante de `docker compose logs`.
- Se o problema começou após atualização, importação ou sincronização.

**Nunca envie PIN, tokens de API, arquivos `.key`, bancos completos ou backups sem remover/proteger informações sensíveis.**

---

**Lonely Wallet v4.2.5**  
Guia do usuário e referência de operação. Valores e informações são auxiliares e devem ser conferidos com as fontes oficiais aplicáveis.

## Atualização 4.2.5

Esta versão parte da linha 4.2.2 e mantém os bancos e arquivos persistentes em `/data`.

### Segurança do container

- O processo Gunicorn é executado como `app` (UID/GID 10001), sem privilégios de root.
- O entrypoint inicia como root apenas para migrar a propriedade do volume `/data` uma única vez. O marcador `/data/.lonely-wallet-permissions-v1` evita repetir a varredura em inicializações seguintes.
- Na primeira inicialização após esta atualização, a migração de propriedade pode levar algum tempo em volumes grandes. Não interrompa o container durante essa etapa.
- A migração altera a propriedade dos arquivos e diretórios dentro de `/data` para UID/GID 10001. Faça backup antes da atualização.

### Backup

- Os backups são montados em uma pasta temporária no próprio destino.
- Bancos SQLite são copiados pela API de backup e passam por `PRAGMA integrity_check`.
- O cache `quotes_last_valid.json` é validado como objeto JSON antes de ser incluído.
- Um manifesto `manifest.json` registra data, arquivos incluídos e se as chaves foram exportadas.
- A pasta definitiva só aparece após a conclusão bem-sucedida. Falhas removem a pasta temporária.
- Para o destino ser gravável pelo usuário `app`, monte uma pasta de backup com permissões para UID/GID 10001.
- As chaves continuam excluídas por padrão. Use `--include-keys` somente em destino privado e protegido.

### Cabeçalhos de segurança e ícone

- A aplicação envia Content Security Policy com restrições para objetos, origem base, formulários e enquadramento. `unsafe-inline` permanece temporariamente habilitado por causa dos handlers inline existentes; portanto, não é uma CSP estrita.
- O ícone do catálogo usa caminho relativo `/static/lonely-wallet-logo.png`, evitando dependência do IP `192.168.31.79`.

### Atualização no ZimaOS

Preserve o mapeamento persistente `/DATA/AppData/lonely-wallet/data:/data`. Faça uma cópia de segurança antes de atualizar. A primeira inicialização pode demorar devido à migração de propriedade do volume.
