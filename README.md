# AI Schematic Generator for KiCad / SunvEDA

This repository contains a KiCad PCM Python plugin. Install the release ZIP in
Plugin and Content Manager, open a schematic editor, then run **AI schematic
design assistant**. The archive root contains `metadata.json`, `plugins/`, and
`resources/`.

## Design workflow

1. Enter the FastAPI service root, such as `http://127.0.0.1:8000`. Use
   **检测连接** to read `/api/v1/capabilities`. An optional Bearer token can be
   entered in the window or supplied as `AI_EDA_API_KEY`.
2. Choose a `.docx` file, enter a pure text requirement, or import local
   Graph IR v2 JSON. Word goes to `POST /api/v1/jobs`, text to
   `POST /api/v1/jobs/text`. JSON import does not contact the server.
3. The plugin polls the accepted job and downloads Graph IR only after
   `succeeded`. It resolves local library symbols and draws the complete graph
   in one KiCad Undo transaction. Missing symbols, invalid edges, and failed
   wires stop publication. `needs_attention` and `failed` keep the current
   successful graph in place.
4. Enter feedback or a question to the current successful design. The plugin
   checks the server's revision head and hash, verifies the schematic snapshot,
   maps the current KiCad selection to Graph IR IDs, and posts a revision.
   A successful revision replaces only the objects previously created by the
   plugin. A question or request for clarification preserves the current graph
   and can be continued in the next round.

**新建设计** starts an independent first-round job. **版本记录** queries a locally
saved job without changing the current schematic. **继续上次任务** resumes a job
without reuploading its Word file. If an upload response is lost, the plugin
keeps the request ID and asks you to locate the server job before resubmitting.

The **本轮 HTML 报告** and **当前图报告** buttons download the respective job's
`report_url` using Bearer authentication, save the HTML file, and open it with
the Windows default app. **当前需求** reads the successful job's requirements
JSON. Report HTML does not include the linked archived JSON or log files.

## Host and runtime requirements

The Python plugin requires Python 3.10+, `protobuf`, `pynng`, and
`PySide6-Essentials` from `plugins/requirements.txt`. The editor supplies
`KICAD_API_SOCKET` and `KICAD_API_TOKEN`. `KICAD_AI_SERVICE_URL` sets the
service root; `KICAD_AI_GENERATE_URL` remains a compatibility alias.

**Revision replacement requires the matching KiCad / SunvEDA host changes** in
`C:\Users\Inori\source\repos\kicad`: schematic `GetItems`, `GetItemsById`,
`GetSelection`, and `DeleteItems`, plus per-item deletion results. The Python
protobuf schema is unchanged. A stock or older host without these handlers
cannot verify a schematic snapshot and will refuse feedback or replacement.
Rebuild and run the changed host before end-to-end testing.

The plugin stores job IDs, URLs, revision metadata, and object fingerprints in
`%LOCALAPPDATA%\SunvEDA\AI Schematic Generator\session.json`. Access tokens
are not stored there. Feedback is blocked if the active sheet or any item has
changed since the last plugin import; make a working copy and resolve local
changes before continuing. The current implementation rebuilds the full
plugin-managed graph on each successful revision, so object UUIDs, manually
adjusted positions, and reference numbering may change.

The service administration and symbol knowledge-base controls described in
`二期插件修改方案.md` are separate work and are not included in this plugin release.
