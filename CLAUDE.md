# Portal de Motoboys

Portal de cadastro de motoboys das OLs (Operadores Logísticos) para a operação
logística de moto do Grupo Bueno. Orquestra dois sistemas existentes:

- **ERP SIAC** — recebe os dados de motorista (tela "Atualização de Motoristas").
- **DMP Access II** (Dimep) — sistema de reconhecimento facial; recebe nome, CPF e foto.

O **PostgreSQL é a fonte da verdade**; SIAC e DMP recebem cópias via adaptadores
(`integracoes/`). Assim, se uma integração cair, o cadastro não se perde.

## Perfis
- **admin** (Grupo Bueno): governança total, limites por OL/loja, bloqueio permanente cross-OL.
- **ol**: cadastra SÓ os próprios motoboys. Nada mais.
- **operador**: vê fila FIFO e motos disponíveis. Sem cadastro.

## Regras de negócio
- Limite de motoboys ativos por OL e por OL/loja (configurável pelo admin).
- Bloqueia cadastro com CNH vencida.
- Cadastro tem validade ("válido até"); vencido bloqueia acesso.
- Bloqueio permanente de um motoboy prevalece sobre qualquer OL (cross-OL).
- Identidade do motoboy é única por CPF (mesmo motoboy pode servir várias OLs).

## Estrutura
- `app.py` — entrada do Streamlit (a fazer).
- `db/schema.sql` — modelo de dados.
- `integracoes/dmp_client.py` — adaptador do DMP (roda em modo simulado por enquanto).
- `integracoes/siac_client.py` — adaptador do SIAC (a fazer, quando vier a API).
- `diagnostico_dmp.py` — testa conectividade com o DMP.

## Integração DMP — RESOLVIDA (validada ao vivo em 18/06/2026)
A API do DMP está integrada de verdade (`integracoes/dmp_client.py`). Logon v1
funciona; a foto vai como base64 no campo `Photo` do cadastro de pessoa; IDs reais
no `.env` (estrutura 398, perfil 47, situação 10=permitido/11=bloqueado). O app
liga o modo real com `DMP_SIMULADO=false`. Teste controlado (criar+apagar) passou.

## Pendências que dependem de terceiros
1. **SIAC / TI**: endpoint e credenciais da API de cadastro de motorista.
2. **TI**: string de conexão do PostgreSQL (hoje rodando em SQLite local).
3. **Gabriela**: nomes reais das 5 lojas (hoje "Loja 01..05").

## Regras do projeto
- Credenciais SÓ no `.env` (está no `.gitignore`). Nunca versionar segredo.
- Código comentado em português. Commits em Conventional Commits.
- Repositório vai para a org GitHub privada `Dados-Pecista`.
