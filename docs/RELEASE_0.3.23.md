# HoH 0.3.23 — the setup command survives a Markdown client

## What broke

A Planner setup for a new project stopped before it began. The generated prompt
carried the setup command as ordinary text inside a numbered list, so a client
rendering the prompt as Markdown read `hoh_control_plane` as two italic markers
and escaped them. PowerShell received `hoh\_control\_plane` and answered:

```
ModuleNotFoundError: No module named 'hoh\_control\_plane'
```

The prompt's own rule, "do not repeat setup", then held the chat: it had run the
one command it was allowed to run, that command had failed for a reason it could
not attribute, and it stopped to ask for permission to retype it. Nothing was
installed, nothing was corrupted, and an iteration was lost to a text-transport
defect.

## What changed

- The command now sits in a fenced `powershell` block. A fenced block is copied
  verbatim; paragraph text is not.
- The prompt names this specific failure and authorises the one action that
  clears it: if the command fails with a `ModuleNotFoundError` whose module name
  contains backslashes, delete them and run it once more. Landing the same
  command intact is not a repeat of setup, so the chat does not stop to ask.

`python -I -m` is kept. Isolation is load-bearing here: `-I` ignores `PYTHONPATH`,
and a shell whose `PYTHONPATH` points at an older HoH checkout would otherwise
install a profile from that checkout.

Prompts are 331–346 words per role, up from 274–289. The increase is one fenced
block and one recovery sentence, against the cost of a blocked setup.

## Test

`test_gui_wait_profiles_have_long_timeout_and_unique_instances` was written to
guard exactly these shell-transport properties, including `\.venv` never
appearing in a prompt. It had asserted 0.3.18 wording that the 0.3.19 card
rewrite removed, so it had been failing rather than guarding since then. It is
rewritten around the properties that still hold.

New `test_the_setup_command_survives_a_markdown_client` asserts, for every role,
that the command lies between the two fences, that the module name is intact,
that no backslash appears anywhere in the prompt, and that the recovery
instruction is present.

## Compatibility

No schema, tool or runtime behaviour change. Only the generated setup prompt and
its tests. An existing installed profile is unaffected; this changes what a new
role chat is told to run.
