from __future__ import annotations

import json

from deskmate_agent.agent_events import SessionActivityUpdated
from deskmate_agent.claude_transcripts import ClaudeTranscriptWatcher


async def test_claude_transcript_watcher_reads_only_appended_rows(tmp_path) -> None:
    root = tmp_path / "projects"
    project = root / "-tmp-work"
    project.mkdir(parents=True)
    transcript = project / "abc.jsonl"
    transcript.write_text(
        json.dumps({"type": "summary", "summary": "old"}) + "\n",
        encoding="utf-8",
    )
    seen = []

    async def handle(event):
        seen.append(event)

    watcher = ClaudeTranscriptWatcher(handle, roots=(root,), poll_interval_s=0.01, clock=lambda: 1_000)

    assert await watcher.scan_once() == 0
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "new thought"}],
                    },
                }
            )
            + "\n"
        )

    assert await watcher.scan_once() == 1
    assert isinstance(seen[0], SessionActivityUpdated)
    assert seen[0].session_id == "claude-jsonl-abc"
    assert seen[0].cwd == "/tmp/work"
