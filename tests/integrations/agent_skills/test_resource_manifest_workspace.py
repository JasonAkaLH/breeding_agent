from __future__ import annotations

import asyncio
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

    def test_runner_preserves_original_tsv_mount_for_csv_family_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = LocalConversationFileStore(root / "conversation_files")
            tsv_content = b"FID\tIID\tRootAngle_deg\n0\tCML103\t46.734638\n"
            stored = store.save_original(conversation_id="conv-tsv", upload_id="upl-tsv", content=tsv_content)
            conversation_dir = store.conversation_dir("conv-tsv")
            (conversation_dir / "index.md").write_text("# Conversation Files Index\n", encoding="utf-8")
            skill = root / "SKILL.md"
            script = root / "scripts" / "read_manifest.py"
            script.parent.mkdir()
            skill.write_text(
                """---
name: manifest-tsv-skill
description: manifest tsv skill
triggers: [manifest-tsv]
scripts:
  - name: run
    path: scripts/read_manifest.py
    runtime: python
    auto_run: true
outputs:
  required: [answer]
---
Manifest TSV skill.
""",
                encoding="utf-8",
            )
            script.write_text(
                """import json, sys
from pathlib import Path
payload = json.load(sys.stdin)
manifest = json.loads(Path(payload['resource_manifest_path']).read_text(encoding='utf-8'))
artifact = payload['uploaded_artifacts'][0]
mounted = Path(artifact['mount_path'])
content = mounted.read_text(encoding='utf-8')
manifest_file = manifest['files'][0]
print(json.dumps({
    'answer': content.splitlines()[0],
    'mounted_name': mounted.name,
    'manifest_filename': manifest_file['filename'],
    'manifest_file_type': manifest_file['file_type'],
}))
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
                                "upload_id": "upl-tsv",
                                "filename": "phenotype.csv",
                                "original_filename": "phenotype.tsv",
                                "content_type": "text/csv",
                                "normalized_content_type": "text/csv",
                                "file_type": "csv",
                                "size_bytes": len(tsv_content),
                                "sha256": stored.sha256,
                                "storage_key": stored.storage_key,
                                "conversation_id": "conv-tsv",
                                "content": "FID,IID,RootAngle_deg\n0,CML103,46.734638\n",
                            }
                        ],
                    },
                )
            )

            self.assertEqual(output["answer"], "FID\tIID\tRootAngle_deg")
            self.assertTrue(output["mounted_name"].endswith("__phenotype.tsv"))
            self.assertEqual(output["manifest_filename"], "phenotype.tsv")
            self.assertEqual(output["manifest_file_type"], "csv")


if __name__ == "__main__":
    unittest.main()
