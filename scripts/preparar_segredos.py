"""Prepara as senhas do projeto SEM coloca-las no codigo nem no Git.

1. Se nao existir, cria o arquivo .env na raiz com senhas aleatorias
   (o .env esta no .gitignore; o modelo versionado e o .env.exemplo).
2. Gera mosquitto/senhas-amqtt.txt com os HASHES Argon2 das senhas, que e o que o broker
   de teste (amqtt) le. O broker guarda so o hash, nunca a senha.

Uso: python scripts/preparar_segredos.py      (o rodar-local.ps1 ja chama sozinho)
"""
import secrets
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ENV = RAIZ / ".env"
SENHAS_AMQTT = RAIZ / "mosquitto" / "senhas-amqtt.txt"

# usuario do broker -> variavel do .env com a senha dele
USUARIOS = {"no-moinho01": "MQTT_SENHA_NO", "supervisorio": "MQTT_SENHA_SUPERVISORIO"}


def ler_env():
    valores = {}
    for linha in ENV.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            chave, valor = linha.split("=", 1)
            valores[chave.strip()] = valor.strip()
    return valores


def criar_env():
    ENV.write_text(
        "# Segredos do projeto. NAO versionar (esta no .gitignore). Modelo: .env.exemplo\n"
        f"MQTT_SENHA_NO={secrets.token_urlsafe(18)}\n"
        f"MQTT_SENHA_SUPERVISORIO={secrets.token_urlsafe(18)}\n"
        f"GATEWAY_TOKEN={secrets.token_urlsafe(18)}\n",
        encoding="utf-8")
    print(f"criado {ENV.name} com senhas aleatorias")


def main():
    if not ENV.exists():
        criar_env()
    valores = ler_env()
    faltando = [v for v in [*USUARIOS.values(), "GATEWAY_TOKEN"] if not valores.get(v)]
    if faltando:
        sys.exit(f"faltam no .env: {', '.join(faltando)} (veja o .env.exemplo)")

    from pwdlib import PasswordHash
    from pwdlib.hashers.argon2 import Argon2Hasher
    hasher = PasswordHash((Argon2Hasher(),))
    linhas = [f"{usuario}:{hasher.hash(valores[var])}" for usuario, var in USUARIOS.items()]
    SENHAS_AMQTT.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"gerado {SENHAS_AMQTT.relative_to(RAIZ)} (hashes Argon2 de {', '.join(USUARIOS)})")


if __name__ == "__main__":
    main()
