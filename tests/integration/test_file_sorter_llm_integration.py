import os
from pathlib import Path

import pytest
import requests

from files_manager import file_sorter_assistant as sorter


pytestmark = pytest.mark.integration


def require_llm_backend() -> None:
    payload = {
        "model": sorter.MODEL_NAME,
        "messages": [
            {"role": "system", "content": "Return only JSON."},
            {"role": "user", "content": 'Return {"ok": true}.'},
        ],
        "temperature": 0,
        "max_tokens": 128,
        "stream": False,
    }

    try:
        response = requests.post(sorter.LLAMACPP_URL, json=payload, timeout=10)
        if response.status_code >= 400:
            payload.pop("response_format", None)
            response = requests.post(sorter.LLAMACPP_URL, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        pytest.skip(f"local LLM backend is not available: {exc}")

    content = response.json()["choices"][0]["message"].get("content", "")
    data = sorter.parse_json_object_response(content)

    if not isinstance(data, dict):
        pytest.skip(f"local LLM backend did not return parseable JSON: {content[:200]}")


def touch_file(path: Path, size: int, mtime: int) -> Path:
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_local_llm_json_endpoint_is_reachable() -> None:
    require_llm_backend()


def test_ask_llm_duplicate_groups_returns_valid_shape(monkeypatch, tmp_path: Path) -> None:
    require_llm_backend()
    files = [
        touch_file(tmp_path / "Gaea-2.2.6.rar", 74_513, 1_764_000_000),
        touch_file(tmp_path / "Quadspinner.Gaea2-2.2.6.0-ARGIE.rar", 75_166, 1_761_700_000),
        touch_file(tmp_path / "QuadSpinner_Gaea_2.2.3.2.rar", 68_716, 1_756_000_000),
        touch_file(tmp_path / "Photoshop 2026 (27.2.0.15).part1.rar", 1_000, 1_767_000_000),
        touch_file(tmp_path / "Photoshop 2026 (27.2.0.15).part2.rar", 2_000, 1_767_000_060),
    ]

    monkeypatch.setattr(sorter, "REQUEST_TIMEOUT", 45)
    monkeypatch.setattr(sorter, "MAX_TOKENS", 4096)
    groups, id_to_path, rejected = sorter.ask_llm_duplicate_groups(files)

    assert isinstance(groups, list)
    assert isinstance(id_to_path, dict)
    assert isinstance(rejected, list)
    assert set(id_to_path) == {1, 2, 3, 4, 5}

    for group in groups:
        assert isinstance(group["keep"], int)
        assert isinstance(group["delete"], list)
        assert group["keep"] not in group["delete"]
        assert not sorter.group_contains_multipart_set_parts([group["keep"], *group["delete"]], id_to_path)
