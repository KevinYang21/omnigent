Output recordings/5606/after-cold-resume-restored.mp4
Set Shell "bash"
Set FontSize 13
Set Width 1280
Set Height 720
Set TypingSpeed 15ms
Set Padding 10
Set WaitTimeout 90s

Type "# FIXED build: cold resume of a LARGE claude-native conversation"
Enter
Type "# 1) the prior conversation exists on the server (600 items):"
Enter
Type "/tmp/res5606/steps/step1_history_ok.sh"
Enter
Wait+Screen /has_more: True/
Sleep 1.5s

Type "# 2) the deployed failure is STILL live: limit=1000 pages 500:"
Enter
Type "/tmp/res5606/steps/step2_limit1000.sh"
Enter
Wait+Screen /internal_error/
Sleep 1.5s

Type "# 3) resume the session (relaunch; runner auto-creates the Claude terminal):"
Enter
Type "/tmp/res5606/steps/step3_resume.sh"
Enter
Wait+Screen /claude terminal launched/
Type "sleep 12; clear"
Enter
Sleep 14s

Type "# 4) the fix in action: the history fetch fell back to smaller pages -> --resume:"
Enter
Type "/tmp/res5606/steps/step3b_decision.sh"
Enter
Wait+Screen /resume_enabled=True/
Sleep 2s

Type "# 5) the terminal the user lands in after the resume (prior history restored):"
Enter
Type "/tmp/res5606/steps/step4_attach.sh"
Enter
Sleep 10s
