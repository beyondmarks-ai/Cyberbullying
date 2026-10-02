import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import setup_config


class ConfigTests(unittest.TestCase):
    def values(self):
        return {'IG_APP_ID': 'test-id', 'IG_APP_SECRET': 'secret',
                'AZURE_OPENAI_ENDPOINT': 'https://example.openai.azure.com',
                'AZURE_OPENAI_DEPLOYMENT': 'deployment', 'AZURE_OPENAI_KEY': 'key',
                'GEMINI_API_PROVIDER': 'gemini', 'GEMINI_MODEL': 'model', 'GEMINI_API_KEY': 'key'}

    def test_valid_required_settings_and_optional_storage(self):
        self.assertEqual(setup_config.problems(self.values()), [])
        values = {**self.values(), 'AZURE_STORAGE_ACCOUNT': 'storage'}
        self.assertTrue(setup_config.problems(values))
        values.update(AZURE_STORAGE_KEY='secret', AZURE_STORAGE_CONTAINER='previews')
        self.assertEqual(setup_config.problems(values), [])

    def test_placeholder_and_endpoint_validation_never_echo_secrets(self):
        for endpoint in ('https://YOUR-RESOURCE.openai.azure.com', 'http://example.com', 'https://user:secret@example.com', 'https://example.com?key=secret'):
            issues = setup_config.problems({**self.values(), 'AZURE_OPENAI_ENDPOINT': endpoint})
            self.assertTrue(issues)
            self.assertNotIn('secret', ' '.join(issues))
        self.assertTrue(setup_config.problems({**self.values(), 'IG_APP_SECRET': 'value\nINJECTED=true'}))
        self.assertTrue(setup_config.problems({**self.values(), 'GEMINI_API_PROVIDER': 'wrong'}))

    def test_missing_settings_and_bom_quoted_values(self):
        self.assertTrue(setup_config.problems({}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('# comment\nIG_APP_SECRET="secret=with=equals"\n', encoding='utf-8-sig')
            self.assertEqual(setup_config.load_values(path)['IG_APP_SECRET'], 'secret=with=equals')

    def test_save_preserves_existing_unknown_settings_and_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('IG_REDIRECT_URI=https://existing/callback\nCUSTOM=unchanged\nIG_APP_SECRET=old\n')
            setup_config.update_env(path, self.values())
            result = setup_config.load_values(path)
            self.assertEqual(result['CUSTOM'], 'unchanged')
            self.assertEqual(result['IG_REDIRECT_URI'], 'https://existing/callback')
            self.assertEqual(result['IG_APP_SECRET'], 'secret')


@unittest.skipUnless(os.name == 'nt', 'Windows installer checks')
class WindowsSetupTests(unittest.TestCase):
    def run_script(self, script):
        setup = str(ROOT / 'tools/setup_windows.ps1').replace("'", "''")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                                 f". '{setup}'; $ErrorActionPreference='Stop'; {script}; exit 0"],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_busy_port_never_installs_or_changes_settings(self):
        output = self.run_script("""
function Test-DashboardRunning { return $true }
function Install-WingetTool { throw 'Must not install' }
function Invoke-Checked { throw 'Must not execute' }
Start-Setup
""")
        self.assertIn('No packages or settings were changed', output)

    def test_fresh_install_sequences_tools_environment_form_then_launch(self):
        with tempfile.TemporaryDirectory(prefix='setup test ') as directory:
            folder = directory.replace("'", "''")
            output = self.run_script(f"$projectRoot='{folder}';" + r"""
$script:installedPython = $false
$script:installedMedia = $false
$script:steps = [Collections.Generic.List[string]]::new()
function Test-DashboardRunning { return $false }
function Find-CompatiblePython { if ($script:installedPython) { return 'C:\fake python\python.exe' }; return $null }
function Install-WingetTool([string]$Id, [switch]$PerUser) {
    $script:steps.Add($Id)
    if ($Id -eq 'Python.Python.3.13') { $script:installedPython=$true } else { $script:installedMedia=$true }
}
function Get-Command { param($Name) if ($script:installedMedia) { return @{Source='fake'} }; return $null }
function Invoke-Checked([string]$Program, [string[]]$Arguments) { $script:steps.Add(($Arguments -join '|')) }
function Test-LocalConfiguration { return $false }
Start-Setup
$joined=$script:steps -join "`n"
if ($joined -notmatch 'Python.Python.3.13' -or $joined -notmatch 'Gyan.FFmpeg') { throw 'Tools missing' }
if ($joined -notmatch '\-m\|venv\|' -or $joined -notmatch 'pip\|install') { throw 'Environment missing' }
$formIndex=-1; $launchIndex=-1
for ($i=0; $i -lt $script:steps.Count; $i++) {
    if ($script:steps[$i] -like '*setup_config.py') { $formIndex=$i }
    if ($script:steps[$i] -like '*launch_dashboard.py') { $launchIndex=$i }
}
if ($formIndex -lt 0 -or $launchIndex -le $formIndex) { throw 'Form must precede launch' }
Write-Host 'FRESH_SETUP_SEQUENCE_OK'
""")
            self.assertIn('FRESH_SETUP_SEQUENCE_OK', output)

    def test_check_only_missing_python_does_not_install(self):
        output = self.run_script("""
$CheckOnly=$true
function Find-CompatiblePython { return $null }
function Install-WingetTool { throw 'Attempted install in check-only mode' }
try { Start-Setup; throw 'Should have failed' }
catch { if ($_.Exception.Message -ne 'Python 3.11-3.14 is missing.') { throw }; Write-Host 'READ_ONLY_OK' }
""")
        self.assertIn('READ_ONLY_OK', output)

    def test_failed_command_stops_setup(self):
        python = sys.executable.replace("'", "''")
        self.run_script(f"""
try {{ Invoke-Checked '{python}' @('-c', 'import sys; sys.exit(7)'); throw 'Should fail' }}
catch {{ if ($_.Exception.Message -notlike '*exit 7*') {{ throw }} }}
""")


if __name__ == '__main__':
    unittest.main()
