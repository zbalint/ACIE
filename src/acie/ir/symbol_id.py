from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolIdParts:
    path: str
    qualname: str
    kind: str
    ordinal: int | None


def build_symbol_id(
    path: str, qualname: str, kind: str, ordinal: int | None = None
) -> str:
    # kind-vocabulary validation is owned by the storage layer (a CHECK constraint
    # on symbols_live.kind), not by this pure ID-grammar module. See SALTMDB
    # memory ff2f9abc-6f4e-4906-8928-a1b8556674f2 for why.
    symbol_id = f"{path}:{qualname}#{kind}"
    if ordinal is not None:
        symbol_id += f"@{ordinal}"
    return symbol_id


def parse_symbol_id(symbol_id: str) -> SymbolIdParts:
    if ":" not in symbol_id or "#" not in symbol_id:
        raise ValueError(f"malformed symbol id {symbol_id!r}: missing ':' or '#'")
    path, _, rest = symbol_id.partition(":")
    qualname, _, kind_and_ordinal = rest.partition("#")
    kind, _, ordinal_str = kind_and_ordinal.partition("@")
    ordinal = int(ordinal_str) if ordinal_str else None
    return SymbolIdParts(path=path, qualname=qualname, kind=kind, ordinal=ordinal)
