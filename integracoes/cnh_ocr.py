"""
Leitura automática da CNH por IA (visão do Claude).

A OL envia uma foto da CNH; o modelo lê e devolve os campos estruturados
(nome, CPF, nascimento, número de registro, validade, categoria) para
preencher o formulário. A OL confere antes de salvar.

LGPD: a imagem da CNH NÃO é guardada — é usada só para a extração e descartada.
Precisa da variável ANTHROPIC_API_KEY no .env.
"""

from __future__ import annotations

import base64
from typing import Optional

import anthropic
import pydantic


class CNHDados(pydantic.BaseModel):
    """Campos que esperamos extrair da CNH."""
    nome: Optional[str] = None
    cpf: Optional[str] = None
    nascimento: Optional[str] = None     # formato AAAA-MM-DD
    registro: Optional[str] = None       # número de registro da CNH
    validade: Optional[str] = None       # formato AAAA-MM-DD
    categoria: Optional[str] = None       # ex.: A, B, AB


def ler_cnh(foto_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Lê a foto da CNH e devolve um dict com os campos extraídos."""
    client = anthropic.Anthropic()        # usa ANTHROPIC_API_KEY do ambiente
    b64 = base64.standard_b64encode(foto_bytes).decode()

    resp = client.messages.parse(
        model="claude-opus-4-8",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": (
                    "Esta é uma foto de uma CNH (Carteira Nacional de Habilitação) "
                    "brasileira. Extraia os campos. Datas no formato AAAA-MM-DD. "
                    "CPF só com os 11 dígitos. Se algum campo não estiver legível, "
                    "deixe-o nulo. Não invente dados."
                )},
            ],
        }],
        output_format=CNHDados,
    )
    dados = resp.parsed_output
    if dados is None:
        raise ValueError("Não consegui interpretar a imagem da CNH.")
    return dados.model_dump()
