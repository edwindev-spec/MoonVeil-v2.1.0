"""Token-aware wrapper discovery. Strings and comments are never rewritten."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Dict, List

from .errors import MoonVeilError

MAX_SOURCE_BYTES = 8 * 1024 * 1024
# Handlers whose factories the capture script labels and the normalizer understands.
SEMANTIC_ROLES = ("loadk", "getglobal", "self", "call", "call_return", "nop", "xor_index", "xor_reverse")
_KEYWORDS = set("and break do else elseif end false for function if in local nil not or repeat return then true until while bit32 string table getfenv select type".split())
_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--\[(?P<ceq>=*)\[.*?\](?P=ceq)\]|--[^\r\n]*)"
    r"|(?P<long>\[(?P<seq>=*)\[.*?\](?P=seq)\])"
    r'''|(?P<string>"(?:\\(?:\r\n|.)|[^"\\\r\n])*"|'(?:\\(?:\r\n|.)|[^'\\\r\n])*')'''
    r"|(?P<number>0[bB][01_]+|0[xX][\da-fA-F_]+|(?:\d[\d_]*(?:\.(?!\.)[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_]\w*)|(?P<symbol>\.\.\.|\.\.|==|~=|<=|>=|//|::|[+*/%^#=<>~:;,{}()\[\].-])",
    re.DOTALL | re.ASCII,
)


@dataclass(frozen=True)
class Token:
    text: str
    kind: str
    start: int
    end: int


def tokenize(source: str) -> List[Token]:
    result = []
    position = 0
    while position < len(source):
        match = _TOKEN.match(source, position)
        if match is None:
            raise MoonVeilError(f"unsupported or unterminated Lua token at character {position}")
        kind = match.lastgroup
        value = match.group()
        if kind == "symbol" and value == "[" and re.match(r"\[=*\[", source[position:]):
            raise MoonVeilError("unterminated long string")
        if kind == "comment" and value.startswith("--[") and re.match(r"--\[=*\[", value):
            # An unterminated long comment must not silently become a line comment.
            opening = re.match(r"--\[(=*)\[", value)
            if not value.endswith("]" + opening[1] + "]"):
                raise MoonVeilError("unterminated long comment")
        if kind not in {"space", "comment"}:
            result.append(Token(value, kind, position, match.end()))
        position = match.end()
    return result


def canonical(source: str) -> str:
    """Fingerprint equivalent formatting, integer spelling and local renaming."""
    identifiers: Dict[str, str] = {}
    words = []
    previous = ""
    for token in tokenize(source):
        word = token.text
        if token.kind == "name" and word not in _KEYWORDS and previous not in {".", ":"}:
            word = identifiers.setdefault(word, f"v{len(identifiers)}")
        elif token.kind == "number":
            raw = word.replace("_", "")
            try:
                word = str(int(raw, 0) if raw.lower().startswith(("0x", "0b")) else int(raw))
            except ValueError:
                word = repr(float(raw))
        words.append(word)
        previous = token.text
    return hashlib.sha256(" ".join(words).encode()).hexdigest()


def _literal_name(token: Token) -> str:
    if token.kind != "string" or not re.fullmatch(r"[A-Za-z_]\w*", token.text[1:-1], re.ASCII):
        raise MoonVeilError("wrapper method names must be plain string identifiers")
    return token.text[1:-1]


def _fields(tokens: List[Token], opening: int):
    """Split an outer constructor, respecting nested Lua blocks and literals."""
    pairs = {"}": "{", ")": "(", "]": "[", "until": "repeat"}
    stack = []
    result = []
    start = opening + 1
    for index in range(start, len(tokens)):
        word = tokens[index].text
        if word == "}" and not stack:
            if index > start:
                result.append(tokens[start:index])
            return result, index
        if word in {",", ";"} and not stack:
            if index == start:
                raise MoonVeilError("empty wrapper field")
            result.append(tokens[start:index])
            start = index + 1
        elif word in {"{", "(", "[", "function", "if", "do", "repeat"}:
            stack.append(word)
        elif word == "end":
            if not stack or stack.pop() not in {"function", "if", "do"}:
                raise MoonVeilError("unbalanced Lua block in wrapper")
        elif word in pairs:
            if not stack or stack.pop() != pairs[word]:
                raise MoonVeilError("unbalanced delimiter in wrapper")
    raise MoonVeilError("truncated wrapper constructor")


@dataclass
class Wrapper:
    source: str
    table_source: str
    names_source: str
    remapper: str
    entry: str
    methods: Dict[str, str]
    roles: Dict[str, str]


# Profiles are calibrated against handler semantics, not the banner alone.
# Generated from the supplied 2.1.0 table-dispatch family (see UPSTREAM.md).
PROFILE = {
    "remapper": "6513a80b9c7d7dd0c73770572764d1935d4e88a32d51ba8259bcfe1dd8eb6adf",
    "bootstrap": "2bee8614872d9f669c2fbe64a379650a357a57fed749ddbb8db61ecd8cfc59c0",
    "constructor": "de9ea6a4c4325bc2aaedc03cd69521ed05cb30720c6595b99100b2d7cf55a28a",
    "dispatcher": "4bfa7d0bbf78ba860499af8178a5f79f54f81c4f3ff105b7ab39d6f0f8fd860d",
    "loadk": "a9cba608712d8df63e8277c384814e0615a87c1659ea7e088d81cc227ecc374b",
    "getglobal": "70ad4cc6da8c11774e951d7bd6ecfdec9c0d3eeb8d7637a099cd1ea55d072466",
    "self": "e8ed016c6f9ebe48a66dbb23c47717fa9b72fd7b6f5f40a9ea61a7feb64ddb2a",
    "call": "f6c81f4a9b5ae3738a95b671574c87dc9a487f01fe5230d40ec18ab0ca73cc82",
    "call_return": "1b2b03f4634a8ffeabe9d07524789d5ee02bbf0935ca936fc937578d487be70f",
    "nop": "93f8588513af89203adeaa204de31333b820439fa8f857b8d7fd532ed9b5c272",
    "xor_index": "148236aad29a00edc2c82619ceaafedb8a33ee18cb3f5a582346eeca2013ac90",
    "xor_reverse": "c7db58fe5a9a03e3c22f6b0def9e7cfabc939f4f593ff1638e93821049f16313",
    "cache_xor": "5d64f27e79a4759503895f40b0a7890a78c17cf8c53561427638bf3f28781921",
    "cache_or": "85be379037341968a04ac0a5b6bcfbb360eedac0a551d9ba6119526931fea493",
    "cache_arithmetic": "98d2fc82953f17a2ae8c3ca33c77da1699427115f94fa82724768b44da97ca3a",
    "cache_literal": "337ddc810d4cdf00badaff4dc6b59c7e73e7f9fa39e1e763deb7e325ee08ce79",
    "bit_and": "ccb9d35f5eb4a5764d739ff0b1d6a2cb5de4d391cb9c94b0716c5ffe6f49f685",
    "bit_or": "1b55cb8572e772b0eb4079d0d369c43dcafacc943daa6633914c2747bfcc4bb0",
    "bit_xor": "91c25e9f3f86bf5854528c74d7ffa13b9dd5035a1337a61cfab00af62b6fcd8c",
    "bit_not": "b3c8608c7c17d65ba29a0463d96bf19e823f0465e625bba5a21c2aa6633eeb86",
    "vm_context": "1d21cf292d525b21115abae966e87a5036d78a675bf7e173739f1e6fddb817a4",
    "argument_wrapper": "6896f45d012675bf77ed48939796294096afc75101caf076451acf4e33b7c5ed",
    "composition": "adf179ba3acfa57f50d361814c7683a2e464956d16172a752b2c0472f8795f1c",
    "pack_variadic": "6f6ff9f6a0e7de224eebfee9d28650c29522221a4d8a15ae93c7c736b93a1675",
    "unpack_variadic": "8ad3d58463baf2defc5dedb0033399027739fc2af5d7e4e3f61b7be69ce268c1",
}


def parse_wrapper(source: str) -> Wrapper:
    banner = re.search(r"^\s*-- This script was generated using MoonVeil ([\d.]+)\b", source)
    if not banner or banner[1] != "2.1.0":
        version = banner[1] if banner else "unknown"
        raise MoonVeilError(f"unsupported MoonVeil version: {version}; expected 2.1.0")
    tokens = tokenize(source)
    if [t.text for t in tokens[:3]] != ["return", "(", "{"]:
        raise MoonVeilError("unsupported MoonVeil 2.1.0 wrapper: expected return({...}):method(...)")
    fields, end = _fields(tokens, 2)
    tail = tokens[end + 1:]
    if len(tail) < 12 or [t.text for t in tail[:2]] != [")", ":"] or [t.text for t in tail[3:5]] != ["(", "{"]:
        raise MoonVeilError("truncated or unsupported wrapper invocation")
    remapper = tail[2].text
    names = []
    cursor = 5
    while cursor < len(tail) and tail[cursor].text != "}":
        names.append(_literal_name(tail[cursor]))
        cursor += 1
        if cursor < len(tail) and tail[cursor].text == ",":
            cursor += 1
        elif cursor >= len(tail) or tail[cursor].text != "}":
            raise MoonVeilError("invalid wrapper name array")
    if cursor + 6 != len(tail) or [t.text for t in tail[cursor:cursor + 2]] != ["}", ","] or [t.text for t in tail[cursor + 3:]] != [",", "...", ")"]:
        raise MoonVeilError("truncated wrapper invocation or extra statements")
    entry = _literal_name(tail[cursor + 2])
    if len(names) != 35 or len(set(names)) != len(names):
        raise MoonVeilError("unsupported wrapper permutation (expected 35 unique names)")
    methods = {}
    positional = 0
    for field in fields:
        if len(field) >= 3 and field[0].kind == "name" and field[1].text == "=":
            name, body = field[0].text, field[2:]
        else:
            if positional >= len(names):
                raise MoonVeilError("too many positional wrapper fields")
            name = names[(100151 * positional + 58863) % len(names)]
            positional += 1
            body = field
        if name in methods or not body:
            raise MoonVeilError(f"duplicate or empty wrapper method: {name}")
        methods[name] = source[body[0].start:body[-1].end]
    if positional != len(names):
        raise MoonVeilError("truncated wrapper method array")
    roles = {}
    hashes = {name: canonical(body) for name, body in methods.items()}
    for role, fingerprint in PROFILE.items():
        matches = [name for name, digest in hashes.items() if digest == fingerprint]
        if len(matches) != 1:
            raise MoonVeilError(f"unsupported 2.1.0 profile: {role} handler is missing, modified, or ambiguous")
        roles[role] = matches[0]
    if roles["remapper"] != remapper or roles["bootstrap"] != entry:
        raise MoonVeilError("wrapper entry does not match the supported profile")
    return Wrapper(source, source[tokens[2].start:tokens[end].end], source[tail[4].start:tail[cursor].end], remapper, entry, methods, roles)


def diagnose(source: str) -> dict:
    """Describe how a MoonVeil-looking file differs from the supported layout. Never raises."""
    banner = re.search(r"^\s*-- This script was generated using MoonVeil ([\d.]+)", source)
    report = {"banner_version": banner[1] if banner else None, "wrapper_fields": None, "method_names": None,
              "expected_method_names": 35, "matched_handlers": [], "profile_handlers": len(PROFILE)}
    try:
        tokens = tokenize(source)
        if [t.text for t in tokens[:3]] != ["return", "(", "{"]:
            return report
        fields, end = _fields(tokens, 2)
        report["wrapper_fields"] = len(fields)
        report["method_names"] = sum(1 for t in tokens[end + 1:] if t.kind == "string") - 1
        known = {fingerprint: role for role, fingerprint in PROFILE.items()}
        matched = []
        for field in fields:
            body = field[2:] if len(field) >= 3 and field[0].kind == "name" and field[1].text == "=" else field
            if body:
                role = known.get(canonical(source[body[0].start:body[-1].end]))
                if role:
                    matched.append(role)
        report["matched_handlers"] = sorted(set(matched))
    except MoonVeilError:
        pass
    return report


def explain_layout(report: dict) -> str:
    """One-sentence human summary of :func:`diagnose`, with the way forward."""
    if report["banner_version"] is None:
        return "no MoonVeil banner was found"
    if report["wrapper_fields"] is None:
        return f"MoonVeil {report['banner_version']} banner, but the file does not have the supported wrapper shape"
    return (f"MoonVeil {report['banner_version']} banner, but a different layout: {report['wrapper_fields']} wrapper fields and "
            f"{report['method_names']} method names (the supported profile has {report['expected_method_names']}); "
            f"{len(report['matched_handlers'])} of {report['profile_handlers']} handler fingerprints match. "
            "The static VM profile cannot read it; --mode trace runs it in a symbolic sandbox instead")
