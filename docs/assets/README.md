# Repository presentation assets

`web-console.jpg` and `web-preview.jpg` show the real local Web console running
against the disposable files in `examples/demo_workspace/`, not personal data.
The demo uses an actual configured model; its wording and timings can vary.
They illustrate the interface, not a performance or security benchmark.

To reproduce after configuring a tool-capable model:

```bash
python examples/web_demo.py
```

Open the printed loopback URL. Ask the agent to read `README.md` and `notes.md`,
then start a new session in preview mode and request a heading change. Expand
the difference in the activity panel before capturing the preview. The temporary
workspace and database are removed when the demo exits normally.

Capture only the page, without browser or desktop chrome. Review model names, paths, replies, and errors
before sharing; never capture a personal workspace or an existing session list.
Keep screenshots unedited and free of keys, real service addresses, and unrelated UI.
