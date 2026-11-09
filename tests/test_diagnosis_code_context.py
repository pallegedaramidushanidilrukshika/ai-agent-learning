"""The diagnosis must see the handler where the bug is, not only manifests."""
import json
import os
import re
from pathlib import Path

import repository_agent

SOURCE = (Path(__file__).resolve().parents[1] / "bug_diagnosis_agent.py").read_text(encoding="utf-8")
helpers = {"re": re, "json": json, "os": os}
exec(SOURCE[SOURCE.index("def compact_evidence_text"):SOURCE.index("def build_diagnosis_prompt")], helpers)

ROUTES = (
    'import express from "express";\nconst router = express.Router();\n'
    + "// list\nrouter.get(\"/\", async (req, res) => {\n" + "  // read all\n" * 12 + "});\n"
    + "// one\nrouter.get(\"/:id\", async (req, res) => {\n" + "  // read one\n" * 12 + "});\n"
    + "// create\nrouter.post(\"/\", async (req, res) => {\n  const doc = { name: req.body.name };\n"
    + "  await collection.insertOne(doc);\n});\n"
    + "// update\nrouter.patch(\"/:id\", async (req, res) => {\n" + "  // update\n" * 20 + "});\n"
    + "router.delete(\"/:id\", async (req, res) => {\n" + "  // delete\n" * 20 + "});\nexport default router;\n"
)


def mern_repository(root):
    server = root / "mern" / "server"
    (server / "routes").mkdir(parents=True)
    (server / "package.json").write_text(json.dumps({"scripts": {"start": "node --env-file=config.env server"}}), encoding="utf-8")
    (server / "server.js").write_text('import records from "./routes/record.js";\napp.use("/record", records);\n', encoding="utf-8")
    (server / "routes" / "record.js").write_text(ROUTES, encoding="utf-8")
    return root


def test_repository_analysis_follows_the_entry_files_local_imports(tmp_path):
    root = mern_repository(tmp_path)
    files = [os.path.join("mern", "server", name) for name in ("package.json", "server.js", os.path.join("routes", "record.js"))]
    important = repository_agent.find_important_files(files, str(root))
    assert os.path.join("mern", "server", "routes", "record.js") in important


def test_npm_start_is_resolved_to_the_backend_entry_file(tmp_path):
    root = mern_repository(tmp_path)
    context = {
        "repository_path": str(root),
        "execution_strategy": {"start_command": "npm start", "backend_start_command": "npm start", "backend_path": "mern/server"},
    }
    assert "mern/server/server.js" in helpers["application_entry_files"](context)
    context = {"repository_path": str(root), "execution_strategy": {"start_command": "node index.js"}}
    assert helpers["application_entry_files"](context) == {"index.js"}


def test_long_source_is_clipped_around_the_bugs_operation():
    terms = helpers["bug_focus_terms"]("blank employee record can be saved")
    assert "post" in terms and "insert" in terms
    excerpt = helpers["focused_excerpt"](ROUTES, 400, terms)
    assert 'router.post("/"' in excerpt
    assert "insertOne" in excerpt
    assert len(excerpt) <= 460
    # Head-and-tail clipping (the old behaviour) loses the handler.
    assert 'router.post("/"' not in helpers["compact_evidence_text"](ROUTES, 400)


def test_without_an_operation_word_clipping_keeps_head_and_tail():
    assert helpers["focused_excerpt"](ROUTES, 400, ()) == helpers["compact_evidence_text"](ROUTES, 400)
