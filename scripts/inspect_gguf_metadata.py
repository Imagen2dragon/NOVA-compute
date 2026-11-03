"""
NOVA Compute — GGUF Metadata and Tensor Inspector (Pure Python)
Extracts GGUF key-value metadata, tensor inventory, and type breakdown without external dependencies.
"""

import sys
import os
import struct
import json

GGUF_TYPE_UINT8 = 0
GGUF_TYPE_INT8 = 1
GGUF_TYPE_UINT16 = 2
GGUF_TYPE_INT16 = 3
GGUF_TYPE_UINT32 = 4
GGUF_TYPE_INT32 = 5
GGUF_TYPE_FLOAT32 = 6
GGUF_TYPE_BOOL = 7
GGUF_TYPE_STRING = 8
GGUF_TYPE_ARRAY = 9
GGUF_TYPE_UINT64 = 10
GGUF_TYPE_INT64 = 11
GGUF_TYPE_FLOAT64 = 12

GGML_TYPE_NAMES = {
    0: "F32",
    1: "F16",
    2: "Q4_0",
    3: "Q4_1",
    6: "Q5_0",
    7: "Q5_1",
    8: "Q8_0",
    9: "Q8_1",
    10: "Q2_K",
    11: "Q3_K",
    12: "Q4_K",
    13: "Q5_K",
    14: "Q6_K",
    15: "Q8_K",
    16: "IQ2_XXS",
    17: "IQ2_XS",
    18: "IQ3_XXS",
    19: "IQ1_S",
    20: "IQ4_NL",
    21: "IQ3_S",
    22: "IQ2_S",
    23: "IQ4_XS",
    24: "I8",
    25: "I16",
    26: "I32",
    27: "I64",
    28: "F64",
    29: "IQ1_M",
    30: "BF16"
}

def read_str(f):
    l = struct.unpack("<Q", f.read(8))[0]
    return f.read(l).decode("utf-8", errors="replace")

def read_val(f, vtype):
    if vtype == GGUF_TYPE_UINT8:
        return struct.unpack("<B", f.read(1))[0]
    elif vtype == GGUF_TYPE_INT8:
        return struct.unpack("<b", f.read(1))[0]
    elif vtype == GGUF_TYPE_UINT16:
        return struct.unpack("<H", f.read(2))[0]
    elif vtype == GGUF_TYPE_INT16:
        return struct.unpack("<h", f.read(2))[0]
    elif vtype == GGUF_TYPE_UINT32:
        return struct.unpack("<I", f.read(4))[0]
    elif vtype == GGUF_TYPE_INT32:
        return struct.unpack("<i", f.read(4))[0]
    elif vtype == GGUF_TYPE_FLOAT32:
        return struct.unpack("<f", f.read(4))[0]
    elif vtype == GGUF_TYPE_BOOL:
        return bool(struct.unpack("<B", f.read(1))[0])
    elif vtype == GGUF_TYPE_STRING:
        return read_str(f)
    elif vtype == GGUF_TYPE_UINT64:
        return struct.unpack("<Q", f.read(8))[0]
    elif vtype == GGUF_TYPE_INT64:
        return struct.unpack("<q", f.read(8))[0]
    elif vtype == GGUF_TYPE_FLOAT64:
        return struct.unpack("<d", f.read(8))[0]
    elif vtype == GGUF_TYPE_ARRAY:
        elem_type = struct.unpack("<I", f.read(4))[0]
        array_len = struct.unpack("<Q", f.read(8))[0]
        # For large arrays (like tokens), do not store all elements in JSON
        if array_len > 100:
            # Skip or partially sample
            items = []
            for i in range(array_len):
                v = read_val(f, elem_type)
                if i < 5:
                    items.append(v)
            return {"_array_len": array_len, "sample": items}
        else:
            return [read_val(f, elem_type) for _ in range(array_len)]
    else:
        raise ValueError(f"Unknown value type: {vtype}")

def inspect_gguf(filepath):
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return None

    print(f"Inspecting GGUF file: {filepath} ({os.path.getsize(filepath):,} bytes)")
    with open(filepath, "rb") as f:
        magic = f.read(4)
        if magic != b"GGUF":
            raise ValueError(f"Invalid magic: {magic}")
        version = struct.unpack("<I", f.read(4))[0]
        tensor_count = struct.unpack("<Q", f.read(8))[0]
        metadata_kv_count = struct.unpack("<Q", f.read(8))[0]

        metadata = {}
        for _ in range(metadata_kv_count):
            k = read_str(f)
            vtype = struct.unpack("<I", f.read(4))[0]
            val = read_val(f, vtype)
            metadata[k] = val

        tensors = []
        type_counts = {}
        type_params = {}
        total_param_count = 0

        for _ in range(tensor_count):
            t_name = read_str(f)
            n_dims = struct.unpack("<I", f.read(4))[0]
            dims = [struct.unpack("<Q", f.read(8))[0] for _ in range(n_dims)]
            t_type = struct.unpack("<I", f.read(4))[0]
            t_offset = struct.unpack("<Q", f.read(8))[0]

            num_elements = 1
            for d in dims:
                num_elements *= d
            total_param_count += num_elements

            type_name = GGML_TYPE_NAMES.get(t_type, f"TYPE_{t_type}")
            type_counts[type_name] = type_counts.get(type_name, 0) + 1
            type_params[type_name] = type_params.get(type_name, 0) + num_elements

            tensors.append({
                "name": t_name,
                "n_dims": n_dims,
                "dims": dims,
                "type": type_name,
                "offset": t_offset,
                "params": num_elements
            })

    result = {
        "file": os.path.basename(filepath),
        "file_size_bytes": os.path.getsize(filepath),
        "gguf_version": version,
        "tensor_count": tensor_count,
        "metadata_kv_count": metadata_kv_count,
        "total_parameters": total_param_count,
        "type_counts": type_counts,
        "type_params": type_params,
        "metadata": {k: v for k, v in metadata.items() if not k.endswith("tokens") and not k.endswith("scores") and not k.endswith("merges")}
    }
    return result, tensors

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "models/qwen3-1.7b/Qwen3-1.7B-Q8_0.gguf"
    res, tensors = inspect_gguf(target)
    print("Metadata summary:")
    for k, v in res["metadata"].items():
        if isinstance(v, (int, float, str, bool)):
            print(f"  {k}: {v}")
    print(f"\nTotal Tensors: {res['tensor_count']}")
    print(f"Total Parameters: {res['total_parameters']:,}")
    print("Tensor Types Breakdown:")
    for tname, cnt in res["type_counts"].items():
        params = res["type_params"][tname]
        print(f"  {tname}: {cnt} tensors ({params:,} params, {params/res['total_parameters']*100:.1f}%)")
