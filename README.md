# AI Schematic Generator for KiCad

This folder is the source of a KiCad PCM Python plugin. Package the **contents**
of this directory as a ZIP with `metadata.json`, `plugins/`, and `resources/`
at the archive root. Install that ZIP with KiCad's Plugin and Content Manager,
then launch **Generate schematic from Word or JSON** from an open schematic editor.

Choose **选择 JSON…** to load a local Graph IR v2 JSON file and generate directly;
this route makes no HTTP request. Choose **选择 Word…** to use the AI service.

The action uploads `.doc` or `.docx` as a FastAPI multipart field named `file`
to `POST http://127.0.0.1:8000/generate`. Change the address in the plugin window
or set `KICAD_AI_GENERATE_URL`. The service must return Graph IR v2 JSON with
`nodes` and `edges`, an envelope containing that object in `graph`, `graph_ir`,
or `result`, or `{"download_url": "/path/to/graph.json"}`. The downloaded file
must be on the same origin as the upload endpoint. For example:

```python
from fastapi import FastAPI, UploadFile, File

app = FastAPI()

@app.post("/generate")
async def generate(file: UploadFile = File(...)):
    contents = await file.read()
    # Send contents to the model and return its Graph IR v2 object.
    return {"graph_schema": "ai-eda-graph-ir/v2.0", "nodes": [], "edges": []}
```

The plugin needs Python 3.10+, `protobuf`, `pynng`, and `PySide6-Essentials`
from `plugins/requirements.txt`. It is launched by KiCad so
`KICAD_API_SOCKET` and `KICAD_API_TOKEN` are provided by the editor. Its
protobuf Python bindings were generated from this checkout's `api/proto`.

`nodes` are symbol instances, `edges` are the only electrical connections,
and `paths` order symbol placement. An edge must be `connected` and map both
ports to pin numbers in the chosen library symbol. Missing symbols and their
incident edges are reported and skipped. Other symbols and edges are created
in one KiCad Undo operation. The generator uses library pin geometry and graph
edges to choose symbol rotations, then routes orthogonal wires around symbol
bodies while penalizing crossings with already routed wires. Graph IR port
`direction` describes signal flow, not a symbol's geometric rotation. Shared
pins and dense or ambiguous connections can still require manual inspection.

For Graph IR v2 binding output, `model_metadata.kicad_symbol_name` identifies
the preferred library entry, while the stem of `kicad_library_path` is only a
library nickname hint. The generator falls back to project-wide symbol search
when the hint differs from the active project's library table. Each edge uses
`source/target_node_id` and `source/target_port_id`; the corresponding node
port's `pin_number` and optional `pin_name` select the physical library pin.
Optional port `unit` and `body_style` select a matching symbol variant.

No KiCad C++ build changes are required. To regenerate the bindings after an
API schema change, run `protoc -I api/proto --python_out=plugins/ai-schematic-generator/plugins/generated_proto`
for the six `.proto` files presently in `plugins/generated_proto`.
