import struct
import collections

GGML_TYPE_NAMES = {
    0: 'F32', 1: 'F16', 2: 'Q4_0', 3: 'Q4_1', 6: 'Q5_0', 7: 'Q5_1',
    8: 'Q8_0', 9: 'Q8_1', 10: 'Q2_K', 11: 'Q3_K', 12: 'Q4_K', 13: 'Q5_K', 14: 'Q6_K'
}

TYPE_BLOCK_SIZES = {
    'F32': (1, 4), 'F16': (1, 2), 'Q8_0': (32, 34), 'Q4_0': (32, 18), 'Q4_1': (32, 20),
    'Q2_K': (256, 84), 'Q3_K': (256, 110), 'Q4_K': (256, 144), 'Q5_K': (256, 176), 'Q6_K': (256, 210)
}

f = open('models/qwen3-30b/Qwen_Qwen3-30B-A3B-Q4_K_M.gguf', 'rb')
magic = f.read(4)
version, n_tensors, n_kv = struct.unpack('<IQQ', f.read(20))

def read_str():
    length, = struct.unpack('<Q', f.read(8))
    return f.read(length).decode('utf-8', errors='ignore')

def skip_val(vtype):
    if vtype in (0, 1, 7):
        f.seek(1, 1)
    elif vtype in (2, 3):
        f.seek(2, 1)
    elif vtype in (4, 5, 6):
        f.seek(4, 1)
    elif vtype in (10, 11, 12):
        f.seek(8, 1)
    elif vtype == 8:
        read_str()
    elif vtype == 9:
        sub_type, count = struct.unpack('<IQ', f.read(12))
        for _ in range(count):
            skip_val(sub_type)
    else:
        raise ValueError(f"Unknown value type {vtype}")

for _ in range(n_kv):
    k = read_str()
    vt, = struct.unpack('<I', f.read(4))
    skip_val(vt)

tensors = []
for _ in range(n_tensors):
    name = read_str()
    ndims, = struct.unpack('<I', f.read(4))
    dims = struct.unpack(f'<{ndims}Q', f.read(8 * ndims))
    ttype, offset = struct.unpack('<IQ', f.read(12))
    tname = GGML_TYPE_NAMES.get(ttype, f'Type_{ttype}')
    tensors.append((name, dims, tname, offset))

categories = collections.defaultdict(lambda: {'count': 0, 'types': collections.Counter(), 'nelements': 0, 'stored_bytes': 0})

for name, dims, tname, _ in tensors:
    nelem = 1
    for d in dims: nelem *= d
    
    blk_size, type_bytes = TYPE_BLOCK_SIZES.get(tname, (1, 4))
    stored_bytes = (nelem // blk_size) * type_bytes
    
    cat = 'other'
    if 'token_embd' in name: cat = 'embeddings'
    elif 'output.weight' in name: cat = 'output_head'
    elif 'output_norm' in name or 'attn_norm' in name or 'ffn_norm' in name or 'attn_q_norm' in name or 'attn_k_norm' in name: cat = 'layernorms'
    elif 'attn_q' in name or 'attn_k' in name or 'attn_v' in name or 'attn_output' in name: cat = 'attention_weights'
    elif 'ffn_gate_exps' in name or 'ffn_down_exps' in name or 'ffn_up_exps' in name: cat = 'moe_expert_weights'
    elif 'ffn_gate_inp' in name: cat = 'moe_routers'
    elif 'ffn_gate' in name or 'ffn_down' in name or 'ffn_up' in name: cat = 'dense_ffn'
    
    categories[cat]['count'] += 1
    categories[cat]['types'][tname] += 1
    categories[cat]['nelements'] += nelem
    categories[cat]['stored_bytes'] += stored_bytes

total_stored = sum(c['stored_bytes'] for c in categories.values())
total_elements = sum(c['nelements'] for c in categories.values())

print(f"Total Tensors: {len(tensors)}")
print(f"Total Elements: {total_elements:,}")
print(f"Total Stored Size: {total_stored / (1024**3):.2f} GiB ({total_stored:,} bytes)\n")

print(f"{'Category':20} | {'Count':5} | {'Quant Types':22} | {'Elements':12} | {'Stored GiB':10} | {'Placement in Config D'}")
print("-" * 115)

for cat, data in sorted(categories.items()):
    types_str = ', '.join(f'{k}:{v}' for k, v in data['types'].items())
    gib = data['stored_bytes'] / (1024**3)
    placement = "GPU VRAM"
    if cat == 'moe_expert_weights':
        placement = "CPU Host RAM (-ncmoe 40 pinned)"
    elif cat in ('embeddings', 'output_head', 'layernorms', 'attention_weights', 'moe_routers'):
        placement = "GPU VRAM (-ngl 48 offload)"
    print(f"{cat:20} | {data['count']:5} | {types_str:22} | {data['nelements']:12,} | {gib:9.2f} GiB | {placement}")
