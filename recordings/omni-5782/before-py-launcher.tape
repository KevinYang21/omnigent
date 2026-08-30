Output recordings/omni-5782/before-py-launcher.mp4
Set FontSize 15
Set Width 1400
Set Height 800
Set Theme "Catppuccin Mocha"
Set Shell bash
Set TypingSpeed 30ms

Type "# OMNI-5782: claude-sdk receives a .py executable on native Windows"
Enter
Sleep 1s
Type "# Journey: Windows user starts a claude-sdk session with the default windows_jobobject sandbox"
Enter
Type "# -> prepare_claude_cli_path() hands the Claude Agent SDK an omnigent-sandbox-*.py launcher"
Enter
Type "# -> Windows CreateProcess cannot exec a .py: OSError [WinError 193] before connect"
Enter
Sleep 2s
Type "PYTHONPATH=$PWD python3 probe_5782.py"
Enter
Sleep 12s
Type "# ^ facet 1: the SDK gets a .py path (not Windows-executable); facet 2: no post_spawn containment"
Enter
Sleep 2s
Type "PYTHONPATH=$PWD python3 -m pytest tests/e2e/test_claude_sdk_windows_cli_path_omni5782.py -v -p no:cacheprovider 2>&1 | tail -14"
Enter
Sleep 18s
Type "# FAILED: the regression test catches the .py CLI path handed to the Claude Agent SDK"
Enter
Sleep 4s
