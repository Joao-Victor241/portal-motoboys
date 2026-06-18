"""Validações de documentos brasileiros (CPF e placa de veículo)."""

import re


def limpar_cpf(cpf: str) -> str:
    """Mantém só os dígitos do CPF."""
    return "".join(filter(str.isdigit, cpf or ""))


def validar_cpf(cpf: str) -> tuple[bool, str]:
    """
    Valida o CPF: 11 dígitos + dígitos verificadores.
    Devolve (True, "") se válido, ou (False, mensagem) se inválido.
    """
    nums = limpar_cpf(cpf)
    if len(nums) != 11:
        return False, f"CPF deve ter 11 dígitos (digitados: {len(nums)})."
    if nums == nums[0] * 11:                       # todos iguais (000..., 111...)
        return False, "CPF inválido."
    # Confere os dois dígitos verificadores.
    for i in (9, 10):
        soma = sum(int(nums[n]) * ((i + 1) - n) for n in range(i))
        digito = (soma * 10 % 11) % 10
        if digito != int(nums[i]):
            return False, "CPF inválido (dígito verificador não confere)."
    return True, ""


def validar_placa(placa: str) -> tuple[bool, str]:
    """
    Aceita placa antiga (ABC1234) e Mercosul (ABC1D23), com ou sem hífen.
    Devolve (True, placa_normalizada) ou (False, mensagem de erro).
    """
    p = re.sub(r"[^A-Za-z0-9]", "", placa or "").upper()
    if not p:
        return False, "Informe a placa."
    if re.fullmatch(r"[A-Z]{3}[0-9]{4}", p) or re.fullmatch(r"[A-Z]{3}[0-9][A-Z][0-9]{2}", p):
        return True, p
    return False, "Placa inválida. Use o formato ABC1234 ou ABC1D23 (Mercosul)."
