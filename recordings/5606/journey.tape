Output recordings/5606/before-blank-cold-resume.mp4
Set Shell "bash"
Set FontSize 13
Set Width 1280
Set Height 720
Set TypingSpeed 15ms
Set Padding 10
Set WaitTimeout 60s

Type "# OMNI-5606 / GH#5498: cold resume of a LARGE claude-native conversation"
Enter
Type "# 1) the prior conversation exists on the server (600 items):"
Enter
Type "scratch5606/steps/step1_history_ok.sh"
Enter
Wait+Screen /has_more: True/
Sleep 1.5s

Type "# 2) but the page size the resume path hardcodes (limit=1000) 500s:"
Enter
Type "scratch5606/steps/step2_limit1000.sh"
Enter
Wait+Screen /internal_error/
Sleep 1.5s

Type "# 3) resume the session (relaunch; runner auto-creates the Claude terminal):"
Enter
Type "scratch5606/steps/step3_resume.sh"
Enter
Wait+Screen /claude terminal launched/
Type "sleep 12; clear"
Enter
Sleep 14s

Type "# 4) the terminal the user lands in after the resume:"
Enter
Type "scratch5606/steps/step4_attach.sh"
Enter
Wait+Screen /Welcome/
Sleep 8s
