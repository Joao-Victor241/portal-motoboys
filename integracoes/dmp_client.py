"""
Adaptador da API do DMP Access II (Dimep) — reconhecimento facial.

Validado AO VIVO contra produção em 18/06/2026 (conta VOGA PARK):
  - Base: https://dmpaccess.dimep-ams.com.br/itk
  - Auth: header `Authorization: NAK <token>` no Logon; depois `Bearer <token>`.
  - GET /api/v1/Logon (v1) funciona; bearer expira em ~30 min.
  - O cadastro de pessoa (POST /api/v1/Person) usa o CPF como RegistrationNumber
    e tem o campo `Photo` (foto em base64) — é assim que a face é matriculada
    (não precisa de template de sensor). PUT /api/v1/Person atualiza.
  - Situações de acesso reais: 10 = ACESSO PERMITIDO, 11 = ACESSO BLOQUEADO.
  - Estrutura organizacional: 398 (VOGA PARK). Perfil de acesso: 47 (ACESSO VOGA DF).
  - Eventos de entrada/saída p/ fila FIFO: GET /api/v1/AccessLog/Pointer/{id}.

Modo `simulado=True`: não toca na rede (para desenvolver sem criar registros
reais no DMP). `simulado=False`: integra de verdade.
"""

from __future__ import annotations

import os
import time
import base64
import requests
from datetime import datetime, timezone, timedelta

# Fuso de Brasília (UTC-3, sem horário de verão desde 2019). Usado para os
# horários enviados ao DMP — senão, na nuvem (servidor em UTC) ficariam +3h.
FUSO_BR = timezone(timedelta(hours=-3))


def _agora_br_iso() -> str:
    """Horário atual de Brasília em ISO sem fuso (ex.: 2026-06-25T16:14:45),
    no mesmo formato que o DMP grava (naive, hora local)."""
    return datetime.now(FUSO_BR).replace(tzinfo=None).isoformat(timespec="seconds")


class DMPClient:
    def __init__(self, simulado: bool = True):
        self.simulado = simulado
        self.base_url = os.getenv("DMP_BASE_URL", "").rstrip("/")
        self.username = os.getenv("DMP_USERNAME", "")
        self.password = os.getenv("DMP_PASSWORD", "")
        self.nak = os.getenv("DMP_NAK", "")
        # Configurações específicas da conta (com defaults descobertos na API).
        self.org_structure = int(os.getenv("DMP_ORG_STRUCTURE", "398"))
        self.access_profile = int(os.getenv("DMP_ACCESS_PROFILE", "47"))
        self.situ_permitido = int(os.getenv("DMP_SITU_PERMITIDO", "10"))
        self.situ_bloqueado = int(os.getenv("DMP_SITU_BLOQUEADO", "11"))
        self._bearer: str | None = None
        self._bearer_expira_em: float = 0.0
        # Sessão HTTP reutilizável (keep-alive) — acelera chamadas repetidas.
        self._sessao = requests.Session()

    # ---- Autenticação -----------------------------------------------------

    def logon(self) -> str:
        """Autentica (v1) e guarda o bearer da sessão."""
        if self.simulado:
            self._bearer = "BEARER-SIMULADO"
            self._bearer_expira_em = time.time() + 1700
            return self._bearer
        resp = self._sessao.get(
            f"{self.base_url}/api/v1/Logon",
            params={"username": self.username, "password": self.password, "culture": "pt-BR"},
            headers={"Authorization": f"NAK {self.nak}"},
            timeout=30,
        )
        resp.raise_for_status()
        dados = resp.json()
        self._bearer = dados["access_token"]
        self._bearer_expira_em = time.time() + int(dados.get("expires_in", 1700)) - 60
        return self._bearer

    def _auth(self) -> dict:
        if not self.simulado and (not self._bearer or time.time() >= self._bearer_expira_em):
            self.logon()
        return {"Authorization": f"Bearer {self._bearer}"}

    def diagnostico(self) -> dict:
        """
        Testa a conexão com o DMP e devolve um relatório (sem levantar exceção).
        Usado pelo painel admin para confirmar a integração ao vivo — inclusive
        quando o app roda no Streamlit Cloud (IP/segredos diferentes do local).
        """
        info = {
            "simulado": self.simulado,
            "base_url": self.base_url,
            "username": self.username,
            "tem_nak": bool(self.nak),
            "nak_len": len(self.nak or ""),
            "ok": False,
            "user_name": None,
            "expira_em": None,
            "erro": None,
        }
        if self.simulado:
            info["erro"] = "App está em MODO SIMULADO (DMP_SIMULADO != false)."
            return info
        try:
            resp = self._sessao.get(
                f"{self.base_url}/api/v1/Logon",
                params={"username": self.username, "password": self.password,
                        "culture": "pt-BR"},
                headers={"Authorization": f"NAK {self.nak}"},
                timeout=30,
            )
            if resp.status_code == 200:
                dados = resp.json()
                info["ok"] = True
                info["user_name"] = dados.get("user_name")
                info["expira_em"] = dados.get("expires")
                # Conta quantas pessoas existem no DMP (sanity check do Bearer).
                bearer = dados.get("access_token", "")
                g = self._sessao.get(
                    f"{self.base_url}/api/v1/Person",
                    headers={"Authorization": f"Bearer {bearer}"},
                    params={"pageSize": 1, "pageIndex": 0}, timeout=30,
                )
                info["leitura_pessoas_ok"] = (g.status_code == 200)
            else:
                info["erro"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as ex:
            info["erro"] = str(ex)
        return info

    # ---- Cadastro de pessoa (motoboy) ------------------------------------

    def _montar_pessoa(self, cpf, nome, foto_base64, telefone, situacao) -> dict:
        # RegistrationNumber = CPF (numérico), como o DMP usa.
        corpo = {
            "RegistrationNumber": int("".join(filter(str.isdigit, cpf))),
            "Name": nome,
            "Cpf": cpf,
            "OrganizationalStructure": self.org_structure,
            "OrganizationalStructureCompany": self.org_structure,
            "AccessProfile": self.access_profile,
            "PersonSituation": situacao,
        }
        if telefone:
            corpo["CellPhone"] = telefone
        if foto_base64:
            corpo["Photo"] = foto_base64
        return corpo

    @staticmethod
    def _fim_do_dia(valido_ate) -> str | None:
        """Converte 'AAAA-MM-DD' (ou date) nas 18:30 daquele dia em ISO. None se vazio.
        18:30 é o horário de corte oficial dos motoboys free."""
        if not valido_ate:
            return None
        return f"{str(valido_ate)[:10]}T18:30:00"

    def _situacao_atual(self, cpf):
        """Lê a PersonSituation atual da pessoa no DMP (None se não achar)."""
        try:
            numero = int("".join(filter(str.isdigit, str(cpf))))
            g = self._sessao.get(f"{self.base_url}/api/v1/Person/{numero}",
                             headers=self._auth(), timeout=30)
            if g.status_code == 200 and g.json():
                js = g.json()
                return (js[0] if isinstance(js, list) else js).get("PersonSituation")
        except Exception:
            pass
        return None

    def _corpo_credencial(self, numero, valido_ate=None, ativa=True) -> dict:
        """Monta o corpo da credencial. ativa=True → CredentialStatus=0 (válida);
        False → 1 (bloqueada). Pública/global (chega nas leitoras), tipo PESSOA,
        Proximidade. FREE (valido_ate) = temporária até 18:30; FIXO = permanente."""
        fim = self._fim_do_dia(valido_ate)
        temporaria = fim is not None
        corpo = {
            "CredentialType": 1,                       # PESSOA
            "TechnologyType": 3,                        # Proximity
            "DurationType": 0 if temporaria else 1,    # 0=Temporária, 1=Permanente
            "CredentialStatus": 0 if ativa else 1,     # 0=Válida, 1=Bloqueada
            "MasterType": 0,
            "Number": numero,
            "OrganizationalStructure": 0,              # 0 = pública (chega nas leitoras)
            "IsCredentialPublic": True,
            "IsEquipmentSupervisor": False,
        }
        if not ativa:
            # Obrigatório quando CredentialStatus = Bloqueada.
            corpo["BlockingReason"] = "Acesso suspenso pelo portal"
        if temporaria:
            corpo["ValidityBegin"] = _agora_br_iso()
            corpo["ValidityEnd"] = fim
        return corpo

    def definir_status_credencial(self, cpf, ativa: bool, valido_ate=None) -> dict:
        """Liga/desliga a credencial na leitora rapidamente (1 PUT).
        É O QUE A LEITORA OBEDECE: ativa=True libera o reconhecimento facial;
        ativa=False bloqueia. Usado no ativar/suspender (resposta rápida)."""
        numero = int("".join(filter(str.isdigit, str(cpf))))
        if self.simulado:
            return {"_simulado": True, "ativa": ativa}
        corpo = self._corpo_credencial(numero, valido_ate, ativa)
        r = self._sessao.put(f"{self.base_url}/api/v1/Credential/{numero}",
                             json=corpo, headers=self._auth(), timeout=30)
        if r.status_code not in (200, 201, 204):
            r.raise_for_status()
        return {"ok": True, "ativa": ativa}

    def _person_id(self, numero):
        """Busca o Id interno da pessoa no DMP a partir do número (CPF)."""
        g = self._sessao.get(f"{self.base_url}/api/v1/Person/{numero}",
                             headers=self._auth(), timeout=30)
        if g.status_code == 200 and g.json():
            js = g.json()
            return (js[0] if isinstance(js, list) else js).get("Id")
        return None

    def garantir_credencial(self, cpf, valido_ate=None) -> dict:
        """Cria/atualiza a credencial (SEM vincular ao FACE). Sempre válida.
        FREE = validade até 18:30 do valido_ate; FIXO = permanente. Idempotente."""
        numero = int("".join(filter(str.isdigit, str(cpf))))
        if self.simulado:
            return {"_simulado": True, "credencial": numero}
        corpo = self._corpo_credencial(numero, valido_ate, ativa=True)
        r = self._sessao.post(f"{self.base_url}/api/v1/Credential", json=corpo,
                              headers=self._auth(), timeout=30)
        if r.status_code in (400, 409):           # já existe → atualiza
            self._sessao.put(f"{self.base_url}/api/v1/Credential/{numero}", json=corpo,
                             headers=self._auth(), timeout=30)
        elif r.status_code not in (200, 201, 204):
            r.raise_for_status()
        return {"ok": True, "credencial": numero}

    def vincular_face(self, cpf, person_id=None, valido_ate=None) -> dict:
        """ATIVA o reconhecimento facial do motoboy FREE com prazo.

        A VALIDADE fica na CREDENCIAL (atualizável por PUT, sem recriar):
          - Válida de  (ValidityBegin) = agora (momento da ativação)
          - Válida até (ValidityEnd)   = 18:30 do valido_ate (data do cadastro)
        A associação FACE é criada UMA vez (o DMP não deixa re-associar a mesma
        credencial). Editar a data depois só atualiza a credencial."""
        numero = int("".join(filter(str.isdigit, str(cpf))))
        fim = self._fim_do_dia(valido_ate)
        if self.simulado:
            return {"_simulado": True, "credencial": numero, "valido_ate": fim}

        # 1) Cria/atualiza a credencial com a validade (ValidityBegin=agora, ValidityEnd=fim).
        self.garantir_credencial(cpf, valido_ate)
        if person_id is None:
            person_id = self._person_id(numero)

        # 2) Já existe vínculo FACE (não-baixado)? Não dá pra re-associar a mesma
        # credencial — então só cria se ainda não houver.
        ja_vinculado = False
        a = self._sessao.get(f"{self.base_url}/api/v1/PersonCredential/{person_id}",
                             headers=self._auth(), timeout=30)
        if a.status_code == 200 and a.text.strip():
            for x in a.json():
                if (x.get("CredentialReleasementDatetime") is None and x.get("ForFaceUse")
                        and int(x.get("CredentialNumber") or 0) == numero):
                    ja_vinculado = True
                    break

        if not ja_vinculado:
            corpo_assoc = {
                "PersonId": person_id,
                "CredentialNumber": numero,
                "InitialDate": _agora_br_iso(),
                "FinalDate": fim,
                "ForREPUse": False,
                "ForFaceUse": True,
            }
            r = self._sessao.post(f"{self.base_url}/api/v1/PersonCredential/Association",
                                  json=corpo_assoc, headers=self._auth(), timeout=30)
            if r.status_code not in (200, 201, 204, 400, 409):
                r.raise_for_status()
        return {"ok": True, "credencial": numero, "person_id": person_id,
                "valido_ate": fim, "ja_vinculado": ja_vinculado}

    def desvincular_face(self, cpf, person_id=None) -> dict:
        """SUSPENDE o reconhecimento facial: desvincula (WriteOff) as associações
        FACE ativas da pessoa — tira o rosto da leitora."""
        numero = int("".join(filter(str.isdigit, str(cpf))))
        if self.simulado:
            return {"_simulado": True, "desvinculadas": 1}
        if person_id is None:
            person_id = self._person_id(numero)
        if not person_id:
            return {"ok": False, "motivo": "pessoa não encontrada"}
        a = self._sessao.get(f"{self.base_url}/api/v1/PersonCredential/{person_id}",
                             headers=self._auth(), timeout=30)
        n = 0
        if a.status_code == 200 and a.text.strip():
            for x in a.json():
                # só as associações ativas (sem baixa) com uso no FACE
                if x.get("FinalDate") is None and x.get("ForFaceUse"):
                    cred = int(x.get("CredentialNumber"))
                    self._sessao.post(
                        f"{self.base_url}/api/v1/PersonCredential/WriteOff/{person_id}/{cred}",
                        headers=self._auth(), timeout=30)
                    n += 1
        return {"ok": True, "desvinculadas": n}

    # Compatibilidade: usado no cadastro/ativação. Aqui apenas vincula.
    def criar_credencial_face(self, cpf, person_id=None, valido_ate=None, **_) -> dict:
        return self.vincular_face(cpf, person_id=person_id, valido_ate=valido_ate)

    def cadastrar_pessoa(self, cpf, nome, foto_bytes: bytes | None = None,
                         telefone: str | None = None,
                         com_credencial_face: bool = True,
                         valido_ate=None, liberado: bool = False) -> dict:
        """POST /api/v1/Person — cadastra o motoboy no DMP.

        liberado=False (padrão): entra BLOQUEADO (ACESSO BLOQUEADO). O cadastro
        sozinho NÃO libera a catraca — só quando o acesso for ATIVADO em uma loja
        (liberar_pessoa). liberado=True força ACESSO PERMITIDO de imediato.

        Se com_credencial_face=True (padrão), já cria a credencial e associa
        para uso no FACE — a facial fica enrolada, mas só abre a catraca quando
        a pessoa estiver PERMITIDA. valido_ate (FREE) define a validade da
        credencial (até 18:30 daquele dia); vazio = permanente (FIXO).
        """
        situacao = self.situ_permitido if liberado else self.situ_bloqueado
        foto_b64 = base64.b64encode(foto_bytes).decode() if foto_bytes else None
        corpo = self._montar_pessoa(cpf, nome, foto_b64, telefone, situacao)
        if self.simulado:
            return {"Id": corpo["RegistrationNumber"], "_simulado": True,
                    "credencial_face_ok": True}
        resp = self._sessao.post(f"{self.base_url}/api/v1/Person", json=corpo,
                             headers=self._auth(), timeout=30)
        resp.raise_for_status()
        # O POST ecoa o corpo (Id=0). Buscamos o registro para pegar o Id real do DMP.
        reg = corpo["RegistrationNumber"]
        pessoa = corpo
        g = self._sessao.get(f"{self.base_url}/api/v1/Person/{reg}",
                         headers=self._auth(), timeout=30)
        if g.status_code == 200 and g.json():
            js = g.json()
            pessoa = js[0] if isinstance(js, list) else js
        # NÃO cria credencial no cadastro. O acesso é controlado pela SITUAÇÃO
        # (permitido/bloqueado), que manda os comandos de adicionar/retirar a
        # biometria na leitora. A credencial é criada só para motoboy FREE, na
        # ativação, para limitar a validade (auto-remoção no vencimento).
        return pessoa

    def atualizar_foto(self, cpf, nome, foto_bytes: bytes) -> dict:
        """PUT /api/v1/Person — atualiza a pessoa com a foto (selfie) enviada pelo motoboy.
        Preserva a situação de acesso atual: enviar a selfie NÃO libera a catraca
        (se a pessoa está bloqueada/aguardando ativação, continua bloqueada)."""
        foto_b64 = base64.b64encode(foto_bytes).decode()
        if self.simulado:
            return {"_simulado": True, "bytes": len(foto_bytes)}
        situacao = self._situacao_atual(cpf)
        if situacao is None:
            situacao = self.situ_bloqueado
        corpo = self._montar_pessoa(cpf, nome, foto_b64, None, situacao)
        resp = self._sessao.put(f"{self.base_url}/api/v1/Person", json=corpo,
                            headers=self._auth(), timeout=30)
        resp.raise_for_status()
        return {"ok": True}

    def liberar_pessoa(self, cpf, nome) -> dict:
        """PUT /api/v1/Person com PersonSituation = ACESSO PERMITIDO (10). Usa na reativação."""
        corpo = self._montar_pessoa(cpf, nome, None, None, self.situ_permitido)
        if self.simulado:
            return {"_simulado": True, "situacao": self.situ_permitido}
        resp = self._sessao.put(f"{self.base_url}/api/v1/Person", json=corpo,
                            headers=self._auth(), timeout=30)
        resp.raise_for_status()
        return {"ok": True}

    def bloquear_pessoa(self, cpf, nome) -> dict:
        """PUT /api/v1/Person com PersonSituation = ACESSO BLOQUEADO (11)."""
        # PersonSituation é GLOBAL — bloqueia em todas as lojas/equipamentos.
        # Só chamar quando o motoboy não tiver mais nenhum acesso ativo.
        corpo = self._montar_pessoa(cpf, nome, None, None, self.situ_bloqueado)
        if self.simulado:
            return {"_simulado": True, "situacao": self.situ_bloqueado}
        resp = self._sessao.put(f"{self.base_url}/api/v1/Person", json=corpo,
                            headers=self._auth(), timeout=30)
        resp.raise_for_status()
        return {"ok": True}

    def listar_cpfs(self) -> set:
        """
        Devolve o conjunto de identificadores das pessoas existentes no DMP
        (CPF só dígitos e RegistrationNumber). Usado para sincronizar exclusões:
        quem sumiu do DMP é removido do portal. Pagina a lista /Person.
        """
        if self.simulado:
            return set()
        ids = set()
        page = 0
        while page <= 50:  # trava de segurança (máx 50 páginas)
            r = self._sessao.get(f"{self.base_url}/api/v1/Person", headers=self._auth(),
                             params={"pageSize": 200, "pageIndex": page}, timeout=30)
            r.raise_for_status()
            js = r.json()
            pessoas = js if isinstance(js, list) else js.get("items", js.get("Items", []))
            if not pessoas:
                break
            for p in pessoas:
                if p.get("Cpf"):
                    ids.add("".join(filter(str.isdigit, str(p["Cpf"]))))
                if p.get("RegistrationNumber"):
                    ids.add(str(int(p["RegistrationNumber"])))
            if len(pessoas) < 200:
                break
            page += 1
        return ids

    # ---- Eventos de acesso (entrada/saída) p/ fila FIFO -------------------

    def ler_acessos_desde(self, ponteiro: int) -> list[dict]:
        """GET /api/v1/AccessLog/Pointer/{id} — leitura incremental dos acessos."""
        if self.simulado:
            return []
        resp = self._sessao.get(f"{self.base_url}/api/v1/AccessLog/Pointer/{ponteiro}",
                            headers=self._auth(), timeout=30)
        resp.raise_for_status()
        return resp.json()
