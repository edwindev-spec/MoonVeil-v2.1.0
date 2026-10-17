"""Command-line interface for analysis, decoded graphs and source recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import __version__
from .analysis import indicators
from .decoder import decode
from .emit import STYLES, emit, emit_trace
from .errors import MoonVeilError
from .runtime import DEFAULT_MEMORY_MB, compile_source, find_luau, positive_memory, positive_timeout
from .source import diagnose, explain_layout, parse_wrapper
from .storage import check_destinations, read_source, write_json, write_text
from .trace import log_json, trace, trace_ir
from .vm import disassemble, json_ir, normalize

STDOUT = Path("-")
ARTIFACT_NAMES = ("decoded.json", "normalized.json", "instructions.txt", "manifest.json")
TRACE_ARTIFACT_NAMES = ("trace.json", "manifest.json")
MODES = ("vm", "auto", "trace")


def _timeout(value):
    try:
        return positive_timeout(float(value))
    except (ValueError, MoonVeilError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _memory(value):
    try:
        return positive_memory(int(value))
    except (ValueError, MoonVeilError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parser():
    parser = argparse.ArgumentParser(description="Recover Luau from the supported MoonVeil 2.1.0 table-dispatch family.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    analysis = commands.add_parser("analyze", help="identify the supported profile without executing Lua")
    analysis.add_argument("input", type=Path)
    analysis.add_argument("--json", action="store_true")
    for name, help_text in (("dump", "extract the decoded VM graph as JSON"),
                            ("decompile", "recover readable Luau source")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("input", type=Path)
        command.add_argument("-o", "--output", type=Path,
                             help="destination, or - for standard output "
                                  f"(default: <input>.{'recovered.luau' if name == 'decompile' else 'decoded.json'})")
        command.add_argument("--luau", type=Path, help="path to the official luau executable")
        command.add_argument("--timeout", type=_timeout, default=30.0, help="seconds allowed per subprocess (default: 30)")
        command.add_argument("--memory-limit", type=_memory, default=DEFAULT_MEMORY_MB, metavar="MIB",
                             help=f"address-space limit per subprocess on POSIX, 0 disables (default: {DEFAULT_MEMORY_MB})")
        command.add_argument("-q", "--quiet", action="store_true", help="print only errors")
        if name == "decompile":
            command.add_argument("--artifacts", type=Path, help="directory for graph, IR, disassembly and manifest")
            command.add_argument("--require-compile", action="store_true", help="fail if luau-compile is unavailable")
            command.add_argument("--style", choices=STYLES, default="auto",
                                 help="auto: idiomatic Luau when provable, else registers (default)")
            command.add_argument("--json", action="store_true", help="print the manifest as JSON instead of text")
            command.add_argument("--mode", choices=MODES, default="vm",
                                 help="vm: static profile only, never runs the program (default); trace: run it in a "
                                      "symbolic sandbox; auto: vm, and trace when the layout is not supported")
    return parser


def _default_output(args) -> Path:
    suffix = ".recovered.luau" if args.command == "decompile" else ".decoded.json"
    return args.input.with_name(args.input.stem + suffix)


def _emit_checked(ir, style, luau, timeout, memory):
    """Emit and let luau-compile vet the result; ``auto`` retreats to the register model."""
    source, profile = emit(ir, style)
    try:
        return source, profile, compile_source(source, luau, timeout, memory), None
    except MoonVeilError as exc:
        if style != "auto" or profile != "structured":
            raise
        note = f"structured output was rejected by luau-compile ({exc}); used the register model instead"
    source, profile = emit(ir, "registers")
    return source, profile, compile_source(source, luau, timeout, memory), note


def _summary(found, status):
    for behavior in found["behaviors"]:
        print(f"  behavior: {behavior['id']} - {behavior['description']}", file=status)
    for url in found["urls"]:
        # json.dumps escapes control characters: decoded strings are untrusted terminal input.
        print(f"  url: {json.dumps(url)}  (data only; this tool never contacts it)", file=status)
    if found["globals"] or found["methods"]:
        names = ", ".join(json.dumps(name) for name in found["globals"])
        methods = ", ".join(json.dumps(name) for name in found["methods"])
        print(f"  globals: {names or '-'}; methods: {methods or '-'}", file=status)


def _deliver(args, recovered, manifest, json_artifacts, listing, output, to_stdout, status, artifacts,
             headline, profile, compiled, note, found):
    """Write the recovered source, optional artifacts and the final report."""
    if to_stdout:
        sys.stdout.write(recovered)
    else:
        write_text(output, recovered)
    if artifacts:
        for name, value in json_artifacts.items():
            write_json(artifacts / name, value)
        if listing is not None:
            write_text(artifacts / "instructions.txt", listing)
        write_json(artifacts / "manifest.json", manifest)
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    elif not args.quiet:
        verification = "compiled with official Luau" if compiled else "compiler unavailable; syntax was not checked"
        print(f"{headline}: {'standard output' if to_stdout else output} ({profile}, {verification})", file=status)
        if note:
            print(f"  note: {note}", file=status)
        _summary(found, status)


def _recover_by_trace(args, source, digest, luau, output, to_stdout, status, artifacts):
    if args.command == "dump":
        raise MoonVeilError("dump needs the static profile; use decompile --mode trace for other layouts")
    log = trace(source, luau, args.timeout, args.memory_limit)
    recovered = emit_trace(log)
    compiled = compile_source(recovered, luau, args.timeout, args.memory_limit)
    if args.require_compile and compiled is None:
        raise MoonVeilError("luau-compile is required but was not found; set LUAU_COMPILE_PATH")
    found = indicators(trace_ir(log))
    manifest = {
        "tool_version": __version__, "profile": "symbolic-trace", "mode": "trace",
        "source_sha256": digest, "output_sha256": hashlib.sha256(recovered.encode()).hexdigest(),
        "operation_count": len(log["events"]),
        "emission": "trace", "compiled": compiled, "equivalent": None,
        "protected_program_executed": True,
        "indicators": {key: found[key] for key in ("urls", "globals", "methods", "behaviors")},
        "notes": ["The program ran in official Luau against a symbolic environment: every global outside the pure "
                  "standard libraries is a recording stand-in, so nothing reached a real API, file or network.",
                  "Only the operations on the environment along the executed path are recovered; the original "
                  "control flow, names and any computation that does not touch the environment are not."]}
    note = "executed in the symbolic sandbox (protected program ran; no real API was reachable)"
    _deliver(args, recovered, manifest, {"trace.json": log_json(log)}, None, output, to_stdout, status, artifacts,
             f"Recovered {len(log['events'])} environment operations", "trace", compiled, note, found)
    return 0


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        raw, source = read_source(args.input)
        digest = hashlib.sha256(raw).hexdigest()
        if args.command == "analyze":
            report = {"input": str(args.input), "size": len(raw), "sha256": digest, "supported": False}
            try:
                wrapper = parse_wrapper(source)
                report.update(supported=True, version="2.1.0", profile="table-dispatch-210", roles=wrapper.roles)
            except MoonVeilError as exc:
                layout = diagnose(source)
                report.update(reason=str(exc), layout=layout, hint=explain_layout(layout))
            print(json.dumps(report, indent=2) if args.json else (
                f"MoonVeil 2.1.0: supported table-dispatch profile ({len(raw)} bytes)" if report["supported"]
                else f"{report['reason']}\n  {report['hint']}"
            ))
            return 0 if report["supported"] else 2
        to_stdout = args.output == STDOUT
        output = args.output or _default_output(args)
        status = sys.stderr if to_stdout else sys.stdout
        if getattr(args, "json", False) and to_stdout:
            raise MoonVeilError("--json prints to standard output; choose a file with -o")
        mode = getattr(args, "mode", "vm")
        wrapper = None
        if mode != "trace":
            try:
                wrapper = parse_wrapper(source)
            except MoonVeilError as exc:
                if mode != "auto":
                    raise MoonVeilError(f"{exc}\n  {explain_layout(diagnose(source))}") from exc
        artifacts = getattr(args, "artifacts", None)
        destinations = [] if to_stdout else [output]
        if artifacts:
            names = TRACE_ARTIFACT_NAMES if wrapper is None else ARTIFACT_NAMES
            destinations += [artifacts / name for name in names]
        check_destinations(args.input, destinations)
        luau = find_luau(args.luau)
        if wrapper is None:
            return _recover_by_trace(args, source, digest, luau, output, to_stdout, status, artifacts)
        graph = decode(wrapper, luau, args.timeout, args.memory_limit)
        if args.command == "dump":
            if to_stdout:
                json.dump(graph, sys.stdout, ensure_ascii=False, indent=2, allow_nan=False)
                sys.stdout.write("\n")
            else:
                write_json(output, graph)
            if not args.quiet:
                print(f"Decoded graph: {'standard output' if to_stdout else output} ({len(graph['tables'])} tables)", file=status)
            return 0
        ir = normalize(graph)
        recovered, profile, compiled, note = _emit_checked(ir, args.style, luau, args.timeout, args.memory_limit)
        if args.require_compile and compiled is None:
            raise MoonVeilError("luau-compile is required but was not found; set LUAU_COMPILE_PATH")
        found = indicators(ir)
        notes = ["The loader and deserializer ran in official Luau; the protected constructor was replaced before dispatch.",
                 "Behavioral equivalence is not established by compilation alone."]
        if note:
            notes.append(note)
        manifest = {
            "tool_version": __version__, "moonveil_version": "2.1.0", "profile": "table-dispatch-210", "mode": "vm",
            "source_sha256": digest, "output_sha256": hashlib.sha256(recovered.encode()).hexdigest(),
            "instruction_count": len(ir["instructions"]),
            "semantic_instruction_count": sum(ins["op"] != "nop" for ins in ir["instructions"]),
            "emission": profile, "compiled": compiled, "equivalent": None,
            "protected_program_executed": False,
            "indicators": {key: found[key] for key in ("urls", "globals", "methods", "behaviors")},
            "notes": notes}
        extra = {"decoded.json": graph, "normalized.json": json_ir(ir)}
        _deliver(args, recovered, manifest, extra, disassemble(ir), output, to_stdout, status, artifacts,
                 f"Recovered {len(ir['instructions'])} instructions", profile, compiled, note, found)
        return 0
    except BrokenPipeError:
        # The reader went away (e.g. ``| head``); keep Python from printing a second error at exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (MoonVeilError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
