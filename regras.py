"""
Regras de negócio do cadastro (as "barreiras" pedidas pela operação).

CONCEITOS-CHAVE:
  - Cadastro: o registro do motoboy existe e é permanente. Nunca é apagado.
  - Situação de acesso: se o motoboy está PERMITIDO ou SUSPENSO no DMP/catracas.
    Valores: 'ativo' (acesso liberado) | 'inativo' (acesso suspenso).

Barreiras no momento do cadastro:
  - CNH vencida bloqueia o cadastro;
  - validade ("válido até") não pode ser no passado;
  - limite de motoboys com situação 'ativo' por OL em cada loja;
  - limite global da OL;
  - motoboy com bloqueio permanente (cross-OL) não pode ser recadastrado.

Rotina automática de vencimento:
  - buscar_free_vencidos(): retorna cadastros free cuja data valido_ate
    chegou ao horário de corte (18:30). Chamada em cada carregamento do app.
"""

from datetime import date, datetime, time as dtime, timezone, timedelta


def _agora_br() -> datetime:
    """Horário de Brasília (UTC-3). Na nuvem o relógio é UTC, então convertemos —
    senão o corte das 18:30 dispararia às 15:30 no horário local."""
    return (datetime.now(timezone.utc) - timedelta(hours=3)).replace(tzinfo=None)


def _para_data(valor) -> date | None:
    if not valor:
        return None
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor)[:10])


def validar_cadastro(conn, ol_id, loja_id, cpf, cnh_venc, valido_ate, hoje=None):
    """
    Barreiras para REGISTRO do motoboy (sem limite de quantidade — cadastro é ilimitado).
    loja_id pode ser None (cadastro é geral, sem loja).
    Devolve lista de erros (vazia = pode registrar).
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

    return erros


def validar_ativacao(conn, ol_id, loja_id, motoboy_id=None) -> list:
    """
    Barreiras para ATIVAR o acesso de um motoboy (situação → ativo).
    motoboy_id: quando fornecido, verifica se o motoboy já está ativo em outra loja.
    Devolve lista de erros (vazia = pode ativar).
    """
    erros = []

    # Regra: o motoboy só pode estar ativo em UM lugar por vez — em QUALQUER OL
    # ou loja. Se já está ativo (mesmo que por outra OL), não pode ser ativado
    # de novo. Isso impede que duas OLs rodem o mesmo motoboy ao mesmo tempo.
    # E FREE com validade vencida não pode ser ativado.
    if motoboy_id:
        ja_ativo = conn.execute(
            "SELECT l.nome AS loja, o.nome AS ol FROM cadastros c "
            "JOIN lojas l ON l.id = c.loja_id "
            "JOIN ols o ON o.id = c.ol_id "
            "WHERE c.motoboy_id = ? AND c.situacao = 'ativo' "
            "AND NOT (c.ol_id = ? AND c.loja_id = ?)",
            (motoboy_id, ol_id, loja_id)
        ).fetchone()
        if ja_ativo:
            erros.append(
                f"Este motoboy já está ativo em {ja_ativo['loja']} "
                f"(OL: {ja_ativo['ol']}). Ele só pode rodar em um lugar por vez — "
                "suspenda o acesso lá antes de ativar aqui."
            )

        mol = conn.execute(
            "SELECT tipo, valido_ate FROM motoboys_ol WHERE motoboy_id=? AND ol_id=?",
            (motoboy_id, ol_id)
        ).fetchone()
        if mol and mol["tipo"] == "free" and mol["valido_ate"]:
            val = _para_data(mol["valido_ate"])
            if val and val < date.today():
                erros.append(
                    f"Motoboy FREE com validade expirada em "
                    f"{val.strftime('%d/%m/%Y')}. "
                    "Edite o cadastro e atualize a data 'válido até' para reativar."
                )

    # Limite de acessos ativos por OL nesta loja.
    lim_loja = conn.execute(
        "SELECT limite FROM ol_loja_limite WHERE ol_id = ? AND loja_id = ?",
        (ol_id, loja_id)
    ).fetchone()
    if lim_loja and lim_loja["limite"] > 0:
        ativos_loja = conn.execute(
            "SELECT COUNT(*) FROM cadastros WHERE ol_id = ? AND loja_id = ? AND situacao = 'ativo'",
            (ol_id, loja_id)
        ).fetchone()[0]
        if ativos_loja >= lim_loja["limite"]:
            erros.append(
                f"Limite de acessos ativos nesta loja atingido "
                f"({ativos_loja}/{lim_loja['limite']}). "
                "Suspenda o acesso de outro motoboy antes de ativar este."
            )

    # Limite global de acessos ativos da OL.
    ol = conn.execute("SELECT limite_global FROM ols WHERE id = ?", (ol_id,)).fetchone()
    if ol and ol["limite_global"] and ol["limite_global"] > 0:
        ativos_ol = conn.execute(
            "SELECT COUNT(*) FROM cadastros WHERE ol_id = ? AND situacao = 'ativo'",
            (ol_id,)
        ).fetchone()[0]
        if ativos_ol >= ol["limite_global"]:
            erros.append(
                f"Limite global de acessos ativos desta OL atingido "
                f"({ativos_ol}/{ol['limite_global']}). "
                "Suspenda o acesso de outro motoboy antes de ativar este."
            )

    return erros


# Horário de corte para desativação automática de motoboys free.
HORARIO_CORTE = dtime(18, 30)


def buscar_free_vencidos(conn) -> list:
    """
    Retorna todos os cadastros free com situação 'ativo' cujo prazo de acesso
    expirou: valido_ate anterior a hoje, OU valido_ate igual a hoje e hora
    atual >= 18:30.

    Cada item da lista é um dict com: cadastro_id, motoboy_id, cpf, nome, valido_ate.
    A função só identifica — quem suspende o acesso no DMP é o chamador (app.py).
    """
    agora = _agora_br()          # horário de Brasília (não UTC da nuvem)
    hoje = agora.date()
    passou_corte = agora.time() >= HORARIO_CORTE

    candidatos = conn.execute(
        "SELECT c.id AS cadastro_id, m.id AS motoboy_id, m.cpf, m.nome, mol.valido_ate "
        "FROM cadastros c "
        "JOIN motoboys m ON m.id = c.motoboy_id "
        "JOIN motoboys_ol mol ON mol.motoboy_id = c.motoboy_id AND mol.ol_id = c.ol_id "
        "WHERE mol.tipo = 'free' AND c.situacao = 'ativo' AND mol.valido_ate IS NOT NULL"
    ).fetchall()

    vencidos = []
    for r in candidatos:
        val = _para_data(r["valido_ate"])
        if val is None:
            continue
        # Já passou do dia → vencido em qualquer hora.
        # Chegou no dia de vencimento e passou das 18:30 → vencido.
        if val < hoje or (val == hoje and passou_corte):
            vencidos.append(dict(r))
    return vencidos
