"""
Portal de Motoboys — aplicação Streamlit (Fase 1).

Login com 3 perfis (admin / ol / operador), cadastro de motoboy pela OL com
validações em tempo real, painel do Admin (limites, bloqueio cross-OL, cadastro
de OLs) e esboço do painel do operador. Roda em modo SIMULADO para DMP/SIAC.

Como rodar:
    pip install -r requirements.txt
    python -m streamlit run app.py
Usuários de teste: admin/admin123  |  ol_exemplo/ol123  |  operador/op123
"""

import os
import uuid
from datetime import date, timedelta

import streamlit as st
from dotenv import load_dotenv

import db
from auth import autenticar
from regras import validar_cadastro
from validacoes import validar_cpf, validar_placa, limpar_cpf
from integracoes.dmp_client import DMPClient

load_dotenv()
db.inicializar()
# DMP em modo simulado por padrão. Para integrar de verdade, defina no .env:
# DMP_SIMULADO=false
SIMULADO = os.getenv("DMP_SIMULADO", "true").lower() not in ("false", "0", "nao", "não")
dmp = DMPClient(simulado=SIMULADO)

st.set_page_config(page_title="Portal de Motoboys", page_icon="🛵", layout="wide")

HOJE = date.today()


def _data(valor):
    """Converte 'AAAA-MM-DD' (texto) em date; devolve None se não der."""
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except Exception:
        return None


def gerar_link_selfie(conn, motoboy_id) -> str:
    """Cria um token de uso único para o motoboy enviar a própria foto."""
    token = uuid.uuid4().hex[:16]
    expira = (HOJE + timedelta(days=7)).isoformat()
    conn.execute("INSERT INTO selfie_links (token, motoboy_id, expira_em) VALUES (?,?,?)",
                 (token, motoboy_id, expira))
    return f"https://portal.grupobueno.com/selfie/{token}"


# ===========================================================================
# Login
# ===========================================================================

def tela_login():
    st.title("🛵 Portal de Motoboys")
    st.caption("Acesso restrito — Grupo Bueno")
    with st.form("login"):
        login = st.text_input("Usuário")
        senha = st.text_input("Senha", type="password")
        if st.form_submit_button("Entrar"):
            usuario = autenticar(login, senha)
            if usuario:
                st.session_state.usuario = usuario
                st.rerun()
            else:
                st.error("Usuário ou senha inválidos.")
    st.info("Teste: **admin/admin123** · **ol_exemplo/ol123** · **operador/op123**")


# ===========================================================================
# Perfil OL — cadastro dos próprios motoboys
# ===========================================================================

def tela_ol(usuario):
    st.header("Cadastro de Motoboys")
    conn = db.conectar()
    lojas = conn.execute("SELECT id, nome FROM lojas WHERE ativo = 1 ORDER BY nome").fetchall()
    mapa_lojas = {l["nome"]: l["id"] for l in lojas}

    st.subheader("Novo motoboy")

    # Leitura automática da CNH (opcional): a OL envia a foto e os campos preenchem.
    foto_cnh = st.file_uploader("📷 Foto da CNH — preenche os campos automaticamente",
                                type=["jpg", "jpeg", "png"], key="cnh_upload")
    if foto_cnh is not None and st.button("Ler CNH e preencher"):
        with st.spinner("Lendo a CNH..."):
            try:
                from integracoes.cnh_ocr import ler_cnh
                d = ler_cnh(foto_cnh.getvalue(), foto_cnh.type or "image/jpeg")
                st.session_state["c_nome"] = d.get("nome") or ""
                st.session_state["c_cpf"] = limpar_cpf(d.get("cpf") or "")
                st.session_state["c_cnh"] = d.get("registro") or ""
                st.session_state["ocr_nasc"] = _data(d.get("nascimento"))
                st.session_state["ocr_venc"] = _data(d.get("validade"))
                st.success("CNH lida! Confira os campos abaixo antes de cadastrar.")
            except Exception as e:
                st.error(f"Não consegui ler a CNH ({e}). Pode preencher manualmente.")

    # Sem st.form: assim cada campo é validado na hora em que é digitado.
    col1, col2 = st.columns(2)
    with col1:
        nome = st.text_input("Nome do motorista *", key="c_nome")

        cpf = st.text_input("CPF *", key="c_cpf", placeholder="000.000.000-00")
        cpf_ok = False
        if cpf:
            ok, msg = validar_cpf(cpf)
            if not ok:
                st.error(msg)
            else:
                # CPF válido: verifica se já existe e se está bloqueado.
                mb = conn.execute(
                    "SELECT bloqueado_permanente, motivo_bloqueio FROM motoboys WHERE cpf = ?",
                    (limpar_cpf(cpf),)).fetchone()
                if mb and mb["bloqueado_permanente"]:
                    st.error(f"⛔ Motoboy BLOQUEADO permanentemente "
                             f"(motivo: {mb['motivo_bloqueio'] or 'não informado'}). "
                             "Não pode ser cadastrado.")
                else:
                    cpf_ok = True
                    st.success("CPF válido.")

        nascimento = st.date_input("Nascimento", value=st.session_state.get("ocr_nasc"),
                                   format="DD/MM/YYYY",
                                   min_value=date(1950, 1, 1), max_value=HOJE)
        cnh = st.text_input("CNH", key="c_cnh")
        cnh_venc = st.date_input("Vencimento da CNH", value=st.session_state.get("ocr_venc"),
                                 format="DD/MM/YYYY",
                                 min_value=date(2000, 1, 1), max_value=date(2100, 1, 1))
        if cnh_venc and cnh_venc < HOJE:
            st.error(f"CNH vencida em {cnh_venc.strftime('%d/%m/%Y')}.")

    with col2:
        placa = st.text_input("Placa", key="c_placa", placeholder="ABC1D23")
        placa_norm = ""
        if placa:
            ok, res = validar_placa(placa)
            if not ok:
                st.error(res)
            else:
                placa_norm = res
                st.success(f"Placa válida: {placa_norm}")
        tipo = st.radio("Tipo", ["fixo", "free"], horizontal=True)
        loja_nome = st.selectbox("Loja *", list(mapa_lojas.keys()))
        valido_ate = st.date_input("Válido até", value=None, format="DD/MM/YYYY",
                                   min_value=HOJE, max_value=date(2100, 1, 1))
        if valido_ate and valido_ate < HOJE:
            st.error("A data 'válido até' já passou.")

    if st.button("Cadastrar motoboy", type="primary"):
        cpf_limpo = limpar_cpf(cpf)
        # Revalida tudo no envio (defesa contra envio com campo inválido).
        ok_cpf, msg_cpf = validar_cpf(cpf)
        if not nome or not loja_nome:
            st.error("Preencha pelo menos Nome, CPF e Loja.")
            conn.close(); return
        if not ok_cpf:
            st.error(msg_cpf); conn.close(); return
        if placa:
            ok_placa, res_placa = validar_placa(placa)
            if not ok_placa:
                st.error(res_placa); conn.close(); return
            placa_norm = res_placa

        loja_id = mapa_lojas[loja_nome]
        erros = validar_cadastro(conn, usuario["ol_id"], loja_id, cpf_limpo,
                                 cnh_venc, valido_ate)
        if erros:
            for e in erros:
                st.error(e)
            conn.close(); return

        # Motoboy = identidade única por CPF (pode servir várias OLs).
        # Upsert: cria se for novo, atualiza os dados se o CPF já existir.
        conn.execute(
            "INSERT INTO motoboys (cpf, nome, nascimento, cnh, cnh_venc) VALUES (?,?,?,?,?) "
            "ON CONFLICT(cpf) DO UPDATE SET nome=excluded.nome, nascimento=excluded.nascimento, "
            "cnh=excluded.cnh, cnh_venc=excluded.cnh_venc",
            (cpf_limpo, nome, str(nascimento) if nascimento else None, cnh,
             str(cnh_venc) if cnh_venc else None))
        motoboy_id = conn.execute(
            "SELECT id FROM motoboys WHERE cpf = ?", (cpf_limpo,)).fetchone()["id"]

        try:
            conn.execute(
                "INSERT INTO cadastros (motoboy_id, ol_id, loja_id, placa, tipo, "
                "valido_ate, criado_por) VALUES (?,?,?,?,?,?,?)",
                (motoboy_id, usuario["ol_id"], loja_id, placa_norm or placa, tipo,
                 str(valido_ate) if valido_ate else None, usuario["id"]))
        except Exception:
            st.warning("Esse motoboy já está cadastrado por esta OL nesta loja.")
            conn.close(); return

        # Cadastra a pessoa no DMP. A FOTO vem depois, pelo motoboy (link de selfie).
        # Se o DMP falhar, o cadastro local NÃO se perde (Postgres é a fonte da verdade).
        aviso_dmp = None
        try:
            pessoa = dmp.cadastrar_pessoa(cpf=cpf_limpo, nome=nome)
            conn.execute("UPDATE motoboys SET dmp_person_id=? WHERE id=?",
                         (pessoa.get("Id"), motoboy_id))
            conn.execute("UPDATE cadastros SET enviado_dmp=1 WHERE motoboy_id=? AND ol_id=? AND loja_id=?",
                         (motoboy_id, usuario["ol_id"], loja_id))
        except Exception as erro:
            aviso_dmp = f"Cadastro salvo, mas o envio ao DMP falhou ({erro}). Reenviaremos depois."

        link = gerar_link_selfie(conn, motoboy_id)
        db.auditar(conn, usuario["id"], "cadastro_motoboy", "motoboy", motoboy_id, nome)
        conn.commit()

        st.success(f"✅ {nome} cadastrado!")
        if aviso_dmp:
            st.warning(aviso_dmp)
        st.markdown("**Envie este link para o motoboy tirar e enviar a própria foto:**")
        st.code(link)
        st.caption("O link vale 7 dias. Quando o motoboy enviar a selfie, ela vai "
                   "para o reconhecimento facial do DMP (Fase 2 conecta de verdade).")

    # Lista os motoboys desta OL.
    st.divider()
    st.subheader("Meus motoboys")
    linhas = conn.execute(
        "SELECT m.id, m.nome, m.cpf, l.nome AS loja, c.tipo, c.placa, c.valido_ate, "
        "c.situacao, m.bloqueado_permanente AS bloqueado "
        "FROM cadastros c JOIN motoboys m ON m.id=c.motoboy_id "
        "JOIN lojas l ON l.id=c.loja_id WHERE c.ol_id=? ORDER BY m.nome",
        (usuario["ol_id"],)
    ).fetchall()
    if linhas:
        st.dataframe([{k: r[k] for k in r.keys() if k != "id"} for r in linhas],
                     width="stretch")
        with st.expander("Reenviar link de selfie para um motoboy"):
            mapa = {f"{r['nome']} ({r['cpf']})": r["id"] for r in linhas}
            escolhido = st.selectbox("Motoboy", list(mapa.keys()), key="sel_selfie")
            if st.button("Gerar novo link"):
                link = gerar_link_selfie(conn, mapa[escolhido])
                conn.commit()
                st.code(link)
    else:
        st.info("Nenhum motoboy cadastrado ainda.")
    conn.close()


# ===========================================================================
# Perfil Admin — governança
# ===========================================================================

def tela_admin(usuario):
    st.header("Administração (Grupo Bueno)")
    conn = db.conectar()

    tot_mb = conn.execute("SELECT COUNT(*) FROM motoboys").fetchone()[0]
    tot_ativos = conn.execute("SELECT COUNT(*) FROM cadastros WHERE situacao='ativo'").fetchone()[0]
    tot_ols = conn.execute("SELECT COUNT(*) FROM ols WHERE ativo=1").fetchone()[0]
    tot_bloq = conn.execute("SELECT COUNT(*) FROM motoboys WHERE bloqueado_permanente=1").fetchone()[0]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Motoboys", tot_mb)
    c2.metric("Cadastros ativos", tot_ativos)
    c3.metric("OLs ativas", tot_ols)
    c4.metric("Bloqueados", tot_bloq)

    aba_ols, aba_lim, aba_bloq, aba_base = st.tabs(
        ["Cadastrar OLs", "Limites por OL/loja", "Bloqueio de motoboy", "Base completa"])

    # --- Cadastro de OLs ---
    with aba_ols:
        st.caption("Cadastre uma OL e o login que ela usará para acessar o portal.")
        with st.form("nova_ol", clear_on_submit=True):
            nome = st.text_input("Nome da OL *")
            cnpj = st.text_input("CNPJ")
            limite_global = st.number_input("Limite global de motoboys", min_value=0, value=50)
            st.markdown("**Login de acesso desta OL:**")
            login = st.text_input("Usuário (login) *")
            senha = st.text_input("Senha *", type="password")
            if st.form_submit_button("Cadastrar OL"):
                if not nome or not login or not senha:
                    st.error("Preencha nome da OL, usuário e senha.")
                elif conn.execute("SELECT 1 FROM usuarios WHERE login=?", (login.strip(),)).fetchone():
                    st.error("Já existe um usuário com esse login.")
                else:
                    ol_id = db.criar_ol(conn, nome, cnpj, limite_global)
                    db.criar_usuario(conn, login, senha, "ol", ol_id)
                    db.auditar(conn, usuario["id"], "cadastro_ol", "ol", ol_id, nome)
                    conn.commit()
                    st.success(f"OL '{nome}' criada com o login '{login.strip()}'. "
                               "Defina os limites por loja na aba ao lado.")

        st.divider()
        st.write("**OLs cadastradas:**")
        ols = conn.execute(
            "SELECT o.nome, o.cnpj, o.limite_global, "
            "(SELECT COUNT(*) FROM cadastros c WHERE c.ol_id=o.id AND c.situacao='ativo') AS ativos "
            "FROM ols o WHERE o.ativo=1 ORDER BY o.nome").fetchall()
        st.dataframe([dict(r) for r in ols] or [{}], width="stretch")

    # --- Limites ---
    with aba_lim:
        ols = conn.execute("SELECT id, nome, limite_global FROM ols WHERE ativo=1 ORDER BY nome").fetchall()
        if not ols:
            st.info("Cadastre uma OL primeiro.")
        else:
            mapa = {o["nome"]: o for o in ols}
            ol_nome = st.selectbox("OL", list(mapa.keys()))
            ol = mapa[ol_nome]
            with st.form("limites"):
                lg = st.number_input("Limite global da OL", min_value=0,
                                     value=ol["limite_global"] or 0)
                st.write("Limite por loja:")
                lojas = conn.execute("SELECT id, nome FROM lojas ORDER BY nome").fetchall()
                novos = {}
                for l in lojas:
                    atual = conn.execute(
                        "SELECT limite FROM ol_loja_limite WHERE ol_id=? AND loja_id=?",
                        (ol["id"], l["id"])).fetchone()
                    novos[l["id"]] = st.number_input(
                        l["nome"], min_value=0, value=atual["limite"] if atual else 0,
                        key=f"lim_{l['id']}")
                if st.form_submit_button("Salvar limites"):
                    conn.execute("UPDATE ols SET limite_global=? WHERE id=?", (lg, ol["id"]))
                    for loja_id, lim in novos.items():
                        conn.execute(
                            "INSERT INTO ol_loja_limite (ol_id, loja_id, limite) VALUES (?,?,?) "
                            "ON CONFLICT(ol_id, loja_id) DO UPDATE SET limite=excluded.limite",
                            (ol["id"], loja_id, lim))
                    db.auditar(conn, usuario["id"], "ajuste_limites", "ol", ol["id"])
                    conn.commit()
                    st.success("Limites atualizados.")

    # --- Bloqueio permanente (cross-OL) ---
    with aba_bloq:
        st.caption("O bloqueio permanente prevalece sobre qualquer OL (RF-06).")
        mbs = conn.execute("SELECT id, nome, cpf, bloqueado_permanente FROM motoboys ORDER BY nome").fetchall()
        if mbs:
            mapa_mb = {f"{m['nome']} ({m['cpf']})": m for m in mbs}
            escolhido = st.selectbox("Motoboy", list(mapa_mb.keys()))
            m = mapa_mb[escolhido]
            if m["bloqueado_permanente"]:
                st.warning("Este motoboy está BLOQUEADO.")
                if st.button("Desbloquear"):
                    conn.execute("UPDATE motoboys SET bloqueado_permanente=0, motivo_bloqueio=NULL WHERE id=?",
                                 (m["id"],))
                    db.auditar(conn, usuario["id"], "desbloqueio", "motoboy", m["id"])
                    conn.commit()
                    st.rerun()
            else:
                motivo = st.text_input("Motivo do bloqueio")
                if st.button("Bloquear permanentemente", type="primary"):
                    conn.execute("UPDATE motoboys SET bloqueado_permanente=1, motivo_bloqueio=? WHERE id=?",
                                 (motivo, m["id"]))
                    try:
                        dmp.bloquear_pessoa(m["cpf"], m["nome"])
                    except Exception as erro:
                        st.warning(f"Bloqueio salvo localmente, mas falhou no DMP ({erro}).")
                    db.auditar(conn, usuario["id"], "bloqueio", "motoboy", m["id"], motivo)
                    conn.commit()
                    st.rerun()
        else:
            st.info("Nenhum motoboy na base ainda.")

    # --- Base completa (inclui motoboys bloqueados, mesmo sem cadastro ativo) ---
    with aba_base:
        linhas = conn.execute(
            "SELECT m.nome, m.cpf, "
            "CASE WHEN m.bloqueado_permanente=1 THEN 'SIM' ELSE '' END AS bloqueado, "
            "o.nome AS ol, l.nome AS loja, c.tipo, c.placa, c.valido_ate, c.situacao "
            "FROM motoboys m "
            "LEFT JOIN cadastros c ON c.motoboy_id=m.id "
            "LEFT JOIN ols o ON o.id=c.ol_id "
            "LEFT JOIN lojas l ON l.id=c.loja_id "
            "ORDER BY m.bloqueado_permanente DESC, m.nome").fetchall()
        st.dataframe([dict(r) for r in linhas] or [{}], width="stretch")
    conn.close()


# ===========================================================================
# Perfil Operador — fila / motos (esboço; depende dos eventos do DMP)
# ===========================================================================

def tela_operador(usuario):
    st.header("Operação da unidade")
    st.info("A fila FIFO e o painel de motos disponíveis são alimentados pelos "
            "eventos de entrada/saída do DMP (Fase 4). Assim que ligarmos o "
            "`AccessLog` do DMP, esta tela mostra os dados em tempo real.")
    st.subheader("Fila de retirada (prévia)")
    st.dataframe([{"Ordem": "—", "Motoboy": "—", "Chegada": "—"}], width="stretch")


# ===========================================================================
# Roteamento
# ===========================================================================

def main():
    if "usuario" not in st.session_state:
        tela_login()
        return

    usuario = st.session_state.usuario
    with st.sidebar:
        st.write(f"**{usuario['login']}**")
        st.caption(f"Perfil: {usuario['perfil']}")
        if st.button("Sair"):
            del st.session_state.usuario
            st.rerun()

    if usuario["perfil"] == "admin":
        tela_admin(usuario)
    elif usuario["perfil"] == "ol":
        tela_ol(usuario)
    else:
        tela_operador(usuario)


main()
