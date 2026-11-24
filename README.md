# Lonely Wallet

Gerenciador pessoal de carteira de investimentos, executado em Docker no ZimaOS/CasaOS. A aplicação mantém os dados financeiros no servidor local e oferece uma interface web para acompanhar posições, operações, proventos e integrações.

## Instalação e execução

O projeto utiliza Docker Compose. A configuração padrão expõe a aplicação na porta `8085` do host e na porta `8080` do container.

```bash
docker compose up -d --build
docker compose logs -f --tail=100 lonely-wallet
```

## Persistência

Preserve integralmente o volume `/DATA/AppData/lonely-wallet/data:/data`. O diretório contém os bancos SQLite, tokens, arquivos de credenciais criptografadas e a chave necessária para acessá-los. Não remova nem recrie esse diretório durante atualizações.

## Segurança

- Não exponha diretamente a porta `8085` à internet.
- Para acesso remoto, utilize VPN ou proxy reverso com HTTPS e autenticação.
- Mantenha backups do diretório `/DATA/AppData/lonely-wallet/data` antes de atualizar.
- Nunca compartilhe tokens, credenciais ou arquivos de chave.

## Desenvolvimento

- Backend: Python/Flask e SQLite.
- Interface: HTML, CSS e JavaScript.
- Container: Gunicorn com worker `gthread`.
- Fuso horário padrão: `America/Sao_Paulo`.

## Versão

Versão do pacote: `5.0.0`.

### Destaques da versão 5.0.0

- Removido da página Proventos o resumo técnico de eventos importados/ignorados e erros da sincronização.
- Removida da página Integrações e APIs a seção visual de consumo de chamadas e cache dos provedores.
- Simplificado o cartão Pluggy: a recomendação aparece como selo verde `(Recomendado)` ao lado do nome, mantendo o cartão no mesmo tema visual dos demais provedores.
- Incrementado `DASHBOARD_CACHE_FORMAT` para `19`.

A documentação de alterações de cada release é entregue separadamente do ZIP de distribuição.
