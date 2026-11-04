"""
NOVA Compute Phase 2C-A — Extended GGUF Inspector
Tasks 3 & 4: Full tensor inventory with size calculations, semantic classification,
offset validation, and memory placement analysis.

Supports Q4_K, Q6_K, Q5_K, Q8_0, F32 (and common others).
No model weights are loaded into RAM — only header metadata is parsed.
"""

import os
import sys
import struct
import json
import re
from collections import defaultdict

# ---------------------------------------------------------------------------
# GGUF type constants
# ---------------------------------------------------------------------------
GGUF_TYPE_UINT8   = 0
GGUF_TYPE_INT8    = 1
GGUF_TYPE_UINT16  = 2
GGUF_TYPE_INT16   = 3
GGUF_TYPE_UINT32  = 4
GGUF_TYPE_INT32   = 5
GGUF_TYPE_FLOAT32 = 6
GGUF_TYPE_BOOL    = 7
GGUF_TYPE_STRING  = 8
GGUF_TYPE_ARRAY   = 9
GGUF_TYPE_UINT64  = 10
GGUF_TYPE_INT64   = 11
GGUF_TYPE_FLOAT64 = 12

# ---------------------------------------------------------------------------
# GGML quantization block definitions
# Each entry: (block_elements, block_size_bytes)
# "block_elements" = how many fp32 values are encoded per block
# "block_size_bytes" = on-disk bytes for that block
# ---------------------------------------------------------------------------
GGML_QUANT_BLOCKS = {
    # name        : (elements_per_block, bytes_per_block)
    "F32"         : (1,    4),
    "F16"         : (1,    2),
    "BF16"        : (1,    2),
    "Q4_0"        : (32,   18),   # 32 weights: 16 bytes nibbles + 2 bytes scale (fp16)
    "Q4_1"        : (32,   20),   # 32 weights: 16 bytes + 2×fp16 scale/min
    "Q5_0"        : (32,   22),
    "Q5_1"        : (32,   24),
    "Q8_0"        : (32,   34),   # 32 int8 weights + 1 fp16 scale = 32+2 = 34
    "Q8_1"        : (32,   36),
    "Q2_K"        : (256, 84),
    "Q3_K"        : (256, 110),
    "Q4_K"        : (256, 144),   # 256 weights in a super-block of 144 bytes
    "Q5_K"        : (256, 176),
    "Q6_K"        : (256, 210),
    "Q8_K"        : (256, 292),
    "IQ1_S"       : (256, 50),
    "IQ1_M"       : (256, 56),
    "IQ2_XXS"     : (256, 66),
    "IQ2_XS"      : (256, 74),
    "IQ2_S"       : (256, 82),
    "IQ3_XXS"     : (256, 98),
    "IQ3_S"       : (256, 110),
    "IQ4_NL"      : (32,  18),
    "IQ4_XS"      : (256, 136),
}

GGML_TYPE_ID_TO_NAME = {
    0:  "F32",   1:  "F16",   2:  "Q4_0",  3:  "Q4_1",
    6:  "Q5_0",  7:  "Q5_1",  8:  "Q8_0",  9:  "Q8_1",
    10: "Q2_K",  11: "Q3_K",  12: "Q4_K",  13: "Q5_K",
    14: "Q6_K",  15: "Q8_K",  16: "IQ2_XXS", 17: "IQ2_XS",
    18: "IQ3_XXS", 19: "IQ1_S", 20: "IQ4_NL", 21: "IQ3_S",
    22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16",
    26: "I32",   27: "I64",   28: "F64",  29: "IQ1_M", 30: "BF16"
}

ALIGNMENT = 32  # GGUF default tensor alignment

# ---------------------------------------------------------------------------
# Tensor name → semantic category and layer index extraction
# ---------------------------------------------------------------------------
TENSOR_PATTERNS = [
    # (regex, category)
    (r"^token_embd\.weight$",                           "embedding"),
    (r"^output_norm\.weight$",                          "output_norm"),
    (r"^output\.weight$",                               "output_head"),
    (r"\.attn_norm\.weight$",                           "attention_norm"),
    (r"\.ffn_norm\.weight$",                            "ffn_norm"),
    (r"\.attn_q\.weight$",                              "attention_q_proj"),
    (r"\.attn_k\.weight$",                              "attention_k_proj"),
    (r"\.attn_v\.weight$",                              "attention_v_proj"),
    (r"\.attn_output\.weight$",                         "attention_out_proj"),
    (r"\.attn_q_norm\.weight$",                         "attention_q_norm"),
    (r"\.attn_k_norm\.weight$",                         "attention_k_norm"),
    (r"\.ffn_gate_inp\.weight$",                        "router_gate"),
    (r"\.ffn_gate_shexp\.weight$",                      "ffn_shared_gate"),
    (r"\.ffn_up_shexp\.weight$",                        "ffn_shared_up"),
    (r"\.ffn_down_shexp\.weight$",                      "ffn_shared_down"),
    (r"\.ffn_gate_exps\.weight$",                       "expert_gate_stacked"),
    (r"\.ffn_up_exps\.weight$",                         "expert_up_stacked"),
    (r"\.ffn_down_exps\.weight$",                       "expert_down_stacked"),
]

def classify_tensor(name):
    """Return (category, layer_index_or_None)."""
    layer_match = re.search(r"\.(\d+)\.", name)
    layer_idx = int(layer_match.group(1)) if layer_match else None

    for pattern, category in TENSOR_PATTERNS:
        if re.search(pattern, name):
            return category, layer_idx

    return "other", layer_idx


def calc_storage_bytes(type_name, num_elements):
    """
    Compute on-disk storage bytes for num_elements of a given GGML type,
    using block-aware row-quantization (last dim must be divisible by block size).
    Returns (bytes, is_exact) where is_exact=False signals alignment rounding.
    """
    info = GGML_QUANT_BLOCKS.get(type_name)
    if info is None:
        return num_elements * 4, False  # fallback: treat as F32

    elems_per_block, bytes_per_block = info
    full_blocks, remainder = divmod(num_elements, elems_per_block)
    if remainder != 0:
        full_blocks += 1  # partial last block still occupies full block space
    return full_blocks * bytes_per_block, (remainder == 0)


# ---------------------------------------------------------------------------
# GGUF binary parser (header + tensor metadata only, no weight data loaded)
# ---------------------------------------------------------------------------

def read_str(f):
    length = struct.unpack("<Q", f.read(8))[0]
    return f.read(length).decode("utf-8", errors="replace")


def skip_val(f, vtype):
    """Skip a metadata value of given type (without storing)."""
    if vtype in (GGUF_TYPE_UINT8, GGUF_TYPE_INT8, GGUF_TYPE_BOOL):
        f.read(1)
    elif vtype in (GGUF_TYPE_UINT16, GGUF_TYPE_INT16):
        f.read(2)
    elif vtype in (GGUF_TYPE_UINT32, GGUF_TYPE_INT32, GGUF_TYPE_FLOAT32):
        f.read(4)
    elif vtype in (GGUF_TYPE_UINT64, GGUF_TYPE_INT64, GGUF_TYPE_FLOAT64):
        f.read(8)
    elif vtype == GGUF_TYPE_STRING:
        length = struct.unpack("<Q", f.read(8))[0]
        f.read(length)
    elif vtype == GGUF_TYPE_ARRAY:
        elem_type = struct.unpack("<I", f.read(4))[0]
        count = struct.unpack("<Q", f.read(8))[0]
        for _ in range(count):
            skip_val(f, elem_type)
    else:
        raise ValueError(f"Unknown GGUF value type {vtype} at offset {f.tell()}")


def read_val(f, vtype):
    """Read and return a metadata value (arrays are summarised for large ones)."""
    if vtype == GGUF_TYPE_UINT8:  return struct.unpack("<B", f.read(1))[0]
    if vtype == GGUF_TYPE_INT8:   return struct.unpack("<b", f.read(1))[0]
    if vtype == GGUF_TYPE_UINT16: return struct.unpack("<H", f.read(2))[0]
    if vtype == GGUF_TYPE_INT16:  return struct.unpack("<h", f.read(2))[0]
    if vtype == GGUF_TYPE_UINT32: return struct.unpack("<I", f.read(4))[0]
    if vtype == GGUF_TYPE_INT32:  return struct.unpack("<i", f.read(4))[0]
    if vtype == GGUF_TYPE_FLOAT32: return struct.unpack("<f", f.read(4))[0]
    if vtype == GGUF_TYPE_BOOL:   return bool(struct.unpack("<B", f.read(1))[0])
    if vtype == GGUF_TYPE_STRING: return read_str(f)
    if vtype == GGUF_TYPE_UINT64: return struct.unpack("<Q", f.read(8))[0]
    if vtype == GGUF_TYPE_INT64:  return struct.unpack("<q", f.read(8))[0]
    if vtype == GGUF_TYPE_FLOAT64: return struct.unpack("<d", f.read(8))[0]
    if vtype == GGUF_TYPE_ARRAY:
        elem_type = struct.unpack("<I", f.read(4))[0]
        count = struct.unpack("<Q", f.read(8))[0]
        if count > 64:
            for _ in range(count):
                skip_val(f, elem_type)
            return f"<array of {count} elements>"
        return [read_val(f, elem_type) for _ in range(count)]
    raise ValueError(f"Unknown type {vtype}")


def parse_gguf_header(filepath):
    """
    Parse GGUF header: magic, version, metadata KV, tensor metadata records.
    Returns (metadata_dict, tensors_list, data_offset).
    data_offset: byte position in file where tensor data begins.
    """
    file_size = os.path.getsize(filepath)

    with open(filepath, "rb") as f:
        magic = f.read(4)
        assert magic == b"GGUF", f"Invalid GGUF magic: {magic!r}"
        version        = struct.unpack("<I", f.read(4))[0]
        tensor_count   = struct.unpack("<Q", f.read(8))[0]
        kv_count       = struct.unpack("<Q", f.read(8))[0]

        # --- Metadata KV (read all, store non-token-list values) ---
        metadata = {
            "_gguf_version": version,
            "_tensor_count": tensor_count,
            "_kv_count": kv_count,
        }
        for _ in range(kv_count):
            key    = read_str(f)
            vtype  = struct.unpack("<I", f.read(4))[0]
            # Skip large token arrays for speed; store everything else
            if key in ("tokenizer.ggml.tokens", "tokenizer.ggml.scores",
                       "tokenizer.ggml.token_type", "tokenizer.ggml.merges"):
                skip_val(f, vtype)
                metadata[key] = "<large array — skipped>"
            else:
                metadata[key] = read_val(f, vtype)

        # --- Tensor metadata records ---
        tensors_raw = []
        for i in range(tensor_count):
            t_name  = read_str(f)
            n_dims  = struct.unpack("<I", f.read(4))[0]
            dims    = [struct.unpack("<Q", f.read(8))[0] for _ in range(n_dims)]
            t_type  = struct.unpack("<I", f.read(4))[0]
            t_offset = struct.unpack("<Q", f.read(8))[0]  # relative to data section start

            type_name   = GGML_TYPE_ID_TO_NAME.get(t_type, f"UNKNOWN_{t_type}")
            num_elements = 1
            for d in dims:
                num_elements *= d

            storage_bytes, exact = calc_storage_bytes(type_name, num_elements)
            category, layer_idx  = classify_tensor(t_name)

            tensors_raw.append({
                "name":           t_name,
                "dims":           dims,
                "n_dims":         n_dims,
                "type":           type_name,
                "type_id":        t_type,
                "num_elements":   num_elements,
                "storage_bytes":  storage_bytes,
                "storage_exact":  exact,
                "gguf_offset":    t_offset,       # relative to data section
                "layer_idx":      layer_idx,
                "category":       category,
            })

        # Header ends here; data section begins after alignment padding
        header_end = f.tell()

    # Compute actual data section start (align to ALIGNMENT bytes)
    data_offset = ((header_end + ALIGNMENT - 1) // ALIGNMENT) * ALIGNMENT

    return metadata, tensors_raw, data_offset, file_size


def validate_tensors(tensors, data_offset, file_size):
    """
    Validate:
    - All tensor data_offsets point within the file.
    - No two tensors overlap in the data section.
    - Calculated storage sizes match layout (approximate check via offsets).
    Returns list of validation issues.
    """
    issues = []
    # Sort by absolute offset for overlap check
    sorted_t = sorted(tensors, key=lambda x: x["abs_offset"])
    for i, t in enumerate(sorted_t):
        abs_start = t["abs_offset"]
        abs_end   = abs_start + t["storage_bytes"]

        if abs_end > file_size:
            issues.append(f"OVERRUN: {t['name']} ends at {abs_end} > file size {file_size}")

        if i + 1 < len(sorted_t):
            next_start = sorted_t[i+1]["abs_offset"]
            if abs_end > next_start:
                # Allow ALIGNMENT padding between tensors
                padded_end = ((abs_end + ALIGNMENT - 1) // ALIGNMENT) * ALIGNMENT
                if padded_end > next_start:
                    issues.append(
                        f"OVERLAP: {t['name']} (end={abs_end}) overlaps "
                        f"{sorted_t[i+1]['name']} (start={next_start})"
                    )
    return issues


def build_full_inventory(filepath, verbose=False):
    print(f"Parsing GGUF header: {os.path.basename(filepath)} ({os.path.getsize(filepath):,} bytes)")
    metadata, tensors, data_offset, file_size = parse_gguf_header(filepath)
    print(f"  GGUF v{metadata['_gguf_version']}, {metadata['_tensor_count']} tensors, "
          f"{metadata['_kv_count']} metadata KV pairs")
    print(f"  Header ends at byte {data_offset} (data section start after alignment)")

    # Add absolute offset for each tensor
    for t in tensors:
        t["abs_offset"] = data_offset + t["gguf_offset"]

    # Validation
    issues = validate_tensors(tensors, data_offset, file_size)
    print(f"  Validation issues: {len(issues)}")
    for issue in issues[:10]:
        print(f"    {issue}")

    # Compute calculated data section size
    total_storage_bytes = sum(t["storage_bytes"] for t in tensors)
    actual_data_bytes   = file_size - data_offset
    print(f"  Calculated tensor data: {total_storage_bytes:,} bytes ({total_storage_bytes/(1024**3):.3f} GiB)")
    print(f"  Actual data section:    {actual_data_bytes:,} bytes ({actual_data_bytes/(1024**3):.3f} GiB)")
    print(f"  Difference (alignment padding): {actual_data_bytes - total_storage_bytes:,} bytes")

    # Build category summary
    category_stats = defaultdict(lambda: {"count": 0, "params": 0, "bytes": 0, "layers": set()})
    type_stats     = defaultdict(lambda: {"count": 0, "params": 0, "bytes": 0})

    for t in tensors:
        cat = t["category"]
        category_stats[cat]["count"]  += 1
        category_stats[cat]["params"] += t["num_elements"]
        category_stats[cat]["bytes"]  += t["storage_bytes"]
        if t["layer_idx"] is not None:
            category_stats[cat]["layers"].add(t["layer_idx"])

        tp = t["type"]
        type_stats[tp]["count"]  += 1
        type_stats[tp]["params"] += t["num_elements"]
        type_stats[tp]["bytes"]  += t["storage_bytes"]

    # Convert layer sets to sorted lists for JSON serialisation
    for cat in category_stats:
        category_stats[cat]["layers"] = sorted(category_stats[cat]["layers"])

    total_params = sum(t["num_elements"] for t in tensors)
    total_bytes  = sum(t["storage_bytes"] for t in tensors)

    return {
        "filepath":        filepath,
        "file_size_bytes": file_size,
        "data_offset":     data_offset,
        "metadata":        {k: v for k, v in metadata.items()
                            if not k.startswith("tokenizer.ggml.")
                            and not k.startswith("tokenizer.chat_template")},
        "tensor_count":    len(tensors),
        "total_parameters": total_params,
        "total_storage_bytes": total_bytes,
        "actual_data_bytes":   actual_data_bytes,
        "alignment_padding":   actual_data_bytes - total_storage_bytes,
        "validation_issues":   issues,
        "type_stats":          dict(type_stats),
        "category_stats":      dict(category_stats),
        "tensors":             tensors,
    }


def print_summary(inv):
    print("\n" + "=" * 72)
    print(f"TENSOR INVENTORY SUMMARY — {os.path.basename(inv['filepath'])}")
    print("=" * 72)
    print(f"Total tensors:    {inv['tensor_count']:>8}")
    print(f"Total parameters: {inv['total_parameters']:>20,}")
    print(f"Total on-disk:    {inv['total_storage_bytes']:>20,} bytes  ({inv['total_storage_bytes']/(1024**3):.3f} GiB)")
    print(f"Alignment pad:    {inv['alignment_padding']:>20,} bytes")
    print(f"File size:        {inv['file_size_bytes']:>20,} bytes  ({inv['file_size_bytes']/(1024**3):.3f} GiB)")
    print(f"Validation issues:{len(inv['validation_issues']):>8}")

    print("\n--- Quantization Types ---")
    for tp, s in sorted(inv["type_stats"].items(), key=lambda x: -x[1]["bytes"]):
        pct = s["bytes"] / inv["total_storage_bytes"] * 100
        print(f"  {tp:<8} {s['count']:>5} tensors  {s['params']:>18,} params  "
              f"{s['bytes']:>14,} bytes  ({pct:>5.1f}%)")

    print("\n--- Semantic Categories ---")
    for cat, s in sorted(inv["category_stats"].items(), key=lambda x: -x[1]["bytes"]):
        pct = s["bytes"] / inv["total_storage_bytes"] * 100
        layers_str = f"layers {min(s['layers'])}–{max(s['layers'])}" if s["layers"] else "global"
        print(f"  {cat:<30} {s['count']:>4} tensors  "
              f"{s['bytes']:>14,} bytes  ({pct:>5.1f}%)  {layers_str}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf"

    if not os.path.exists(target):
        print(f"File not found: {target}")
        sys.exit(1)

    inv = build_full_inventory(target, verbose=False)
    print_summary(inv)

    # Write full inventory JSON (without the giant tensors list for the report;
    # write tensors separately for size management)
    out_dir = "reports/phase2c"
    os.makedirs(out_dir, exist_ok=True)

    # Compact tensors for JSON output (drop verbose dims for large arrays)
    compact_tensors = []
    for t in inv["tensors"]:
        compact_tensors.append({
            "name":          t["name"],
            "dims":          t["dims"],
            "type":          t["type"],
            "num_elements":  t["num_elements"],
            "storage_bytes": t["storage_bytes"],
            "gguf_offset":   t["gguf_offset"],
            "abs_offset":    t["abs_offset"],
            "layer_idx":     t["layer_idx"],
            "category":      t["category"],
        })

    placement_data = {
        "file":             os.path.basename(inv["filepath"]),
        "file_size_bytes":  inv["file_size_bytes"],
        "data_offset":      inv["data_offset"],
        "tensor_count":     inv["tensor_count"],
        "total_parameters": inv["total_parameters"],
        "total_storage_bytes": inv["total_storage_bytes"],
        "actual_data_bytes":   inv["actual_data_bytes"],
        "alignment_padding":   inv["alignment_padding"],
        "validation_issues":   inv["validation_issues"],
        "type_stats":          inv["type_stats"],
        "category_stats":      inv["category_stats"],
        "tensors":             compact_tensors,
    }

    out_path = os.path.join(out_dir, "TENSOR_PLACEMENT.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(placement_data, f, indent=2)
    print(f"\nFull tensor inventory written to: {out_path}")
    print(f"({os.path.getsize(out_path):,} bytes)")
