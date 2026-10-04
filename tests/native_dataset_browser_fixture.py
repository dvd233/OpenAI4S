"""Owned offline daemon for the fork-only native-input browser acceptance.

Only model replies, remote Zenodo bytes and profile readiness are fixtures.
The Agent, native permission/capture boundary, kernel, Store and HTTP stay real.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import sys
from pathlib import Path

from openai4s import webtools
from openai4s.config import Config, LLMConfig, RoadmapFeatureFlags
from openai4s.server import gateway

BODY = b"wavelength,intensity\n500,0.75\n"
CHECKSUM = "md5:" + hashlib.md5(BODY, usedforsecurity=False).hexdigest()
DOCUMENT = {
    "id": 123,
    "doi": "10.5281/zenodo.123",
    "conceptdoi": "10.5281/zenodo.122",
    "metadata": {
        "resource_type": {"type": "dataset"},
        "title": "Synthetic initial source",
        "access_right": "open",
        "license": {"id": "cc-by-4.0"},
        "version": "1.0.0",
    },
    "files": [{"key": "spectra.csv", "size": len(BODY), "checksum": CHECKSUM}],
}
OBSERVED = {"model_fixture_calls": 0, "metadata_reads": 0, "file_reads": 0}


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
    content = "Running the explicit offline dataset operation."
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
        if m.get("role") == "user" and "NATIVE_BROWSER_" in str(m.get("content") or "")
    ]
    if not goals:
        return {"content": "Offline dataset acceptance", "usage": {}}
    position, goal = goals[-1]
    tail = messages[position + 1 :]
    names = {m.get("name") for m in tail if m.get("role") == "tool"}
    if "NATIVE_BROWSER_IMPORT" in goal:
        DOCUMENT["metadata"]["title"] = (
            "Later source annotation"
            if "SECOND" in goal
            else "Synthetic initial source"
        )
        if "science_search" not in names:
            return native_reply(
                "science_search", {"database": "zenodo", "query": "spectra", "limit": 1}
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
            selected = next(
                f for f in record["attributes"]["files"] if f["key"] == "spectra.csv"
            )
            return native_reply(
                "science_import_dataset",
                {
                    "record_id": record["id"],
                    "file_key": selected["key"],
                    "path": "datasets/spectra.csv",
                    "expected_size": selected["declared_size_bytes"],
                    "expected_checksum": selected["declared_checksum"],
                    "max_bytes": 1024 * 1024,
                },
            )
        code = "host.submit_output({'status': 'imported'}, ['Imported the selected synthetic dataset'])"
    else:
        match = re.search(r"NATIVE_BROWSER_ANALYSE ([A-Za-z0-9_.:-]+)", goal)
        if not match:
            raise RuntimeError("unknown offline browser request")
        version = match.group(1)
        code = (
            "import pandas as pd\n"
            f"input_path = host.artifact_path({version!r})\n"
            "data = pd.read_csv(input_path)\n"
            "data['mean'] = data['intensity'].mean()\n"
            "data['rows'] = len(data)\n"
            f"data['input_version'] = {version!r}\n"
            "data.to_json('summary.json', orient='records')\n"
            f"host.submit_output({{'files': ['summary.json'], 'input_version': {version!r}}}, ['Analysed the explicitly selected frozen input'])\n"
        )
    content = f"```python\n{code}\n```"
    if on_delta:
        on_delta(content)
    return {"content": content, "usage": {}}


def metadata_fetch(url, **_kwargs):
    OBSERVED["metadata_reads"] += 1
    if url.startswith("https://zenodo.org/api/records?"):
        value = {"hits": {"hits": [DOCUMENT]}, "links": {}}
    elif url == "https://zenodo.org/api/records/123":
        value = DOCUMENT
    else:
        raise AssertionError("unapproved external metadata request")
    body = json.dumps(value).encode()
    return {
        "content": body.decode(),
        "raw_sha256": hashlib.sha256(body).hexdigest(),
        "raw_bytes": len(body),
    }


@contextlib.contextmanager
def dataset_response(url, **_kwargs):
    if url != "https://zenodo.org/api/records/123/files/spectra.csv/content":
        raise AssertionError("unapproved external file request")
    OBSERVED["file_reads"] += 1
    yield io.BytesIO(BODY), url, "text/csv"


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
    webtools.web_fetch = metadata_fetch
    webtools._open_http_response = dataset_response
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
                "synthetic_bytes_sha256": hashlib.sha256(BODY).hexdigest(),
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
                        "datasets/spectra.csv",
                        "summary.json",
                    }:
                        continue
                    versions = []
                    for version in store.list_versions(artifact["artifact_id"]):
                        meta = store.version_meta(version["version_id"])
                        versions.append(
                            {
                                **version,
                                "inputs": [
                                    row["version_id"]
                                    for row in store.lineage_inputs(
                                        version["version_id"]
                                    )
                                ],
                                "snapshot": Path(meta["snapshot_path"]).read_text(
                                    encoding="utf-8"
                                ),
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
