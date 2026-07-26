# Regenerate the README recording:
#
#     brew install vhs
#     vhs demo/injection/demo.tape
#
# Writes demo/injection/demo.gif. Nothing is faked: vhs drives a real terminal and
# the command below is the same one the README tells you to run.

Output demo/injection/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 1180
Set Height 920
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.0

Hide
Type "clear" Enter
Show

Sleep 800ms
Type "uv run python demo/injection/run.py"
Sleep 600ms
Enter

# Indexing two corpora takes a moment; the output then arrives in one block.
Sleep 9s
Screenshot demo/injection/demo.png
Sleep 4s
