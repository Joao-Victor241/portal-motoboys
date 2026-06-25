"""Cria credencial FACE para um CPF no DMP real e verifica o resultado."""
import os, json, sys
from dotenv import load_dotenv
load_dotenv()
import requests
from integracoes.dmp_client import DMPClient

CPF = sys.argv[1] if len(sys.argv) > 1 else "64595574460"  # João Victor

d = DMPClient(simulado=False)
d.logon()
H = d._auth()
BASE = d.base_url

print(f"=== ANTES: pessoa CPF {CPF} ===")
g = requests.get(f"{BASE}/api/v1/Person/{CPF}", headers=H, timeout=30)
if g.status_code != 200 or not g.text.strip():
    print(f"  Pessoa NAO encontrada no DMP (status {g.status_code}). Abortando.")
    sys.exit(1)
js = g.json()
pessoa = js[0] if isinstance(js, list) else js
pid = pessoa.get("Id")
print(f"  Id={pid} | Nome={pessoa.get('Name')} | FaceUse atual={pessoa.get('CredentialNumberForFaceUse')}")

print("\n=== Executando criar_credencial_face ===")
res = d.criar_credencial_face(CPF, person_id=pid)
print("  resultado:", res)

print("\n=== DEPOIS: credencial (GET /Credential) ===")
c = requests.get(f"{BASE}/api/v1/Credential/{CPF}", headers=H, timeout=30)
print(f"  Status {c.status_code}")
if c.status_code == 200:
    cc = c.json()
    print(f"  Number={cc.get('Number')} | Tipo={cc.get('CredentialType')} | Tecnologia={cc.get('TechnologyType')} | Status={cc.get('CredentialStatus')}")

print("\n=== DEPOIS: associacao (GET /PersonCredential) ===")
pc = requests.get(f"{BASE}/api/v1/PersonCredential/{pid}", headers=H, timeout=30)
if pc.status_code == 200:
    for a in pc.json():
        print(f"  CredentialNumber={a.get('CredentialNumber')} | ForFaceUse={a.get('ForFaceUse')} | ForREPUse={a.get('ForREPUse')}")

print("\n=== DEPOIS: pessoa (CredentialNumberForFaceUse) ===")
g2 = requests.get(f"{BASE}/api/v1/Person/{CPF}", headers=H, timeout=30).json()
p2 = g2[0] if isinstance(g2, list) else g2
face = p2.get("CredentialNumberForFaceUse")
print(f"  CredentialNumberForFaceUse={face}")
if face:
    print("\n>>> ✅ PRONTO PARA A CATRACA (credencial associada ao FACE).")
else:
    print("\n>>> ⚠️ FaceUse nao ficou setado — revisar.")
