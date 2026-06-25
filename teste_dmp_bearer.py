"""
Teste rápido de integração DMP Access II.
Roda fora do Streamlit. Use: python teste_dmp_bearer.py
"""
import os
from dotenv import load_dotenv
load_dotenv()

from integracoes.dmp_client import DMPClient

dmp = DMPClient(simulado=False)

print("=" * 60)
print("TESTE DE INTEGRAÇÃO DMP ACCESS II")
print("=" * 60)

# 1) Logon
print("\n[1] Logon...")
bearer = dmp.logon()
print(f"    OK — bearer: {bearer[:50]}...")

# 2) Buscar pessoa fictícia (deve retornar 404 ou lista vazia)
print("\n[2] GET /api/v1/Person/{cpf} — CPF fictício...")
import requests
r = requests.get(
    f"{dmp.base_url}/api/v1/Person/00000000000",
    headers=dmp._auth(),
    timeout=30
)
print(f"    Status: {r.status_code}")
if r.status_code not in (200, 404):
    print(f"    Body: {r.text[:200]}")

# 3) GET sem filtro — lista primeiras pessoas cadastradas
print("\n[3] GET /api/v1/Person — listar pessoas (top 5)...")
r2 = requests.get(
    f"{dmp.base_url}/api/v1/Person",
    headers=dmp._auth(),
    params={"pageSize": 5, "pageIndex": 0},
    timeout=30
)
print(f"    Status: {r2.status_code}")
if r2.status_code == 200:
    dados = r2.json()
    pessoas = dados if isinstance(dados, list) else dados.get("items", dados.get("Items", []))
    print(f"    Pessoas retornadas: {len(pessoas) if isinstance(pessoas, list) else '(não é lista)'}")
    if isinstance(pessoas, list):
        for p in pessoas[:3]:
            print(f"      - {p.get('Name','?')} | CPF: {p.get('Cpf','?')} | Situação: {p.get('PersonSituation','?')}")
else:
    print(f"    Body: {r2.text[:300]}")

# 4) GET /api/v1/AccessLog/Pointer/0 — últimos eventos
print("\n[4] GET /api/v1/AccessLog/Pointer/0 — eventos recentes...")
r3 = requests.get(
    f"{dmp.base_url}/api/v1/AccessLog/Pointer/0",
    headers=dmp._auth(),
    timeout=30
)
print(f"    Status: {r3.status_code}")
if r3.status_code == 200:
    ev = r3.json()
    eventos = ev if isinstance(ev, list) else ev.get("AccessLogs", [])
    print(f"    Eventos: {len(eventos) if isinstance(eventos, list) else '(estrutura diferente)'}")
    if isinstance(eventos, list):
        for e in eventos[:2]:
            print(f"      - {e}")
else:
    print(f"    Body: {r3.text[:300]}")

print("\n" + "=" * 60)
print("Teste concluído.")
