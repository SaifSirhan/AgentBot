---
name: qoder_delegate
description: Delegate a complex website, app, or localhost server build to Qoder IDE, then inspect its terminal before claiming success.
---

# Qoder Delegation

If the user asks you to build a complex website, app, or run a localhost
server, use delegate_to_qoder to send the requirements to Qoder IDE.
Do not try to write the code yourself.

## Tool

`delegate_to_qoder(prompt, goal)`

- `prompt` — the complete build requirements to send to Qoder. Include the
  stack, features, and any constraints the user gave.
- `goal` — the success condition to check afterward, for example
  `"Server running or Build successful"`.

The tool performs this exact sequence:

1. Finds and focuses the verified Qoder IDE window. The title and process
   must both contain "Qoder"; a browser tab or document named "Qoder" is
   rejected.
2. Copies `prompt` to the clipboard with pyperclip.
3. Presses **Ctrl+L**, Qoder's documented Windows shortcut for Open/Close
   Chat (verified in Qoder's keyboard-shortcuts documentation).
4. Presses **Ctrl+V** to paste the prompt, then **Enter** to submit.
5. Restores the previous clipboard contents when it can be read, then
   returns exactly: `Prompt sent to Qoder IDE. Waiting for completion.`

## Waiting and verification

The tool returns as soon as the prompt is submitted. It does not wait for the
build. The ReAct loop must inspect the result before saying DONE:

1. Take a screenshot of Qoder IDE and look at the terminal/output panel.
   Prefer `analyze_screenshot("Qoder IDE|Does the terminal say 'Server
   running' or 'Build successful', or does it show an error? Quote the
   relevant lines.")`.
2. If the terminal says **"Server running"** or **"Build successful"**, the
   goal is achieved and you may report success.
3. If it says **"Error"** or shows a failure, read the exact error, correct
   the prompt, and call `delegate_to_qoder` again with the fixed requirements.
4. Do not output DONE until you have inspected the result. Do not claim the
   server is running based only on the "Prompt sent" message.

## Rules

- Only delegate when the user asks for a complex website, app, large script,
  or localhost server. Do not use this for small edits or ordinary questions.
- Do not reproduce the sequence manually with screenshots, clicks, or typing;
  use the tool.
- Do not click Qoder dialogs, buttons, or terminals. The tool only uses the
  chat shortcut and the clipboard.
- The tool verifies the Qoder window is foreground but cannot confirm the chat
  input itself has focus. The screenshot inspection step is what proves the
  prompt landed; if it did not, report that instead of claiming success.
- Do not run delegation in group chats. It is blocked there.
- One delegation runs at a time. If another is active, report that instead
  of retrying.
- If the tool returns `ERROR:` or `ABORTED:`, report that exact result.
