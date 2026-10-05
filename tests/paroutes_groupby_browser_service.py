"""Owned daemon for the opt-in real PaRoutes input-to-result acceptance.

Only model replies and profile readiness are fixtures. Zenodo stays real.
The Agent, native permission/capture boundary, kernel, Store and HTTP stay real.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from openai4s import webtools
from openai4s.config import Config, LLMConfig, RoadmapFeatureFlags
from openai4s.kernel import Kernel
from openai4s.server import gateway

QUERY = 'doi:"10.5281/zenodo.6275421"'
FILE_URL = "https://zenodo.org/api/records/6275421/files/n1-targets.txt/content"
EXPECTED_SHA256 = "c0d1b48379e1ceb1129fba4bf3773f73f27bdb22bb4d468417e6e404d3210c15"
OBSERVED = {
    "model_fixture_calls": 0,
    "metadata_reads": 0,
    "file_reads": 0,
    "kernel_cell_calls": 0,
}
real_metadata_fetch = webtools.web_fetch
real_dataset_response = webtools._open_http_response
real_guard_url = webtools._guard_url
real_kernel_execute = Kernel.execute


def observed_kernel_execute(self, *args, **kwargs):
    # Observe the real entry point without replacing execution or its result.
    OBSERVED["kernel_cell_calls"] += 1
    return real_kernel_execute(self, *args, **kwargs)


def native_reply(name: str, arguments: dict) -> dict:
    call = {
        "id": f"fixture-{OBSERVED['model_fixture_calls']}-{name}",
        "wire_id": f"fixture-{OBSERVED['model_fixture_calls']}-{name}",
        "name": name,
        "ordinal": 0,
        "raw_arguments": json.dumps(arguments),
        "arguments": arguments,
        "parse_error": None,
        "provider_meta": {"provider": "offline-fixture"},
    }
    content = "Running the selected PaRoutes dataset operation."
    return {
        "content": content,
        "usage": {},
        "tool_calls": [call],
        "assistant_message": {
            "role": "assistant",
            "content": content,
            "tool_calls": [call],
        },
    }


def scripted_chat(messages, _cfg, on_delta=None, **_kwargs):
    OBSERVED["model_fixture_calls"] += 1
    goals = [
        (i, str(m.get("content") or ""))
        for i, m in enumerate(messages)
        if m.get("role") == "user"
        and "PAROUTES_BROWSER_" in str(m.get("content") or "")
    ]
    if not goals:
        return {"content": "PaRoutes dataset acceptance", "usage": {}}
    position, goal = goals[-1]
    tail = messages[position + 1 :]
    names = {m.get("name") for m in tail if m.get("role") == "tool"}
    if "PAROUTES_BROWSER_IMPORT" in goal:
        if "science_search" not in names:
            return native_reply(
                "science_search", {"database": "zenodo", "query": QUERY, "limit": 1}
            )
        if "science_import_dataset" not in names:
            discovered = next(
                m
                for m in reversed(tail)
                if m.get("role") == "tool" and m.get("name") == "science_search"
            )
            record = json.loads(
                str(discovered["content"]).split("results (1):\n", 1)[1]
            )
            assert str(record["id"]) == "6275421"
            selected = next(
                f for f in record["attributes"]["files"] if f["key"] == "n1-targets.txt"
            )
            return native_reply(
                "science_import_dataset",
                {
                    "record_id": record["id"],
                    "file_key": selected["key"],
                    "path": "datasets/n1-targets.txt",
                    "expected_size": selected["declared_size_bytes"],
                    "expected_checksum": selected["declared_checksum"],
                    "max_bytes": 1024 * 1024,
                },
            )
        code = "host.submit_output({'status': 'imported'}, ['Imported the selected PaRoutes file'])"
    else:
        match = re.search(r"PAROUTES_BROWSER_ANALYSE ([A-Za-z0-9_.:-]+)", goal)
        if not match:
            raise RuntimeError("unknown PaRoutes browser request")
        version = match.group(1)
        code = (
            "import pandas as pd\n"
            f"input_path = host.artifact_path({version!r})\n"
            "data = pd.read_csv(input_path, header=None, names=['target'], sep='\\t', dtype=str, keep_default_na=False)\n"
            "data['target_length'] = data['target'].str.len()\n"
            "summary = data.groupby('target_length', as_index=False).agg(targets=('target', 'count'))\n"
            f"summary['input_version'] = {version!r}\n"
            "summary.to_json('summary.json', orient='records')\n"
            f"host.submit_output({{'files': ['summary.json'], 'input_version': {version!r}}}, ['Analysed the explicitly selected frozen input'])\n"
        )
    content = f"```python\n{code}\n```"
    if on_delta:
        on_delta(content)
    return {"content": content, "usage": {}}


def allow_metadata_url(url):
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.netloc == "zenodo.org"
    if parsed.path in {"/api/records", "/api/records/"}:
        query = parse_qs(parsed.query)
        assert set(query) <= {"q", "size", "type", "page"}
        assert query["q"] == [QUERY] and query["size"] == ["1"]
        assert query.get("type", ["dataset"]) == ["dataset"]
        assert query.get("page", ["1"]) == ["1"]
    elif parsed.path.rstrip("/") == "/api/records/6275421":
        assert not parsed.query
    else:
        raise AssertionError("unapproved external metadata request")


def is_selected_file_url(url):
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "zenodo.org"
        and parsed.path
        in {
            "/api/records/6275421/files/n1-targets.txt/content",
            "/records/6275421/files/n1-targets.txt",
        }
        and parse_qs(parsed.query) in ({}, {"download": ["1"]})
    )


def guarded_url(url, *args, **kwargs):
    # The production transport calls this before every redirect-hop request.
    if not is_selected_file_url(url):
        allow_metadata_url(url)
    return real_guard_url(url, *args, **kwargs)


def metadata_fetch(url, **_kwargs):
    allow_metadata_url(url)
    OBSERVED["metadata_reads"] += 1
    return real_metadata_fetch(url, **_kwargs)


@contextlib.contextmanager
def dataset_response(url, **_kwargs):
    if url == FILE_URL:
        OBSERVED["file_reads"] += 1
    else:
        # web_fetch itself uses this shared production transport as well.
        allow_metadata_url(url)
    with real_dataset_response(url, **_kwargs) as response:
        yield response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    if data_dir == (Path.home() / ".openai4s").resolve() or args.port == 8760:
        raise RuntimeError("default user resources are forbidden")
    cfg = Config(
        data_dir=data_dir,
        host="127.0.0.1",
        port=args.port,
        llm=LLMConfig(
            provider="openai_responses",
            model="gpt-4.1",
            api_key="synthetic-fixture-only",
            base_url="http://127.0.0.1:1/v1",
        ),
        max_turns=8,
        roadmap_features=RoadmapFeatureFlags(stage1_trusted_delivery=False),
    )
    gateway.chat = scripted_chat
    Kernel.execute = observed_kernel_execute
    webtools.web_fetch = metadata_fetch
    webtools._open_http_response = dataset_response
    webtools._guard_url = guarded_url
    server = gateway.serve_app(cfg, block=False)
    server.runner.standard_profile_readiness = lambda: {"ready": True}
    for tool, pattern in [
        ("science_search", "zenodo"),
        ("science_import_dataset", "zenodo.org"),
    ]:
        server.runner.store.set_permission_rule(
            scope="global", scope_id="", tool=tool, pattern=pattern, decision="allow"
        )
    print(
        json.dumps(
            {
                "ready": True,
                "port": args.port,
                "expected_input_sha256": EXPECTED_SHA256,
                "live_zenodo": True,
            }
        ),
        flush=True,
    )
    try:
        # EOF is a stop condition, never an idle spin; the Node parent owns stdin.
        for line in sys.stdin:
            command = json.loads(line)
            if command.get("stop") is True:
                break
            if command.get("inspect") is True:
                store = server.runner.store
                artifacts = store.list_artifacts({"root_frame_id": command["frame_id"]})
                projected = []
                for artifact in artifacts:
                    if artifact["filename"] not in {
                        "datasets/n1-targets.txt",
                        "summary.json",
                    }:
                        continue
                    versions = []
                    for version in store.list_versions(artifact["artifact_id"]):
                        meta = store.version_meta(version["version_id"])
                        snapshot_bytes = Path(meta["snapshot_path"]).read_bytes()
                        versions.append(
                            {
                                **version,
                                "inputs": [
                                    row["version_id"]
                                    for row in store.lineage_inputs(
                                        version["version_id"]
                                    )
                                ],
                                "snapshot": snapshot_bytes.decode("utf-8"),
                                "snapshot_bytes": len(snapshot_bytes),
                                "snapshot_sha256": hashlib.sha256(
                                    snapshot_bytes
                                ).hexdigest(),
                            }
                        )
                    projected.append({**artifact, "versions": versions})
                print(
                    json.dumps(
                        {
                            "inspection": command["id"],
                            "artifacts": projected,
                            "observed": OBSERVED,
                        }
                    ),
                    flush=True,
                )
    finally:
        server.shutdown()
        server.server_close()
        print(json.dumps({"stopped": True, "observed": OBSERVED}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
