"""合成 benchmark 必须离线、可复跑且不依赖生产数据。"""

from __future__ import annotations

import json

from scripts.benchmark_prompt_tokens import main


def test_synthetic_benchmark_writes_json_without_database(tmp_path, capsys):
    output = tmp_path / "benchmark.json"

    code = main(
        [
            "--synthetic",
            "--scenario",
            "all",
            "--rounds",
            "2",
            "--repeats",
            "20",
            "--seed",
            "7",
            "--json-output",
            str(output),
        ]
    )

    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "synthetic"
    assert payload["scenario"] == "all"
    assert payload["script_sha256"]
    assert payload["scenarios"]["text"]["repeats"] >= 20
    assert payload["scenarios"]["prefix"]["stable_prefix_invalidations"] == 0
    images = payload["scenarios"]["images"]
    assert images["provider_calls"] == 0
    assert images["guard_rejection_code"] == "image_request_count_exceeded"
    assert images["wire_has_data_url"] is True
    assert images["history_has_base64"] is False
    assert "Debata 合成缓存/图片 benchmark" in capsys.readouterr().out
