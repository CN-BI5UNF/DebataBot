"""只读 benchmark：对比重构前后的 prompt token 估算。

用法：
    .venv/bin/python scripts/benchmark_prompt_tokens.py \
      --db ~/.local/share/Debata_Agent/instances/default/memory/小堡/小堡.db \
      --persona 小堡 --model deepseek-flash

不会写数据库、不会调用模型、不会修改任何文件。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.context_builder import (  # noqa: E402
    build_combined_system_prompt,
    first_tail_block_index,
)
from agents.persona_loader import load_persona  # noqa: E402
from app_config.paths import AppPaths  # noqa: E402
from core.model_view import ModelViewPolicy, build_model_view  # noqa: E402
from tools import build_default_registry  # noqa: E402
from utils.image_budget import assert_wire_request_fits  # noqa: E402
from utils.image_contracts import (  # noqa: E402
    ImageAssetError,
    ImageLimits,
)
from utils.token_budget import (  # noqa: E402
    TokenEstimator,
    clear_raw_text_cache,
    raw_text_cache_info,
)


def _load_history(db_path: Path) -> tuple[list[dict[str, Any]], int]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "select coalesce(active_start_index, 0) from rolling_summary limit 1"
        ).fetchone()
        active_start = int(row[0]) if row else 0
        rows = connection.execute(
            "select record_json from history_records order by history_index"
        ).fetchall()
    finally:
        connection.close()
    records: list[dict[str, Any]] = []
    for (raw,) in rows:
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records[max(0, active_start):], active_start


def _fake_config(profile: str, memory_mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        features=SimpleNamespace(
            long_term_memory=SimpleNamespace(mode=memory_mode),
            vision=SimpleNamespace(enabled=True),
            web_search=SimpleNamespace(enabled=True),
            weather=SimpleNamespace(enabled=False),
        ),
        persona_management=None,
        behavior=SimpleNamespace(tool_schema_profile=profile),
    )


def _tool_tokens(profile: str, model: str, memory_mode: str) -> tuple[int, int]:
    registry = build_default_registry(_fake_config(profile, memory_mode))
    estimator = TokenEstimator(model=model)
    schemas = registry.get_schemas()
    return len(schemas), estimator.estimate_tools_schema(schemas)


def _signature(record: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    content = str(record.get("content") or "")
    digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]
    tool_ids = tuple(
        str(tool_call.get("id") or "")
        for tool_call in record.get("tool_calls") or []
        if isinstance(tool_call, dict)
    )
    return str(record.get("role") or ""), digest, tool_ids


def _simulate_prefix(
    records: list[dict[str, Any]],
    *,
    policy: ModelViewPolicy,
    steps: int = 20,
) -> tuple[float, int, int]:
    if len(records) < 2:
        return 0.0, 0, 0
    step = max(1, len(records) // max(1, steps))
    previous: list[tuple[str, str, tuple[str, ...]]] | None = None
    invalidated: list[int] = []
    for end in range(step, len(records) + 1, step):
        view = build_model_view(records[:end], policy=policy)
        current = [_signature(record) for record in view.records]
        if previous is not None:
            common = 0
            for left, right in zip(previous, current, strict=False):
                if left != right:
                    break
                common += 1
            invalidated.append(len(previous) - common)
        previous = current
    if not invalidated:
        return 0.0, 0, 0
    return sum(invalidated) / len(invalidated), max(invalidated), len(invalidated)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001
        return ""


def _environment() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "anthropic": _version("anthropic"),
        "pillow": _version("pillow"),
        "tiktoken": _version("tiktoken"),
        "orjson": _version("orjson"),
    }


def _synthetic_text_metrics(
    *,
    model: str,
    seed: int,
    repeats: int,
) -> dict[str, Any]:
    rng = random.Random(seed)
    system_text = (
        "<synthetic_system>\n"
        + ("固定规则与工具协议。缓存前缀测试。" * 1200)
        + "\n</synthetic_system>"
    )
    user_text = "".join(rng.choice("测试缓存前缀") for _ in range(160))
    tools = build_default_registry(_fake_config("compact", "file")).get_schemas()
    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
    clear_raw_text_cache()
    cold_start = time.perf_counter()
    estimator = TokenEstimator(model=model)
    cold_tokens = estimator.estimate_prompt(messages, tools)
    cold_ms = (time.perf_counter() - cold_start) * 1000.0

    hot_times: list[float] = []
    for _ in range(max(20, repeats)):
        started = time.perf_counter()
        TokenEstimator(model=model).estimate_prompt(messages, tools)
        hot_times.append((time.perf_counter() - started) * 1000.0)

    same_content_times: list[float] = []
    for _ in range(max(10, repeats // 4)):
        copied = json.loads(json.dumps(messages))
        started = time.perf_counter()
        TokenEstimator(model=model).estimate_prompt(copied, tools)
        same_content_times.append((time.perf_counter() - started) * 1000.0)

    before_changed = raw_text_cache_info()
    changed = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "竞" * len(user_text)},
    ]
    TokenEstimator(model=model).estimate_prompt(changed, tools)
    after_changed = raw_text_cache_info()
    return {
        "cold_ms": round(cold_ms, 3),
        "hot_p50_ms": round(statistics.median(hot_times), 3),
        "hot_p95_ms": round(_percentile(hot_times, 0.95), 3),
        "same_content_p50_ms": round(statistics.median(same_content_times), 3),
        "cold_tokens": cold_tokens,
        "system_chars": len(system_text),
        "message_chars": len(user_text),
        "tool_count": len(tools),
        "raw_cache_after_hot": before_changed,
        "changed_tail_misses": (
            after_changed["misses"] - before_changed["misses"]
        ),
        "changed_tail_hits": after_changed["hits"] - before_changed["hits"],
        "repeats": len(hot_times),
    }


def _synthetic_prefix_metrics(*, model: str, rounds: int) -> dict[str, Any]:
    estimator = TokenEstimator(model=model)
    system = {"role": "system", "content": "<stable>固定系统提示</stable>"}
    stable_records: list[dict[str, Any]] = []
    previous_signatures: list[str] = []
    invalidations = 0
    last_stable_tokens = 0
    last_tail_tokens = 0
    last_stable_chars = 0
    for index in range(rounds):
        stable_records.extend(
            [
                {"role": "user", "content": f"第{index}轮用户消息"},
                {"role": "assistant", "content": f"第{index}轮助手回复"},
            ]
        )
        volatile = {
            "role": "user",
            "content": f"<task_context>本轮变化 {index}</task_context>",
            "_prompt_section": "volatile_tail",
        }
        messages = [system, *stable_records, volatile]
        tail_start = first_tail_block_index(messages)
        stable = messages[:tail_start]
        signatures = [
            hashlib.sha256(
                json.dumps(
                    message,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            for message in stable
        ]
        if previous_signatures and signatures[: len(previous_signatures)] != previous_signatures:
            invalidations += 1
        previous_signatures = signatures
        last_stable_tokens = estimator.estimate_prompt(stable)
        last_tail_tokens = estimator.estimate_prompt(messages[tail_start:])
        last_stable_chars = sum(len(str(m.get("content") or "")) for m in stable)
    return {
        "rounds": rounds,
        "stable_prefix_invalidations": invalidations,
        "stable_prefix_tokens_last": last_stable_tokens,
        "runtime_tail_tokens_last": last_tail_tokens,
        "stable_record_count_last": len(stable_records),
        "stable_prefix_chars_last": last_stable_chars,
    }


async def _synthetic_image_metrics(*, rounds: int, seed: int) -> dict[str, Any]:
    from PIL import Image

    from features.vision.image_assets import ImageAssetStore
    from utils.image_contracts import asset_relative_path

    rng = random.Random(seed)
    limits = ImageLimits()
    image_bytes: list[bytes] = []
    for _ in range(4):
        image = Image.new(
            "RGB",
            (64, 64),
            (rng.randrange(256), rng.randrange(256), rng.randrange(256)),
        )
        from io import BytesIO

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        image_bytes.append(buffer.getvalue())

    with tempfile.TemporaryDirectory(prefix="debata-benchmark-images-") as root:
        workspace = Path(root) / "workspace"
        store = ImageAssetStore(workspace, limits=limits)
        refs: list[dict[str, Any]] = []
        for index, data in enumerate(image_bytes):
            refs.append(
                await store.ingest(
                    data,
                    message_id=f"m{index}",
                    index=index,
                )
            )
        source = {
            "role": "user",
            "content": "合成图片消息",
            "conversation_id": "bench:images",
            "metadata": {
                "messages": [
                    {"message_id": f"m{index}", "inbound_seq": index + 1}
                    for index in range(len(refs))
                ],
                "image_assets": refs,
            },
        }
        projected: list[dict[str, Any]] = []
        for _ in range(max(1, rounds)):
            projected = await store.project_records([source])
        info = store.cache_info()
        wire_images = [
            block
            for message in projected
            if isinstance(message.get("content"), list)
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "image_url"
        ]
        wire_bytes = sum(
            len(block["image_url"]["url"])
            for block in wire_images
            if isinstance(block.get("image_url"), dict)
        )
        image_paths = [
            workspace / asset_relative_path(
                ref["transform_version"],
                ref["sha256"],
                ref["mime"],
            )
            for ref in refs
        ]
        assert all(path.is_file() for path in image_paths)

        guard_rejection = ""
        try:
            assert_wire_request_fits(
                projected,
                limits=ImageLimits(max_images_per_request=2),
            )
        except ImageAssetError as exc:
            guard_rejection = exc.code

        first_size = max(1, info["current_bytes"] // max(1, len(refs)))
        cache_store = ImageAssetStore(
            workspace,
            limits=limits,
            cache_max_bytes=min(
                limits.cache_max_bytes,
                max(1, first_size + max(1, first_size // 2)),
            ),
        )
        await cache_store.image_content(refs[0])
        await cache_store.image_content(refs[1])
        eviction_info = cache_store.cache_info()

        return {
            "image_count": len(refs),
            "ingests": info["ingests"],
            "reads": info["reads"],
            "encodes": info["encodes"],
            "cache_hits": info["hits"],
            "cache_misses": info["misses"],
            "cache_entries": info["entries"],
            "cache_bytes": info["current_bytes"],
            "cache_max_bytes": info["max_bytes"],
            "wire_bytes": wire_bytes,
            "wire_has_data_url": all(
                isinstance(block.get("image_url"), dict)
                and block["image_url"]["url"].startswith("data:image/")
                for block in wire_images
            ),
            "history_has_base64": "base64" in json.dumps(source, ensure_ascii=False),
            "guard_rejection_code": guard_rejection,
            "eviction_sample": {
                "max_bytes": eviction_info["max_bytes"],
                "evictions": eviction_info["evictions"],
                "current_bytes": eviction_info["current_bytes"],
            },
            "provider_calls": 0,
            "independent_vision_calls": 0,
            "tool_loop_calls": 0,
        }


def _synthetic_payload(args: argparse.Namespace) -> dict[str, Any]:
    scenarios = (
        ["text", "prefix", "images"] if args.scenario == "all" else [args.scenario]
    )
    payload: dict[str, Any] = {
        "mode": "synthetic",
        "scenario": args.scenario,
        "seed": args.seed,
        "rounds": args.rounds,
        "repeats": args.repeats,
        "model": args.model,
        "script_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest()[:16],
        "input_hash": hashlib.sha256(
            f"{args.seed}:{args.rounds}:{args.repeats}:{args.scenario}".encode()
        ).hexdigest()[:16],
        "environment": _environment(),
        "scenarios": {},
    }
    if "text" in scenarios:
        payload["scenarios"]["text"] = _synthetic_text_metrics(
            model=args.model,
            seed=args.seed,
            repeats=args.repeats,
        )
    if "prefix" in scenarios:
        payload["scenarios"]["prefix"] = _synthetic_prefix_metrics(
            model=args.model,
            rounds=args.rounds,
        )
    if "images" in scenarios:
        payload["scenarios"]["images"] = asyncio.run(
            _synthetic_image_metrics(rounds=args.rounds, seed=args.seed)
        )
    return payload


def _print_synthetic(payload: dict[str, Any]) -> None:
    print("Debata 合成缓存/图片 benchmark（无网络、无 Runtime、无生产数据）")
    print(f"  scenario={payload['scenario']} seed={payload['seed']} "
          f"rounds={payload['rounds']} repeats={payload['repeats']}")
    text = payload["scenarios"].get("text")
    if text:
        print(
            "  文本: cold={cold_ms:.2f}ms hot_p50={hot_p50_ms:.2f}ms "
            "hot_p95={hot_p95_ms:.2f}ms cache_hits={hits} misses={misses}".format(
                cold_ms=text["cold_ms"],
                hot_p50_ms=text["hot_p50_ms"],
                hot_p95_ms=text["hot_p95_ms"],
                hits=text["raw_cache_after_hot"]["hits"],
                misses=text["raw_cache_after_hot"]["misses"],
            )
        )
    prefix = payload["scenarios"].get("prefix")
    if prefix:
        print(
            "  前缀: invalidations={invalidations} stable_tokens={stable} "
            "tail_tokens={tail} records={records}".format(
                invalidations=prefix["stable_prefix_invalidations"],
                stable=prefix["stable_prefix_tokens_last"],
                tail=prefix["runtime_tail_tokens_last"],
                records=prefix["stable_record_count_last"],
            )
        )
    images = payload["scenarios"].get("images")
    if images:
        print(
            "  图片: count={count} ingests={ingests} reads={reads} "
            "encodes={encodes} cache_hits={hits} evictions={evictions} "
            "wire_bytes={wire} guard={guard}".format(
                count=images["image_count"],
                ingests=images["ingests"],
                reads=images["reads"],
                encodes=images["encodes"],
                hits=images["cache_hits"],
                evictions=images["eviction_sample"]["evictions"],
                wire=images["wire_bytes"],
                guard=images["guard_rejection_code"] or "none",
            )
        )
    print(f"  script_sha256={payload['script_sha256']} cp={payload['environment']['cpu_count']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="Debata 人格 SQLite 路径（非合成模式必填）")
    parser.add_argument("--persona", default="小堡", help="人格名")
    parser.add_argument("--model", default="deepseek-flash", help="用于 tokenizer 的模型名")
    parser.add_argument("--memory-mode", default="file", choices=["file", "rag"])
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--json-output", help="把 JSON 结果另写入指定路径")
    parser.add_argument("--no-simulate", action="store_true", help="跳过前缀稳定性模拟")
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="使用合成文本/图片，不读取数据库或生产配置",
    )
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["text", "prefix", "images", "all"],
    )
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    if args.synthetic:
        payload = _synthetic_payload(args)
        if args.json_output:
            Path(args.json_output).expanduser().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            _print_synthetic(payload)
        return 0

    if not args.db:
        parser.error("非合成模式必须提供 --db；离线复验请加 --synthetic")

    records, active_start = _load_history(Path(args.db).expanduser())
    policy = ModelViewPolicy()
    view = build_model_view(records, policy=policy)
    estimator = TokenEstimator(model=args.model)
    raw_tokens = estimator.estimate_messages(records)
    view_tokens = estimator.estimate_messages(view.records)

    persona = None
    try:
        persona = load_persona(AppPaths(), args.persona)
    except Exception:
        persona = None
    system_tokens = 0
    if persona is not None:
        system_prompt = build_combined_system_prompt(
            persona,
            memory_mode=args.memory_mode,
        )
        system_tokens = estimator.estimate_text(system_prompt)

    full_count, full_tools_tokens = _tool_tokens("full", args.model, args.memory_mode)
    compact_count, compact_tools_tokens = _tool_tokens(
        "compact", args.model, args.memory_mode
    )
    compact_registry = build_default_registry(
        _fake_config("compact", args.memory_mode)
    )
    compact_schemas = compact_registry.get_schemas()
    compact_stub_count = sum(
        1
        for schema in compact_schemas
        if "_tool_search_required"
        in (schema.get("function", {}).get("parameters", {}).get("properties", {}))
    )

    before_prompt = raw_tokens + system_tokens + full_tools_tokens
    after_prompt = view_tokens + system_tokens + compact_tools_tokens
    reduction = 0.0 if before_prompt <= 0 else 1 - after_prompt / before_prompt

    payload = {
        "db": str(args.db),
        "active_start": active_start,
        "records_raw": len(records),
        "records_view": len(view.records),
        "tokens_raw": raw_tokens,
        "tokens_view": view_tokens,
        "view_reduction": 0.0 if raw_tokens <= 0 else 1 - view_tokens / raw_tokens,
        "system_tokens": system_tokens,
        "tools_full": {"count": full_count, "tokens": full_tools_tokens},
        "tools_compact": {
            "count": compact_count,
            "tokens": compact_tools_tokens,
            "stub_count": compact_stub_count,
        },
        "estimated_prompt_before": before_prompt,
        "estimated_prompt_after": after_prompt,
        "estimated_prompt_reduction": reduction,
        "model_view": {
            "dropped_runtime": view.dropped_runtime,
            "dropped_no_action_pairs": view.dropped_no_action_pairs,
            "dropped_no_action_records": view.dropped_no_action_records,
            "capped_records": view.capped_records,
            "projected_tool_results": view.projected_tool_results,
        },
    }
    if not args.no_simulate:
        average, maximum, samples = _simulate_prefix(records, policy=policy)
        payload["prefix_stability"] = {
            "avg_invalidated_records": average,
            "max_invalidated_records": maximum,
            "samples": samples,
        }

    if args.json_output:
        Path(args.json_output).expanduser().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("Debata prompt token benchmark（只读估算）")
    print(f"  数据库        : {args.db}")
    print(f"  活跃记录      : {len(records)} -> {len(view.records)}")
    print(f"  活跃 token    : {raw_tokens:,} -> {view_tokens:,} "
          f"({payload['view_reduction']:.1%} 降低)")
    print(f"  系统提示      : {system_tokens:,} token")
    print(f"  工具 schema   : full={full_count} 个/{full_tools_tokens:,} token"
          f" -> compact={compact_count} 个/{compact_tools_tokens:,} token"
          f"（stub {compact_stub_count} 个）")
    print(f"  估算 prompt   : {before_prompt:,} -> {after_prompt:,} "
          f"({reduction:.1%} 降低)")
    print(
        "  模型视图丢弃  : "
        f"runtime={view.dropped_runtime}, "
        f"no_action_pairs={view.dropped_no_action_pairs}, "
        f"capped={view.capped_records}, "
        f"projected_tools={view.projected_tool_results}"
    )
    if "prefix_stability" in payload:
        prefix = payload["prefix_stability"]
        print(
            "  前缀稳定性    : "
            f"平均失效 {prefix['avg_invalidated_records']:.1f} 条，"
            f"最大 {prefix['max_invalidated_records']} 条"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
