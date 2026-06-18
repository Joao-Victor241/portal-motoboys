"""
Regras de negócio do cadastro (as "barreiras" pedidas pela operação).

Centralizadas aqui para ficarem fáceis de auditar e ajustar:
  - CNH vencida bloqueia o cadastro;
  - validade ("válido até") não pode ser no passado;
  - limite de motoboys ativos por OL em cada loja;
  - limite global da OL;
  - motoboy com bloqueio permanente (cross-OL) não pode ser recadastrado.
"""

from datetime import date


def _para_data(valor) -> date | None:
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor)[:10])


def validar_cadastro(conn, ol_id, loja_id, cpf, cnh_venc, valido_ate, hoje=None):
    """
    Roda todas as barreiras. Devolve uma lista de erros (vazia = pode cadastrar).
    """
    hoje = hoje or date.today()
    erros = []

    # 1) Bloqueio permanente cross-OL: prevalece sobre qualquer OL.
    mb = conn.execute(
        "SELECT id, bloqueado_permanente, motivo_bloqueio FROM motoboys WHERE cpf = ?",
        (cpf,)
    ).fetchone()
    if mb and mb["bloqueado_permanente"]:
        motivo = mb["motivo_bloqueio"] or "sem motivo registrado"
        erros.append(f"Motoboy com BLOQUEIO PERMANENTE (motivo: {motivo}). "
                     "Só o Admin do Grupo Bueno pode liberar.")

    # 2) CNH vencida.
    venc = _para_data(cnh_venc)
    if venc and venc < hoje:
        erros.append(f"CNH vencida em {venc.strftime('%d/%m/%Y')}. Não pode cadastrar.")

    # 3) Validade do cadastro no passado.
    val = _para_data(valido_ate)
    if val and val < hoje:
        erros.append("A data 'válido até' já passou. Informe uma data futura.")

    # 4) Limite por OL nesta loja.
    lim_loja = conn.execute(
        "SELECT limite FROM ol_loja_limite WHERE ol_id = ? AND loja_id = ?",
        (ol_id, loja_id)
    ).fetchone()
    if lim_loja:
        ativos_loja = conn.execute(
            "SELECT COUNT(*) FROM cadastros WHERE ol_id = ? AND loja_id = ? AND situacao = 'ativo'",
            (ol_id, loja_id)
        ).fetchone()[0]
        if ativos_loja >= lim_loja["limite"]:
            erros.append(f"Limite desta OL nesta loja atingido "
                         f"({ativos_loja}/{lim_loja['limite']} motoboys ativos).")

    # 5) Limite global da OL.
    ol = conn.execute("SELECT limite_global FROM ols WHERE id = ?", (ol_id,)).fetchone()
    if ol and ol["limite_global"]:
        ativos_ol = conn.execute(
            "SELECT COUNT(*) FROM cadastros WHERE ol_id = ? AND situacao = 'ativo'",
            (ol_id,)
        ).fetchone()[0]
        if ativos_ol >= ol["limite_global"]:
            erros.append(f"Limite global da OL atingido "
                         f"({ativos_ol}/{ol['limite_global']} motoboys ativos).")

    return erros
