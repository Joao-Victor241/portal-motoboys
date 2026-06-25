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

    def cadastrar_pessoa(self, cpf, nome, foto_bytes: bytes | None = None,
                         telefone: str | None = None) -> dict:
        """POST /api/v1/Person — cadastra o motoboy como ACESSO PERMITIDO, com foto se houver."""
        foto_b64 = base64.b64encode(foto_bytes).decode() if foto_bytes else None
        corpo = self._montar_pessoa(cpf, nome, foto_b64, telefone, self.situ_permitido)
        if self.simulado:
            return {"Id": corpo["RegistrationNumber"], "_simulado": True}
        resp = requests.post(f"{self.base_url}/api/v1/Person", json=corpo,
                             headers=self._auth(), timeout=30)
        resp.raise_for_status()
        # O POST ecoa o corpo (Id=0). Buscamos o registro para pegar o Id real do DMP.
        reg = corpo["RegistrationNumber"]
        g = requests.get(f"{self.base_url}/api/v1/Person/{reg}",
                         headers=self._auth(), timeout=30)
        if g.status_code == 200 and g.json():
            js = g.json()
            return js[0] if isinstance(js, list) else js
        return corpo

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

    # ---- Eventos de acesso (entrada/saída) p/ fila FIFO -------------------

    def ler_acessos_desde(self, ponteiro: int) -> list[dict]:
        """GET /api/v1/AccessLog/Pointer/{id} — leitura incremental dos acessos."""
        if self.simulado:
            return []
        resp = requests.get(f"{self.base_url}/api/v1/AccessLog/Pointer/{ponteiro}",
                            headers=self._auth(), timeout=30)
        resp.raise_for_status()
        return resp.json()
