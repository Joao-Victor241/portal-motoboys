"""
Sincronização inicial: envia todos os motoboys cadastrados no portal para o DMP.

Para cada motoboy:
  - Se tiver acesso ativo    → PersonSituation = 10 (ACESSO PERMITIDO)
  - Se estiver bloqueado ou inativo → PersonSituation = 11 (ACESSO BLOQUEADO)

Uso:
    python sincronizar_dmp.py           # mostra o que vai fazer (dry-run)
    python sincronizar_dmp.py --aplicar # aplica de verdade no DMP
"""
import sys
import time
import sqlite3
from dotenv import load_dotenv

load_dotenv()

from integracoes.dmp_client import DMPClient
import requests

APLICAR = "--aplicar" in sys.argv

conn = sqlite3.connect("portal.db")
conn.row_factory = sqlite3.Row

# Coleta motoboys únicos (pode haver duplicatas por JOIN com mol)
vistos = set()
motoboys = []
for r in conn.execute(
    "SELECT DISTINCT m.id, m.nome, m.cpf, m.bloqueado_permanente, m.telefone, "
    "       (SELECT COUNT(*) FROM cadastros c WHERE c.motoboy_id=m.id AND c.situacao='ativo') as ativos "
    "FROM motoboys m ORDER BY m.nome"
).fetchall():
    if r["id"] not in vistos:
        vistos.add(r["id"])
        motoboys.append(dict(r))

conn.close()

dmp = DMPClient(simulado=False)

print("=" * 65)
print("SINCRONIZACAO PORTAL -> DMP ACCESS II")
print("=" * 65)
print(f"Modo: {'APLICAR (escrevendo no DMP)' if APLICAR else 'DRY-RUN (sem escrever)'}")
print(f"Total de motoboys: {len(motoboys)}")
print()

if APLICAR:
    print("Fazendo logon...")
    dmp.logon()
    print(f"OK — bearer obtido.\n")

ok = erro = ja_ok = 0

for mb in motoboys:
    cpf     = mb["cpf"]
    nome    = mb["nome"]
    tel     = mb.get("telefone")
    bloq    = mb["bloqueado_permanente"]
    ativo   = mb["ativos"] > 0

    situacao    = dmp.situ_permitido if ativo else dmp.situ_bloqueado
    situ_label  = "PERMITIDO (10)" if situacao == dmp.situ_permitido else "BLOQUEADO (11)"
    motivo      = "ativo" if ativo else ("bloqueado permanente" if bloq else "inativo/suspenso")

    print(f"  {nome} | CPF {cpf} | {motivo} → {situ_label}")

    if not APLICAR:
        continue

    try:
        # Tenta verificar se já existe no DMP
        cpf_num = "".join(filter(str.isdigit, cpf))
        gr = requests.get(
            f"{dmp.base_url}/api/v1/Person/{cpf_num}",
            headers=dmp._auth(), timeout=30
        )

        if gr.status_code == 200 and gr.json():
            # Já existe → atualiza situação com PUT
            resultado = dmp.liberar_pessoa(cpf, nome) if situacao == dmp.situ_permitido \
                        else dmp.bloquear_pessoa(cpf, nome)
            print(f"    ✔ Atualizado no DMP")
            ok += 1
        else:
            # Não existe → cadastra com POST
            resultado = dmp.cadastrar_pessoa(cpf, nome, telefone=tel)
            # Se a situação for BLOQUEADO, aplica o bloqueio logo em seguida
            if situacao == dmp.situ_bloqueado:
                time.sleep(0.5)
                dmp.bloquear_pessoa(cpf, nome)
            print(f"    ✔ Cadastrado no DMP (novo)")
            ok += 1

        time.sleep(0.3)  # respeita rate-limit da API

    except requests.HTTPError as e:
        print(f"    ✗ HTTP {e.response.status_code}: {e.response.text[:120]}")
        erro += 1
    except Exception as e:
        print(f"    ✗ Erro: {e}")
        erro += 1

print()
print("=" * 65)
if APLICAR:
    print(f"Resultado: {ok} sincronizados, {erro} erros")
else:
    print("Dry-run concluído. Execute com --aplicar para enviar ao DMP.")
print("=" * 65)
