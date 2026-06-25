import sqlite3
conn = sqlite3.connect("portal.db")
conn.row_factory = sqlite3.Row

print("=== MOTOBOYS NO PORTAL ===")
rows = conn.execute(
    "SELECT m.id, m.nome, m.cpf, m.bloqueado_permanente, m.foto_path, "
    "       mol.tipo, mol.valido_ate, "
    "       (SELECT COUNT(*) FROM cadastros c WHERE c.motoboy_id=m.id AND c.situacao='ativo') as ativos "
    "FROM motoboys m "
    "LEFT JOIN motoboys_ol mol ON mol.motoboy_id=m.id "
    "ORDER BY m.nome"
).fetchall()

for r in rows:
    status = "BLOQUEADO" if r["bloqueado_permanente"] else ("ATIVO" if r["ativos"] > 0 else "inativo")
    foto = "sim" if r["foto_path"] else "nao"
    print(f"  [{status}] {r['nome']} | CPF: {r['cpf']} | tipo: {r['tipo']} | foto: {foto}")

print()
print(f"Total: {len(rows)} motoboys")
