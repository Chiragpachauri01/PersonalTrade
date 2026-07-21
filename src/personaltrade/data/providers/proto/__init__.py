"""Vendored + compiled Upstox Market Data Feed V3 protobuf schema.

`market_data_feed_v3.proto` is vendored verbatim (see its header comment for
provenance); `market_data_feed_v3_pb2.py`/`.pyi` are generated — do not hand-edit.
Regenerate with:
    uv run python -m grpc_tools.protoc -I src/personaltrade/data/providers/proto \\
        --python_out=src/personaltrade/data/providers/proto \\
        --pyi_out=src/personaltrade/data/providers/proto \\
        src/personaltrade/data/providers/proto/market_data_feed_v3.proto
"""
