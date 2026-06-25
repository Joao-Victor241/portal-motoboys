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
from datetime import datetime


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

    # ---- Autenticação -----------------------------------------------------

    def logon(self) -> str:
        """Autentica (v1) e guarda o bearer da sessão."""
        if self.simulado:
            self._bearer = "BEARER-SIMULADO"
            self._bearer_expira_em = time.time() + 1700
            return self._bearer
        resp = requests.get(
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
            resp = requests.get(
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
                g = requests.get(
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
        """Converte 'AAAA-MM-DD' (ou date) no fim daquele dia em ISO. None se vazio."""
        if not valido_ate:
            return None
        return f"{str(valido_ate)[:10]}T23:59:59"

    def criar_credencial_face(self, cpf, person_id=None, valido_ate=None) -> dict:
        """
        Cria a credencial e a associa à pessoa para uso no FACE — passo
        obrigatório para o reconhecimento facial liberar a catraca.

        Replica o que é feito manualmente no DMP (aba Credenciais → "Adicionar"
        + "Credencial para uso no FACE"). Estrutura validada no cadastro do
        Felipe DFControl (tipo PESSOA=1, tecnologia Proximidade=3, Number=CPF,
        estrutura 398) e confirmada pelo exemplo enviado pela Dimep.

        Validade:
          - valido_ate informado (motoboy FREE) → credencial TEMPORÁRIA, válida
            até o fim daquele dia. A facial para de funcionar quando expira.
          - valido_ate vazio (motoboy FIXO) → credencial PERMANENTE.
        Idempotente: se a credencial/associação já existir, não quebra.
        """
        numero = int("".join(filter(str.isdigit, str(cpf))))
        fim = self._fim_do_dia(valido_ate)
        temporaria = fim is not None
        if self.simulado:
            return {"_simulado": True, "credencial": numero, "face": True,
                    "valido_ate": fim}

        # 1) Cria a credencial (validade conforme o tipo do motoboy).
        corpo_cred = {
            "CredentialType": 1,                       # PESSOA
            "TechnologyType": 3,                        # Proximity
            "DurationType": 0 if temporaria else 1,    # 0=Temporária, 1=Permanente
            "CredentialStatus": 0,                      # Válida
            "MasterType": 0,                            # Não master
            "Number": numero,
            "OrganizationalStructure": self.org_structure,
            "IsCredentialPublic": False,
            "IsEquipmentSupervisor": False,
        }
        if temporaria:
            corpo_cred["ValidityBegin"] = datetime.now().isoformat(timespec="seconds")
            corpo_cred["ValidityEnd"] = fim
        r1 = requests.post(f"{self.base_url}/api/v1/Credential", json=corpo_cred,
                           headers=self._auth(), timeout=30)
        # 400/409 normalmente = já existe; só levanta para erros inesperados.
        ja_existe = r1.status_code in (400, 409)
        if r1.status_code not in (200, 201, 204, 400, 409):
            r1.raise_for_status()
        # Se já existia, atualiza a validade (caso o motoboy tenha mudado de prazo).
        if ja_existe:
            requests.put(f"{self.base_url}/api/v1/Credential/{numero}", json=corpo_cred,
                         headers=self._auth(), timeout=30)

        # 2) Descobre o PersonId, se não foi passado.
        if person_id is None:
            g = requests.get(f"{self.base_url}/api/v1/Person/{numero}",
                             headers=self._auth(), timeout=30)
            if g.status_code == 200 and g.json():
                js = g.json()
                person_id = (js[0] if isinstance(js, list) else js).get("Id")

        # 3) Associa a credencial à pessoa, marcando uso no FACE (com prazo).
        corpo_assoc = {
            "PersonId": person_id,
            "CredentialNumber": numero,
            "InitialDate": datetime.now().isoformat(timespec="seconds"),
            "FinalDate": fim,
            "ForREPUse": False,
            "ForFaceUse": True,
        }
        r2 = requests.post(f"{self.base_url}/api/v1/PersonCredential/Association",
                           json=corpo_assoc, headers=self._auth(), timeout=30)
        if r2.status_code not in (200, 201, 204, 400, 409):
            r2.raise_for_status()
        return {"ok": True, "credencial": numero, "person_id": person_id,
                "face": True, "valido_ate": fim}

    def cadastrar_pessoa(self, cpf, nome, foto_bytes: bytes | None = None,
                         telefone: str | None = None,
                         com_credencial_face: bool = True,
                         valido_ate=None) -> dict:
        """POST /api/v1/Person — cadastra o motoboy como ACESSO PERMITIDO, com foto se houver.

        Se com_credencial_face=True (padrão), já cria a credencial e associa
        para uso no FACE — assim o motoboy fica pronto para o reconhecimento
        facial assim que enviar a selfie. valido_ate (motoboy FREE) define a
        validade da credencial; vazio = permanente (FIXO).
        """
        foto_b64 = base64.b64encode(foto_bytes).decode() if foto_bytes else None
        corpo = self._montar_pessoa(cpf, nome, foto_b64, telefone, self.situ_permitido)
        if self.simulado:
            return {"Id": corpo["RegistrationNumber"], "_simulado": True,
                    "credencial_face_ok": True}
        resp = requests.post(f"{self.base_url}/api/v1/Person", json=corpo,
                             headers=self._auth(), timeout=30)
        resp.raise_for_status()
        # O POST ecoa o corpo (Id=0). Buscamos o registro para pegar o Id real do DMP.
        reg = corpo["RegistrationNumber"]
        pessoa = corpo
        g = requests.get(f"{self.base_url}/api/v1/Person/{reg}",
                         headers=self._auth(), timeout=30)
        if g.status_code == 200 and g.json():
            js = g.json()
            pessoa = js[0] if isinstance(js, list) else js
        # Cria a credencial + associação FACE (não impede o cadastro se falhar).
        if com_credencial_face:
            try:
                self.criar_credencial_face(cpf, person_id=pessoa.get("Id"),
                                           valido_ate=valido_ate)
                pessoa["credencial_face_ok"] = True
            except Exception as e:
                pessoa["credencial_face_ok"] = False
                pessoa["credencial_face_erro"] = str(e)
        return pessoa

    def atualizar_foto(self, cpf, nome, foto_bytes: bytes) -> dict:
        """PUT /api/v1/Person — atualiza a pessoa com a foto (selfie) enviada pelo motoboy."""
        foto_b64 = base64.b64encode(foto_bytes).decode()
        corpo = self._montar_pessoa(cpf, nome, foto_b64, None, self.situ_permitido)
        if self.simulado:
            return {"_simulado": True, "bytes": len(foto_bytes)}
        resp = requests.put(f"{self.base_url}/api/v1/Person", json=corpo,
                            headers=self._auth(), timeout=30)
        resp.raise_for_status()
        return {"ok": True}

    def liberar_pessoa(self, cpf, nome) -> dict:
        """PUT /api/v1/Person com PersonSituation = ACESSO PERMITIDO (10). Usa na reativação."""
        corpo = self._montar_pessoa(cpf, nome, None, None, self.situ_permitido)
        if self.simulado:
            return {"_simulado": True, "situacao": self.situ_permitido}
        resp = requests.put(f"{self.base_url}/api/v1/Person", json=corpo,
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
        resp = requests.put(f"{self.base_url}/api/v1/Person", json=corpo,
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
            r = requests.get(f"{self.base_url}/api/v1/Person", headers=self._auth(),
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
        resp = requests.get(f"{self.base_url}/api/v1/AccessLog/Pointer/{ponteiro}",
                            headers=self._auth(), timeout=30)
        resp.raise_for_status()
        return resp.json()
