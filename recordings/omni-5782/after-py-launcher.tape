Output recordings/omni-5782/after-py-launcher.mp4
Set FontSize 15
Set Width 1400
Set Height 800
Set Theme "Catppuccin Mocha"
Set Shell bash
Set TypingSpeed 30ms

Type "# Fix: claude-sdk no longer receives a .py executable on native Windows"
Enter
Sleep 1s
Type "# Journey: Windows user starts a claude-sdk session with the default windows_jobobject sandbox"
Enter
Type "# -> prepare_claude_cli_path() now hands the SDK the raw Claude CLI (Windows-executable)"
Enter
Sleep 2s
Type "PYTHONPATH=$PWD python3 probe_windows_cli_path.py"
Enter
Wait+Screen /CreateProcess can execute: True/
Sleep 2s
Type "# ^ the SDK gets the real CLI binary, not an omnigent-sandbox-*.py script"
Enter
Sleep 1s
Type "PYTHONPATH=$PWD python3 -m pytest tests/e2e/test_claude_sdk_windows_cli_path.py tests/inner/test_claude_sdk_executor.py -k 'windows_cli_path or skips_script_launcher_for_windows_jobobject' -v -p no:cacheprovider 2>&1 | tail -12"
Enter
Wait+Screen /passed/
Sleep 2s
Type "# PASSED: the regression tests confirm the WinError-193 pre-connect crash is fixed"
Enter
Sleep 4s
