from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from src.integrations.agent_skills.parser import parse_skill_file
from src.integrations.agent_skills.script_runner import SkillScriptRunner
from src.storage.conversation_files import LocalConversationFileStore


class SkillResourceManifestWorkspaceTest(unittest.TestCase):
    def test_runner_mounts_persistent_upload_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = LocalConversationFileStore(root / "conversation_files")
            stored = store.save_original(conversation_id="conv-1", upload_id="upl-1", content=b"abc")
            conversation_dir = store.conversation_dir("conv-1")
            (conversation_dir / "index.md").write_text("# Conversation Files Index\n", encoding="utf-8")
            skill = root / "SKILL.md"
            script = root / "scripts" / "read_manifest.py"
            script.parent.mkdir()
            skill.write_text(
                """---
name: manifest-skill
description: manifest skill
triggers: [manifest]
scripts:
  - name: run
    path: scripts/read_manifest.py
    runtime: python
    auto_run: true
outputs:
  required: [answer]
---
Manifest skill.
""",
                encoding="utf-8",
            )
            script.write_text(
                """import json, os, sys
from pathlib import Path
payload = json.load(sys.stdin)
manifest = json.loads(Path(payload['resource_manifest_path']).read_text(encoding='utf-8'))
artifact = payload['uploaded_artifacts'][0]
content = Path(artifact['mount_path']).read_text(encoding='utf-8')
assert os.environ['MAF_SKILL_RESOURCE_MANIFEST'] == payload['resource_manifest_path']
assert os.environ['MAF_SKILL_INPUT_DIR'] == payload['input_dir']
print(json.dumps({'answer': f"{content}:{len(manifest['files'])}"}))
""",
                encoding="utf-8",
            )
            manifest = parse_skill_file(skill)
            runner = SkillScriptRunner(conversation_file_store=store)

            output = asyncio.run(
                runner.run(
                    manifest,
                    manifest.scripts[0],
                    {
                        "query": "read",
                        "uploaded_artifacts": [
                            {
                                "upload_id": "upl-1",
                                "filename": "materials.txt",
                                "content_type": "text/plain",
                                "file_type": "text",
                                "size_bytes": 3,
                                "sha256": stored.sha256,
                                "storage_key": stored.storage_key,
                                "conversation_id": "conv-1",
                            }
                        ],
                    },
                )
            )

            self.assertEqual(output["answer"], "abc:1")


if __name__ == "__main__":
    unittest.main()
