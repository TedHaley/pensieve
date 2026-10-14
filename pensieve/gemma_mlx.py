"""EmbeddingGemma 2's text model on Apple's MLX, without torch: the reference is transformers'
`EmbeddingGemma2TextModel` with sentence-transformers' mean pooling. Inputs of up to 512 tokens fit in the model's
512-token sliding window, so every layer attends to the whole text."""
import json
from pathlib import Path

GEMMA_MAX_TOKENS = 512  # the sliding window: longer inputs would need the sliding mask
TEXT_WEIGHTS = "text.safetensors"  # beside the model's files: its text part only (download_text)
TEXT_PREFIX = "language_model."


def download_text(name):
    """Download the model's config and tokenizer, and only the text part of its weights: 542 of 1,489 MB for
    EmbeddingGemma 2 (the rest is its image and audio encoders). A safetensors file starts with a JSON index of
    where each tensor is, so the text tensors are fetched as byte ranges and written to TEXT_WEIGHTS."""
    import struct
    from huggingface_hub import get_session, hf_hub_url, snapshot_download
    folder = Path(snapshot_download(name, allow_patterns=["*.json", "tokenizer*"]))
    out = folder / TEXT_WEIGHTS
    if out.exists():
        return out
    url, s = hf_hub_url(name, "model.safetensors"), get_session()

    def get(a, b):  # bytes a..b, inclusive
        r = s.get(url, headers={"Range": f"bytes={a}-{b}"}, timeout=60, follow_redirects=True)
        r.raise_for_status()
        if r.status_code != 206:
            raise OSError("the server ignored the byte range")
        return r.content

    n = struct.unpack("<Q", get(0, 7))[0]
    header = json.loads(get(8, 8 + n - 1))
    keep = sorted(((k, v) for k, v in header.items() if k.startswith(TEXT_PREFIX)),
                  key=lambda kv: kv[1]["data_offsets"][0])
    base, new_header, pos = 8 + n, {}, 0
    for k, v in keep:
        a, b = v["data_offsets"]
        new_header[k] = {**v, "data_offsets": [pos, pos + b - a]}
        pos += b - a
    head = json.dumps(new_header, separators=(",", ":")).encode()
    head += b" " * (-len(head) % 8)  # the data starts 8-byte aligned
    tmp = out.with_suffix(".part")
    with open(tmp, "wb") as f:
        f.write(struct.pack("<Q", len(head)) + head)
        i = 0
        while i < len(keep):  # neighbouring tensors in one request
            a, b = keep[i][1]["data_offsets"]
            j = i + 1
            while j < len(keep) and keep[j][1]["data_offsets"][0] == b and b - a < 64 << 20:
                b = keep[j][1]["data_offsets"][1]
                j += 1
            f.write(get(base + a, base + b - 1))
            i = j
    tmp.rename(out)
    return out


if __name__ == "__main__":  # python -m pensieve.gemma_mlx <model>: the download, in a process that may go online
    import socket
    import sys
    _getaddrinfo = socket.getaddrinfo  # IPv4 first: on some networks Python hangs on Hugging Face over IPv6
    socket.getaddrinfo = lambda *a, **k: sorted(_getaddrinfo(*a, **k), key=lambda r: r[0] != socket.AF_INET)
    print(download_text(sys.argv[1]))


def _model(cfg):
    import mlx.core as mx
    import mlx.nn as nn

    eps, hidden, per_layer = cfg["rms_norm_eps"], cfg["hidden_size"], cfg["hidden_size_per_layer_input"]
    n_layers, n_heads = cfg["num_hidden_layers"], cfg["num_attention_heads"]

    class RMSNorm(nn.Module):  # the weight multiplies (no "1 +" as in Gemma 1-3)
        def __init__(self, dim, scale=True):
            super().__init__()
            if scale:
                self.weight = mx.ones((dim,))
            self.scale = scale

        def __call__(self, x):
            y = x.astype(mx.float32)
            y = y * mx.rsqrt(mx.mean(y * y, axis=-1, keepdims=True) + eps)
            return (y * self.weight.astype(mx.float32) if self.scale else y).astype(x.dtype)

    def gelu(x):
        return nn.gelu_approx(x)

    def layer_cfg(i):
        over = (cfg.get("per_layer_config") or {}).get(f"{i:02d}", {})
        return over.get("head_dim", cfg["head_dim"]), over.get("num_key_value_heads", cfg["num_key_value_heads"])

    class Attention(nn.Module):
        def __init__(self, i):
            super().__init__()
            self.head_dim, self.kv_heads = layer_cfg(i)
            kind = cfg["layer_types"][i]
            theta = cfg["rope_parameters"][kind]["rope_theta"]
            self._inv_freq = 1.0 / (theta ** (mx.arange(0, self.head_dim, 2, dtype=mx.float32) / self.head_dim))
            self.q_proj = nn.Linear(hidden, n_heads * self.head_dim, bias=False)
            self.k_proj = nn.Linear(hidden, self.kv_heads * self.head_dim, bias=False)
            self.v_proj = nn.Linear(hidden, self.kv_heads * self.head_dim, bias=False)
            self.o_proj = nn.Linear(n_heads * self.head_dim, hidden, bias=False)
            self.q_norm, self.k_norm = RMSNorm(self.head_dim), RMSNorm(self.head_dim)
            self.v_norm = RMSNorm(self.head_dim, scale=False)

        def rope(self, x, pos):
            freqs = pos[:, None] * self._inv_freq[None, :]
            emb = mx.concatenate([freqs, freqs], axis=-1)
            cos, sin = mx.cos(emb).astype(x.dtype), mx.sin(emb).astype(x.dtype)
            half = x.shape[-1] // 2
            rot = mx.concatenate([-x[..., half:], x[..., :half]], axis=-1)
            return x * cos + rot * sin

        def __call__(self, x, mask, pos):
            b, n, _ = x.shape
            q = self.q_norm(self.q_proj(x).reshape(b, n, n_heads, self.head_dim)).transpose(0, 2, 1, 3)
            k = self.k_norm(self.k_proj(x).reshape(b, n, self.kv_heads, self.head_dim)).transpose(0, 2, 1, 3)
            v = self.v_norm(self.v_proj(x).reshape(b, n, self.kv_heads, self.head_dim)).transpose(0, 2, 1, 3)
            q, k = self.rope(q, pos), self.rope(k, pos)
            out = mx.fast.scaled_dot_product_attention(q, k, v, scale=1.0, mask=mask)
            return self.o_proj(out.transpose(0, 2, 1, 3).reshape(b, n, -1))

    class MLP(nn.Module):
        def __init__(self):
            super().__init__()
            self.gate_proj = nn.Linear(hidden, cfg["intermediate_size"], bias=False)
            self.up_proj = nn.Linear(hidden, cfg["intermediate_size"], bias=False)
            self.down_proj = nn.Linear(cfg["intermediate_size"], hidden, bias=False)

        def __call__(self, x):
            return self.down_proj(gelu(self.gate_proj(x)) * self.up_proj(x))

    class PLEBlock(nn.Module):
        def __init__(self):
            super().__init__()
            self.per_layer_input_gate = nn.Linear(hidden, per_layer, bias=False)
            self.per_layer_projection = nn.Linear(per_layer, hidden, bias=False)
            self.post_per_layer_input_norm = RMSNorm(hidden)

        def __call__(self, x, layer_input):
            y = self.per_layer_projection(gelu(self.per_layer_input_gate(x)) * layer_input)
            return x + self.post_per_layer_input_norm(y)

    class Layer(nn.Module):
        def __init__(self, i):
            super().__init__()
            self.self_attn, self.mlp, self.ple_block = Attention(i), MLP(), PLEBlock()
            self.input_layernorm, self.post_attention_layernorm = RMSNorm(hidden), RMSNorm(hidden)
            self.pre_feedforward_layernorm, self.post_feedforward_layernorm = RMSNorm(hidden), RMSNorm(hidden)
            self.layer_scalar = mx.ones((1,))

        def __call__(self, x, layer_input, mask, pos):
            x = x + self.post_attention_layernorm(self.self_attn(self.input_layernorm(x), mask, pos))
            x = x + self.post_feedforward_layernorm(self.mlp(self.pre_feedforward_layernorm(x)))
            return self.ple_block(x, layer_input) * self.layer_scalar

    class PLE(nn.Module):
        def __init__(self):
            super().__init__()
            self.per_layer_model_projection = nn.Linear(hidden, n_layers * per_layer, bias=False)
            self.per_layer_projection_norm = RMSNorm(per_layer)

        def __call__(self, x):
            b, n, _ = x.shape
            y = self.per_layer_model_projection(x) * hidden ** -0.5
            return self.per_layer_projection_norm(y.reshape(b, n, n_layers, per_layer))

    class TextModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.embed_tokens = nn.Embedding(cfg["vocab_size"], hidden)
            self.layers = [Layer(i) for i in range(n_layers)]
            self.norm = RMSNorm(hidden)
            self.ple = PLE()
            self.embedding_projection = nn.Linear(hidden, cfg["embedding_dim"], bias=False)

        def __call__(self, ids, keep):
            # the embedding scale rounds like the reference's bfloat16 weights: sqrt(512) -> 22.625
            x = self.embed_tokens(ids) * mx.array(hidden ** 0.5, dtype=mx.bfloat16).astype(self.embed_tokens.weight.dtype)
            layer_inputs = self.ple(x)
            pos = mx.arange(ids.shape[1], dtype=mx.float32)
            mask = mx.where(keep[:, None, None, :], 0.0, -mx.inf).astype(x.dtype)  # padding is never attended to
            for i, layer in enumerate(self.layers):
                x = layer(x, layer_inputs[:, :, i, :], mask, pos)
            return self.embedding_projection(self.norm(x))

    return TextModel()


class MLXGemmaEmbedder:
    """EmbeddingGemma 2's text model on MLX. `encode` takes the arguments that SentenceTransformer.encode takes here."""

    def __init__(self, name, max_tokens=GEMMA_MAX_TOKENS):
        import mlx.core as mx
        from huggingface_hub import snapshot_download
        from transformers import AutoTokenizer
        path = Path(snapshot_download(name, allow_patterns=["*.json", "tokenizer*"]))
        weights = path / TEXT_WEIGHTS if (path / TEXT_WEIGHTS).exists() else path / "model.safetensors"
        if not weights.exists():
            raise ValueError(f"{name}: no weights downloaded (python -m pensieve.gemma_mlx {name})")
        cfg = json.loads((path / "config.json").read_text())
        if cfg.get("model_type") != "embedding_gemma2":
            raise ValueError(f"not an EmbeddingGemma 2 model: {cfg.get('model_type')}")
        st = path / "config_sentence_transformers.json"
        self.prompts = json.loads(st.read_text()).get("prompts", {}) if st.exists() else {}
        self.mx, self.max_tokens = mx, min(max_tokens, GEMMA_MAX_TOKENS)
        self.model = _model(cfg["text_config"])
        self.model.load_weights([(k[len(TEXT_PREFIX):], v) for k, v in mx.load(str(weights)).items()
                                 if k.startswith(TEXT_PREFIX)])
        mx.eval(self.model.parameters())
        self.tok = AutoTokenizer.from_pretrained(str(path))

    def get_sentence_embedding_dimension(self):
        return self.model.embedding_projection.weight.shape[0]

    def encode(self, texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False, prompt=None,
               prompt_name=None):
        import numpy as np
        mx = self.mx
        prompt = prompt if prompt is not None else self.prompts.get(prompt_name, "") if prompt_name else ""
        texts = [prompt + t for t in texts]
        out = np.zeros((len(texts), self.get_sentence_embedding_dimension()), np.float32)
        order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))  # similar lengths in a batch: less padding
        for s in range(0, len(order), batch_size):
            idx = order[s:s + batch_size]
            # lengths padded to a multiple of 64: each new shape builds new GPU kernels (25 -> 43 chunks/s on an M4 Max)
            enc = self.tok([texts[i] for i in idx], padding=True, truncation=True, max_length=self.max_tokens,
                           pad_to_multiple_of=64, return_tensors="np")
            ids, keep = mx.array(enc["input_ids"]), mx.array(enc["attention_mask"].astype(bool))
            h = self.model(ids, keep).astype(mx.float32)
            w = keep.astype(mx.float32)[..., None]
            v = (h * w).sum(axis=1) / mx.maximum(w.sum(axis=1), 1.0)  # mean over the text's tokens, prompt included
            if normalize_embeddings:
                v = v / mx.maximum(mx.linalg.norm(v, axis=-1, keepdims=True), 1e-12)
            out[idx] = np.array(v)
        mx.clear_cache()  # MLX keeps freed buffers for reuse; between calls they only hold memory
        return out
