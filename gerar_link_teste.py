"""Gera um link de selfie para um motoboy existente. Uso: python gerar_link_teste.py"""
import os
from dotenv import load_dotenv
load_dotenv()

import sqlite3, uuid
from datetime import date, timedelta

conn = sqlite3.connect("portal.db")
conn.row_factory = sqlite3.Row

print("=== Motoboys disponíveis ===")
rows = conn.execute("SELECT id, nome, cpf FROM motoboys ORDER BY nome").fetchall()
for r in rows:
    print(f"  [{r['id']}] {r['nome']} | {r['cpf']}")

print()
# Gera link para o primeiro motoboy ativo (ou escolha pelo id)
ativo = conn.execute(
    "SELECT m.id, m.nome, m.cpf FROM motoboys m "
    "JOIN cadastros c ON c.motoboy_id=m.id AND c.situacao='ativo' "
    "LIMIT 1"
).fetchone()

if not ativo:
    print("Nenhum motoboy ativo encontrado.")
    conn.close()
    exit()

mb_id = ativo["id"]
token = uuid.uuid4().hex[:16]
expira = (date.today() + timedelta(days=7)).isoformat()

conn.execute(
    "INSERT INTO selfie_links (token, motoboy_id, expira_em) VALUES (?,?,?)",
    (token, mb_id, expira)
)
conn.commit()
conn.close()

base = os.getenv("PORTAL_BASE_URL", "http://localhost:8501")
link = f"{base}/?page=selfie&token={token}"

print(f"Motoboy : {ativo['nome']} | CPF {ativo['cpf']}")
print(f"Token   : {token}")
print(f"Expira  : {expira}")
print()
print(f"LINK DE SELFIE:")
print(f"  {link}")
print()
print("Abra esse link no navegador (ou escaneie o QR se estiver no celular).")
