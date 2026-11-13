"""REDESIGN §8: what can be checked of the installer scripts without Windows.

- Every PowerShell script parses and carries a UTF-8 BOM (Windows PowerShell
  5.1 reads a file without one as ANSI).
- The elevated part gets exactly the arguments setup/uninstall meant, however
  odd the paths (spaces, quotes, Japanese): the -EncodedCommand round trip.
- engine.json says what the broker needs, and its "-c" bootstrap really runs a
  module from the working directory when Python is isolated (the bundled
  runtime's ._pth leaves the working directory off sys.path).

pwsh runs these (GitHub's Ubuntu runners have it; OYAYUBI_PWSH names another).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted(list((ROOT / "packaging").glob("*.ps1")) + list((ROOT / "src").rglob("*.ps1")))
PWSH = os.environ.get("OYAYUBI_PWSH") or shutil.which("pwsh")


def boot_string() -> str:
    """$Boot from setup.ps1 (the one engine.json and the setup steps use)."""
    text = (ROOT / "packaging" / "setup.ps1").read_text(encoding="utf-8-sig")
    return re.search(r'^\$Boot = "(.*)"$', text, re.M).group(1)


def pwsh(script: str) -> str:
    result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return result.stdout


def function_text(path: Path, name: str) -> str:
    """One function's definition, taken from the script by PowerShell's own parser."""
    out = pwsh(
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$null);"
        f"$ast.FindAll({{ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]"
        f" -and $n.Name -eq '{name}' }}, $true)[0].Extent.Text")
    if not out.strip():
        raise AssertionError(f"{name} not found in {path}")
    return out


@unittest.skipIf(PWSH is None, "pwsh is not installed (set OYAYUBI_PWSH)")
class PowerShellTest(unittest.TestCase):
    def test_every_script_parses_and_has_a_bom(self):
        self.assertTrue(SCRIPTS)
        for path in SCRIPTS:
            with self.subTest(script=path.relative_to(ROOT)):
                self.assertEqual(path.read_bytes()[:3], b"\xef\xbb\xbf", "UTF-8 BOM")
                out = pwsh(
                    f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                    f"'{path}', [ref]$null, [ref]$e); $e | ForEach-Object {{ $_.Message }}")
                self.assertEqual(out.strip(), "")

    def test_workflow_powershell_steps_parse(self):
        import yaml
        for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
            for job, spec in jobs.items():
                for step in spec.get("steps", []):
                    if step.get("shell") not in ("powershell", "pwsh") or "run" not in step:
                        continue
                    with self.subTest(workflow=workflow.name, job=job, step=step.get("name")):
                        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False,
                                                         encoding="utf-8") as f:
                            # GitHub replaces ${{ ... }} before the shell sees the script.
                            f.write(re.sub(r"\$\{\{.*?\}\}", "X", step["run"]))
                        try:
                            out = pwsh(
                                "$e = $null; [void][System.Management.Automation.Language.Parser]::"
                                f"ParseFile('{f.name}', [ref]$null, [ref]$e); $e | ForEach-Object {{ $_.Message }}")
                        finally:
                            os.unlink(f.name)
                        self.assertEqual(out.strip(), "")

    def test_elevated_arguments_survive_encoding(self):
        odd = {
            "Tag": "v0.2.0",
            "DllSource": r"C:\Users\テスト user\AppData\Local\oyayubi-ime\versions\v0.2.0\download\NicolaIME3.dll",
            "Leftover": r"C:\Users\O'Brien\AppData\Local\oyayubi-ime\app",
            "ResultFile": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result dir" / "r.json"
            result.parent.mkdir()
            odd["ResultFile"] = str(result)
            fake = ("param([string]$Action, [string]$ResultFile, [string]$Tag, [string]$DllSource, "
                    "[string]$Leftover)\n"
                    "[IO.File]::WriteAllText($ResultFile, ($PSBoundParameters | ConvertTo-Json))")
            for script in ("setup.ps1", "uninstall.ps1"):
                with self.subTest(script=script):
                    func = function_text(ROOT / "packaging" / script, "New-ElevatedCommand")
                    args = ["-Action", "install"]
                    for key, value in odd.items():
                        args += [f"-{key}", value]
                    listed = ", ".join("'" + a.replace("'", "''") + "'" for a in args)
                    fake_quoted = fake.replace("'", "''")
                    encoded = pwsh(f"{func}\nNew-ElevatedCommand '{fake_quoted}' @({listed})").strip()
                    command = base64.b64decode(encoded).decode("utf-16-le")
                    self.assertTrue(command.startswith("& {"))
                    run = subprocess.run([PWSH, "-NoProfile", "-EncodedCommand", encoded],
                                         capture_output=True, text=True, timeout=120)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    got = json.loads(result.read_text(encoding="utf-8-sig"))
                    self.assertEqual(got, {"Action": "install", **odd})
                    result.unlink()

    def test_engine_json(self):
        func = function_text(ROOT / "packaging" / "setup.ps1", "Write-EngineJson")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "engine.json"
            boot = boot_string().replace("'", "''")
            # Paths in this OS's form (Split-Path follows the running OS).
            sep = "\\" if os.name == "nt" else "/"
            rt = sep.join(["", "rt", "python.exe"]) if os.name != "nt" else r"C:\rt\python.exe"
            pwsh(f"$Boot = '{boot}'; $Utf8NoBom = New-Object Text.UTF8Encoding($false);"
                 f"$script:Python = '{rt}'; $script:Dict = 'D'; $script:Cache = 'C';"
                 f"$script:Model = 'M';\n{func}\n"
                 f"Write-EngineJson '{path}' 'APP' @('--data-dir', 'X')")
            engine = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(engine["python"], rt)
        self.assertEqual(engine["pythonw"], rt.replace("python.exe", "pythonw.exe"))
        self.assertEqual(engine["app"], "APP")
        self.assertEqual(engine["args"][:3], ["-c", boot_string(), "oyayubi.engine"])
        self.assertEqual(engine["args"][3:], ["--dict-dir", "D", "--cache-dir", "C",
                                              "--lm-model", "M", "--data-dir", "X"])
        self.assertEqual(engine["admin_args"], ["-c", boot_string(), "oyayubi.ime.admin_ui"])


class BootstrapTest(unittest.TestCase):
    def test_boot_runs_a_module_from_the_working_directory_under_isolation(self):
        # -I: no working directory on sys.path, as with the runtime's ._pth.
        plain = subprocess.run([sys.executable, "-I", "-m", "oyayubi.lattice.dict_manifest", "verify"],
                               cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(plain.returncode, 0)  # the module is not found without the boot
        booted = subprocess.run([sys.executable, "-I", "-c", boot_string(),
                                 "oyayubi.lattice.dict_manifest", "verify"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(booted.returncode, 0, booted.stdout + booted.stderr)
        self.assertIn(": OK", booted.stdout)


if __name__ == "__main__":
    unittest.main()
