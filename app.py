"""
Portal de Motoboys — aplicação Streamlit (Fase 1).

Login com 3 perfis (admin / ol / operador), cadastro de motoboy pela OL com
validações em tempo real, painel do Admin (limites, bloqueio cross-OL, cadastro
de OLs) e esboço do painel do operador. Roda em modo SIMULADO para DMP/SIAC.

Como rodar:
    pip install -r requirements.txt
    python -m streamlit run app.py
Usuários: admin / ol_exemplo / operador. Senhas vêm dos Secrets/.env
(ADMIN_PASSWORD, OL_EXEMPLO_PASSWORD, OPERADOR_PASSWORD); sem isso, caem
nos fallbacks de desenvolvimento.
"""

import os
import uuid
from datetime import date, datetime, timedelta

import streamlit as st
from dotenv import load_dotenv

import db
from auth import autenticar
from regras import validar_cadastro, validar_ativacao, buscar_free_vencidos
from validacoes import validar_cpf, validar_placa, limpar_cpf
from integracoes.dmp_client import DMPClient

load_dotenv()


def _carregar_segredos_streamlit():
    """
    No Streamlit Cloud não existe .env — os segredos vêm de st.secrets
    (configurados no painel "Settings → Secrets"). Copiamos para os.environ
    para que todo o código continue lendo via os.getenv, igual ao local.
    Localmente, se não houver secrets.toml, isto é ignorado sem erro.
    """
    try:
        segredos = st.secrets
    except Exception:
        return
    try:
        for chave, valor in segredos.items():
            if isinstance(valor, (str, int, float, bool)):
                os.environ.setdefault(chave, str(valor))
    except Exception:
        pass


_carregar_segredos_streamlit()

db.inicializar()
# DMP em modo simulado por padrão. Para integrar de verdade, defina no .env
# (local) ou em st.secrets (Streamlit Cloud): DMP_SIMULADO=false
SIMULADO = os.getenv("DMP_SIMULADO", "true").lower() not in ("false", "0", "nao", "não")
dmp = DMPClient(simulado=SIMULADO)

# set_page_config deve ser a 1ª chamada Streamlit — escolhemos aqui conforme a rota.
if st.query_params.get("page") == "selfie":
    st.set_page_config(page_title="Selfie — Portal de Motoboys",
                       page_icon="📷", layout="centered")
else:
    st.set_page_config(page_title="Portal de Motoboys", page_icon="🛵", layout="wide")

HOJE = date.today()


def _desativar_free_vencidos():
    """
    Roda silenciosamente a cada carregamento do app.

    Conceito: o CADASTRO nunca some — o motoboy continua registrado no sistema.
    O que muda é a SITUAÇÃO DE ACESSO: passa de 'ativo' para 'inativo', e o DMP
    recebe o bloqueio para impedir a entrada nas catracas.

    Motoboys free cujo valido_ate chegou ao horário de corte (18:30) têm a
    situação suspensa aqui. A OL pode reativar manualmente se precisar.
    """
    conn = db.conectar()
    try:
        vencidos = buscar_free_vencidos(conn)
        for r in vencidos:
            conn.execute(
                "UPDATE cadastros SET situacao='inativo' WHERE id=?",
                (r["cadastro_id"],))
            db.auditar(conn, None, "vencimento_automatico", "cadastro",
                       r["cadastro_id"],
                       f"{r['nome']} — valido_ate {r['valido_ate']}")
            try:
                # Situação bloqueado → leitora retira a biometria.
                dmp.bloquear_pessoa(r["cpf"], r["nome"])
            except Exception:
                pass  # falha no DMP não impede a suspensão local
        if vencidos:
            conn.commit()
    finally:
        conn.close()


@st.cache_data(ttl=60, show_spinner=False)
def _cpfs_no_dmp_cache():
    """
    Conjunto de CPFs/matrículas existentes no DMP, cacheado por 60s (evita
    bater na API a cada interação). Retorna None se falhar ou estiver simulado
    — None é tratado como 'não sincronizar' (segurança contra exclusão em massa).
    """
    if SIMULADO:
        return None
    try:
        ids = dmp.listar_cpfs()
        return ids if ids else None
    except Exception:
        return None


def _sincronizar_exclusoes_dmp():
    """
    Remove do portal os motoboys que foram EXCLUÍDOS no DMP — assim, apagar
    no DMP reflete no Streamlit.

    Segurança contra remoção indevida:
      - só age se a leitura do DMP teve sucesso e retornou pessoas;
      - só remove quem já foi enviado ao DMP (dmp_person_id setado);
      - só remove cadastros com mais de 2 min (evita apagar recém-criado
        antes do cache do DMP atualizar).
    """
    ids = _cpfs_no_dmp_cache()
    if not ids:
        return
    conn = db.conectar()
    try:
        # "há mais de 2 min" — sintaxe difere entre PostgreSQL e SQLite.
        corte = ("to_char(now() - interval '2 minutes', 'YYYY-MM-DD HH24:MI:SS')"
                 if db.usando_pg() else "datetime('now', '-2 minutes')")
        candidatos = conn.execute(
            "SELECT id, cpf, nome FROM motoboys "
            "WHERE dmp_person_id IS NOT NULL "
            f"AND criado_em < {corte}"
        ).fetchall()
        removidos = []
        for m in candidatos:
            cpf_digitos = "".join(filter(str.isdigit, str(m["cpf"])))
            if cpf_digitos and cpf_digitos not in ids:
                conn.execute("DELETE FROM cadastros WHERE motoboy_id=?", (m["id"],))
                conn.execute("DELETE FROM motoboys_ol WHERE motoboy_id=?", (m["id"],))
                conn.execute("DELETE FROM selfie_links WHERE motoboy_id=?", (m["id"],))
                conn.execute("DELETE FROM motoboys WHERE id=?", (m["id"],))
                db.auditar(conn, None, "exclusao_sincronizada_dmp", "motoboy",
                           m["id"], f"{m['nome']} — removido (excluído no DMP)")
                removidos.append(m["nome"])
        if removidos:
            conn.commit()
    finally:
        conn.close()


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
    base = os.getenv("PORTAL_BASE_URL", "http://localhost:8501")
    return f"{base}/?page=selfie&token={token}"


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


# ===========================================================================
# Perfil OL — cadastro dos próprios motoboys
# ===========================================================================

def _montar_msg_wpp(nome_motoboy, link):
    return (
        f"Olá! 👋\n\n"
        f"Para concluir o seu cadastro, é necessário realizar a *captura da foto para o "
        f"reconhecimento facial*.\n\n"
        f"Basta acessar o link abaixo e seguir as instruções na tela:\n\n"
        f"🔗 {link}\n\n"
        f"*Importante:*\n\n"
        f"• Tire a foto em um local bem iluminado;\n"
        f"• Remova bonés, capacetes, óculos escuros ou qualquer item que cubra o rosto;\n"
        f"• Mantenha o rosto totalmente visível e olhe diretamente para a câmera.\n\n"
        f"O processo é rápido e leva apenas alguns minutos.\n\n"
        f"Após finalizar a captura da foto, seu cadastro seguirá para validação.\n\n"
        f"Em caso de dúvidas, entre em contato conosco."
    )


def tela_ol(usuario):
    import urllib.parse

    conn = db.conectar()
    lojas = conn.execute("SELECT id, nome FROM lojas WHERE ativo = 1 ORDER BY nome").fetchall()
    mapa_lojas = {l["nome"]: l["id"] for l in lojas}

    # Limite mínimo de nascimento para maior de idade (18 anos completos).
    MAX_NASC = HOJE.replace(year=HOJE.year - 18)

    lojas = conn.execute("SELECT id, nome FROM lojas WHERE ativo=1 ORDER BY nome").fetchall()
    mapa_lojas = {l["nome"]: l["id"] for l in lojas}

    aba_cadastro, aba_motoboys = st.tabs(["➕ Novo cadastro", "👥 Meus motoboys"])

    # =========================================================================
    # ABA 1 — Novo cadastro (sem campo de loja — cadastro é geral)
    # =========================================================================
    with aba_cadastro:
        sub_novo, sub_editar = st.tabs(["➕ Novo", "✏️ Editar cadastro"])

    with sub_novo:
        st.markdown("### Cadastrar novo motoboy")
        st.caption("Todos os campos são obrigatórios.")

        with st.expander("📷 Preencher automaticamente com foto da CNH"):
            foto_cnh = st.file_uploader("Envie a foto da CNH",
                                        type=["jpg", "jpeg", "png"], key="cnh_upload")
            if foto_cnh is not None and st.button("Ler CNH e preencher campos"):
                with st.spinner("Lendo a CNH com IA..."):
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
                        st.error(f"Não foi possível ler a CNH ({e}). Preencha manualmente.")

        st.divider()

        # ---- Linha 1: dados pessoais ----------------------------------------
        st.markdown("**Dados pessoais**")
        col1, col2, col3 = st.columns(3, gap="medium")

        with col1:
            nome = st.text_input("Nome completo", key="c_nome", placeholder="Ex: João da Silva")

        with col2:
            cpf = st.text_input("CPF", key="c_cpf", placeholder="000.000.000-00")
            cpf_ok = False
            if cpf:
                ok, msg = validar_cpf(cpf)
                if not ok:
                    st.error(msg)
                else:
                    mb = conn.execute(
                        "SELECT bloqueado_permanente, motivo_bloqueio FROM motoboys WHERE cpf = ?",
                        (limpar_cpf(cpf),)).fetchone()
                    if mb and mb["bloqueado_permanente"]:
                        st.error(f"⛔ Bloqueado permanentemente "
                                 f"({mb['motivo_bloqueio'] or 'sem motivo'}). "
                                 "Contate o Grupo Bueno.")
                    else:
                        cpf_ok = True
                        st.success("CPF válido ✓")

        with col3:
            nascimento = st.date_input(
                "Data de nascimento (maior de 18 anos)",
                value=st.session_state.get("ocr_nasc"),
                format="DD/MM/YYYY",
                min_value=date(1950, 1, 1),
                max_value=MAX_NASC,
                help="Apenas maiores de 18 anos podem ser cadastrados.",
            )

        # ---- Linha 2: contato -----------------------------------------------
        st.markdown("**Contato** — usado para enviar o link de cadastro facial")
        telefone = st.text_input("WhatsApp (com DDD)", key="c_tel",
                                 placeholder="61999990000")

        st.divider()

        # ---- Linha 3: habilitação e moto ------------------------------------
        st.markdown("**Habilitação e moto**")
        col6, col7, col8 = st.columns(3, gap="medium")

        with col6:
            cnh = st.text_input("Número da CNH", key="c_cnh", placeholder="Ex: 12345678900")

        with col7:
            cnh_venc = st.date_input(
                "Vencimento da CNH",
                value=st.session_state.get("ocr_venc"),
                format="DD/MM/YYYY",
                min_value=date(2000, 1, 1),
                max_value=date(2100, 1, 1),
            )
            if cnh_venc and cnh_venc < HOJE:
                st.error(f"CNH vencida em {cnh_venc.strftime('%d/%m/%Y')}.")

        with col8:
            placa = st.text_input("Placa da moto", key="c_placa", placeholder="ABC1D23")
            placa_norm = ""
            if placa:
                ok, res = validar_placa(placa)
                if not ok:
                    st.error(res)
                else:
                    placa_norm = res
                    st.success(f"Placa válida: {placa_norm} ✓")

        st.divider()

        # ---- Linha 4: tipo de vínculo (sem loja — loja é escolhida na ativação)
        st.markdown("**Tipo de vínculo**")
        st.caption("A loja é definida na hora de ativar o motoboy, em **Meus motoboys**.")
        col9, col10 = st.columns(2, gap="medium")

        with col9:
            tipo = st.radio(
                "Tipo de vínculo",
                ["fixo", "free"],
                horizontal=True,
                help="**Fixo:** permanente, sem prazo de saída.\n\n"
                     "**Free:** temporário, com data de encerramento obrigatória.",
            )

        with col10:
            if tipo == "free":
                valido_ate = st.date_input(
                    "Válido até",
                    value=None,
                    format="DD/MM/YYYY",
                    min_value=HOJE,
                    max_value=date(2100, 1, 1),
                    help="Acesso será suspenso automaticamente às 18:30 desta data.",
                )
                if valido_ate:
                    st.caption(f"Acesso suspende em {valido_ate.strftime('%d/%m/%Y')} às 18:30.")
            else:
                valido_ate = None
                st.info("Fixo — sem data de encerramento.", icon="ℹ️")

        st.divider()
        if st.button("Cadastrar motoboy", type="primary", use_container_width=True):
            cpf_limpo = limpar_cpf(cpf)
            erros_form = []
            if not nome.strip():
                erros_form.append("Nome completo é obrigatório.")
            if not cpf.strip():
                erros_form.append("CPF é obrigatório.")
            else:
                ok_cpf, msg_cpf = validar_cpf(cpf)
                if not ok_cpf:
                    erros_form.append(msg_cpf)
            if not nascimento:
                erros_form.append("Data de nascimento é obrigatória.")
            elif nascimento > MAX_NASC:
                erros_form.append("O motoboy deve ter pelo menos 18 anos.")
            if not telefone.strip():
                erros_form.append("WhatsApp (telefone) é obrigatório.")
            if not cnh.strip():
                erros_form.append("Número da CNH é obrigatório.")
            if not cnh_venc:
                erros_form.append("Vencimento da CNH é obrigatório.")
            elif cnh_venc < HOJE:
                erros_form.append(f"CNH vencida em {cnh_venc.strftime('%d/%m/%Y')}.")
            if not placa.strip():
                erros_form.append("Placa da moto é obrigatória.")
            else:
                ok_placa, res_placa = validar_placa(placa)
                if not ok_placa:
                    erros_form.append(res_placa)
                else:
                    placa_norm = res_placa
            if tipo == "free" and not valido_ate:
                erros_form.append("Para motoboy free, a data 'válido até' é obrigatória.")
            if erros_form:
                for e in erros_form:
                    st.error(e)
                conn.close(); st.stop()

            # Validações de negócio (CNH, bloqueio permanente — sem limite de loja aqui).
            erros_reg = validar_cadastro(conn, usuario["ol_id"], None, cpf_limpo,
                                         cnh_venc, valido_ate)
            if erros_reg:
                for e in erros_reg:
                    st.error(e)
                conn.close(); st.stop()

            # Salva ou atualiza o registro da pessoa.
            conn.execute(
                "INSERT INTO motoboys (cpf, nome, nascimento, cnh, cnh_venc, telefone) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(cpf) DO UPDATE SET nome=excluded.nome, nascimento=excluded.nascimento, "
                "cnh=excluded.cnh, cnh_venc=excluded.cnh_venc, "
                "telefone=excluded.telefone",
                (cpf_limpo, nome.strip(), str(nascimento), cnh.strip(),
                 str(cnh_venc), telefone.strip()))
            motoboy_id = conn.execute(
                "SELECT id FROM motoboys WHERE cpf=?", (cpf_limpo,)).fetchone()["id"]

            # Vínculo OL → motoboy (sem loja). Já existindo, atualiza placa/tipo/valido_ate.
            try:
                conn.execute(
                    "INSERT INTO motoboys_ol (motoboy_id, ol_id, placa, tipo, valido_ate, criado_por) "
                    "VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(motoboy_id, ol_id) DO UPDATE SET "
                    "placa=excluded.placa, tipo=excluded.tipo, valido_ate=excluded.valido_ate",
                    (motoboy_id, usuario["ol_id"], placa_norm, tipo,
                     str(valido_ate) if valido_ate else None, usuario["id"]))
            except Exception as ex:
                st.error(f"Erro ao salvar cadastro: {ex}")
                conn.close(); st.stop()

            # Registra no DMP (reconhecimento facial — sem loja).
            # Para free, a credencial herda a validade (valido_ate); fixo = permanente.
            aviso_dmp = None
            try:
                pessoa = dmp.cadastrar_pessoa(
                    cpf=cpf_limpo, nome=nome.strip(),
                    valido_ate=str(valido_ate) if (tipo == "free" and valido_ate) else None)
                conn.execute("UPDATE motoboys SET dmp_person_id=? WHERE id=?",
                             (pessoa.get("Id"), motoboy_id))
                if pessoa.get("credencial_face_ok") is False:
                    aviso_dmp = ("pessoa criada, mas a credencial facial falhou: "
                                 + pessoa.get("credencial_face_erro", ""))
            except Exception as erro:
                aviso_dmp = str(erro)

            link = gerar_link_selfie(conn, motoboy_id)
            db.auditar(conn, usuario["id"], "cadastro_motoboy", "motoboy", motoboy_id, nome.strip())
            conn.commit()
            # Atualiza o cache de CPFs do DMP para não remover o recém-cadastrado.
            _cpfs_no_dmp_cache.clear()

            st.success(f"✅ {nome.strip()} cadastrado com sucesso!")
            st.info("Agora vá em **Meus motoboys** para ativar este motoboy em uma loja.")
            if aviso_dmp:
                st.warning(f"Cadastro salvo, mas falha no sistema de acesso ({aviso_dmp}). Será reenviado.")

            # ---- Envio do link de cadastro facial ---------------------------
            st.divider()
            st.markdown("#### 📲 Enviar link de cadastro facial ao motoboy")
            st.caption(
                "O motoboy precisa acessar o link abaixo para registrar o reconhecimento facial. "
                "Sem esse passo, a catraca não libera a entrada."
            )
            st.code(link)

            tel_limpo = "".join(filter(str.isdigit, telefone))
            msg_wpp = _montar_msg_wpp(nome.strip(), link)
            st.link_button(
                "💬 Enviar pelo WhatsApp",
                f"https://wa.me/55{tel_limpo}?text={urllib.parse.quote(msg_wpp)}",
                use_container_width=True,
                type="primary",
            )

    # =========================================================================
    # SUB-ABA — Editar cadastro
    # =========================================================================
    with sub_editar:
        st.markdown("### Editar cadastro de motoboy")
        st.caption(
            "Campos editáveis: tipo de vínculo, data de validade (free), "
            "celular e vencimento da CNH (com foto)."
        )

        todos_mol = conn.execute(
            "SELECT mol.id AS mol_id, m.id AS motoboy_id, m.nome, m.cpf, "
            "m.telefone, m.cnh, m.cnh_venc, "
            "mol.placa, mol.tipo, mol.valido_ate "
            "FROM motoboys_ol mol "
            "JOIN motoboys m ON m.id=mol.motoboy_id "
            "WHERE mol.ol_id=? ORDER BY m.nome",
            (usuario["ol_id"],)
        ).fetchall()

        if not todos_mol:
            st.info("Nenhum motoboy cadastrado ainda. Use a aba **Novo** para cadastrar.")
        else:
            mapa_ed = {f"{r['nome']} — CPF {r['cpf']}": r for r in todos_mol}
            escolhido_label = st.selectbox(
                "Selecione o motoboy para editar",
                list(mapa_ed.keys()),
                key="ed_sel",
            )
            mb = mapa_ed[escolhido_label]

            # ---- Info somente-leitura ----------------------------------------
            with st.container(border=True):
                st.caption("Dados fixos (não editáveis)")
                r1, r2, r3, r4 = st.columns(4)
                r1.markdown(f"**Nome:** {mb['nome']}")
                r2.markdown(f"**CPF:** {mb['cpf']}")
                r3.markdown(f"**Placa:** {mb['placa'] or '—'}")
                r4.markdown(f"**CNH nº:** {mb['cnh'] or '—'}")

            st.divider()

            # ---- Campos editáveis -------------------------------------------
            st.markdown("**Alterar dados**")
            ed_tel = st.text_input(
                "WhatsApp (com DDD)", value=mb["telefone"] or "", key="ed_tel",
                placeholder="61999990000")

            st.markdown("**Tipo de vínculo**")
            ec3, ec4 = st.columns(2, gap="medium")
            with ec3:
                ed_tipo = st.radio(
                    "Tipo",
                    ["fixo", "free"],
                    index=0 if mb["tipo"] == "fixo" else 1,
                    horizontal=True,
                    key="ed_tipo",
                )
            with ec4:
                if ed_tipo == "free":
                    val_atual = _data(mb["valido_ate"])
                    ed_valido_ate = st.date_input(
                        "Válido até",
                        value=val_atual if val_atual and val_atual >= HOJE else None,
                        format="DD/MM/YYYY",
                        min_value=HOJE,
                        max_value=date(2100, 1, 1),
                        key="ed_valido_ate",
                        help="Acesso suspende automaticamente às 18:30 desta data.",
                    )
                else:
                    ed_valido_ate = None
                    st.info("Fixo — sem data de encerramento.", icon="ℹ️")

            st.divider()
            st.markdown("**Vencimento da CNH** — obrigatório enviar foto para alterar")
            venc_atual_str = mb["cnh_venc"] or "não informado"
            st.caption(f"Vencimento atual: **{venc_atual_str}**")

            with st.expander("📷 Atualizar vencimento com foto da CNH"):
                foto_ed_cnh = st.file_uploader(
                    "Foto da CNH (frente)",
                    type=["jpg", "jpeg", "png"],
                    key="ed_cnh_foto",
                )
                ed_cnh_venc_ocr = st.session_state.get("ed_ocr_venc")
                if foto_ed_cnh and st.button("Ler CNH e extrair vencimento", key="ed_ler_cnh"):
                    with st.spinner("Lendo a CNH com IA..."):
                        try:
                            from integracoes.cnh_ocr import ler_cnh
                            d = ler_cnh(foto_ed_cnh.getvalue(),
                                        foto_ed_cnh.type or "image/jpeg")
                            nova_venc = _data(d.get("validade"))
                            if nova_venc:
                                st.session_state["ed_ocr_venc"] = nova_venc
                                st.session_state["ed_ocr_cpf_confirmado"] = mb["cpf"]
                                st.success(
                                    f"Vencimento lido: **{nova_venc.strftime('%d/%m/%Y')}**. "
                                    "Clique em **Salvar alterações** para confirmar."
                                )
                            else:
                                st.warning("Não foi possível ler a data da CNH. "
                                           "Tente outra foto ou ajuste manualmente.")
                        except Exception as e:
                            st.error(f"Erro ao ler CNH: {e}")
                # Mostra o vencimento lido (se for do mesmo motoboy)
                if (st.session_state.get("ed_ocr_cpf_confirmado") == mb["cpf"]
                        and st.session_state.get("ed_ocr_venc")):
                    ed_cnh_venc_ocr = st.session_state["ed_ocr_venc"]
                    st.info(
                        f"Novo vencimento a salvar: **{ed_cnh_venc_ocr.strftime('%d/%m/%Y')}**"
                    )
                else:
                    ed_cnh_venc_ocr = None

            st.divider()
            if st.button("💾 Salvar alterações", type="primary",
                         use_container_width=True, key="ed_salvar"):
                erros_ed = []
                if not ed_tel.strip():
                    erros_ed.append("WhatsApp é obrigatório.")
                if ed_tipo == "free" and not ed_valido_ate:
                    erros_ed.append("Para tipo FREE, a data 'válido até' é obrigatória.")
                if ed_cnh_venc_ocr and ed_cnh_venc_ocr < HOJE:
                    erros_ed.append(
                        f"Vencimento da CNH lido ({ed_cnh_venc_ocr.strftime('%d/%m/%Y')}) "
                        "já está vencido."
                    )
                if erros_ed:
                    for e in erros_ed:
                        st.error(e)
                else:
                    # Atualiza motoboys (contato + CNH se lida)
                    if ed_cnh_venc_ocr:
                        conn.execute(
                            "UPDATE motoboys SET telefone=?, cnh_venc=? WHERE id=?",
                            (ed_tel.strip(), str(ed_cnh_venc_ocr), mb["motoboy_id"]))
                    else:
                        conn.execute(
                            "UPDATE motoboys SET telefone=? WHERE id=?",
                            (ed_tel.strip(), mb["motoboy_id"]))

                    # Atualiza motoboys_ol (tipo + valido_ate)
                    conn.execute(
                        "UPDATE motoboys_ol SET tipo=?, valido_ate=? "
                        "WHERE motoboy_id=? AND ol_id=?",
                        (ed_tipo,
                         str(ed_valido_ate) if ed_tipo == "free" and ed_valido_ate else None,
                         mb["motoboy_id"], usuario["ol_id"]))

                    db.auditar(conn, usuario["id"], "editar_cadastro",
                               "motoboy", mb["motoboy_id"], mb["nome"])
                    conn.commit()

                    # Só FREE usa credencial (para limitar a validade). Ao mudar a
                    # data, atualiza a validade; se estiver com acesso ativo, revincula
                    # com o novo prazo para reprogramar a auto-remoção na leitora.
                    if ed_tipo == "free" and ed_valido_ate:
                        try:
                            nova_validade = str(ed_valido_ate)
                            dmp.garantir_credencial(mb["cpf"], valido_ate=nova_validade)
                            cad_ativo = conn.execute(
                                "SELECT 1 FROM cadastros WHERE motoboy_id=? AND situacao='ativo' LIMIT 1",
                                (mb["motoboy_id"],)).fetchone()
                            if cad_ativo:
                                dmp.vincular_face(mb["cpf"], valido_ate=nova_validade)
                        except Exception:
                            pass  # não impede a edição local

                    # Limpa o estado do OCR deste motoboy
                    st.session_state.pop("ed_ocr_venc", None)
                    st.session_state.pop("ed_ocr_cpf_confirmado", None)

                    st.success(f"✅ Cadastro de **{mb['nome']}** atualizado com sucesso!")
                    st.rerun()

    # =========================================================================
    # ABA 2 — Meus motoboys
    # =========================================================================
    with aba_motoboys:
        st.markdown("### Meus motoboys")
        st.caption(
            "**Cadastrado** = registro permanente no sistema. "
            "**Acesso ativo em loja** = liberado nas catracas daquela unidade agora."
        )

        # ---- Seção 1: ativos por loja (só mostra lojas com pelo menos 1 ativo) ---
        lojas_com_ativos = conn.execute(
            "SELECT l.id, l.nome, COALESCE(oll.limite, 0) AS limite, "
            "COUNT(c.id) AS n_ativos "
            "FROM lojas l "
            "JOIN cadastros c ON c.loja_id=l.id AND c.ol_id=? AND c.situacao='ativo' "
            "LEFT JOIN ol_loja_limite oll ON oll.ol_id=? AND oll.loja_id=l.id "
            "WHERE l.ativo=1 "
            "GROUP BY l.id ORDER BY l.nome",
            (usuario["ol_id"], usuario["ol_id"])
        ).fetchall()

        # Mostra também lojas sem ativos mas que têm limite configurado
        todas_lojas = conn.execute(
            "SELECT l.id, l.nome, COALESCE(oll.limite, 0) AS limite "
            "FROM lojas l "
            "LEFT JOIN ol_loja_limite oll ON oll.ol_id=? AND oll.loja_id=l.id "
            "WHERE l.ativo=1 ORDER BY l.nome",
            (usuario["ol_id"],)
        ).fetchall()

        for loja in todas_lojas:
            ativos_loja = conn.execute(
                "SELECT c.id AS cadastro_id, m.nome, m.cpf, mol.tipo, mol.placa, "
                "mol.valido_ate, m.bloqueado_permanente AS bloqueado "
                "FROM cadastros c "
                "JOIN motoboys m ON m.id=c.motoboy_id "
                "LEFT JOIN motoboys_ol mol ON mol.motoboy_id=c.motoboy_id AND mol.ol_id=c.ol_id "
                "WHERE c.ol_id=? AND c.loja_id=? AND c.situacao='ativo' "
                "ORDER BY m.nome",
                (usuario["ol_id"], loja["id"])
            ).fetchall()

            n = len(ativos_loja)
            cap = loja["limite"]
            rest = (cap - n) if cap > 0 else None
            pct = int(n / cap * 100) if cap > 0 else 0

            if cap > 0:
                cor = "🔴" if rest == 0 else ("🟡" if pct >= 80 else "🟢")
                titulo = f"{cor} {loja['nome']} — {n}/{cap} ativos"
            else:
                titulo = f"{'🟢' if n > 0 else '⚪'} {loja['nome']} — {n} ativo(s)"

            with st.expander(titulo, expanded=(n > 0)):
                if not ativos_loja:
                    st.caption("Nenhum motoboy ativo nesta loja.")
                else:
                    for r in ativos_loja:
                        with st.container(border=True):
                            c1, c2, c3, c4 = st.columns([3, 2, 3, 1])
                            with c1:
                                bloq = " 🔴 BLOQUEADO" if r["bloqueado"] else ""
                                st.markdown(f"**{r['nome']}**{bloq}")
                                st.caption(f"CPF: {r['cpf']}")
                            with c2:
                                st.markdown(f"🏍️ {r['placa'] or '—'}")
                                if r["tipo"] == "free":
                                    st.markdown("🟠 **FREE**")
                                else:
                                    st.caption("Fixo")
                            with c3:
                                if r["tipo"] == "free" and r["valido_ate"]:
                                    st.caption(f"Válido até **{r['valido_ate']}** às 18:30")
                                else:
                                    st.caption("Sem prazo de encerramento")
                            with c4:
                                if r["bloqueado"]:
                                    st.caption("🔴 Bloqueado")
                                elif st.button("Suspender", key=f"susp_{r['cadastro_id']}",
                                               help="Suspende o acesso. Cadastro continua salvo."):
                                    conn.execute(
                                        "UPDATE cadastros SET situacao='inativo' WHERE id=?",
                                        (r["cadastro_id"],))
                                    db.auditar(conn, usuario["id"], "suspender_acesso",
                                               "cadastro", r["cadastro_id"], r["nome"])
                                    # DMP: situação bloqueado → leitora retira a biometria.
                                    try:
                                        dmp.bloquear_pessoa(r["cpf"], r["nome"])
                                    except Exception:
                                        pass
                                    conn.commit()
                                    st.rerun()

        # ---- Seção 2: todos os cadastrados ------------------------------------
        st.divider()
        st.markdown("### Motoboys cadastrados")
        st.caption(
            "Um motoboy só pode estar ativo em **uma loja por vez**. "
            "Selecione a loja e clique em **Ativar acesso**."
        )

        cpf_busca = st.text_input(
            "🔍 Buscar por CPF",
            placeholder="Digite o CPF para filtrar",
            key="busca_cpf",
        )

        # Carrega todos com situação atual (loja ativa se houver)
        todos_cad_raw = conn.execute(
            "SELECT mol.id AS mol_id, m.id AS motoboy_id, m.nome, m.cpf, "
            "mol.placa, mol.tipo, mol.valido_ate, m.bloqueado_permanente AS bloqueado, "
            "m.telefone, "
            "(SELECT l.nome FROM cadastros c2 JOIN lojas l ON l.id=c2.loja_id "
            " WHERE c2.motoboy_id=m.id AND c2.ol_id=mol.ol_id AND c2.situacao='ativo' "
            " LIMIT 1) AS loja_ativa_nome, "
            "(SELECT c2.loja_id FROM cadastros c2 "
            " WHERE c2.motoboy_id=m.id AND c2.ol_id=mol.ol_id AND c2.situacao='ativo' "
            " LIMIT 1) AS loja_ativa_id "
            "FROM motoboys_ol mol "
            "JOIN motoboys m ON m.id=mol.motoboy_id "
            "WHERE mol.ol_id=? "
            "ORDER BY m.nome",
            (usuario["ol_id"],)
        ).fetchall()

        if cpf_busca.strip():
            termo = "".join(filter(str.isdigit, cpf_busca))
            todos_cad_raw = [r for r in todos_cad_raw if termo in r["cpf"]]

        # Separa: disponíveis para ativar (sem loja ativa) e já ativos
        disponiveis = [r for r in todos_cad_raw if not r["loja_ativa_nome"]]
        ja_ativos   = [r for r in todos_cad_raw if r["loja_ativa_nome"]]

        if not todos_cad_raw:
            if cpf_busca.strip():
                st.info(f"Nenhum motoboy encontrado para o CPF '{cpf_busca}'.")
            else:
                st.info("Nenhum motoboy cadastrado ainda. Cadastre na aba **Novo cadastro**.")
        else:
            # ---- Disponíveis para ativar ------------------------------------
            if disponiveis:
                for r in disponiveis:
                    with st.container(border=True):
                        h1, h2, h3, h4 = st.columns([3, 2, 2, 2])
                        with h1:
                            bloq = " 🔴 BLOQUEADO" if r["bloqueado"] else ""
                            st.markdown(f"**{r['nome']}**{bloq}")
                            st.caption(f"CPF: {r['cpf']}")
                        with h2:
                            st.markdown(f"🏍️ {r['placa'] or '—'}")
                            if r["tipo"] == "free":
                                st.markdown("🟠 **FREE**")
                                if r["valido_ate"]:
                                    st.caption(f"Até {r['valido_ate']} 18:30")
                            else:
                                st.caption("Fixo")
                        with h3:
                            st.caption("⚪ Sem acesso ativo")
                        with h4:
                            if not r["bloqueado"]:
                                loja_sel = st.selectbox(
                                    "Loja",
                                    [l["nome"] for l in lojas],
                                    key=f"loja_sel_{r['motoboy_id']}",
                                    label_visibility="collapsed",
                                )
                                if st.button("Ativar acesso", key=f"ativ_{r['motoboy_id']}",
                                             type="primary", use_container_width=True):
                                    loja_id_sel = mapa_lojas[loja_sel]
                                    erros_at = validar_ativacao(conn, usuario["ol_id"], loja_id_sel, r["motoboy_id"])
                                    if erros_at:
                                        for e in erros_at:
                                            st.error(e)
                                    else:
                                        conn.execute(
                                            "INSERT INTO cadastros "
                                            "(motoboy_id, ol_id, loja_id, situacao, criado_por) "
                                            "VALUES (?,?,?,'ativo',?) "
                                            "ON CONFLICT(motoboy_id, ol_id, loja_id) "
                                            "DO UPDATE SET situacao='ativo'",
                                            (r["motoboy_id"], usuario["ol_id"],
                                             loja_id_sel, usuario["id"]))
                                        db.auditar(conn, usuario["id"], "ativar_acesso",
                                                   "cadastro", r["motoboy_id"],
                                                   f"{r['nome']} → {loja_sel}")
                                        # DMP: a SITUAÇÃO permitido manda a leitora
                                        # adicionar a biometria. Para FREE, cria também
                                        # a credencial com validade (auto-remove às 18:30
                                        # do valido_ate). Fixo = só situação.
                                        is_free = r["tipo"] == "free"
                                        val_cred = (str(r["valido_ate"])
                                                    if is_free and r["valido_ate"] else None)
                                        try:
                                            dmp.liberar_pessoa(r["cpf"], r["nome"])
                                            if is_free:
                                                dmp.vincular_face(r["cpf"], valido_ate=val_cred)
                                        except Exception:
                                            try:
                                                dmp.cadastrar_pessoa(r["cpf"], r["nome"],
                                                                     liberado=True)
                                                if is_free:
                                                    dmp.vincular_face(r["cpf"], valido_ate=val_cred)
                                            except Exception:
                                                pass
                                        conn.commit()
                                        st.rerun()
            else:
                st.info("Todos os motoboys cadastrados já estão ativos em alguma loja.")

            # ---- Já ativos (aparecem no final, compactos) -------------------
            if ja_ativos:
                st.divider()
                st.markdown("**Motoboys com acesso ativo** *(desça para ver todos)*")
                for r in ja_ativos:
                    with st.container(border=True):
                        h1, h2, h3, h4 = st.columns([3, 2, 3, 1])
                        with h1:
                            st.markdown(f"**{r['nome']}**")
                            st.caption(f"CPF: {r['cpf']}")
                        with h2:
                            st.markdown(f"🏍️ {r['placa'] or '—'}")
                        with h3:
                            st.markdown(f"✅ Ativo em **{r['loja_ativa_nome']}**")
                            if r["tipo"] == "free" and r["valido_ate"]:
                                st.caption(f"🟠 FREE — até {r['valido_ate']} às 18:30")
                        with h4:
                            # Botão de suspender acesso direto desta lista
                            cad_row = conn.execute(
                                "SELECT id FROM cadastros WHERE motoboy_id=? AND ol_id=? AND situacao='ativo'",
                                (r["motoboy_id"], usuario["ol_id"])).fetchone()
                            if cad_row and st.button(
                                    "Suspender", key=f"susp_cad_{r['motoboy_id']}",
                                    help="Suspende o acesso nas catracas."):
                                conn.execute(
                                    "UPDATE cadastros SET situacao='inativo' WHERE id=?",
                                    (cad_row["id"],))
                                db.auditar(conn, usuario["id"], "suspender_acesso",
                                           "cadastro", cad_row["id"], r["nome"])
                                # DMP: situação bloqueado → leitora retira a biometria.
                                try:
                                    dmp.bloquear_pessoa(r["cpf"], r["nome"])
                                except Exception:
                                    pass
                                conn.commit()
                                st.rerun()

        # ---- Reenvio de link de selfie --------------------------------------
        if todos_cad_raw:
            st.divider()
            with st.expander("🔗 Reenviar link de cadastro facial"):
                todos_mb = conn.execute(
                    "SELECT m.id AS motoboy_id, m.nome, m.cpf, m.telefone "
                    "FROM motoboys_ol mol JOIN motoboys m ON m.id=mol.motoboy_id "
                    "WHERE mol.ol_id=? ORDER BY m.nome", (usuario["ol_id"],)
                ).fetchall()
                mapa_mb = {f"{r['nome']} ({r['cpf']})": r for r in todos_mb}
                escolhido_label = st.selectbox("Motoboy", list(mapa_mb.keys()), key="sel_selfie")
                mb_sel = mapa_mb[escolhido_label]
                if st.button("Gerar novo link"):
                    link = gerar_link_selfie(conn, mb_sel["motoboy_id"])
                    conn.commit()
                    st.code(link)
                    tel_limpo = "".join(filter(str.isdigit, mb_sel["telefone"] or ""))
                    if tel_limpo:
                        msg_r = _montar_msg_wpp(mb_sel["nome"], link)
                        st.link_button(
                            "💬 Enviar pelo WhatsApp",
                            f"https://wa.me/55{tel_limpo}?text={urllib.parse.quote(msg_r)}",
                            type="primary", use_container_width=True)

    conn.close()


# ===========================================================================
# Perfil Admin — governança
# ===========================================================================

def tela_admin(usuario):
    st.header("Administração (Grupo Bueno)")
    conn = db.conectar()

    # Cadastro = registro existe. Situação de acesso = ativo/inativo no DMP.
    tot_mb = conn.execute("SELECT COUNT(*) FROM motoboys").fetchone()[0]
    tot_cad = conn.execute("SELECT COUNT(*) FROM cadastros").fetchone()[0]
    tot_acesso_ativo = conn.execute("SELECT COUNT(*) FROM cadastros WHERE situacao='ativo'").fetchone()[0]
    tot_ols = conn.execute("SELECT COUNT(*) FROM ols WHERE ativo=1").fetchone()[0]
    tot_bloq = conn.execute("SELECT COUNT(*) FROM motoboys WHERE bloqueado_permanente=1").fetchone()[0]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Motoboys cadastrados", tot_mb)
    c2.metric("Vínculos totais", tot_cad, help="Cadastros (OL/loja). Um motoboy pode ter mais de um.")
    c3.metric("Com acesso ativo", tot_acesso_ativo, help="Situação de acesso = ativo no DMP agora.")
    c4.metric("OLs ativas", tot_ols)
    c5.metric("Bloqueados permanentes", tot_bloq)

    # --- Diagnóstico da integração com o DMP --------------------------------
    estado_dmp = "🟡 Modo simulado" if SIMULADO else "🟢 Modo real (integrado)"
    with st.expander(f"🔌 Integração DMP Access II — {estado_dmp}", expanded=False):
        st.caption(
            "Confirma se ESTE servidor (onde o portal está rodando) consegue "
            "falar com o DMP. Útil para validar a integração na nuvem."
        )
        if st.button("Testar conexão com o DMP agora"):
            with st.spinner("Conectando ao DMP..."):
                diag = dmp.diagnostico()
            if diag["ok"]:
                st.success(
                    f"✅ Conectado ao DMP como **{diag['user_name']}**. "
                    f"Token válido até {diag['expira_em']}."
                )
                if diag.get("leitura_pessoas_ok"):
                    st.caption("Leitura de pessoas (Bearer) também funcionou. ✔")
                else:
                    st.warning("Logon OK, mas a leitura de pessoas falhou "
                               "(pode ser permissão do perfil).")
            elif diag["simulado"]:
                st.warning(
                    "🟡 O portal está em **modo simulado** — não escreve no DMP. "
                    "Para integrar de verdade, defina `DMP_SIMULADO=false` "
                    "nos Secrets do Streamlit Cloud e reinicie o app."
                )
            else:
                st.error(f"❌ Falha ao conectar ao DMP: {diag['erro']}")
                if diag["erro"] and ("401" in diag["erro"] or "denied" in diag["erro"].lower()):
                    st.caption(
                        "401 = credenciais erradas OU o IP deste servidor não está "
                        "liberado no DMP. Confira o `DMP_NAK`/`DMP_PASSWORD` nos Secrets "
                        "e, se persistir, peça à DIMEP para liberar o IP do Streamlit Cloud."
                    )
            # Mostra o que o servidor enxerga (sem expor segredos).
            nak_ok = "✅ 489 (íntegro)" if diag.get("nak_len") == 489 else f"⚠️ {diag.get('nak_len')} (esperado 489)"
            st.caption(
                f"Base: `{diag['base_url']}` · Usuário: `{diag['username']}` · "
                f"Tamanho do NAK recebido: {nak_ok}"
            )

        st.divider()
        st.caption(
            "**Sincronizar exclusões:** remove do portal os motoboys que foram "
            "apagados no DMP. Roda sozinho a cada minuto; use o botão para forçar agora."
        )
        if st.button("🔄 Sincronizar exclusões do DMP agora"):
            _cpfs_no_dmp_cache.clear()
            ids = _cpfs_no_dmp_cache()
            if not ids:
                st.warning("Não consegui ler a lista do DMP agora (ou está simulado). "
                           "Nada foi removido.")
            else:
                antes = conn.execute("SELECT COUNT(*) FROM motoboys").fetchone()[0]
                _sincronizar_exclusoes_dmp()
                depois = conn.execute("SELECT COUNT(*) FROM motoboys").fetchone()[0]
                n = antes - depois
                if n > 0:
                    st.success(f"✅ {n} motoboy(s) removido(s) do portal (excluídos no DMP).")
                    st.rerun()
                else:
                    st.info("Nada para remover — portal e DMP já estão sincronizados.")

    aba_ols, aba_lim, aba_bloq, aba_base, aba_rel = st.tabs(
        ["Cadastrar OLs", "Limites de acesso ativo", "Bloqueio permanente",
         "Base completa", "📊 Relatórios"])

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
            "(SELECT COUNT(*) FROM cadastros c WHERE c.ol_id=o.id) AS total_cadastrados, "
            "(SELECT COUNT(*) FROM cadastros c WHERE c.ol_id=o.id AND c.situacao='ativo') AS com_acesso_ativo "
            "FROM ols o WHERE o.ativo=1 ORDER BY o.nome").fetchall()
        st.dataframe([dict(r) for r in ols] or [{}], use_container_width=True)

    # --- Limites de acesso ativo ---
    with aba_lim:
        st.caption(
            "**Cadastro é ilimitado** — a OL pode registrar quantos motoboys quiser. "
            "O limite aqui controla quantos podem ter **acesso ativo** (liberado nas catracas) ao mesmo tempo. "
            "Use 0 para sem limite."
        )
        ols = conn.execute("SELECT id, nome, limite_global FROM ols WHERE ativo=1 ORDER BY nome").fetchall()
        if not ols:
            st.info("Cadastre uma OL primeiro.")
        else:
            mapa = {o["nome"]: o for o in ols}
            ol_nome = st.selectbox("OL", list(mapa.keys()))
            ol = mapa[ol_nome]
            with st.form("limites"):
                lg = st.number_input(
                    "Limite global de acessos ativos (0 = sem limite)",
                    min_value=0, value=ol["limite_global"] or 0,
                    help="Máximo de motoboys com acesso ativo somando todas as lojas desta OL.")
                st.write("**Limite de acessos ativos por loja** (0 = sem limite):")
                lojas = conn.execute("SELECT id, nome FROM lojas ORDER BY nome").fetchall()
                novos = {}
                for l in lojas:
                    atual = conn.execute(
                        "SELECT limite FROM ol_loja_limite WHERE ol_id=? AND loja_id=?",
                        (ol["id"], l["id"])).fetchone()
                    # Mostra também quantos estão ativos agora nessa loja
                    ativos_agora = conn.execute(
                        "SELECT COUNT(*) FROM cadastros WHERE ol_id=? AND loja_id=? AND situacao='ativo'",
                        (ol["id"], l["id"])).fetchone()[0]
                    novos[l["id"]] = st.number_input(
                        f"{l['nome']} — {ativos_agora} ativo(s) agora",
                        min_value=0, value=atual["limite"] if atual else 0,
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
                    st.success("Limites de acesso ativo atualizados.")

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

    # --- Base completa ---
    with aba_base:
        st.caption(
            "**Cadastro** = o motoboy existe no sistema (registro permanente). "
            "**Situação de acesso** = se ele está liberado ou suspenso nas catracas (DMP)."
        )
        linhas = conn.execute(
            "SELECT m.nome, m.cpf, "
            "CASE WHEN m.bloqueado_permanente=1 THEN 'Bloqueado permanente' ELSE 'Cadastrado' END AS cadastro, "
            "o.nome AS ol, mol.tipo, mol.placa, mol.valido_ate, "
            "l.nome AS loja, "
            "CASE c.situacao "
            "  WHEN 'ativo'   THEN '✅ Acesso ativo' "
            "  WHEN 'inativo' THEN '🔴 Acesso suspenso' "
            "  ELSE COALESCE(c.situacao, '—') END AS situacao_acesso "
            "FROM motoboys m "
            "LEFT JOIN motoboys_ol mol ON mol.motoboy_id=m.id "
            "LEFT JOIN ols o ON o.id=mol.ol_id "
            "LEFT JOIN cadastros c ON c.motoboy_id=m.id AND c.ol_id=mol.ol_id AND c.situacao='ativo' "
            "LEFT JOIN lojas l ON l.id=c.loja_id "
            "ORDER BY m.bloqueado_permanente DESC, m.nome").fetchall()
        st.dataframe([dict(r) for r in linhas] or [{}], use_container_width=True)
    # =========================================================================
    # ABA RELATÓRIOS
    # =========================================================================
    with aba_rel:
        from datetime import timedelta
        HOJE_REL = HOJE  # alias para clareza

        st.markdown("### 📊 Relatórios operacionais")
        st.caption(
            "Visão consolidada do sistema: quem está ativo, alertas de vencimento, "
            "performance por OL e trilha de auditoria."
        )

        rel_tabs = st.tabs([
            "🏪 Situação por loja",
            "🚨 Alertas",
            "🏢 Resumo por OL",
            "📋 Auditoria",
        ])

        # ------------------------------------------------------------------
        # 1) SITUAÇÃO POR LOJA — quem está ativo agora em cada unidade
        # ------------------------------------------------------------------
        with rel_tabs[0]:
            st.markdown("#### Acesso ativo por loja agora")
            st.caption("Motoboys com situação **ativo** nas catracas no momento.")

            lojas_rel = conn.execute(
                "SELECT id, nome FROM lojas WHERE ativo=1 ORDER BY nome").fetchall()

            for loja in lojas_rel:
                ativos = conn.execute(
                    "SELECT m.nome, m.cpf, o.nome AS ol, mol.tipo, mol.placa, mol.valido_ate "
                    "FROM cadastros c "
                    "JOIN motoboys m ON m.id=c.motoboy_id "
                    "JOIN motoboys_ol mol ON mol.motoboy_id=c.motoboy_id AND mol.ol_id=c.ol_id "
                    "JOIN ols o ON o.id=c.ol_id "
                    "WHERE c.loja_id=? AND c.situacao='ativo' "
                    "ORDER BY o.nome, m.nome",
                    (loja["id"],)
                ).fetchall()

                # Capacidade total desta loja (soma dos limites de todas as OLs)
                cap_total = conn.execute(
                    "SELECT COALESCE(SUM(limite),0) FROM ol_loja_limite WHERE loja_id=?",
                    (loja["id"],)).fetchone()[0]

                n = len(ativos)
                pct = int(n / cap_total * 100) if cap_total > 0 else 0
                cor = "🔴" if pct >= 100 else ("🟡" if pct >= 75 else "🟢")
                titulo = f"{cor} **{loja['nome']}** — {n} ativo(s)"
                if cap_total > 0:
                    titulo += f" / {cap_total} vagas ({pct}%)"

                with st.expander(titulo, expanded=(n > 0)):
                    if not ativos:
                        st.caption("Nenhum motoboy ativo nesta loja.")
                    else:
                        dados = []
                        for r in ativos:
                            tipo_label = "FREE" if r["tipo"] == "free" else "Fixo"
                            validade = r["valido_ate"] if r["tipo"] == "free" else "—"
                            dados.append({
                                "Nome": r["nome"],
                                "CPF": r["cpf"],
                                "OL": r["ol"],
                                "Tipo": tipo_label,
                                "Placa": r["placa"] or "—",
                                "Válido até": validade or "—",
                            })
                        st.dataframe(dados, use_container_width=True, hide_index=True)

        # ------------------------------------------------------------------
        # 2) ALERTAS — itens que precisam de atenção
        # ------------------------------------------------------------------
        with rel_tabs[1]:
            st.markdown("#### Alertas operacionais")

            # --- CNH vencida ou vencendo em 30 dias ---
            st.markdown("**🪪 CNH — vencidas ou vencendo em 30 dias**")
            limite_cnh = HOJE_REL + timedelta(days=30)
            cnh_alertas = conn.execute(
                "SELECT m.nome, m.cpf, m.cnh_venc, o.nome AS ol, "
                "CASE WHEN m.cnh_venc < ? THEN 'VENCIDA' ELSE 'Vence em breve' END AS status "
                "FROM motoboys m "
                "JOIN motoboys_ol mol ON mol.motoboy_id=m.id "
                "JOIN ols o ON o.id=mol.ol_id "
                "WHERE m.cnh_venc IS NOT NULL AND m.cnh_venc <= ? "
                "ORDER BY m.cnh_venc",
                (str(HOJE_REL), str(limite_cnh))
            ).fetchall()
            if cnh_alertas:
                st.dataframe(
                    [{"Nome": r["nome"], "CPF": r["cpf"], "OL": r["ol"],
                      "Vencimento CNH": r["cnh_venc"], "Situação": r["status"]}
                     for r in cnh_alertas],
                    use_container_width=True, hide_index=True)
            else:
                st.success("Nenhuma CNH vencida ou vencendo nos próximos 30 dias.")

            st.divider()

            # --- FREE vencendo em 7 dias ou já vencidos ---
            st.markdown("**🟠 Motoboys FREE — validade vencida ou vencendo em 7 dias**")
            limite_free = HOJE_REL + timedelta(days=7)
            free_alertas = conn.execute(
                "SELECT m.nome, m.cpf, o.nome AS ol, mol.valido_ate, "
                "(SELECT c.situacao FROM cadastros c "
                " WHERE c.motoboy_id=m.id AND c.ol_id=mol.ol_id AND c.situacao='ativo' "
                " LIMIT 1) AS tem_acesso_ativo "
                "FROM motoboys_ol mol "
                "JOIN motoboys m ON m.id=mol.motoboy_id "
                "JOIN ols o ON o.id=mol.ol_id "
                "WHERE mol.tipo='free' AND mol.valido_ate IS NOT NULL AND mol.valido_ate <= ? "
                "ORDER BY mol.valido_ate",
                (str(limite_free),)
            ).fetchall()
            if free_alertas:
                st.dataframe(
                    [{"Nome": r["nome"], "CPF": r["cpf"], "OL": r["ol"],
                      "Válido até": r["valido_ate"],
                      "Acesso ativo?": "Sim" if r["tem_acesso_ativo"] else "Não"}
                     for r in free_alertas],
                    use_container_width=True, hide_index=True)
            else:
                st.success("Nenhum motoboy FREE com validade vencida ou vencendo em 7 dias.")

            st.divider()

            # --- Sem reconhecimento facial (sem foto / dmp_person_id nulo) ---
            st.markdown("**📷 Motoboys sem reconhecimento facial cadastrado**")
            sem_facial = conn.execute(
                "SELECT m.nome, m.cpf, o.nome AS ol, "
                "CASE WHEN m.dmp_person_id IS NULL THEN 'Nunca enviado ao DMP' "
                "     ELSE 'Enviado, aguardando selfie' END AS status_facial "
                "FROM motoboys_ol mol "
                "JOIN motoboys m ON m.id=mol.motoboy_id "
                "JOIN ols o ON o.id=mol.ol_id "
                "WHERE m.foto_path IS NULL "
                "ORDER BY o.nome, m.nome"
            ).fetchall()
            if sem_facial:
                st.dataframe(
                    [{"Nome": r["nome"], "CPF": r["cpf"], "OL": r["ol"],
                      "Status facial": r["status_facial"]}
                     for r in sem_facial],
                    use_container_width=True, hide_index=True)
                st.caption(
                    "Motoboys sem selfie não passam na catraca de reconhecimento facial. "
                    "Reenvie o link na aba **Meus motoboys → Reenviar link**.")
            else:
                st.success("Todos os motoboys têm reconhecimento facial cadastrado.")

            st.divider()

            # --- Motoboys bloqueados permanentemente ---
            st.markdown("**⛔ Bloqueados permanentemente**")
            # juntar nomes de OLs em uma string: função difere entre PG e SQLite
            agg_ols = ("string_agg(o.nome, ' | ')" if db.usando_pg()
                       else "GROUP_CONCAT(o.nome, ' | ')")
            bloq_perm = conn.execute(
                "SELECT m.nome, m.cpf, m.motivo_bloqueio, "
                f"{agg_ols} AS ols "
                "FROM motoboys m "
                "LEFT JOIN motoboys_ol mol ON mol.motoboy_id=m.id "
                "LEFT JOIN ols o ON o.id=mol.ol_id "
                "WHERE m.bloqueado_permanente=1 "
                "GROUP BY m.id ORDER BY m.nome"
            ).fetchall()
            if bloq_perm:
                st.dataframe(
                    [{"Nome": r["nome"], "CPF": r["cpf"],
                      "Motivo": r["motivo_bloqueio"] or "—", "OLs vinculadas": r["ols"] or "—"}
                     for r in bloq_perm],
                    use_container_width=True, hide_index=True)
            else:
                st.info("Nenhum motoboy com bloqueio permanente.")

        # ------------------------------------------------------------------
        # 3) RESUMO POR OL
        # ------------------------------------------------------------------
        with rel_tabs[2]:
            st.markdown("#### Resumo por Operador Logístico")

            ols_rel = conn.execute(
                "SELECT o.id, o.nome, o.limite_global, "
                "(SELECT COUNT(*) FROM motoboys_ol mol WHERE mol.ol_id=o.id) AS total_cadastrados, "
                "(SELECT COUNT(*) FROM cadastros c WHERE c.ol_id=o.id AND c.situacao='ativo') AS ativos_agora, "
                "(SELECT COUNT(DISTINCT c.loja_id) FROM cadastros c WHERE c.ol_id=o.id AND c.situacao='ativo') AS lojas_em_uso "
                "FROM ols o WHERE o.ativo=1 ORDER BY o.nome"
            ).fetchall()

            for o in ols_rel:
                pct_global = 0
                if o["limite_global"] and o["limite_global"] > 0:
                    pct_global = int(o["ativos_agora"] / o["limite_global"] * 100)
                cor_ol = "🔴" if pct_global >= 100 else ("🟡" if pct_global >= 80 else "🟢")

                with st.container(border=True):
                    ca, cb, cc, cd = st.columns(4)
                    ca.metric("OL", o["nome"])
                    cb.metric("Cadastrados", o["total_cadastrados"],
                              help="Motoboys vinculados a esta OL (cadastro permanente).")
                    cc.metric("Ativos agora",
                              f"{o['ativos_agora']}/{o['limite_global'] or '∞'}",
                              help="Com acesso ativo nas catracas neste momento.")
                    cd.metric("Lojas em uso", o["lojas_em_uso"],
                              help="Quantidade de lojas com pelo menos 1 motoboy ativo.")

                    # Detalhe por loja para esta OL
                    detalhe_lojas = conn.execute(
                        "SELECT l.nome AS loja, "
                        "COALESCE(oll.limite,0) AS limite, "
                        "COUNT(c.id) AS ativos "
                        "FROM lojas l "
                        "LEFT JOIN ol_loja_limite oll ON oll.ol_id=? AND oll.loja_id=l.id "
                        "LEFT JOIN cadastros c ON c.loja_id=l.id AND c.ol_id=? AND c.situacao='ativo' "
                        "WHERE l.ativo=1 GROUP BY l.id ORDER BY l.nome",
                        (o["id"], o["id"])
                    ).fetchall()
                    dados_loja = []
                    for dl in detalhe_lojas:
                        ocup = f"{dl['ativos']}/{dl['limite']}" if dl["limite"] > 0 else str(dl["ativos"])
                        status = (
                            "🔴 Cheio" if dl["limite"] > 0 and dl["ativos"] >= dl["limite"]
                            else ("🟡 Quase cheio" if dl["limite"] > 0 and dl["ativos"] >= dl["limite"] * 0.8
                                  else ("🟢 Ok" if dl["ativos"] > 0 else "⚪ Vazio"))
                        )
                        dados_loja.append({
                            "Loja": dl["loja"],
                            "Ocupação": ocup,
                            "Status": status,
                        })
                    st.dataframe(dados_loja, use_container_width=True, hide_index=True)

        # ------------------------------------------------------------------
        # 4) AUDITORIA — trilha de quem fez o quê e quando
        # ------------------------------------------------------------------
        with rel_tabs[3]:
            st.markdown("#### Trilha de auditoria")
            st.caption(
                "Registro completo de todas as ações realizadas no portal: "
                "cadastros, ativações, suspensões, bloqueios e vencimentos automáticos."
            )

            # Filtros
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                filtro_acao = st.selectbox(
                    "Filtrar por ação",
                    ["Todas", "cadastro_motoboy", "ativar_acesso", "suspender_acesso",
                     "bloqueio", "desbloqueio", "vencimento_automatico", "editar_cadastro",
                     "cadastro_ol", "ajuste_limites"],
                    key="rel_filtro_acao",
                )
            with fc2:
                filtro_dias = st.selectbox(
                    "Período",
                    ["Últimos 7 dias", "Últimos 30 dias", "Últimos 90 dias", "Tudo"],
                    key="rel_filtro_dias",
                )
            with fc3:
                filtro_limite = st.number_input(
                    "Máximo de registros", min_value=10, max_value=500,
                    value=100, step=10, key="rel_filtro_limite")

            dias_map = {
                "Últimos 7 dias": 7, "Últimos 30 dias": 30,
                "Últimos 90 dias": 90, "Tudo": None
            }
            n_dias = dias_map[filtro_dias]

            where_parts = []
            params_aud: list = []
            if filtro_acao != "Todas":
                where_parts.append("a.acao = ?")
                params_aud.append(filtro_acao)
            if n_dias:
                corte = str(HOJE_REL - timedelta(days=n_dias))
                where_parts.append("a.criado_em >= ?")
                params_aud.append(corte)

            where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

            auditoria = conn.execute(
                f"SELECT a.criado_em, COALESCE(u.login,'sistema') AS usuario, "
                f"a.acao, a.entidade, a.entidade_id, a.detalhe "
                f"FROM auditoria a "
                f"LEFT JOIN usuarios u ON u.id=a.usuario_id "
                f"{where_sql} "
                f"ORDER BY a.criado_em DESC LIMIT ?",
                params_aud + [filtro_limite]
            ).fetchall()

            ACOES_PT = {
                "cadastro_motoboy": "Cadastro",
                "ativar_acesso": "Ativação",
                "suspender_acesso": "Suspensão",
                "bloqueio": "Bloqueio permanente",
                "desbloqueio": "Desbloqueio",
                "vencimento_automatico": "Vencimento automático",
                "editar_cadastro": "Edição de cadastro",
                "cadastro_ol": "Cadastro de OL",
                "ajuste_limites": "Ajuste de limites",
            }

            if auditoria:
                st.dataframe(
                    [{"Data/hora": r["criado_em"][:19].replace("T", " "),
                      "Usuário": r["usuario"],
                      "Ação": ACOES_PT.get(r["acao"], r["acao"]),
                      "Detalhe": r["detalhe"] or "—"}
                     for r in auditoria],
                    use_container_width=True, hide_index=True)
                st.caption(f"{len(auditoria)} registro(s) exibido(s).")
            else:
                st.info("Nenhum registro de auditoria para os filtros selecionados.")

    conn.close()


# ===========================================================================
# Perfil Operador — fila / motos (esboço; depende dos eventos do DMP)
# ===========================================================================

def tela_operador(usuario):
    st.header("Operação da unidade")

    conn = db.conectar()

    # --- Operador escolhe em qual loja está operando -----------------------
    lojas = conn.execute(
        "SELECT id, nome FROM lojas WHERE ativo=1 ORDER BY nome").fetchall()
    if not lojas:
        st.warning("Nenhuma loja cadastrada.")
        conn.close()
        return

    mapa = {l["nome"]: l["id"] for l in lojas}
    loja_nome = st.selectbox("Loja / unidade", list(mapa.keys()), key="op_loja")
    loja_id = mapa[loja_nome]

    col_a, col_b = st.columns([3, 1])
    with col_b:
        if st.button("🔄 Atualizar", use_container_width=True):
            st.rerun()

    st.divider()

    # =======================================================================
    # 1) MOTOBOYS LIBERADOS NESTA LOJA AGORA (dado real, sem depender do
    #    AccessLog). É o que vale para conferir no teste da catraca: quem
    #    passar pelo reconhecimento facial tem que estar nesta lista.
    # =======================================================================
    st.subheader("✅ Motoboys liberados nesta loja agora")
    st.caption(
        "Estes são os motoboys com acesso ATIVO aqui — o reconhecimento facial "
        "da catraca deve liberar apenas estas pessoas."
    )

    liberados = conn.execute(
        "SELECT m.nome, m.cpf, o.nome AS ol, mol.placa, mol.tipo, "
        "       CASE WHEN m.foto_path IS NOT NULL THEN 'Sim' ELSE 'NÃO' END AS facial "
        "FROM cadastros c "
        "JOIN motoboys m   ON m.id = c.motoboy_id "
        "JOIN motoboys_ol mol ON mol.motoboy_id = c.motoboy_id AND mol.ol_id = c.ol_id "
        "JOIN ols o        ON o.id = c.ol_id "
        "WHERE c.loja_id = ? AND c.situacao = 'ativo' "
        "ORDER BY m.nome",
        (loja_id,)
    ).fetchall()

    if liberados:
        st.dataframe(
            [{"Motoboy": r["nome"], "CPF": r["cpf"], "OL": r["ol"],
              "Placa": r["placa"] or "—",
              "Tipo": "FREE" if r["tipo"] == "free" else "Fixo",
              "Facial cadastrado": r["facial"]}
             for r in liberados],
            use_container_width=True, hide_index=True)
        sem_facial = [r["nome"] for r in liberados if r["facial"] == "NÃO"]
        if sem_facial:
            st.warning(
                "⚠️ Sem reconhecimento facial (não vão passar na catraca): "
                + ", ".join(sem_facial)
            )
    else:
        st.info("Nenhum motoboy liberado nesta loja no momento.")

    st.divider()

    # =======================================================================
    # 2) FILA / ÚLTIMOS ACESSOS (depende do AccessLog do DMP)
    # =======================================================================
    st.subheader("🛵 Movimentação na catraca")

    # Tenta puxar eventos novos do DMP.
    estado = conn.execute(
        "SELECT ultimo_pointer FROM acesso_estado WHERE id=1").fetchone()
    ultimo_pointer = estado["ultimo_pointer"] if estado else 0

    erro_accesslog = None
    novos = []
    if st.button("📡 Buscar acessos no DMP", help="Lê os eventos de entrada/saída da catraca."):
        try:
            novos = dmp.ler_acessos_desde(ultimo_pointer)
        except Exception as ex:
            erro_accesslog = ex

        # Persiste os eventos novos (estrutura defensiva — formato do DMP varia).
        maior_ptr = ultimo_pointer
        for ev in (novos or []):
            try:
                ptr = int(ev.get("Pointer", ev.get("Id", 0)) or 0)
                cpf_ev = str(ev.get("Cpf", ev.get("RegistrationNumber", "")))
                nome_ev = ev.get("Name", ev.get("PersonName", ""))
                quando = ev.get("AccessDate", ev.get("Date", ""))
                mb = conn.execute(
                    "SELECT id FROM motoboys WHERE cpf=? OR cpf=?",
                    (cpf_ev, "".join(filter(str.isdigit, cpf_ev)))
                ).fetchone()
                conn.execute(
                    "INSERT INTO acesso_eventos "
                    "(motoboy_id, loja_id, cpf, nome, tipo, ocorrido_em, dmp_pointer) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (mb["id"] if mb else None, loja_id, cpf_ev, nome_ev,
                     "entrada", str(quando), ptr))
                maior_ptr = max(maior_ptr, ptr)
            except Exception:
                continue
        if maior_ptr > ultimo_pointer:
            conn.execute("UPDATE acesso_estado SET ultimo_pointer=? WHERE id=1",
                         (maior_ptr,))
            conn.commit()

    if erro_accesslog:
        msg = str(erro_accesslog)
        if "401" in msg or "denied" in msg.lower():
            st.warning(
                "🔒 O AccessLog (eventos da catraca) ainda **não está liberado** "
                "para esta conta no DMP. Solicite à DIMEP/TAGUS-TEC a permissão "
                "`ACCESS_LOG` para o CNPJ 32757781000150. "
                "Enquanto isso, a lista de liberados acima já permite validar o teste facial."
            )
        else:
            st.error(f"Erro ao ler o AccessLog: {msg}")
    elif novos:
        st.success(f"{len(novos)} evento(s) novos importados do DMP.")

    # Mostra os últimos acessos registrados desta loja.
    eventos = conn.execute(
        "SELECT nome, cpf, tipo, ocorrido_em FROM acesso_eventos "
        "WHERE loja_id=? ORDER BY id DESC LIMIT 20",
        (loja_id,)
    ).fetchall()

    if eventos:
        st.dataframe(
            [{"Motoboy": e["nome"] or e["cpf"], "CPF": e["cpf"],
              "Evento": e["tipo"], "Quando": e["ocorrido_em"]}
             for e in eventos],
            use_container_width=True, hide_index=True)
    else:
        st.caption("Nenhum acesso registrado ainda nesta loja.")

    conn.close()


# ===========================================================================
# Tela de selfie — pública, sem login, acessada pelo motoboy via link
# ===========================================================================

def tela_selfie():
    """
    Página aberta pelo motoboy no celular para tirar a selfie.
    URL: ?page=selfie&token=<token>
    Não requer login. Envia a foto direto para o DMP via dmp.atualizar_foto().
    """
    token = st.query_params.get("token", "")

    st.title("📷 Cadastro facial")
    st.caption("Grupo Bueno — Portal de Motoboys")

    if not token:
        st.error("Link inválido. Solicite um novo link à sua empresa.")
        st.stop()

    conn = db.conectar()
    try:
        link = conn.execute(
            "SELECT sl.token, sl.motoboy_id, sl.expira_em, sl.usado_em, "
            "m.nome, m.cpf "
            "FROM selfie_links sl "
            "JOIN motoboys m ON m.id = sl.motoboy_id "
            "WHERE sl.token = ?",
            (token,)
        ).fetchone()
    finally:
        conn.close()

    if not link:
        st.error("Link não encontrado. Solicite um novo link à sua empresa.")
        st.stop()

    if link["usado_em"]:
        st.success(f"Foto já enviada em {link['usado_em'][:16]}. "
                   "Seu reconhecimento facial já está cadastrado.")
        st.stop()

    from datetime import date as _date
    if link["expira_em"] < str(_date.today()):
        st.error(f"Link expirado em {link['expira_em']}. "
                 "Solicite um novo link à sua empresa.")
        st.stop()

    nome = link["nome"]
    cpf  = link["cpf"]

    st.markdown(f"### Olá, **{nome}**!")
    st.markdown(
        "Para liberar seu acesso às lojas via reconhecimento facial, "
        "tire uma selfie abaixo."
    )
    st.info(
        "**Dicas para uma boa foto:**\n"
        "- Rosto centralizado e bem iluminado\n"
        "- Sem óculos escuros ou boné\n"
        "- Olhe direto para a câmera"
    )

    foto = st.camera_input("Tire a selfie", key="cam_selfie")

    if foto:
        foto_bytes = foto.read()
        st.image(foto_bytes, caption="Foto capturada", use_container_width=True)

        if st.button("✅ Confirmar e enviar foto", type="primary"):
            with st.spinner("Enviando para o sistema..."):
                try:
                    dmp.atualizar_foto(cpf, nome, foto_bytes)
                    conn2 = db.conectar()
                    try:
                        conn2.execute(
                            "UPDATE selfie_links SET usado_em=? WHERE token=?",
                            (datetime.now().isoformat(timespec="seconds"), token)
                        )
                        conn2.execute(
                            "UPDATE motoboys SET foto_path=? WHERE cpf=?",
                            (f"dmp_upload:{token}", cpf)
                        )
                        db.auditar(conn2, None, "selfie_enviada", "motoboy",
                                   link["motoboy_id"],
                                   f"{nome} — foto enviada ao DMP")
                        conn2.commit()
                    finally:
                        conn2.close()

                    st.success("✅ Foto enviada com sucesso! "
                               "Seu reconhecimento facial está ativo.")
                    st.balloons()
                    st.stop()

                except Exception as ex:
                    st.error(f"Erro ao enviar: {ex}")
                    st.caption("Tente novamente ou entre em contato com sua empresa.")


# ===========================================================================
# Roteamento
# ===========================================================================

def main():
    # Rota pública: selfie (antes de qualquer verificação de login)
    if st.query_params.get("page") == "selfie":
        tela_selfie()
        return

    # Roda a cada carregamento: suspende acesso de motoboys free vencidos às 18:30.
    _desativar_free_vencidos()
    # Sincroniza exclusões: quem foi apagado no DMP some do portal também.
    _sincronizar_exclusoes_dmp()

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
