from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parent
FORTRAN_ROOT = ROOT / "fortran_tep"
MODERN_ROOT = ROOT / "modern_tep"
OUTPUT_ROOT = ROOT / "benchmark_outputs"

SELECTED_MEASUREMENTS = {
    6: "XMEAS(7) Reactor pressure",
    8: "XMEAS(9) Reactor temperature",
    13: "XMEAS(14) Separator underflow",
    16: "XMEAS(17) Product underflow",
    39: "XMEAS(40) Product G",
    40: "XMEAS(41) Product H",
}


@dataclass
class RunData:
    implementation: str
    scenario: str
    time_h: np.ndarray
    measurements: np.ndarray
    manipulated_variables: np.ndarray
    shutdown: bool
    shutdown_time_h: float | None
    wall_time_s: float
    error: str | None = None

    @property
    def final_time_h(self) -> float:
        if self.time_h.size:
            return float(self.time_h[-1])
        return 0.0

    @property
    def ms_per_process_hour(self) -> float | None:
        if self.final_time_h <= 0:
            return None
        return 1000.0 * self.wall_time_s / self.final_time_h


def clean_import_environment() -> list[str]:
    """Prefer the checked-out projects over stale editable-install hooks."""
    removed: list[str] = []
    kept = []
    for finder in sys.meta_path:
        finder_name = type(finder).__name__.lower()
        finder_repr = repr(finder).lower()
        if "mesonpy" in finder_name or "_tep_editable" in finder_repr:
            removed.append(repr(finder))
            continue
        kept.append(finder)
    sys.meta_path = kept
    for src in (MODERN_ROOT / "src", FORTRAN_ROOT / "src"):
        src_str = str(src)
        if src_str not in sys.path:
            sys.path.insert(0, src_str)
    return removed


def _command_status(command: list[str]) -> dict[str, Any]:
    executable = shutil.which(command[0])
    if executable is None:
        return {"available": False, "executable": "", "version": "not found"}
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        text = (completed.stdout or completed.stderr).strip().splitlines()
        version = text[0] if text else f"exit {completed.returncode}"
        return {
            "available": completed.returncode == 0,
            "executable": executable,
            "version": version,
        }
    except Exception as exc:
        return {"available": False, "executable": executable, "version": f"{type(exc).__name__}: {exc}"}


def _package_status(import_name: str, distribution_name: str | None = None) -> dict[str, Any]:
    spec = importlib.util.find_spec(import_name)
    if spec is None:
        return {"available": False, "version": "not installed", "origin": ""}
    version = "unknown"
    try:
        module = __import__(import_name)
        version = getattr(module, "__version__", "unknown")
    except Exception:
        if distribution_name:
            try:
                version = importlib_metadata.version(distribution_name)
            except importlib_metadata.PackageNotFoundError:
                version = "installed, version unknown"
    return {"available": True, "version": version, "origin": spec.origin or ""}


def _subprocess_python_probe(code: str) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=env,
        )
        output = (completed.stdout or completed.stderr).strip()
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "output": output,
        }
    except Exception as exc:
        return {"ok": False, "returncode": None, "output": f"{type(exc).__name__}: {exc}"}


def collect_environment_health(removed_import_hooks: list[str]) -> dict[str, Any]:
    packages = {
        "numpy": _package_status("numpy"),
        "scipy": _package_status("scipy"),
        "matplotlib": _package_status("matplotlib"),
        "cffi": _package_status("cffi"),
        "_cffi_backend": _package_status("_cffi_backend"),
        "gymnasium": _package_status("gymnasium"),
    }
    tools = {
        "gfortran": _command_status(["gfortran", "--version"]),
        "gcc": _command_status(["gcc", "--version"]),
        "ninja": _command_status(["ninja", "--version"]),
        "meson": _command_status(["meson", "--version"]),
    }

    installed_fortran_probe = _subprocess_python_probe(
        "import os; "
        "os.add_dll_directory(r'C:\\Strawberry\\c\\bin'); "
        "from tep import TEPSimulator, ControlMode; import tep; "
        "s=TEPSimulator(backend='fortran', control_mode=ControlMode.OPEN_LOOP); "
        "s.initialize(); "
        "print(tep.__file__); print(s.backend); print(float(s.get_measurements()[6]))"
    )

    workspace_fortran_probe: dict[str, Any]
    workspace_python_probe: dict[str, Any]
    modern_native_probe: dict[str, Any]
    try:
        _, TEPSimulator, ControlMode, _ = load_fortran_api()
        try:
            sim = TEPSimulator(backend="fortran", control_mode=ControlMode.OPEN_LOOP)
            sim.initialize()
            workspace_fortran_probe = {
                "ok": True,
                "output": f"{sim.backend}; XMEAS7={float(sim.get_measurements()[6]):.10g}",
            }
        except Exception as exc:
            workspace_fortran_probe = {"ok": False, "output": f"{type(exc).__name__}: {exc}"}
        try:
            sim = TEPSimulator(backend="python", control_mode=ControlMode.OPEN_LOOP)
            sim.initialize()
            workspace_python_probe = {
                "ok": True,
                "output": f"{sim.backend}; XMEAS7={float(sim.get_measurements()[6]):.10g}",
            }
        except Exception as exc:
            workspace_python_probe = {"ok": False, "output": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:
        workspace_fortran_probe = {"ok": False, "output": f"{type(exc).__name__}: {exc}"}
        workspace_python_probe = {"ok": False, "output": f"{type(exc).__name__}: {exc}"}

    try:
        TennesseeEastmanProcess, _ = load_modern_api()
        sim = TennesseeEastmanProcess()
        meas, _info = sim.reset(mode="mode1", seed=4651207995)
        result = sim.advance(sim.state[38:50], control_interval=0.001)
        modern_native_probe = {
            "ok": True,
            "output": (
                f"reset_shape={tuple(meas.shape)}; advance_time={result.time:.6g}; "
                f"XMEAS7={float(result.measurements[6]):.10g}; terminated={result.shutdown_status['terminated']}"
            ),
        }
    except Exception as exc:
        modern_native_probe = {"ok": False, "output": f"{type(exc).__name__}: {exc}"}

    return {
        "workspace": {
            "path": str(ROOT),
            "expected_path": r"C:\Users\Aybars\Desktop\envComp",
            "path_matches_expected": str(ROOT) == r"C:\Users\Aybars\Desktop\envComp",
            "path_has_spaces": " " in str(ROOT),
        },
        "python": {
            "executable": sys.executable,
            "version": sys.version.replace("\n", " "),
            "platform": platform.platform(),
        },
        "packages": packages,
        "tools": tools,
        "editable_import_hooks_removed": removed_import_hooks,
        "backend_probes": {
            "installed_site_packages_tep_fortran": installed_fortran_probe,
            "workspace_fortran_tep_fortran": workspace_fortran_probe,
            "workspace_fortran_tep_python": workspace_python_probe,
            "modern_tep_native": modern_native_probe,
        },
    }


def load_schema_static():
    """Load modern schema metadata even if the native modern kernel is unavailable."""
    try:
        from tep_studio.simulation.schema import TEP_SCHEMA

        return TEP_SCHEMA, None
    except Exception as exc:
        schema_path = MODERN_ROOT / "src" / "tep_studio" / "simulation" / "schema.py"
        try:
            spec = importlib.util.spec_from_file_location("_tep_schema_static", schema_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Could not create import spec for {schema_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module.TEP_SCHEMA, f"Loaded schema directly after package import failed: {exc}"
        except Exception as fallback_exc:
            return None, f"Could not load modern schema: {fallback_exc}"


def load_fortran_api():
    import tep
    from tep import ControlMode, TEPSimulator
    from tep import constants as c

    return tep, TEPSimulator, ControlMode, c


def load_modern_api():
    from tep_studio.control import RickerMultiLoopController
    from tep_studio.simulation.core import TennesseeEastmanProcess

    return TennesseeEastmanProcess, RickerMultiLoopController


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("|", "\\|").replace("\n", "<br>")

    out = ["| " + " | ".join(headers) + " |"]
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        out.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(out)


def static_mappings(schema, constants_module) -> dict[str, list[dict[str, Any]]]:
    state_rows: list[dict[str, Any]] = []
    if schema is not None:
        for i, variable in enumerate(schema.states, start=1):
            initial = ""
            if i <= len(constants_module.INITIAL_STATES):
                initial = f"{float(constants_module.INITIAL_STATES[i - 1]):.10g}"
            state_rows.append(
                {
                    "index": i,
                    "fortran_symbol": f"YY({i})",
                    "modern_name": variable.name,
                    "unit": variable.unit,
                    "description": variable.description,
                    "initial_mode1": initial,
                }
            )
    else:
        for i, value in enumerate(constants_module.INITIAL_STATES, start=1):
            state_rows.append(
                {
                    "index": i,
                    "fortran_symbol": f"YY({i})",
                    "modern_name": "",
                    "unit": "",
                    "description": "",
                    "initial_mode1": f"{float(value):.10g}",
                }
            )

    measurement_rows: list[dict[str, Any]] = []
    for i, (name, unit) in enumerate(
        zip(constants_module.MEASUREMENT_NAMES, constants_module.MEASUREMENT_UNITS),
        start=1,
    ):
        modern = schema.measurements[i - 1] if schema is not None and i <= len(schema.measurements) else None
        measurement_rows.append(
            {
                "index": i,
                "fortran_symbol": f"XMEAS({i})",
                "fortran_name": name,
                "fortran_unit": unit,
                "modern_name": "" if modern is None else modern.name,
                "modern_unit": "" if modern is None else modern.unit,
                "modern_update_mode": "" if modern is None else modern.update_mode,
                "modern_sample_period_h": "" if modern is None else modern.sample_period_hours,
            }
        )

    mv_rows: list[dict[str, Any]] = []
    for i, name in enumerate(constants_module.MANIPULATED_VAR_NAMES, start=1):
        modern = schema.manipulated_variables[i - 1] if schema is not None and i <= len(schema.manipulated_variables) else None
        mv_rows.append(
            {
                "index": i,
                "fortran_symbol": f"XMV({i})",
                "fortran_name": name,
                "modern_name": "" if modern is None else modern.name,
                "modern_unit": "" if modern is None else modern.unit,
                "lower": "" if modern is None else modern.lower,
                "upper": "" if modern is None else modern.upper,
                "initial_mode1": f"{float(constants_module.INITIAL_STATES[38 + i - 1]):.10g}",
            }
        )

    disturbance_rows: list[dict[str, Any]] = []
    max_idv = max(len(constants_module.DISTURBANCE_NAMES), len(schema.disturbances) if schema is not None else 0)
    for i in range(1, max_idv + 1):
        fortran_name = constants_module.DISTURBANCE_NAMES[i - 1] if i <= len(constants_module.DISTURBANCE_NAMES) else ""
        modern = schema.disturbances[i - 1] if schema is not None and i <= len(schema.disturbances) else None
        disturbance_rows.append(
            {
                "index": i,
                "fortran_symbol": f"IDV({i})" if i <= len(constants_module.DISTURBANCE_NAMES) else "",
                "fortran_name": fortran_name,
                "modern_name": "" if modern is None else modern.name,
                "modern_description": "" if modern is None else modern.description,
                "modern_model": "" if modern is None else modern.perturbation_model,
            }
        )

    return {
        "states": state_rows,
        "measurements": measurement_rows,
        "manipulated_variables": mv_rows,
        "disturbances": disturbance_rows,
    }


def run_fortran_simulator(
    *,
    scenario: str,
    backend: str,
    closed_loop: bool,
    horizon_h: float,
    record_dt_h: float,
    seed: int,
    disturbance_id: int | None = None,
    disturbance_time_h: float | None = None,
) -> RunData:
    started = time.perf_counter()
    try:
        _, TEPSimulator, ControlMode, _ = load_fortran_api()
        mode = ControlMode.CLOSED_LOOP if closed_loop else ControlMode.OPEN_LOOP
        sim = TEPSimulator(random_seed=seed, control_mode=mode, backend=backend)
        sim.initialize()
        record_interval_steps = max(1, int(round(record_dt_h * 3600.0)))
        disturbances = None
        if disturbance_id is not None and disturbance_time_h is not None:
            disturbances = {int(disturbance_id): (float(disturbance_time_h), 1)}
        result = sim.simulate(
            duration_hours=horizon_h,
            disturbances=disturbances,
            record_interval=record_interval_steps,
        )
        wall = time.perf_counter() - started
        return RunData(
            implementation=f"fortran_tep:{backend}",
            scenario=scenario,
            time_h=np.asarray(result.time, dtype=float),
            measurements=np.asarray(result.measurements, dtype=float),
            manipulated_variables=np.asarray(result.manipulated_vars, dtype=float),
            shutdown=bool(result.shutdown),
            shutdown_time_h=None if result.shutdown_time is None else float(result.shutdown_time),
            wall_time_s=wall,
        )
    except Exception as exc:
        wall = time.perf_counter() - started
        return RunData(
            implementation=f"fortran_tep:{backend}",
            scenario=scenario,
            time_h=np.array([], dtype=float),
            measurements=np.empty((0, 41), dtype=float),
            manipulated_variables=np.empty((0, 12), dtype=float),
            shutdown=False,
            shutdown_time_h=None,
            wall_time_s=wall,
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )


def run_modern_open_loop(
    *,
    scenario: str,
    horizon_h: float,
    record_dt_h: float,
    control_interval_h: float,
    seed: int,
    solver_method: str,
    fixed_step_h: float,
    disturbance_id: int | None = None,
    disturbance_time_h: float | None = None,
) -> RunData:
    started = time.perf_counter()
    try:
        TennesseeEastmanProcess, _ = load_modern_api()
        sim = TennesseeEastmanProcess(solver_method=solver_method, fixed_step=fixed_step_h)
        meas, _ = sim.reset(mode="mode1", seed=seed)
        action = sim.state[38:50].copy()
        idv = np.zeros(28, dtype=float)
        times = [0.0]
        measurements = [np.asarray(meas, dtype=float).copy()]
        mvs = [action.copy()]
        next_record = record_dt_h
        shutdown = False
        shutdown_time = None
        eps = min(control_interval_h, record_dt_h) * 1e-6
        while sim.time < horizon_h - eps:
            if disturbance_id is not None and disturbance_time_h is not None and sim.time >= disturbance_time_h:
                idv[int(disturbance_id) - 1] = 1.0
            interval = min(control_interval_h, horizon_h - sim.time)
            result = sim.advance(action, control_interval=interval, disturbances=idv)
            if result.time + eps >= next_record or result.shutdown_status["terminated"]:
                times.append(float(result.time))
                measurements.append(result.measurements.copy())
                mvs.append(result.implemented_action.copy())
                while next_record <= result.time + eps:
                    next_record += record_dt_h
            if result.shutdown_status["terminated"]:
                shutdown = True
                shutdown_time = float(result.time)
                break
        wall = time.perf_counter() - started
        return RunData(
            implementation=f"modern_tep:{solver_method}",
            scenario=scenario,
            time_h=np.asarray(times, dtype=float),
            measurements=np.asarray(measurements, dtype=float),
            manipulated_variables=np.asarray(mvs, dtype=float),
            shutdown=shutdown,
            shutdown_time_h=shutdown_time,
            wall_time_s=wall,
        )
    except Exception as exc:
        wall = time.perf_counter() - started
        return RunData(
            implementation=f"modern_tep:{solver_method}",
            scenario=scenario,
            time_h=np.array([], dtype=float),
            measurements=np.empty((0, 41), dtype=float),
            manipulated_variables=np.empty((0, 12), dtype=float),
            shutdown=False,
            shutdown_time_h=None,
            wall_time_s=wall,
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )


def run_modern_closed_loop(
    *,
    scenario: str,
    horizon_h: float,
    record_dt_h: float,
    control_interval_h: float,
    seed: int,
    solver_method: str,
    fixed_step_h: float,
    disturbance_id: int | None = None,
    disturbance_time_h: float | None = None,
) -> RunData:
    started = time.perf_counter()
    try:
        TennesseeEastmanProcess, RickerMultiLoopController = load_modern_api()
        sim = TennesseeEastmanProcess(solver_method=solver_method, fixed_step=fixed_step_h)
        controller = RickerMultiLoopController()
        meas, _ = sim.reset(mode="mode1", seed=seed)
        controller.reset(meas, time=sim.time)
        idv = np.zeros(28, dtype=float)
        times = [0.0]
        measurements = [np.asarray(meas, dtype=float).copy()]
        mvs = [sim.state[38:50].copy()]
        next_record = record_dt_h
        shutdown = False
        shutdown_time = None
        eps = min(control_interval_h, record_dt_h) * 1e-6
        while sim.time < horizon_h - eps:
            if disturbance_id is not None and disturbance_time_h is not None and sim.time >= disturbance_time_h:
                idv[int(disturbance_id) - 1] = 1.0
            action, _diagnostics = controller.compute_action(meas, time=sim.time)
            interval = min(control_interval_h, horizon_h - sim.time)
            result = sim.advance(action, control_interval=interval, disturbances=idv)
            meas = result.measurements
            if result.time + eps >= next_record or result.shutdown_status["terminated"]:
                times.append(float(result.time))
                measurements.append(result.measurements.copy())
                mvs.append(result.implemented_action.copy())
                while next_record <= result.time + eps:
                    next_record += record_dt_h
            if result.shutdown_status["terminated"]:
                shutdown = True
                shutdown_time = float(result.time)
                break
        wall = time.perf_counter() - started
        return RunData(
            implementation=f"modern_tep:{solver_method}+Ricker",
            scenario=scenario,
            time_h=np.asarray(times, dtype=float),
            measurements=np.asarray(measurements, dtype=float),
            manipulated_variables=np.asarray(mvs, dtype=float),
            shutdown=shutdown,
            shutdown_time_h=shutdown_time,
            wall_time_s=wall,
        )
    except Exception as exc:
        wall = time.perf_counter() - started
        return RunData(
            implementation=f"modern_tep:{solver_method}+Ricker",
            scenario=scenario,
            time_h=np.array([], dtype=float),
            measurements=np.empty((0, 41), dtype=float),
            manipulated_variables=np.empty((0, 12), dtype=float),
            shutdown=False,
            shutdown_time_h=None,
            wall_time_s=wall,
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}",
        )


def compare_pair(fortran_run: RunData, modern_run: RunData, record_dt_h: float) -> list[dict[str, Any]]:
    if fortran_run.error or modern_run.error or fortran_run.time_h.size < 2 or modern_run.time_h.size < 2:
        return []
    end = min(fortran_run.final_time_h, modern_run.final_time_h)
    if end <= 0:
        return []
    n_points = max(2, int(math.floor(end / record_dt_h)) + 1)
    grid = np.linspace(0.0, end, n_points)
    rows: list[dict[str, Any]] = []
    for idx, label in SELECTED_MEASUREMENTS.items():
        f = np.interp(grid, fortran_run.time_h, fortran_run.measurements[:, idx])
        m = np.interp(grid, modern_run.time_h, modern_run.measurements[:, idx])
        diff = m - f
        rmse = float(np.sqrt(np.mean(diff**2)))
        baseline = float(np.mean(np.abs(f)))
        rows.append(
            {
                "scenario": fortran_run.scenario,
                "measurement_index": idx + 1,
                "measurement": label,
                "n_aligned_samples": len(grid),
                "aligned_until_h": f"{end:.6g}",
                "max_abs_error": f"{float(np.max(np.abs(diff))):.8g}",
                "mean_abs_error": f"{float(np.mean(np.abs(diff))):.8g}",
                "rmse": f"{rmse:.8g}",
                "relative_rmse_percent": "" if baseline == 0 else f"{100.0 * rmse / baseline:.8g}",
            }
        )
    return rows


def plot_pair(fortran_run: RunData, modern_run: RunData, out_dir: Path, record_dt_h: float) -> list[str]:
    if fortran_run.error or modern_run.error or fortran_run.time_h.size < 2 or modern_run.time_h.size < 2:
        return []
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    end = min(fortran_run.final_time_h, modern_run.final_time_h)
    if end <= 0:
        return []
    n_points = max(2, int(math.floor(end / record_dt_h)) + 1)
    grid = np.linspace(0.0, end, n_points)
    plot_paths: list[str] = []

    fig, axes = plt.subplots(3, 2, figsize=(13, 9), constrained_layout=True)
    for ax, (idx, label) in zip(axes.ravel(), SELECTED_MEASUREMENTS.items()):
        f = np.interp(grid, fortran_run.time_h, fortran_run.measurements[:, idx])
        m = np.interp(grid, modern_run.time_h, modern_run.measurements[:, idx])
        ax.plot(grid, f, label="fortran_tep", linewidth=1.2)
        ax.plot(grid, m, label="modern_tep", linewidth=1.2)
        ax.set_title(label)
        ax.set_xlabel("time [h]")
        ax.grid(True, alpha=0.25)
    axes.ravel()[0].legend(loc="best")
    trajectory_path = out_dir / f"trajectory_{fortran_run.scenario}.png"
    fig.savefig(trajectory_path, dpi=160)
    plt.close(fig)
    plot_paths.append(str(trajectory_path))

    fig, axes = plt.subplots(3, 2, figsize=(13, 9), constrained_layout=True)
    for ax, (idx, label) in zip(axes.ravel(), SELECTED_MEASUREMENTS.items()):
        f = np.interp(grid, fortran_run.time_h, fortran_run.measurements[:, idx])
        m = np.interp(grid, modern_run.time_h, modern_run.measurements[:, idx])
        ax.plot(grid, m - f, color="#b32134", linewidth=1.2)
        ax.axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
        ax.set_title(f"modern - reference: {label}")
        ax.set_xlabel("time [h]")
        ax.grid(True, alpha=0.25)
    deviation_path = out_dir / f"deviation_{fortran_run.scenario}.png"
    fig.savefig(deviation_path, dpi=160)
    plt.close(fig)
    plot_paths.append(str(deviation_path))
    return plot_paths


def run_summary_rows(runs: list[RunData]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        rows.append(
            {
                "scenario": run.scenario,
                "implementation": run.implementation,
                "status": "error" if run.error else ("shutdown" if run.shutdown else "completed"),
                "final_time_h": f"{run.final_time_h:.8g}",
                "shutdown_time_h": "" if run.shutdown_time_h is None else f"{run.shutdown_time_h:.8g}",
                "wall_time_s": f"{run.wall_time_s:.8g}",
                "ms_per_process_hour": "" if run.ms_per_process_hour is None else f"{run.ms_per_process_hour:.8g}",
                "samples": int(run.time_h.size),
                "error": "" if run.error is None else run.error.splitlines()[0],
            }
        )
    return rows


def write_report(
    *,
    out_dir: Path,
    health: dict[str, Any],
    schema_note: str | None,
    mappings: dict[str, list[dict[str, Any]]],
    runs: list[RunData],
    metric_rows: list[dict[str, Any]],
    plot_paths: list[str],
    args: argparse.Namespace,
) -> Path:
    summary = run_summary_rows(runs)
    lines: list[str] = []
    lines.append("# Tennessee Eastman Process Implementasyon Karsilastirmasi")
    lines.append("")
    lines.append(f"Olusturma zamani: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Ortam Saglik Kontrolu")
    lines.append("")
    workspace = health["workspace"]
    lines.append(
        markdown_table(
            ["Kontrol", "Sonuc"],
            [
                ["Calisma dizini", workspace["path"]],
                ["Beklenen dizin", workspace["expected_path"]],
                ["Yol beklenenle ayni mi", workspace["path_matches_expected"]],
                ["Yolda bosluk var mi", workspace["path_has_spaces"]],
                ["Python", health["python"]["version"]],
                ["Python executable", health["python"]["executable"]],
                ["Platform", health["python"]["platform"]],
            ],
        )
    )
    lines.append("")
    lines.append("### Paketler ve Derleyiciler")
    lines.append("")
    pkg_rows = [
        [name, data["available"], data["version"], data["origin"]]
        for name, data in health["packages"].items()
    ]
    tool_rows = [
        [name, data["available"], data["version"], data["executable"]]
        for name, data in health["tools"].items()
    ]
    lines.append(markdown_table(["Paket", "Var", "Versiyon", "Kaynak"], pkg_rows))
    lines.append("")
    lines.append(markdown_table(["Arac", "Var", "Versiyon", "Executable"], tool_rows))
    lines.append("")
    if health["editable_import_hooks_removed"]:
        lines.append("Eski editable import hook'lari temizlendi:")
        for hook in health["editable_import_hooks_removed"]:
            lines.append(f"- `{hook}`")
    else:
        lines.append("Eski Meson/editable import hook kalintisi gorulmedi.")
    lines.append("")
    lines.append("### Backend Smoke Testleri")
    lines.append("")
    probe_rows = []
    for name, data in health["backend_probes"].items():
        probe_rows.append([name, data["ok"], data["output"]])
    lines.append(markdown_table(["Probe", "OK", "Cikti"], probe_rows))
    lines.append("")
    lines.append("## Kisa Sonuc")
    lines.append("")
    lines.append(
        "Bu rapor `fortran_tep` referans sarmalayicisi ile `modern_tep` CFFI tabanli "
        "modern cekirdegini Mode 1 baslangicindan karsilastirir. Dinamik kosular ayni "
        "baslangic MV'leri, ayni seed ve ortak XMEAS indeksleri uzerinden hizalanir."
    )
    if not health["backend_probes"]["workspace_fortran_tep_fortran"]["ok"]:
        lines.append(
            "Workspace icindeki `fortran_tep` native f2py modulu yuklenemedigi icin "
            "dinamik referans kosularinda `backend='python'` kullanildi. Site-packages "
            "`tep` Fortran backend smoke testi ayri olarak raporlandi; bu kurulu paket "
            "workspace checkout'i olmadigi icin ana karsilastirmada referans alinmadi."
        )
    if health["backend_probes"]["modern_tep_native"]["ok"]:
        lines.append("`modern_tep` native CFFI cekirdegi reset/advance smoke testini gecti.")
    else:
        lines.append(
            "`modern_tep` native cekirdegi bu ortamda calismadi; dinamik modern kosulari hata olarak raporlanir."
        )
    if not health["packages"]["gymnasium"]["available"]:
        lines.append(
            "Gymnasium kurulu degil. `modern_tep` import guard'i proses cekirdegini bozmadigi icin "
            "bu durum RL adapter kullanilabilirligiyle sinirlidir."
        )
    if schema_note:
        lines.append("")
        lines.append(f"Schema notu: {schema_note}")
    lines.append("")
    lines.append("## Statik ve Mimari Analiz")
    lines.append("")
    lines.append(
        markdown_table(
            ["Baslik", "fortran_tep", "modern_tep"],
            [
                ["Cekirdek", "`teprob.f` / `TEINIT` / `TEFUNC`; Python portu ve opsiyonel f2py", "`temexd_mod.c` native C cekirdegi; CFFI bridge"],
                ["Durum sayisi", "50 adet `YY(1:50)`", "50 adet schema'li state; `legacy_index` YY ile uyumlu"],
                ["Olcumler", "41 adet `XMEAS`; 22 surekli + 19 analizor", "41 adet online measurement; analizor sample-and-hold metadata'si var"],
                ["MV", "12 adet `XMV`; ilk 11 valve 0-100, 12 agitator", "12 adet bounded action/MV, 0-100%"],
                ["IDV", "20 klasik IDV", "28 IDV; ilk 20 ortak, 21-28 genisletme"],
                ["Integrator", "High-level wrapper 1 s explicit Euler uygular", "Varsayilan fixed-step RK4 (`fixed_step=0.0005 h`); Euler ve SciPy RK23/RK45 secilebilir"],
                ["Kinetik", "Arrhenius ifadeleri ve kismi basinc kuvvetleri TEFUNC icinde", "Ayni TEP ailesinin native C kinetigi; ek monitor/disturbance ciktilari var"],
                ["VLE / termodinamik", "Antoine buhar basinci, entalpi ve yogunluk yardimci subroutine'leri", "Native C cekirdekte legacy termodinamik; schema dis yuzeyi adlandirir"],
                ["RL / Gymnasium", "Dogal Gym API yok; custom controller/detector var", "`GymTEPEnv`, Box action/observation, terminated/truncated ayrimi"],
                ["LLM / MCP", "MCP arayuzu yok", "`tep_studio.agent.mcp_server` ile MCP tool server"],
            ],
        )
    )
    lines.append("")
    lines.append("### Numerik ve Fiziksel Model Notlari")
    lines.append("")
    lines.append(
        markdown_table(
            ["Konu", "Gozlem"],
            [
                [
                    "Fortran wrapper integrasyonu",
                    "`TEPSimulator.dt = 1/3600 h`; her adimda kontrol/MV guncellemesi, `TEFUNC` turevi ve explicit Euler `YY <- YY + YP*dt` uygulanir.",
                ],
                [
                    "Fortran backend cagrisi",
                    "f2py sarmalayici `TEINIT` ile baslatir, `FortranTEProcess.evaluate()` icinde `teprob.tefunc(nn, time, yy)` cagrisi yapar.",
                ],
                [
                    "Modern integrator",
                    "`TennesseeEastmanProcess` varsayilan olarak fixed-step RK4 kullanir (`fixed_step=0.0005 h`); `Euler` ayni fixed-step dongusunde, `RK23/RK45` ise SciPy `solve_ivp` ile calisir.",
                ],
                [
                    "Kinetik",
                    "Klasik TEFUNC ailesinde Arrhenius ifadeleri reaktor sicakligina ve kismi basinclara baglidir; ana hizlar A/C/D/E bilesen kismi basinclariyla carpilir ve G/H/F uretim-tuketim terimlerine dagitilir.",
                ],
                [
                    "VLE ve termodinamik",
                    "A-C non-condensable kabul edilir; D-H icin Antoine `ln(P)=A+B/(T+C)`, sivi yogunluk polinomu, sivi/gaz entalpi polinomlari ve buharlasma isi katsayilari kullanilir.",
                ],
                [
                    "Ayrim/stripper",
                    "Stripper basitlestirmesi ampirik ayrim faktorleriyle calisir; bu faktorler buhar/sivi oranina ve sicaklik bagimli `tmpfac` terimine gore guncellenir.",
                ],
                [
                    "Modern genisletmeler",
                    "`modern_tep` 28 IDV, 32 ek olcum, 21 disturbance monitor, 62 process monitor ve 96 concentration monitor metadata'si tasir; klasik `fortran_tep` dis arayuzu 20 IDV/41 XMEAS ile sinirlidir.",
                ],
            ],
        )
    )
    lines.append("")
    lines.append("### XMEAS Eslesmesi")
    lines.append("")
    lines.append(
        markdown_table(
            ["i", "Fortran", "Birim", "Modern schema", "Modern timing"],
            [
                [
                    row["index"],
                    row["fortran_name"],
                    row["fortran_unit"],
                    row["modern_name"],
                    row["modern_update_mode"],
                ]
                for row in mappings["measurements"]
            ],
        )
    )
    lines.append("")
    lines.append("### XMV Eslesmesi")
    lines.append("")
    lines.append(
        markdown_table(
            ["i", "Fortran", "Modern schema", "Baslangic"],
            [
                [row["index"], row["fortran_name"], row["modern_name"], row["initial_mode1"]]
                for row in mappings["manipulated_variables"]
            ],
        )
    )
    lines.append("")
    lines.append("### State Eslesmesi")
    lines.append("")
    lines.append("Tum 50 satir `state_mapping.csv` dosyasina da yazildi.")
    lines.append("")
    lines.append(
        markdown_table(
            ["i", "Fortran", "Modern state", "Birim", "Baslangic"],
            [
                [row["index"], row["fortran_symbol"], row["modern_name"], row["unit"], row["initial_mode1"]]
                for row in mappings["states"]
            ],
        )
    )
    lines.append("")
    lines.append("## Dinamik Dogrulama")
    lines.append("")
    lines.append(
        f"Ayarlar: horizon={args.horizon} h, record_dt={args.record_dt} h, "
        f"modern solver={args.modern_solver}, modern fixed_step={args.modern_fixed_step} h, "
        f"modern control interval={args.modern_control_interval} h, seed={args.seed}."
    )
    lines.append("")
    lines.append("### Kosu Ozeti")
    lines.append("")
    lines.append(
        markdown_table(
            ["Senaryo", "Implementasyon", "Durum", "Final h", "Shutdown h", "Wall s", "ms/proc-h", "Samples", "Hata"],
            [
                [
                    row["scenario"],
                    row["implementation"],
                    row["status"],
                    row["final_time_h"],
                    row["shutdown_time_h"],
                    row["wall_time_s"],
                    row["ms_per_process_hour"],
                    row["samples"],
                    row["error"],
                ]
                for row in summary
            ],
        )
    )
    lines.append("")
    lines.append("### Backend Hiz Karsilastirmasi")
    lines.append("")
    speed_rows = []
    implementations = sorted({run.implementation for run in runs})
    for implementation in implementations:
        impl_runs = [run for run in runs if run.implementation == implementation and run.error is None]
        if not impl_runs:
            continue
        total_wall = sum(run.wall_time_s for run in impl_runs)
        total_proc_h = sum(run.final_time_h for run in impl_runs)
        avg_ms = None if total_proc_h <= 0 else 1000.0 * total_wall / total_proc_h
        speed_rows.append(
            [
                implementation,
                len(impl_runs),
                f"{total_wall:.6g}",
                f"{total_proc_h:.6g}",
                "" if avg_ms is None else f"{avg_ms:.6g}",
                ", ".join(sorted({"shutdown" if run.shutdown else "completed" for run in impl_runs})),
            ]
        )
    lines.append(
        markdown_table(
            ["Implementasyon", "Kosular", "Toplam wall s", "Toplam proses h", "Agirlikli ms/proc-h", "Durumlar"],
            speed_rows,
        )
    )
    lines.append("")
    lines.append(
        "Not: `installed_site_packages_tep_fortran` smoke testi calisti, ancak workspace checkout'i degil. "
        "Bu nedenle senaryo runtime tablolarinda ana referans olarak workspace `fortran_tep:python` kullanildi."
    )
    lines.append("")
    lines.append("### Hata Metrikleri")
    lines.append("")
    if metric_rows:
        lines.append(
            markdown_table(
                [
                    "Senaryo",
                    "XMEAS",
                    "Olcum",
                    "N",
                    "Until h",
                    "MaxAbs",
                    "MeanAbs",
                    "RMSE",
                    "Rel RMSE %",
                ],
                [
                    [
                        row["scenario"],
                        row["measurement_index"],
                        row["measurement"],
                        row["n_aligned_samples"],
                        row["aligned_until_h"],
                        row["max_abs_error"],
                        row["mean_abs_error"],
                        row["rmse"],
                        row["relative_rmse_percent"],
                    ]
                    for row in metric_rows
                ],
            )
        )
    else:
        lines.append("Hata metrigi uretilemedi; en az bir implementasyon kosusu basarisiz veya veri yetersiz.")
    lines.append("")
    lines.append("Not: Kullanici istegindeki `MAE` ifadesi burada `MaxAbs` (maksimum mutlak sapma) ve ek olarak `MeanAbs` ile ayrildi; `Rel RMSE %`, RMSE'nin referans ortalama mutlak degerine oranidir.")
    lines.append("")
    lines.append("## Kullanilabilirlik, RL ve Agent Katmani")
    lines.append("")
    lines.append(
        markdown_table(
            ["Baslik", "Degerlendirme"],
            [
                [
                    "Gymnasium",
                    "`modern_tep` kodu `GymTEPEnv` saglar: observation space 41 online XMEAS, direct-MV action space 12 boyutlu `Box(0,100)`, opsiyonel setpoint action modu ve `terminated`/`truncated` ayrimi. Bu ortamda `gymnasium` kurulu degil; proses cekirdegi yine calisti.",
                ],
                [
                    "fortran_tep RL",
                    "Dogal Gymnasium adapter yok. `TEPSimulator` ve controller API ile ozel ortam yazilabilir, ama termination, action/observation schema ve dataset kaydi modern paket kadar hazir degil.",
                ],
                [
                    "MCP / LLM",
                    "`modern_tep` icinde `tep_studio.agent.mcp_server` ve `TepToolset` katmani var; `mcp` ekstra bagimlilikla `tep-mcp` stdio server olarak calisir. `fortran_tep` icinde benzer MCP/tool server arayuzu bulunmadi.",
                ],
                [
                    "PPO",
                    "Egitim ortami icin `modern_tep` daha uygun: native CFFI hizli, Gym API dogrudan, episode bitisi acik ve schema/action metadata hazir. `fortran_tep` daha cok referans/dogrulama ve klasik 20-IDV veri uretimi icin konumlanmali.",
                ],
                [
                    "Mamba+PPO",
                    "Sequence model egitimi icin modern schema, monitor katmanlari ve hiz avantaj saglar. Ancak politika egitiminde closed-loop/Ricker stabil senaryolari ve termination cezasi dikkatle tasarlanmali; acik cevrim dogal shutdown bir model hatasi degil, ortam dinaminin parcasi.",
                ],
            ],
        )
    )
    lines.append("")
    lines.append("### Sapma Grafikleri")
    lines.append("")
    if plot_paths:
        for path in plot_paths:
            rel = Path(path).relative_to(out_dir)
            lines.append(f"![{rel.stem}]({rel.as_posix()})")
            lines.append("")
    else:
        lines.append("Grafik uretilemedi.")
        lines.append("")
    lines.append("## Cikti Dosyalari")
    lines.append("")
    lines.append("- `state_mapping.csv`, `measurement_mapping.csv`, `mv_mapping.csv`, `disturbance_mapping.csv`")
    lines.append("- `run_summary.csv`, `metrics.csv`, `runs.json`")
    lines.append("- `trajectory_*.png`, `deviation_*.png`")
    report_path = out_dir / "TEP_COMPARISON_REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare fortran_tep and modern_tep TEP implementations.")
    parser.add_argument("--horizon", type=float, default=48.0, help="Scenario horizon in process hours.")
    parser.add_argument("--record-dt", type=float, default=0.05, help="Output sampling period in hours; 0.05 h = 3 min.")
    parser.add_argument("--seed", type=int, default=4651207995, help="Random seed passed to both simulators.")
    parser.add_argument("--fortran-backend", choices=("auto", "python", "fortran"), default="auto")
    parser.add_argument("--modern-solver", default="RK4", help="Modern solver method: RK4, Euler, RK23, RK45, ...")
    parser.add_argument("--modern-fixed-step", type=float, default=0.0005, help="Modern fixed solver substep in hours.")
    parser.add_argument("--modern-control-interval", type=float, default=0.01, help="Modern advance/controller interval in hours.")
    parser.add_argument("--disturbance-id", type=int, default=1, help="IDV index for disturbance scenarios.")
    parser.add_argument("--open-disturbance-time", type=float, default=0.5, help="Open-loop disturbance onset in hours.")
    parser.add_argument("--closed-disturbance-time", type=float, default=8.0, help="Closed-loop disturbance onset in hours.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--skip-closed-loop", action="store_true", help="Run only open-loop base and disturbance scenarios.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    removed_import_hooks = clean_import_environment()
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    health = collect_environment_health(removed_import_hooks)

    schema, schema_note = load_schema_static()
    try:
        tep_module, _, _, constants_module = load_fortran_api()
        fortran_backend = args.fortran_backend
        if fortran_backend == "auto":
            fortran_backend = tep_module.get_default_backend()
        mappings = static_mappings(schema, constants_module)
    except Exception as exc:
        print(f"Could not load fortran_tep metadata: {exc}", file=sys.stderr)
        return 2

    write_csv(out_dir / "state_mapping.csv", mappings["states"], ["index", "fortran_symbol", "modern_name", "unit", "description", "initial_mode1"])
    write_csv(out_dir / "measurement_mapping.csv", mappings["measurements"], ["index", "fortran_symbol", "fortran_name", "fortran_unit", "modern_name", "modern_unit", "modern_update_mode", "modern_sample_period_h"])
    write_csv(out_dir / "mv_mapping.csv", mappings["manipulated_variables"], ["index", "fortran_symbol", "fortran_name", "modern_name", "modern_unit", "lower", "upper", "initial_mode1"])
    write_csv(out_dir / "disturbance_mapping.csv", mappings["disturbances"], ["index", "fortran_symbol", "fortran_name", "modern_name", "modern_description", "modern_model"])

    runs: list[RunData] = []
    scenarios = [
        ("open_loop_base", False, None, None),
        ("open_loop_idv", False, args.disturbance_id, args.open_disturbance_time),
    ]
    if not args.skip_closed_loop:
        scenarios.extend(
            [
                ("closed_loop_base", True, None, None),
                ("closed_loop_idv", True, args.disturbance_id, args.closed_disturbance_time),
            ]
        )

    for scenario, closed_loop, disturbance_id, disturbance_time in scenarios:
        print(f"Running {scenario}: fortran_tep ({fortran_backend})")
        runs.append(
            run_fortran_simulator(
                scenario=scenario,
                backend=fortran_backend,
                closed_loop=closed_loop,
                horizon_h=args.horizon,
                record_dt_h=args.record_dt,
                seed=args.seed,
                disturbance_id=disturbance_id,
                disturbance_time_h=disturbance_time,
            )
        )
        print(f"Running {scenario}: modern_tep ({args.modern_solver})")
        if closed_loop:
            runs.append(
                run_modern_closed_loop(
                    scenario=scenario,
                    horizon_h=args.horizon,
                    record_dt_h=args.record_dt,
                    control_interval_h=args.modern_control_interval,
                    seed=args.seed,
                    solver_method=args.modern_solver,
                    fixed_step_h=args.modern_fixed_step,
                    disturbance_id=disturbance_id,
                    disturbance_time_h=disturbance_time,
                )
            )
        else:
            runs.append(
                run_modern_open_loop(
                    scenario=scenario,
                    horizon_h=args.horizon,
                    record_dt_h=args.record_dt,
                    control_interval_h=args.modern_control_interval,
                    seed=args.seed,
                    solver_method=args.modern_solver,
                    fixed_step_h=args.modern_fixed_step,
                    disturbance_id=disturbance_id,
                    disturbance_time_h=disturbance_time,
                )
            )

    summary_rows = run_summary_rows(runs)
    write_csv(out_dir / "run_summary.csv", summary_rows, ["scenario", "implementation", "status", "final_time_h", "shutdown_time_h", "wall_time_s", "ms_per_process_hour", "samples", "error"])
    write_json(
        out_dir / "runs.json",
        [
            {
                "scenario": run.scenario,
                "implementation": run.implementation,
                "status": "error" if run.error else ("shutdown" if run.shutdown else "completed"),
                "final_time_h": run.final_time_h,
                "shutdown_time_h": run.shutdown_time_h,
                "wall_time_s": run.wall_time_s,
                "ms_per_process_hour": run.ms_per_process_hour,
                "samples": int(run.time_h.size),
                "error": run.error,
            }
            for run in runs
        ],
    )

    metric_rows: list[dict[str, Any]] = []
    plot_paths: list[str] = []
    by_key = {(run.scenario, run.implementation.split(":")[0]): run for run in runs}
    for scenario, *_ in scenarios:
        f_run = by_key.get((scenario, "fortran_tep"))
        m_run = by_key.get((scenario, "modern_tep"))
        if f_run is None or m_run is None:
            continue
        metric_rows.extend(compare_pair(f_run, m_run, args.record_dt))
        plot_paths.extend(plot_pair(f_run, m_run, out_dir, args.record_dt))

    write_csv(
        out_dir / "metrics.csv",
        metric_rows,
        [
            "scenario",
            "measurement_index",
            "measurement",
            "n_aligned_samples",
            "aligned_until_h",
            "max_abs_error",
            "mean_abs_error",
            "rmse",
            "relative_rmse_percent",
        ],
    )
    report_path = write_report(
        out_dir=out_dir,
        health=health,
        schema_note=schema_note,
        mappings=mappings,
        runs=runs,
        metric_rows=metric_rows,
        plot_paths=plot_paths,
        args=args,
    )
    print(f"Wrote {report_path}")
    has_errors = any(run.error for run in runs)
    return 1 if has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
