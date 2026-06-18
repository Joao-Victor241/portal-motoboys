# Portal de Motoboys

Portal de cadastro de motoboys das OLs (Operadores Logísticos) para a operação
logística de moto do Grupo Bueno. Ele orquestra dois sistemas:

- **ERP SIAC** — recebe os dados de motorista.
- **DMP Access II** (Dimep) — reconhecimento facial; recebe nome, CPF e foto.

O **PostgreSQL é a fonte da verdade**; SIAC e DMP recebem cópias via adaptadores
(`integracoes/`). Na Fase 1, o desenvolvimento usa um SQLite local (`portal.db`).

## Funcionalidades

- Login com 3 perfis: **admin** (governança), **OL** (cadastra os próprios
  motoboys) e **operador** (fila da unidade).
- Cadastro de motoboy com validações em tempo real (CPF, placa, datas BR).
- Leitura automática da **CNH por foto** (IA) para preencher os campos.
- Geração de **link de selfie** para o motoboy enviar a própria foto.
- Regras: limite por OL/loja, CNH vencida, validade, bloqueio permanente cross-OL.
- Integração real com o DMP (cadastro/bloqueio de pessoa).

## Como rodar

```bash
# 1. Instale as dependências
pip install -r requirements.txt

# 2. Crie o arquivo de configuração a partir do modelo e preencha
#    (copie .env.example para .env e coloque suas credenciais)

# 3. Suba o portal
python -m streamlit run app.py
```

Usuários de teste (criados na primeira execução): `admin/admin123`,
`ol_exemplo/ol123`, `operador/op123`.

## Configuração

Todas as credenciais ficam no arquivo `.env` (que **não** vai para o Git).
Veja `.env.example` para a lista de variáveis.

## Estrutura

| Arquivo | O que faz |
|---|---|
| `app.py` | Aplicação Streamlit (login, telas, roteamento por perfil) |
| `db.py` | Banco de dados e dados de exemplo |
| `auth.py` | Login (senha com bcrypt) |
| `regras.py` | Regras de negócio / barreiras do cadastro |
| `validacoes.py` | Validação de CPF e placa |
| `integracoes/dmp_client.py` | Adaptador da API do DMP Access II |
| `integracoes/cnh_ocr.py` | Leitura da CNH por IA |
| `db/schema.sql` | Modelo de dados (PostgreSQL, alvo de produção) |
| `diagnostico_dmp.py` | Teste de conexão com o DMP |

## Status

Fase 1 (cadastro + regras + integração DMP + leitura de CNH) concluída.
Pendências: API do SIAC, conexão do PostgreSQL de produção, e a chave
`ANTHROPIC_API_KEY` para a leitura de CNH.
