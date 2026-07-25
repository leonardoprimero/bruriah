#!/usr/bin/env python3
"""Release signing for Cerebro packs.

A maintainer tool, deliberately kept out of the MCP surface and out of `cerebro-mcp`: signing is an
act of authorship, not something a running server or an agent ever does. The logic lives in
`cerebro_router.signing` so that this script and the CLI's `skill-sign` produce byte-identical
manifests instead of drifting apart.

    # once, on your own machine -- the private key never leaves this file
    python scripts/sign_pack.py keygen --out ~/.config/cerebro/release-key.pem

    # paste the printed public half into src/cerebro_router/data/trust-roots.json
    python scripts/sign_pack.py public --key ~/.config/cerebro/release-key.pem

    # sign a pack; the manifest lands beside it unless --out says otherwise
    python scripts/sign_pack.py sign --key ~/.config/cerebro/release-key.pem \\
        --signer cerebro-release --pack src/cerebro_router/data/research-policy.json

Signing asserts WHO signed the bytes. It is not a claim that the pack is well-formed, current, or
safe -- the fail-closed load path decides that.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cerebro_router.signing import SigningError, generate_key, load_public, sign_pack  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sign_pack", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("keygen", help="generate a release key and print its public half")
    keygen.add_argument("--out", type=Path, required=True, help="where the private key is written")

    public = subparsers.add_parser("public", help="print an existing key's public half")
    public.add_argument("--key", type=Path, required=True)

    sign = subparsers.add_parser("sign", help="sign a pack and write its release manifest")
    sign.add_argument("--key", type=Path, required=True)
    sign.add_argument("--signer", required=True, help="signer id, must match trust-roots.json")
    sign.add_argument("--pack", type=Path, required=True)
    sign.add_argument("--out", type=Path, default=None, help="manifest path; defaults beside the pack")

    args = parser.parse_args(argv)
    try:
        if args.command == "keygen":
            print(generate_key(args.out))
            print(f"private key written to {args.out} (mode 0600) -- never commit it", file=sys.stderr)
        elif args.command == "public":
            print(load_public(args.key))
        else:
            print(sign_pack(args.key, args.signer, args.pack, args.out))
    except SigningError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
