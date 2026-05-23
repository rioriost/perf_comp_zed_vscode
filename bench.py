#!/usr/bin/env python3
"""
macOS editor benchmark harness for reproducing Zed/VS Code-style measurements.

This script intentionally measures local behavior on the current machine instead
of trying to reuse numbers from an article. It supports multiple anonymized
editors so results can be reported as Editor A/B/C/D/E without disparaging a
specific product.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "editors.example.json"
LOCAL_CONFIG = ROOT / "editors.local.json"
FIXTURES = ROOT / "fixtures"
RESULTS = ROOT / "results"
TMP = ROOT / "tmp"
TOOLS = ROOT / "tools"
MAC_GUI_PROBE_SOURCE = TOOLS / "mac_gui_probe.swift"
MAC_GUI_PROBE_BIN = TMP / "mac_gui_probe"

BENCH_SUITE_VERSION = "2026-05-23-strict-title-ready"

DEFAULT_CASES = [
    "open_100k_js",
    "edit_ready_small",
    "edit_ready_100k_js",
    "scroll_100k_js",
    "search_large_project",
    "memory_folder",
    "memory_10_files",
    "memory_large_project",
]

CASE_CHOICES = DEFAULT_CASES


@dataclass(frozen=True)
class Editor:
    label: str
    app_name: str | None
    bundle_id: str | None
    process_name: str | None
    process_regex: str
    command: list[str] | None
    extra_args: list[str]
    target_arg_mode: str

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Editor":
        target_arg_mode = data.get("target_arg_mode", "open_document")
        if target_arg_mode not in ("open_document", "app_args"):
            raise ValueError(
                f"editor {data['label']!r} has invalid target_arg_mode: {target_arg_mode!r}"
            )
        return cls(
            label=data["label"],
            app_name=data.get("app_name"),
            bundle_id=data.get("bundle_id"),
            process_name=data.get("process_name") or data.get("app_name"),
            process_regex=data["process_regex"],
            command=data.get("command"),
            extra_args=data.get("extra_args", []),
            target_arg_mode=target_arg_mode,
        )


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    rss_kb: int
    command: str


def applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run(
    cmd: list[str],
    *,
    check: bool = False,
    capture: bool = True,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
        env=env,
    )


def ensure_mac_gui_probe() -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("AX GUI probe is only available on macOS")
    if not MAC_GUI_PROBE_SOURCE.exists():
        raise RuntimeError(f"missing GUI probe source: {MAC_GUI_PROBE_SOURCE}")

    TMP.mkdir(parents=True, exist_ok=True)
    needs_build = not MAC_GUI_PROBE_BIN.exists()
    if not needs_build:
        needs_build = (
            MAC_GUI_PROBE_BIN.stat().st_mtime < MAC_GUI_PROBE_SOURCE.stat().st_mtime
        )
    if needs_build:
        swiftc = shutil.which("swiftc")
        if not swiftc:
            raise RuntimeError("swiftc not found; install Xcode command line tools")
        module_cache = TMP / "clang-module-cache"
        module_cache.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["CLANG_MODULE_CACHE_PATH"] = str(module_cache)
        result = run(
            [swiftc, str(MAC_GUI_PROBE_SOURCE), "-o", str(MAC_GUI_PROBE_BIN)],
            timeout=30,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "failed to build mac_gui_probe: "
                + (result.stderr.strip() or result.stdout.strip())
            )
    return MAC_GUI_PROBE_BIN


def run_mac_gui_probe(
    command: str,
    editor: Editor,
    *,
    timeout: float,
    title_contains: str | None = None,
    text: str | None = None,
    activation_delay: float | None = None,
    window_geometry: tuple[int, int, int, int] | None = None,
    allow_window_fallback: bool = False,
    prompt_permissions: bool = False,
) -> dict[str, Any]:
    if not editor.process_name and not editor.bundle_id:
        raise ValueError("AX GUI probe requires process_name or bundle_id")

    cmd = [
        str(ensure_mac_gui_probe()),
        command,
        "--timeout",
        f"{timeout:.3f}",
    ]
    if editor.process_name:
        cmd += ["--process-name", editor.process_name]
    if editor.bundle_id:
        cmd += ["--bundle-id", editor.bundle_id]
    if title_contains:
        cmd += ["--title-contains", title_contains]
    if text is not None:
        cmd += ["--text", text]
    if activation_delay is not None:
        cmd += ["--activation-delay", f"{activation_delay:.3f}"]
    if window_geometry is not None:
        x, y, width, height = window_geometry
        cmd += [
            "--window-x",
            str(x),
            "--window-y",
            str(y),
            "--window-width",
            str(width),
            "--window-height",
            str(height),
        ]
    if allow_window_fallback:
        cmd.append("--allow-window-fallback")
    if prompt_permissions:
        cmd.append("--prompt-permissions")

    result = run(cmd, timeout=timeout + 5)
    raw = (result.stdout or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {}
    if result.returncode != 0 or not payload.get("ok"):
        message = payload.get("error") or result.stderr.strip() or raw
        raise RuntimeError(f"mac_gui_probe {command} failed: {message}")
    return payload


def repo_root() -> Path:
    return ROOT.parent


def load_config(path: Path | None) -> list[Editor]:
    config_path = path or (LOCAL_CONFIG if LOCAL_CONFIG.exists() else DEFAULT_CONFIG)
    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [
        Editor.from_json(item) for item in data["editors"] if item.get("enabled", True)
    ]


def ensure_fixtures() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "empty_folder").mkdir(exist_ok=True)

    sample = FIXTURES / "sample_project"
    (sample / "src").mkdir(parents=True, exist_ok=True)
    (sample / "package.json").write_text(
        json.dumps(
            {
                "name": "zed-bench-sample-project",
                "private": True,
                "type": "module",
                "scripts": {"test": "node src/index.js"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (sample / "src" / "index.js").write_text(
        "import { add } from './lib.js';\nconsole.log(add(1, 2));\n",
        encoding="utf-8",
    )
    (sample / "src" / "lib.js").write_text(
        "export function add(a, b) {\n  return a + b;\n}\n",
        encoding="utf-8",
    )

    ten = FIXTURES / "ten_files"
    ten.mkdir(exist_ok=True)
    for i in range(1, 11):
        (ten / f"file_{i:02}.js").write_text(
            "\n".join(
                [
                    f"// fixture file {i:02}",
                    "export function value() {",
                    f"  return {i};",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    large_js = FIXTURES / "large_100k_lines.js"
    if not large_js.exists() or count_lines(large_js) != 100_000:
        with large_js.open("w", encoding="utf-8") as f:
            f.write("// 100,000-line JavaScript fixture generated by bench/bench.py\n")
            f.write("export const bench = {\n")
            for i in range(99_996):
                f.write(f"  key_{i:05d}: {i},\n")
            f.write("  done: true\n")
            f.write("};\n")

    readme = FIXTURES / "README.md"
    readme.write_text(
        "# Benchmark fixtures\n\n"
        "Generated by `python3 bench/bench.py prepare`.\n\n"
        "- `sample_project/`: small folder-open target\n"
        "- `ten_files/`: 10 small files for multi-file memory tests\n"
        "- `large_100k_lines.js`: 100,000-line JavaScript file\n",
        encoding="utf-8",
    )


def count_lines(path: Path) -> int:
    with path.open("rb") as f:
        return sum(1 for _ in f)


def system_info() -> dict[str, Any]:
    def output(cmd: list[str]) -> str | None:
        try:
            result = run(cmd, timeout=3)
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    memsize = output(["sysctl", "-n", "hw.memsize"])
    return {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "macos_product_version": output(["sw_vers", "-productVersion"]),
        "macos_build_version": output(["sw_vers", "-buildVersion"]),
        "hardware_model": output(["sysctl", "-n", "hw.model"]),
        "cpu_brand": output(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "physical_cpu": output(["sysctl", "-n", "hw.physicalcpu"]),
        "logical_cpu": output(["sysctl", "-n", "hw.logicalcpu"]),
        "memory_bytes": int(memsize) if memsize and memsize.isdigit() else None,
        "perflevel0_physicalcpu": output(["sysctl", "-n", "hw.perflevel0.physicalcpu"]),
        "perflevel1_physicalcpu": output(["sysctl", "-n", "hw.perflevel1.physicalcpu"]),
    }


def ps_list() -> list[ProcessInfo]:
    result = run(["ps", "-axo", "pid=,ppid=,rss=,command="])
    processes: list[ProcessInfo] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_kb = int(parts[2])
        except ValueError:
            continue
        processes.append(
            ProcessInfo(pid=pid, ppid=ppid, rss_kb=rss_kb, command=parts[3])
        )
    return processes


def matching_processes(editor: Editor) -> list[ProcessInfo]:
    pattern = re.compile(editor.process_regex)
    processes = ps_list()
    direct = {p.pid for p in processes if pattern.search(p.command)}
    if not direct:
        return []

    # Include descendants, because Electron-style apps often spawn helpers whose
    # command lines vary by version.
    by_parent: dict[int, list[ProcessInfo]] = {}
    for p in processes:
        by_parent.setdefault(p.ppid, []).append(p)

    all_pids = set(direct)
    queue = list(direct)
    while queue:
        parent = queue.pop()
        for child in by_parent.get(parent, []):
            if child.pid not in all_pids:
                all_pids.add(child.pid)
                queue.append(child.pid)

    return [p for p in processes if p.pid in all_pids]


def memory_snapshot(editor: Editor) -> dict[str, Any]:
    processes = matching_processes(editor)
    rss_kb = sum(p.rss_kb for p in processes)
    return {
        "rss_mb": rss_kb / 1024,
        "process_count": len(processes),
        "pids": [p.pid for p in processes],
    }


def quit_editor(editor: Editor, *, force_kill: bool, timeout: float) -> None:
    if editor.app_name:
        run(
            ["osascript", "-e", f'tell application "{editor.app_name}" to quit'],
            capture=True,
        )
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if not matching_processes(editor):
            return
        time.sleep(0.2)

    if force_kill:
        for p in matching_processes(editor):
            try:
                os.kill(p.pid, 15)
            except ProcessLookupError:
                pass
        time.sleep(1)


def launch_editor(editor: Editor, targets: list[Path]) -> None:
    target_args = [str(p) for p in targets]
    if editor.command:
        cmd = editor.command + editor.extra_args + target_args
    else:
        if not editor.app_name:
            raise ValueError(
                f"editor {editor.label!r} requires either command or app_name"
            )
        cmd = ["open", "-na", editor.app_name]
        if editor.target_arg_mode == "app_args":
            cmd += ["--args"] + editor.extra_args + target_args
        else:
            cmd += target_args
            if editor.extra_args:
                cmd += ["--args"] + editor.extra_args
    run(cmd, capture=True)


def window_count(process_name: str) -> int | None:
    script = (
        'tell application "System Events"\n'
        f'  if exists application process "{process_name}" then\n'
        f'    tell application process "{process_name}"\n'
        "      return count of windows\n"
        "    end tell\n"
        "  else\n"
        "    return 0\n"
        "  end if\n"
        "end tell\n"
    )
    result = run(["osascript", "-e", script])
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def window_titles(process_name: str) -> list[str] | None:
    script = (
        'tell application "System Events"\n'
        f'  if exists application process "{process_name}" then\n'
        f'    tell application process "{process_name}"\n'
        "      set namesList to {}\n"
        "      repeat with w in windows\n"
        "        try\n"
        "          set end of namesList to name of w\n"
        "        end try\n"
        "      end repeat\n"
        "      return namesList\n"
        "    end tell\n"
        "  else\n"
        '    return ""\n'
        "  end if\n"
        "end tell\n"
    )
    result = run(["osascript", "-e", script])
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def run_osascript(script: str) -> tuple[float, str | None]:
    started = time.perf_counter()
    result = run(["osascript", "-e", script])
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else "unknown osascript error"
        raise RuntimeError(stderr)
    return elapsed, result.stderr.strip() if result.stderr else None


def wait_ready(
    editor: Editor,
    *,
    readiness: str,
    timeout: float,
    title_contains: str | None = None,
    gui_driver: str = "ax",
    allow_window_fallback: bool | None = None,
) -> tuple[float, str, str | None]:
    resolved_window_fallback = (
        readiness == "auto" if allow_window_fallback is None else allow_window_fallback
    )
    if gui_driver == "ax" and readiness in ("auto", "window", "window_title"):
        use_title = title_contains if readiness in ("auto", "window_title") else None
        try:
            payload = run_mac_gui_probe(
                "wait-ready",
                editor,
                timeout=timeout,
                title_contains=use_title,
                allow_window_fallback=resolved_window_fallback,
                prompt_permissions=True,
            )
            return (
                float(payload["elapsed_seconds"]),
                str(payload["mode"]),
                None,
            )
        except Exception as exc:
            if readiness != "auto" or not resolved_window_fallback:
                raise
            warning = f"AX readiness unavailable; falling back to process readiness: {exc}"
            start = time.perf_counter()
            deadline = start + timeout
            while time.perf_counter() < deadline:
                if matching_processes(editor):
                    return time.perf_counter() - start, "process", warning
                time.sleep(0.05)
            raise TimeoutError(
                f"{editor.label}: not ready after {timeout:.1f}s using readiness={readiness}"
            )

    start = time.perf_counter()
    deadline = start + timeout
    warning: str | None = None
    mode = readiness

    while time.perf_counter() < deadline:
        if mode in ("auto", "window_title") and title_contains and editor.process_name:
            titles = window_titles(editor.process_name)
            if titles is not None:
                if any(title_contains in title for title in titles):
                    return time.perf_counter() - start, "window_title", warning
                if not resolved_window_fallback:
                    time.sleep(0.05)
                    continue
            elif mode == "auto":
                if not resolved_window_fallback:
                    warning = "window title readiness unavailable"
                    time.sleep(0.05)
                    continue
                warning = "window title readiness unavailable; falling back to process readiness"
                mode = "process"
            else:
                warning = "window title readiness unavailable"

        if mode in ("auto", "window") and editor.process_name:
            if title_contains and not resolved_window_fallback:
                time.sleep(0.05)
                continue
            count = window_count(editor.process_name)
            if count is not None:
                if count > 0:
                    return time.perf_counter() - start, "window", warning
            elif mode == "auto":
                warning = (
                    "window readiness unavailable; falling back to process readiness"
                )
                mode = "process"
            else:
                warning = "window readiness unavailable"

        if mode == "process" or mode == "auto":
            if title_contains and not resolved_window_fallback:
                time.sleep(0.05)
                continue
            if matching_processes(editor):
                return time.perf_counter() - start, "process", warning

        time.sleep(0.05)

    raise TimeoutError(
        f"{editor.label}: not ready after {timeout:.1f}s using readiness={readiness}"
    )


def fixed_window_geometry(args: argparse.Namespace) -> tuple[int, int, int, int]:
    return (
        args.window_x,
        args.window_y,
        args.window_width,
        args.window_height,
    )


def set_window_geometry(
    editor: Editor,
    args: argparse.Namespace,
    *,
    title_contains: str | None = None,
    allow_window_fallback: bool = True,
) -> tuple[float | None, dict[str, Any] | None, str | None]:
    if not args.fix_window_geometry:
        return None, None, None
    if args.gui_driver != "ax":
        return None, None, "fixed window geometry requires --gui-driver ax"

    try:
        payload = run_mac_gui_probe(
            "set-window",
            editor,
            timeout=args.timeout,
            title_contains=title_contains,
            window_geometry=fixed_window_geometry(args),
            allow_window_fallback=allow_window_fallback,
            prompt_permissions=True,
        )
    except Exception as exc:
        return None, None, f"window geometry unavailable: {exc}"
    return float(payload["elapsed_seconds"]), payload.get("after"), None


def combine_warnings(*items: str | None) -> str | None:
    return "; ".join(item for item in items if item) or None


def case_targets(
    case: str, large_project: Path | None
) -> tuple[list[Path], str | None]:
    if case.startswith("edit_ready_"):
        raise ValueError(f"{case} uses a per-iteration temporary file")
    if case == "memory_folder":
        return [FIXTURES / "sample_project"], None
    if case == "memory_10_files":
        return [FIXTURES / "ten_files" / f"file_{i:02}.js" for i in range(1, 11)], None
    if case in ("open_100k_js", "scroll_100k_js"):
        path = FIXTURES / "large_100k_lines.js"
        return [path], path.name
    if case in ("memory_large_project", "search_large_project"):
        if large_project is None:
            candidate = repo_root() / "zed"
            large_project = (
                candidate if candidate.exists() else FIXTURES / "sample_project"
            )
        return [large_project], None
    raise ValueError(f"unknown case: {case}")


def run_open_file_case(
    editor: Editor,
    case: str,
    targets: list[Path],
    title: str | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)
    time.sleep(args.cold_delay)
    start = time.perf_counter()
    launch_editor(editor, targets)
    ready_elapsed, ready_mode, warning = wait_ready(
        editor,
        readiness="window_title" if title else args.readiness,
        timeout=args.timeout,
        title_contains=title,
        gui_driver=args.gui_driver,
        allow_window_fallback=False if title else None,
    )
    window_geometry_seconds, window_geometry, geometry_warning = set_window_geometry(
        editor,
        args,
        title_contains=title,
        allow_window_fallback=False if title else True,
    )
    elapsed = time.perf_counter() - start

    if args.post_case_quit:
        quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)

    return {
        "metric": "window_title_ready_seconds" if title else "file_ready_seconds",
        "value": elapsed,
        "ready_seconds": ready_elapsed,
        "ready_mode": ready_mode,
        "window_geometry_seconds": window_geometry_seconds,
        "window_geometry": window_geometry,
        "warning": combine_warnings(warning, geometry_warning),
    }


def require_osascript_editor(editor: Editor) -> None:
    if not editor.app_name or not editor.process_name:
        raise ValueError("osascript-driven cases require app_name and process_name")


def activate_editor_script(editor: Editor, delay: float) -> str:
    require_osascript_editor(editor)
    process_name = applescript_string(editor.process_name or "")
    return f"""
tell application {applescript_string(editor.app_name or "")} to activate
delay {delay:.3f}
tell application "System Events"
  if exists application process {process_name} then
    tell application process {process_name}
      set frontmost to true
      try
        set w to front window
        set p to position of w
        set s to size of w
        set click_x to (item 1 of p) + ((item 1 of s) div 2)
        set click_y to (item 2 of p) + ((item 2 of s) div 2)
        click at {{click_x, click_y}}
      end try
    end tell
  end if
  delay 0.100
  key code 53
  delay 0.050
  key code 18 using command down
  delay 0.100
  key code 53
end tell
"""


def send_text_and_save(
    editor: Editor, text: str, delay: float
) -> tuple[float, str | None]:
    script = (
        activate_editor_script(editor, delay)
        + f"""
tell application "System Events"
  keystroke {applescript_string(text)}
  delay 0.050
  keystroke "s" using command down
end tell
"""
    )
    return run_osascript(script)


def send_text_and_save_ax(
    editor: Editor,
    text: str,
    delay: float,
    timeout: float,
    title_contains: str | None,
) -> dict[str, Any]:
    return run_mac_gui_probe(
        "type-save",
        editor,
        timeout=timeout,
        title_contains=title_contains,
        text=text,
        activation_delay=delay,
        allow_window_fallback=True,
        prompt_permissions=True,
    )


def make_edit_target(case: str, editor: Editor, iteration: int) -> tuple[Path, str]:
    TMP.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", editor.label)
    nonce = time.time_ns()
    suffix = ".js" if case == "edit_ready_100k_js" else ".txt"
    path = TMP / f"{case}_{safe_label}_{iteration}_{nonce}{suffix}"

    if case == "edit_ready_small":
        path.write_text("", encoding="utf-8")
    elif case == "edit_ready_100k_js":
        shutil.copyfile(FIXTURES / "large_100k_lines.js", path)
    else:
        raise ValueError(f"unknown edit readiness case: {case}")

    marker = f"BENCH_EDIT_{safe_label}_{iteration}_{nonce}"
    return path, marker


def wait_for_file_to_contain(path: Path, marker: str, timeout: float) -> float:
    start = time.perf_counter()
    deadline = start + timeout
    while time.perf_counter() < deadline:
        try:
            if marker in path.read_text(encoding="utf-8", errors="ignore"):
                return time.perf_counter() - start
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise TimeoutError(
        f"file did not contain typed marker after {timeout:.1f}s: {path}"
    )


def run_edit_ready_case(
    editor: Editor, case: str, args: argparse.Namespace, iteration: int
) -> dict[str, Any]:
    target, marker = make_edit_target(case, editor, iteration)
    quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)
    time.sleep(args.cold_delay)

    start = time.perf_counter()
    launch_editor(editor, [target])
    ready_elapsed, ready_mode, warning = wait_ready(
        editor,
        readiness="window_title",
        timeout=args.timeout,
        title_contains=target.name,
        gui_driver=args.gui_driver,
        allow_window_fallback=False,
    )
    window_geometry_seconds, window_geometry, geometry_warning = set_window_geometry(
        editor,
        args,
        title_contains=target.name,
        allow_window_fallback=False,
    )
    input_probe_started = time.perf_counter()
    if args.gui_driver == "ax":
        input_payload = send_text_and_save_ax(
            editor,
            marker,
            args.edit_delay,
            args.edit_timeout,
            target.name,
        )
        input_seconds = float(input_payload["elapsed_seconds"])
        input_started_seconds = (
            input_probe_started
            - start
            + float(input_payload.get("input_dispatched_seconds", input_seconds))
        )
        input_warning = None
        input_driver = "ax"
    else:
        input_seconds, input_warning = send_text_and_save(
            editor, marker, args.edit_delay
        )
        input_started_seconds = input_probe_started - start + input_seconds
        input_driver = "osascript"
    save_observed_delta = wait_for_file_to_contain(target, marker, args.edit_timeout)
    elapsed = time.perf_counter() - start

    if args.post_case_quit:
        quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)

    return {
        "metric": "editable_saved_seconds",
        "value": elapsed,
        "ready_seconds": ready_elapsed,
        "ready_mode": ready_mode,
        "input_driver": input_driver,
        "input_seconds": input_seconds,
        "input_started_seconds": input_started_seconds,
        "save_observed_wait_seconds": save_observed_delta,
        "window_geometry_seconds": window_geometry_seconds,
        "window_geometry": window_geometry,
        "typed_marker": marker,
        "target_path": str(target),
        "warning": combine_warnings(warning, geometry_warning, input_warning),
    }


def run_scroll_case(
    editor: Editor,
    case: str,
    targets: list[Path],
    title: str | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)
    time.sleep(args.cold_delay)
    start = time.perf_counter()
    launch_editor(editor, targets)
    ready_elapsed, ready_mode, warning = wait_ready(
        editor,
        readiness="window_title" if title else args.readiness,
        timeout=args.timeout,
        title_contains=title,
        gui_driver=args.gui_driver,
        allow_window_fallback=False if title else None,
    )
    window_geometry_seconds, window_geometry, geometry_warning = set_window_geometry(
        editor,
        args,
        title_contains=title,
        allow_window_fallback=False if title else True,
    )

    scroll_script = (
        activate_editor_script(editor, args.edit_delay)
        + f"""
tell application "System Events"
  repeat {args.scroll_steps} times
    key code 121
    delay 0.010
  end repeat
end tell
"""
    )
    osascript_seconds, osascript_warning = run_osascript(scroll_script)
    elapsed = time.perf_counter() - start

    if args.post_case_quit:
        quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)

    return {
        "metric": "open_scroll_seconds",
        "value": elapsed,
        "ready_seconds": ready_elapsed,
        "ready_mode": ready_mode,
        "osascript_seconds": osascript_seconds,
        "scroll_steps": args.scroll_steps,
        "window_geometry_seconds": window_geometry_seconds,
        "window_geometry": window_geometry,
        "warning": combine_warnings(warning, geometry_warning, osascript_warning),
    }


def run_search_case(
    editor: Editor,
    case: str,
    targets: list[Path],
    title: str | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)
    time.sleep(args.cold_delay)
    start = time.perf_counter()
    launch_editor(editor, targets)
    ready_elapsed, ready_mode, warning = wait_ready(
        editor,
        readiness=args.readiness,
        timeout=args.timeout,
        title_contains=title,
        gui_driver=args.gui_driver,
    )
    window_geometry_seconds, window_geometry, geometry_warning = set_window_geometry(
        editor,
        args,
        title_contains=title,
    )

    search_script = (
        activate_editor_script(editor, args.edit_delay)
        + f"""
tell application "System Events"
  keystroke "f" using {{command down, shift down}}
  delay 0.100
  keystroke {applescript_string(args.search_query)}
  delay 0.050
  key code 36
end tell
"""
    )
    osascript_seconds, osascript_warning = run_osascript(search_script)
    time.sleep(args.search_settle_seconds)
    elapsed = time.perf_counter() - start

    if args.post_case_quit:
        quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)

    return {
        "metric": "project_search_ui_seconds",
        "value": elapsed,
        "ready_seconds": ready_elapsed,
        "ready_mode": ready_mode,
        "osascript_seconds": osascript_seconds,
        "search_query": args.search_query,
        "search_settle_seconds": args.search_settle_seconds,
        "window_geometry_seconds": window_geometry_seconds,
        "window_geometry": window_geometry,
        "warning": combine_warnings(warning, geometry_warning, osascript_warning),
    }


def run_memory_case(
    editor: Editor,
    case: str,
    targets: list[Path],
    title: str | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)
    time.sleep(args.cold_delay)
    start = time.perf_counter()
    launch_editor(editor, targets)
    ready_elapsed, ready_mode, warning = wait_ready(
        editor,
        readiness=args.readiness,
        timeout=args.timeout,
        title_contains=title,
        gui_driver=args.gui_driver,
    )
    window_geometry_seconds, window_geometry, geometry_warning = set_window_geometry(
        editor,
        args,
        title_contains=title,
    )
    launch_elapsed = time.perf_counter() - start
    time.sleep(args.settle_seconds)

    samples = []
    process_counts = []
    for _ in range(args.memory_samples):
        snap = memory_snapshot(editor)
        samples.append(snap["rss_mb"])
        process_counts.append(snap["process_count"])
        time.sleep(args.memory_interval)

    if args.post_case_quit:
        quit_editor(editor, force_kill=args.force_kill, timeout=args.quit_timeout)

    return {
        "metric": "rss_mb",
        "value": statistics.mean(samples) if samples else 0.0,
        "rss_mb_mean": statistics.mean(samples) if samples else 0.0,
        "rss_mb_max": max(samples) if samples else 0.0,
        "process_count_mean": statistics.mean(process_counts)
        if process_counts
        else 0.0,
        "launch_seconds": launch_elapsed,
        "ready_seconds": ready_elapsed,
        "ready_mode": ready_mode,
        "window_geometry_seconds": window_geometry_seconds,
        "window_geometry": window_geometry,
        "warning": combine_warnings(warning, geometry_warning),
    }


def run_bench(args: argparse.Namespace) -> Path:
    ensure_fixtures()
    RESULTS.mkdir(parents=True, exist_ok=True)

    editors = load_config(args.config)
    if args.editors:
        requested = set(args.editors)
        editors = [editor for editor in editors if editor.label in requested]
    if not editors:
        raise SystemExit("No editors selected. Check --editors and config.")

    large_project = Path(args.large_project).resolve() if args.large_project else None
    cases = args.cases or DEFAULT_CASES
    print(f"bench.py suite: {BENCH_SUITE_VERSION}", flush=True)
    print("effective cases:", ", ".join(cases), flush=True)

    started = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_base = (
        Path(args.output).resolve() if args.output else RESULTS / f"bench-{started}"
    )
    json_path = output_base.with_suffix(".json")
    csv_path = output_base.with_suffix(".csv")

    records: list[dict[str, Any]] = []
    info = system_info()
    for editor in editors:
        for case in cases:
            if case.startswith("edit_ready_"):
                targets: list[Path] = []
                title = None
            else:
                targets, title = case_targets(case, large_project)
            for iteration in range(1, args.iterations + 1):
                print(
                    f"[{editor.label}] {case} iteration {iteration}/{args.iterations}",
                    flush=True,
                )
                try:
                    if case == "open_100k_js":
                        measurement = run_open_file_case(
                            editor, case, targets, title, args
                        )
                    elif case.startswith("edit_ready_"):
                        measurement = run_edit_ready_case(editor, case, args, iteration)
                    elif case == "scroll_100k_js":
                        measurement = run_scroll_case(
                            editor, case, targets, title, args
                        )
                    elif case == "search_large_project":
                        measurement = run_search_case(
                            editor, case, targets, title, args
                        )
                    elif case.startswith("memory_"):
                        measurement = run_memory_case(
                            editor, case, targets, title, args
                        )
                    else:
                        raise ValueError(f"unhandled case: {case}")
                    status = "ok"
                    error = None
                except (
                    Exception
                ) as exc:  # keep going so one editor failure does not lose all data
                    measurement = {"metric": "error", "value": None}
                    status = "error"
                    error = repr(exc)
                    print(f"  ERROR: {error}", file=sys.stderr)
                    if args.post_case_quit:
                        quit_editor(
                            editor,
                            force_kill=True,
                            timeout=args.quit_timeout,
                        )

                records.append(
                    {
                        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "editor": editor.label,
                        "case": case,
                        "iteration": iteration,
                        "targets": [str(p) for p in targets],
                        "status": status,
                        "error": error,
                        **measurement,
                    }
                )

    payload = {
        "system": info,
        "config": {
            "iterations": args.iterations,
            "readiness": args.readiness,
            "gui_driver": args.gui_driver,
            "settle_seconds": args.settle_seconds,
            "memory_samples": args.memory_samples,
            "memory_interval": args.memory_interval,
            "edit_delay": args.edit_delay,
            "edit_timeout": args.edit_timeout,
            "fix_window_geometry": args.fix_window_geometry,
            "window_geometry": {
                "x": args.window_x,
                "y": args.window_y,
                "width": args.window_width,
                "height": args.window_height,
            },
            "scroll_steps": args.scroll_steps,
            "search_query": args.search_query,
            "search_settle_seconds": args.search_settle_seconds,
            "cases": cases,
        },
        "records": records,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        fieldnames = sorted(
            {key for record in records for key in record.keys()} - {"targets"}
        ) + ["targets"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = dict(record)
            row["targets"] = ";".join(record.get("targets", []))
            writer.writerow(row)

    write_summary(json_path)
    return json_path


def write_summary(json_path: Path) -> Path:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    records = [
        r
        for r in payload["records"]
        if r.get("status") == "ok" and isinstance(r.get("value"), (int, float))
    ]
    grouped: dict[tuple[str, str, str], list[float]] = {}
    for r in records:
        key = (r["case"], r["editor"], r["metric"])
        grouped.setdefault(key, []).append(float(r["value"]))

    lines = [
        "# Benchmark summary",
        "",
        f"Source: `{json_path.name}`",
        "",
        "## System",
        "",
    ]
    for key, value in payload.get("system", {}).items():
        if value is not None:
            lines.append(f"- `{key}`: {value}")

    lines += [
        "",
        "## Results",
        "",
        "| Case | Editor | Metric | n | mean | median | min | max |",
        "|---|---:|---|---:|---:|---:|---:|---:|",
    ]
    for (case, editor, metric), values in sorted(grouped.items()):
        lines.append(
            "| {case} | {editor} | {metric} | {n} | {mean:.3f} | {median:.3f} | {minv:.3f} | {maxv:.3f} |".format(
                case=case,
                editor=editor,
                metric=metric,
                n=len(values),
                mean=statistics.mean(values),
                median=statistics.median(values),
                minv=min(values),
                maxv=max(values),
            )
        )

    errors = [r for r in payload["records"] if r.get("status") != "ok"]
    if errors:
        lines += ["", "## Errors", ""]
        for r in errors:
            lines.append(
                f"- `{r['editor']}` `{r['case']}` iteration {r['iteration']}: {r.get('error')}"
            )

    summary_path = json_path.with_suffix(".md")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path


def cmd_prepare(_: argparse.Namespace) -> None:
    ensure_fixtures()
    print(f"Prepared fixtures in {FIXTURES}")


def cmd_summary(args: argparse.Namespace) -> None:
    path = write_summary(Path(args.input).resolve())
    print(f"Wrote {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run macOS editor benchmarks.")
    sub = parser.add_subparsers(dest="command_name", required=True)

    prepare = sub.add_parser("prepare", help="Generate benchmark fixtures.")
    prepare.set_defaults(func=cmd_prepare)

    run_p = sub.add_parser("run", help="Run benchmark cases.")
    run_p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Editor config JSON. Defaults to editors.local.json if present, otherwise editors.example.json.",
    )
    run_p.add_argument(
        "--editors", nargs="*", help="Editor labels to run, e.g. zed vscode."
    )
    run_p.add_argument(
        "--cases",
        nargs="*",
        choices=CASE_CHOICES,
        help="Cases to run. Defaults to the practical Zed vs VS Code suite, including GUI operations.",
    )
    run_p.add_argument("--iterations", type=int, default=5)
    run_p.add_argument(
        "--output", help="Output path without extension, or with extension ignored."
    )
    run_p.add_argument(
        "--large-project",
        help="Folder for large-project memory test. Defaults to ../zed if present.",
    )
    run_p.add_argument(
        "--readiness",
        choices=["auto", "process", "window", "window_title"],
        default="auto",
    )
    run_p.add_argument(
        "--gui-driver",
        choices=["ax", "osascript"],
        default="ax",
        help="GUI observation/input backend. ax uses the Swift Accessibility/CoreGraphics probe; osascript keeps the legacy System Events path.",
    )
    run_p.add_argument("--timeout", type=float, default=30.0)
    run_p.add_argument("--quit-timeout", type=float, default=8.0)
    run_p.add_argument(
        "--cold-delay",
        type=float,
        default=1.0,
        help="Delay after quitting before relaunch. This does not clear OS disk cache.",
    )
    run_p.add_argument(
        "--settle-seconds",
        type=float,
        default=5.0,
        help="Delay after ready before memory sampling.",
    )
    run_p.add_argument("--memory-samples", type=int, default=5)
    run_p.add_argument("--memory-interval", type=float, default=1.0)
    run_p.add_argument(
        "--edit-delay",
        type=float,
        default=1.0,
        help="Delay after activating the editor before sending GUI input.",
    )
    run_p.add_argument(
        "--edit-timeout",
        type=float,
        default=20.0,
        help="How long to wait for the typed marker to appear on disk after Command-S.",
    )
    run_p.add_argument(
        "--no-fix-window-geometry",
        dest="fix_window_geometry",
        action="store_false",
        help="Do not normalize editor window position and size before GUI cases.",
    )
    run_p.add_argument(
        "--window-x",
        type=int,
        default=80,
        help="Fixed editor window X position used before GUI cases.",
    )
    run_p.add_argument(
        "--window-y",
        type=int,
        default=80,
        help="Fixed editor window Y position used before GUI cases.",
    )
    run_p.add_argument(
        "--window-width",
        type=int,
        default=1400,
        help="Fixed editor window width used before GUI cases.",
    )
    run_p.add_argument(
        "--window-height",
        type=int,
        default=900,
        help="Fixed editor window height used before GUI cases.",
    )
    run_p.add_argument(
        "--scroll-steps",
        type=int,
        default=30,
        help="Number of Page Down keystrokes to send for the 100K-line scroll case.",
    )
    run_p.add_argument(
        "--search-query",
        default="struct ",
        help="Project-wide search query used by search_large_project.",
    )
    run_p.add_argument(
        "--search-settle-seconds",
        type=float,
        default=3.0,
        help="Delay after dispatching project search before recording completion.",
    )
    run_p.add_argument(
        "--force-kill",
        action="store_true",
        help="SIGTERM matching processes that do not quit within --quit-timeout.",
    )
    run_p.add_argument(
        "--no-post-case-quit",
        dest="post_case_quit",
        action="store_false",
        help="Leave app open after each case.",
    )
    run_p.set_defaults(
        func=lambda args: print(f"Wrote {run_bench(args)}"),
        post_case_quit=True,
        fix_window_geometry=True,
    )

    summary = sub.add_parser(
        "summary", help="Regenerate markdown summary from a JSON result."
    )
    summary.add_argument("input")
    summary.set_defaults(func=cmd_summary)

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
