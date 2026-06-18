"""
Diagnóstico de conectividade com a API do DMP Access II.

Roda uma bateria de testes seguros (só leitura/autenticação) e mostra o que
está funcionando. Use sempre que a Dimep mexer no ambiente ou mandar o spec
da v2, para revalidar rápido.

    python diagnostico_dmp.py
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("DMP_BASE_URL", "https://dmpaccess.dimep-ams.com.br/itk").rstrip("/")
NAK = os.getenv("DMP_NAK", "")
USER = os.getenv("DMP_USERNAME", "")
PWD = os.getenv("DMP_PASSWORD", "")


def testar(desc, fn):
    try:
        r = fn()
        print(f"[{r.status_code}] {desc}")
        print(f"        {r.text[:160].strip()}")
    except Exception as e:
        print(f"[ERRO] {desc}: {type(e).__name__} {str(e)[:120]}")


if __name__ == "__main__":
    h = {"Authorization": f"NAK {NAK}"}
    print(f"Base: {BASE}\n")
    # v1 deve responder 403 "use v2 com senha criptografada".
    testar("v1 Logon (esperado: 403 v1 bloqueado)",
           lambda: requests.get(f"{BASE}/api/v1/Logon",
                                params={"username": USER, "password": PWD, "culture": "pt-BR"},
                                headers=h, timeout=25))
    # v2 com senha em texto puro deve dar 401 (precisa criptografar).
    testar("v2 Logon senha pura (esperado: 401 ate termos o spec)",
           lambda: requests.get(f"{BASE}/api/v2/Logon",
                                params={"username": USER, "password": PWD, "culture": "pt-BR"},
                                headers=h, timeout=25))
