"""REDESIGN §8: what can be checked of the installer scripts without Windows.

- Every PowerShell script parses and carries a UTF-8 BOM (Windows PowerShell
  5.1 reads a file without one as ANSI).
- The elevated part gets exactly the arguments setup/uninstall meant, however
  odd the paths (spaces, quotes, Japanese): the -EncodedCommand round trip.
- Old user data (.oyayubi-ime) is moved to .yuragi-ime, never deleted.
- The release's uninstall.ps1 (packaging/uninstall-entry.ps1) says so and
  changes nothing when no version is installed.
- engine.json says what the broker needs, and its "-c" bootstrap really runs a
  module from the working directory when Python is isolated (the bundled
  runtime's ._pth leaves the working directory off sys.path).
- The release publishes the tkinter files setup.ps1 adds to the runtime, taken
  from the same Python version, and the runtime's name tells it from v0.1.0's.

pwsh runs these (GitHub's Ubuntu runners have it; YURAGI_PWSH names another).
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
PWSH = os.environ.get("YURAGI_PWSH") or shutil.which("pwsh")


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


@unittest.skipIf(PWSH is None, "pwsh is not installed (set YURAGI_PWSH)")
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
            "DllSource": r"C:\Users\テスト user\AppData\Local\yuragi-ime\versions\v0.2.0\download\yuragi_tip.dll",
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

    def test_uninstall_entry_when_nothing_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "LOCALAPPDATA": tmp, "ProgramFiles": str(Path(tmp) / "pf")}
            run = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File",
                                  str(ROOT / "packaging" / "uninstall-entry.ps1"), "-Yes"],
                                 capture_output=True, text=True, timeout=120, env=env)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("入っていません", run.stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])

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
        self.assertEqual(engine["args"][:3], ["-c", boot_string(), "yuragi.engine"])
        self.assertEqual(engine["args"][3:], ["--dict-dir", "D", "--cache-dir", "C",
                                              "--lm-model", "M", "--data-dir", "X"])
        self.assertEqual(engine["admin_args"], ["-c", boot_string(), "yuragi.ime.admin_ui"])

    def test_old_user_data_is_moved_never_deleted(self):
        """.oyayubi-ime -> .yuragi-ime: what is only in the old folder moves; a name
        both have stays in both; the old folder goes only once it is empty."""
        func = function_text(ROOT / "packaging" / "setup.ps1", "Move-UserData")
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / ".oyayubi-ime", Path(tmp) / ".yuragi-ime"
            (old / "logs").mkdir(parents=True)
            (old / "logs" / "broker.log").write_text("log", encoding="utf-8")
            (old / "learning.jsonl").write_text("old learning", encoding="utf-8")
            (old / "config.json").write_text("old config", encoding="utf-8")
            new.mkdir()
            (new / "config.json").write_text("new config", encoding="utf-8")
            notes = pwsh(f"$ErrorActionPreference = 'Stop'\n{func}\nMove-UserData '{old}' '{new}'")
            self.assertEqual((new / "learning.jsonl").read_text(encoding="utf-8"), "old learning")
            self.assertEqual((new / "logs" / "broker.log").read_text(encoding="utf-8"), "log")
            self.assertEqual((new / "config.json").read_text(encoding="utf-8"), "new config")
            self.assertEqual((old / "config.json").read_text(encoding="utf-8"), "old config")
            self.assertEqual(sorted(p.name for p in old.iterdir()), ["config.json"])
            self.assertIn("config.json", notes)

            (new / "config.json").unlink()
            pwsh(f"$ErrorActionPreference = 'Stop'\n{func}\nMove-UserData '{old}' '{new}'")
            self.assertFalse(old.exists())
            self.assertEqual((new / "config.json").read_text(encoding="utf-8"), "old config")


class BootstrapTest(unittest.TestCase):
    def test_boot_runs_a_module_from_the_working_directory_under_isolation(self):
        # -I: no working directory on sys.path, as with the runtime's ._pth.
        plain = subprocess.run([sys.executable, "-I", "-m", "yuragi.lattice.dict_manifest", "verify"],
                               cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(plain.returncode, 0)  # the module is not found without the boot
        booted = subprocess.run([sys.executable, "-I", "-c", boot_string(),
                                 "yuragi.lattice.dict_manifest", "verify"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(booted.returncode, 0, booted.stdout + booted.stderr)
        self.assertIn(": OK", booted.stdout)


class TkinterAssetTest(unittest.TestCase):
    """The release builds the tkinter asset that setup.ps1 fetches into the runtime."""

    def test_release_and_setup_agree(self):
        import yaml
        setup = (ROOT / "packaging" / "setup.ps1").read_text(encoding="utf-8-sig")
        version = re.search(r'^\$EmbedPythonVersion = "([0-9.]+)"$', setup, re.M).group(1)
        build = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml")
                               .read_text(encoding="utf-8"))["jobs"]["build"]["steps"]
        # The normal Python the tkinter files are taken from is the embedded one's version.
        python = next(s for s in build if s.get("uses", "").startswith("actions/setup-python"))
        self.assertEqual(python["with"]["python-version"], version)
        # What setup.ps1 downloads is published, with its hash.
        self.assertIn('"$baseUrl/python-$EmbedPythonVersion-tkinter-amd64.zip"', setup)
        files = next(s for s in build if s.get("uses", "").startswith("softprops/"))["with"]["files"].split()
        self.assertIn(f"python-{version}-tkinter-amd64.zip", files)
        self.assertIn(f"python-{version}-tkinter-amd64.zip.sha256", files)
        # The asset puts tkinter in Lib\tkinter; the runtime's ._pth has Lib on the path.
        assemble = next(s for s in build if s.get("name") == "アセットの組み立て")["run"]
        self.assertIn('"Lib\\tkinter"', assemble)
        self.assertRegex(setup, r'"\.", "Lib", "Lib\\site-packages", "import site"')
        # A runtime without tkinter (v0.1.0's) is not taken for this one.
        self.assertIn('$runtimeId = "py$EmbedPythonVersion-tk-torch', setup)


if __name__ == "__main__":
    unittest.main()
