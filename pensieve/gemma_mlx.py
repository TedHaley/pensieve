"""EmbeddingGemma 2 on Apple's MLX, without torch: the reference is transformers' `EmbeddingGemma2Model` with
sentence-transformers' mean pooling. Its text model, and the encoders that turn an image, a video's frames or a
sound into soft tokens for that text model."""
import json
from pathlib import Path

GEMMA_MAX_TOKENS = 512  # texts are cut here; an image, a video or a sound can be longer (the sliding mask)
TEXT_PREFIX = "language_model."
# The parts of the model's weights, each downloaded on its own into a file beside the model's config: the text model
# (542 of EmbeddingGemma 2's 1,489 MB) always, the image encoder (335 MB, images and video) and the sound encoder
# (611 MB) only when they are switched on.
PARTS = {"text": ("text.safetensors", (TEXT_PREFIX,)),
         "vision": ("vision.safetensors", ("vision_tower.", "embed_vision.")),
         "audio": ("audio.safetensors", ("audio_tower.", "embed_audio."))}
TEXT_WEIGHTS = PARTS["text"][0]
VIDEO_FPS, VIDEO_MAX_FRAMES, VIDEO_FRAME_TOKENS = 1, 32, 140  # the model's video processor: frames and their size
AUDIO_RATE, AUDIO_MAX_SECONDS = 16000, 30  # the model's sound processor: samples per second, and the most it reads


def part_file(folder, part):
    """The file that holds `part` of the model in its folder, or None when it isn't downloaded."""
    for f in (PARTS[part][0], "model.safetensors"):
        if (Path(folder) / f).exists():
            return Path(folder) / f
    return None


def download_text(name):
    """Download the model's config and tokenizer, and only the text part of its weights."""
    return download_part(name, "text")


def download_part(name, part, progress=None):
    """The model's config and tokenizer, and the weights of `part` (PARTS), written to their file beside them. A
    safetensors file starts with a JSON index of where each tensor is, so only those tensors are fetched, as byte
    ranges. progress(done, total) gets the bytes so far."""
    import struct
    from huggingface_hub import get_session, hf_hub_url, snapshot_download
    filename, prefixes = PARTS[part]
    folder = Path(snapshot_download(name, allow_patterns=["*.json", "tokenizer*"]))
    out = folder / filename
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
    keep = sorted(((k, v) for k, v in header.items() if k.startswith(prefixes)),
                  key=lambda kv: kv[1]["data_offsets"][0])
    base, new_header, pos = 8 + n, {}, 0
    for k, v in keep:
        a, b = v["data_offsets"]
        new_header[k] = {**v, "data_offsets": [pos, pos + b - a]}
        pos += b - a
    head = json.dumps(new_header, separators=(",", ":")).encode()
    head += b" " * (-len(head) % 8)  # the data starts 8-byte aligned
    tmp = out.with_suffix(".part")
    done = 0
    with open(tmp, "wb") as f:
        f.write(struct.pack("<Q", len(head)) + head)
        i = 0
        while i < len(keep):  # neighbouring tensors in one request
            a, b = keep[i][1]["data_offsets"]
            j = i + 1
            while j < len(keep) and keep[j][1]["data_offsets"][0] == b and b - a < 16 << 20:
                b = keep[j][1]["data_offsets"][1]
                j += 1
            f.write(get(base + a, base + b - 1))
            done += b - a
            if progress:
                progress(done, pos)
            i = j
    tmp.rename(out)
    return out


if __name__ == "__main__":  # python -m pensieve.gemma_mlx <model> [part]: a download, in a process that may go online
    import socket
    import sys
    _getaddrinfo = socket.getaddrinfo  # IPv4 first: on some networks Python hangs on Hugging Face over IPv6
    socket.getaddrinfo = lambda *a, **k: sorted(_getaddrinfo(*a, **k), key=lambda r: r[0] != socket.AF_INET)
    print(download_part(sys.argv[1], sys.argv[2] if sys.argv[2:] else "text",
                        lambda d, t: print(f"progress {d} {t}", flush=True)))


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
            self.kind = kind = cfg["layer_types"][i]
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

        def embed(self, ids):
            # the embedding scale rounds like the reference's bfloat16 weights: sqrt(512) -> 22.625
            return self.embed_tokens(ids) * mx.array(hidden ** 0.5, dtype=mx.bfloat16).astype(self.embed_tokens.weight.dtype)

        def __call__(self, ids, keep, x=None):
            """x: the input embeddings, when some positions are not tokens (an image's soft tokens)."""
            x = self.embed(ids) if x is None else x
            layer_inputs = self.ple(x)
            n = x.shape[1]
            pos = mx.arange(n, dtype=mx.float32)
            ok = keep[:, None, None, :]  # padding is never attended to
            masks = {"full_attention": mx.where(ok, 0.0, -mx.inf).astype(x.dtype)}
            masks["sliding_attention"] = masks["full_attention"]
            if n > cfg["sliding_window"] + 1:  # a sliding layer sees the tokens at most sliding_window positions away
                near = mx.abs(pos[:, None] - pos[None, :]) <= cfg["sliding_window"]
                masks["sliding_attention"] = mx.where(ok & near[None, None], 0.0, -mx.inf).astype(x.dtype)
            for i, layer in enumerate(self.layers):
                x = layer(x, layer_inputs[:, :, i, :], masks[layer.self_attn.kind], pos)
            return self.embedding_projection(self.norm(x))

    return TextModel()


def _vision(cfg, text_hidden):
    """The image encoder (transformers' Gemma4VisionModel, without clipped linears or standardization) and the
    projection of its pooled patches into the text model's input space (embed_vision)."""
    import mlx.core as mx
    import mlx.nn as nn

    eps, hidden, n_heads, head_dim = cfg["rms_norm_eps"], cfg["hidden_size"], cfg["num_attention_heads"], cfg["head_dim"]
    if cfg.get("use_clipped_linears") or cfg.get("standardize"):
        raise ValueError("this image encoder has clipped linears or standardization, which the MLX port lacks")
    half = head_dim // 2  # each axis (x, y) rotates half of a head
    inv_freq = 1.0 / (cfg["rope_parameters"]["rope_theta"] ** (mx.arange(0, half, 2, dtype=mx.float32) / half))

    def norm(x, weight=None):
        y = x.astype(mx.float32)
        y = y * mx.rsqrt(mx.mean(y * y, axis=-1, keepdims=True) + eps)
        return (y * weight.astype(mx.float32) if weight is not None else y).astype(x.dtype)

    class Norm(nn.Module):
        def __init__(self, dim):
            super().__init__()
            self.weight = mx.ones((dim,))

        def __call__(self, x):
            return norm(x, self.weight)

    class Clippable(nn.Module):  # the reference's ClippableLinear, unclipped: its weight sits under .linear
        def __init__(self, a, b):
            super().__init__()
            self.linear = nn.Linear(a, b, bias=False)

        def __call__(self, x):
            return self.linear(x)

    def rope(x, cos, sin):  # x: (b, n, heads, head_dim); the first half rotates by x position, the second by y
        out = []
        for k in range(2):
            p = x[..., k * half:(k + 1) * half]
            rot = mx.concatenate([-p[..., half // 2:], p[..., :half // 2]], axis=-1)
            out.append(p * cos[k] + rot * sin[k])
        return mx.concatenate(out, axis=-1)

    class Attention(nn.Module):
        def __init__(self):
            super().__init__()
            self.q_proj, self.k_proj = Clippable(hidden, n_heads * head_dim), Clippable(hidden, n_heads * head_dim)
            self.v_proj, self.o_proj = Clippable(hidden, n_heads * head_dim), Clippable(n_heads * head_dim, hidden)
            self.q_norm, self.k_norm = Norm(head_dim), Norm(head_dim)

        def __call__(self, x, cos, sin):
            b, n, _ = x.shape
            q = rope(self.q_norm(self.q_proj(x).reshape(b, n, n_heads, head_dim)), cos, sin).transpose(0, 2, 1, 3)
            k = rope(self.k_norm(self.k_proj(x).reshape(b, n, n_heads, head_dim)), cos, sin).transpose(0, 2, 1, 3)
            v = norm(self.v_proj(x).reshape(b, n, n_heads, head_dim)).transpose(0, 2, 1, 3)
            out = mx.fast.scaled_dot_product_attention(q, k, v, scale=1.0)
            return self.o_proj(out.transpose(0, 2, 1, 3).reshape(b, n, -1))

    class MLP(nn.Module):
        def __init__(self):
            super().__init__()
            self.gate_proj = Clippable(hidden, cfg["intermediate_size"])
            self.up_proj = Clippable(hidden, cfg["intermediate_size"])
            self.down_proj = Clippable(cfg["intermediate_size"], hidden)

        def __call__(self, x):
            return self.down_proj(nn.gelu_approx(self.gate_proj(x)) * self.up_proj(x))

    class Layer(nn.Module):
        def __init__(self):
            super().__init__()
            self.self_attn, self.mlp = Attention(), MLP()
            self.input_layernorm, self.post_attention_layernorm = Norm(hidden), Norm(hidden)
            self.pre_feedforward_layernorm, self.post_feedforward_layernorm = Norm(hidden), Norm(hidden)

        def __call__(self, x, cos, sin):
            x = x + self.post_attention_layernorm(self.self_attn(self.input_layernorm(x), cos, sin))
            return x + self.post_feedforward_layernorm(self.mlp(self.pre_feedforward_layernorm(x)))

    class PatchEmbedder(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_proj = nn.Linear(3 * cfg["patch_size"] ** 2, hidden, bias=False)
            self.position_embedding_table = mx.zeros((2, cfg["position_embedding_size"], hidden))

        def __call__(self, pixels, pos):
            x = self.input_proj((2 * (pixels - 0.5)).astype(self.input_proj.weight.dtype))
            return x + self.position_embedding_table[0][pos[..., 0]] + self.position_embedding_table[1][pos[..., 1]]

    class Encoder(nn.Module):
        def __init__(self):
            super().__init__()
            self.layers = [Layer() for _ in range(cfg["num_hidden_layers"])]

    class VisionTower(nn.Module):
        def __init__(self):
            super().__init__()
            self.patch_embedder, self.encoder = PatchEmbedder(), Encoder()

        def __call__(self, pixels, pos, grid):
            """pixels: (b, patches, 3 * patch²) in 0..1, every image the same size, without padding; pos: each
            patch's (x, y); grid: the image's (rows, columns) of patches. Returns (b, soft tokens, hidden)."""
            x = self.patch_embedder(pixels, pos)
            f = pos[..., None].astype(mx.float32) * inv_freq  # (b, n, 2, half / 2)
            f = mx.concatenate([f, f], axis=-1)[:, :, None]  # (b, n, 1, 2, half), one per axis; the heads broadcast
            cos, sin = mx.cos(f).astype(x.dtype), mx.sin(f).astype(x.dtype)
            cos, sin = [cos[..., k, :] for k in range(2)], [sin[..., k, :] for k in range(2)]
            for layer in self.encoder.layers:
                x = layer(x, cos, sin)
            k, (rows, cols), b = cfg["pooling_kernel_size"], grid, x.shape[0]
            # the mean of each k×k block of patches, scaled by sqrt(hidden), in float32 as the reference does
            x = x.astype(mx.float32).reshape(b, rows // k, k, cols // k, k, hidden).mean(axis=(2, 4))
            return x.reshape(b, -1, hidden) * hidden ** 0.5

    class Projection(nn.Module):  # embed_vision
        def __init__(self):
            super().__init__()
            self.embedding_projection = nn.Linear(hidden, text_hidden, bias=False)

        def __call__(self, x):
            return self.embedding_projection(norm(x).astype(self.embedding_projection.weight.dtype))

    return VisionTower(), Projection()


def _audio(cfg, text_hidden):
    """The sound encoder (transformers' Gemma4AudioModel: two strided convolutions, then a conformer whose attention
    sees the 12 frames up to each frame) and the projection of its frames into the text model's input (embed_audio).
    Each output frame covers 40 ms."""
    import math
    import mlx.core as mx
    import mlx.nn as nn

    eps, hidden, n_heads = cfg["rms_norm_eps"], cfg["hidden_size"], cfg["num_attention_heads"]
    head_dim, span = hidden // n_heads, cfg["attention_context_left"] - 1
    cap = cfg["attention_logit_cap"]
    if cfg["attention_context_right"]:
        raise ValueError("the MLX sound encoder sees only past frames; this model also looks ahead")

    def norm(x, weight=None):
        y = x.astype(mx.float32)
        y = y * mx.rsqrt(mx.mean(y * y, axis=-1, keepdims=True) + eps)
        return (y * weight.astype(mx.float32) if weight is not None else y).astype(x.dtype)

    class Norm(nn.Module):
        def __init__(self, dim):
            super().__init__()
            self.weight = mx.ones((dim,))

        def __call__(self, x):
            return norm(x, self.weight)

    class Clipped(nn.Module):  # ClippableLinear: inputs and outputs clamped to ranges learned in training
        def __init__(self, a, b):
            super().__init__()
            self.linear = nn.Linear(a, b, bias=False)
            self.input_min, self.input_max = mx.array(-mx.inf), mx.array(mx.inf)
            self.output_min, self.output_max = mx.array(-mx.inf), mx.array(mx.inf)

        def __call__(self, x):
            return mx.clip(self.linear(mx.clip(x, self.input_min, self.input_max)), self.output_min, self.output_max)

    class FeedForward(nn.Module):
        def __init__(self):
            super().__init__()
            self.ffw_layer_1, self.ffw_layer_2 = Clipped(hidden, 4 * hidden), Clipped(4 * hidden, hidden)
            self.pre_layer_norm, self.post_layer_norm = Norm(hidden), Norm(hidden)

        def __call__(self, x):
            y = self.ffw_layer_2(nn.silu(self.ffw_layer_1(self.pre_layer_norm(x))))
            return x + self.post_layer_norm(y) * cfg["residual_weight"]

    class Attention(nn.Module):
        def __init__(self):
            super().__init__()
            self.q_proj, self.k_proj = Clipped(hidden, hidden), Clipped(hidden, hidden)
            self.v_proj, self.post = Clipped(hidden, hidden), Clipped(hidden, hidden)
            self.relative_k_proj = nn.Linear(hidden, hidden, bias=False)
            self.per_dim_scale = mx.zeros((head_dim,))

        def __call__(self, x, rel, ok):
            """x: (1, n, hidden); rel: the position encoding of distances span..0; ok: (n, span) which keys count.
            Key j of query i is frame i - j; its relative term uses distance j, row span - j of rel."""
            n = x.shape[1]
            q = self.q_proj(x).astype(mx.float32).reshape(n, n_heads, head_dim)
            q = q * (head_dim ** -0.5 / math.log(2)) * nn.softplus(self.per_dim_scale.astype(mx.float32))
            k = self.k_proj(x).astype(mx.float32).reshape(n, n_heads, head_dim) * (math.log(1 + math.e) / math.log(2))
            v = self.v_proj(x).astype(mx.float32).reshape(n, n_heads, head_dim)
            rk = self.relative_k_proj(rel).astype(mx.float32).reshape(-1, n_heads, head_dim)[::-1][:span]  # distance j
            pad = mx.zeros((span - 1, n_heads, head_dim), mx.float32)
            kp, vp = mx.concatenate([pad, k]), mx.concatenate([pad, v])
            ks = mx.stack([kp[span - 1 - j:span - 1 - j + n] for j in range(span)], 1)  # (n, span, heads, dim)
            vs = mx.stack([vp[span - 1 - j:span - 1 - j + n] for j in range(span)], 1)
            logits = (q[:, None] * ks).sum(-1) + (q[:, None] * rk[None]).sum(-1)  # (n, span, heads)
            logits = mx.tanh(logits / cap) * cap
            logits = mx.where(ok[..., None], logits, cfg["attention_invalid_logits_value"])
            w = mx.softmax(logits, axis=1)
            out = (w[..., None] * vs).sum(1).reshape(1, n, hidden)
            return self.post(out.astype(x.dtype))

    class LightConv(nn.Module):
        def __init__(self):
            super().__init__()
            self.linear_start, self.linear_end = Clipped(hidden, 2 * hidden), Clipped(hidden, hidden)
            self.depthwise_conv1d = nn.Conv1d(hidden, hidden, cfg["conv_kernel_size"], groups=hidden, bias=False)
            self.pre_layer_norm, self.conv_norm = Norm(hidden), Norm(hidden)

        def __call__(self, x):
            y = self.linear_start(self.pre_layer_norm(x))
            y = y[..., :hidden] * mx.sigmoid(y[..., hidden:])  # GLU
            y = mx.pad(y, [(0, 0), (cfg["conv_kernel_size"] - 1, 0), (0, 0)])  # causal: only past frames
            y = self.linear_end(nn.silu(self.conv_norm(self.depthwise_conv1d(y))))
            return x + y

    class Layer(nn.Module):
        def __init__(self):
            super().__init__()
            self.feed_forward1, self.feed_forward2 = FeedForward(), FeedForward()
            self.self_attn, self.lconv1d = Attention(), LightConv()
            self.norm_pre_attn, self.norm_post_attn, self.norm_out = Norm(hidden), Norm(hidden), Norm(hidden)

        def __call__(self, x, rel, ok):
            x = self.feed_forward1(x)
            x = x + self.norm_post_attn(self.self_attn(self.norm_pre_attn(x), rel, ok))
            return self.norm_out(self.feed_forward2(self.lconv1d(x)))

    class ConvLayer(nn.Module):  # a 3x3 convolution with stride 2 over (time, frequency), LayerNorm, ReLU
        def __init__(self, a, b):
            super().__init__()
            self.conv = nn.Conv2d(a, b, 3, stride=2, padding=1, bias=False)
            self.norm = nn.LayerNorm(b, eps=eps, bias=False)

        def __call__(self, x, keep):
            x = x * keep[None, :, None, None]
            return nn.relu(self.norm(self.conv(x.astype(self.conv.weight.dtype)))), keep[::2]

    class Subsample(nn.Module):
        def __init__(self):
            super().__init__()
            c0, c1 = cfg["subsampling_conv_channels"]
            self.layer0, self.layer1 = ConvLayer(1, c0), ConvLayer(c0, c1)
            self.input_proj_linear = nn.Linear(c0 // 4 * c1, hidden, bias=False)

        def __call__(self, mel, keep):
            x, keep = self.layer0(mel[None, :, :, None], keep)  # (1, time, frequency, channels)
            x, keep = self.layer1(x, keep)
            return self.input_proj_linear(x.reshape(1, x.shape[1], -1)), keep

    class AudioTower(nn.Module):
        def __init__(self):
            super().__init__()
            self.subsample_conv_projection = Subsample()
            self.layers = [Layer() for _ in range(cfg["num_hidden_layers"])]
            self.output_proj = nn.Linear(hidden, cfg["output_proj_dims"], bias=True)
            half = hidden // 2
            inv = mx.exp(mx.arange(half, dtype=mx.float32) * -(math.log(10000.0) / max(half - 1, 1)))
            t = mx.arange(span, -1, -1, dtype=mx.float32)[:, None] * inv[None]  # distances span..0
            self._rel = mx.concatenate([mx.sin(t), mx.cos(t)], axis=-1)

        def __call__(self, mel, keep):
            """mel: (frames, 128) log-mel features; keep: (frames,) which are sound. Returns (1, tokens, output)
            and which tokens are sound."""
            x, keep = self.subsample_conv_projection(mel, keep)
            n = x.shape[1]
            back = mx.arange(n)[:, None] - mx.arange(span)[None, :]  # the frame each key is
            ok = (back >= 0) & keep[mx.maximum(back, 0)] & keep[:, None]
            rel = self._rel.astype(x.dtype)
            for layer in self.layers:
                x = layer(x, rel, ok)
            return self.output_proj(x), keep

    class Projection(nn.Module):  # embed_audio
        def __init__(self):
            super().__init__()
            self.embedding_projection = nn.Linear(cfg["output_proj_dims"], text_hidden, bias=False)

        def __call__(self, x):
            return self.embedding_projection(norm(x).astype(self.embedding_projection.weight.dtype))

    return AudioTower(), Projection()


def image_patches(path, patch=16, pool=3, max_tokens=280):
    """An image as the model's processor prepares it: resized with its aspect ratio kept to the largest size whose
    sides are multiples of patch × pool and that has at most max_tokens soft tokens, then cut into patches.
    Returns (patches, positions, (rows, columns)). The resize is PIL's bicubic, as in transformers' PIL processor."""
    from PIL import Image
    if Path(path).suffix.lower() in (".heic", ".heif"):  # PIL can't decode these: macOS converts them
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "image.png"
            subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(out)], capture_output=True,
                           check=True, timeout=60)
            return image_patches(out, patch, pool, max_tokens)
    with Image.open(path) as im:
        im.draft("RGB", (2048, 2048))  # a JPEG decodes at a smaller scale when that is enough: faster
        return picture_patches(im.convert("RGB"), patch, pool, max_tokens)


def picture_patches(im, patch=16, pool=3, max_tokens=280):
    """image_patches for a decoded RGB picture (a PIL image): an image file, or a frame of a video."""
    import math
    import numpy as np
    from PIL import Image
    w, h = im.size
    side, budget = patch * pool, max_tokens * pool * pool * patch * patch
    f = math.sqrt(budget / (w * h))
    th, tw = int(h * f // side) * side, int(w * f // side) * side
    longest = max_tokens * side
    if th == 0:
        th, tw = side, min(int(w // h) * side, longest)
    elif tw == 0:
        tw, th = side, min(int(h // w) * side, longest)
    if (th, tw) != (h, w):
        im = im.resize((tw, th), Image.BICUBIC)
    a = np.asarray(im, np.float32) / 255.0  # (h, w, 3)
    rows, cols = th // patch, tw // patch
    p = a.reshape(rows, patch, cols, patch, 3).transpose(0, 2, 1, 3, 4).reshape(rows * cols, -1)
    gx, gy = np.meshgrid(np.arange(cols), np.arange(rows), indexing="xy")
    return p, np.stack([gx, gy], -1).reshape(-1, 2), (rows, cols)


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
        self.path, self.cfg, self.vision = path, cfg, None

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

    def _load_part(self, part):
        """The encoder and its projection for `part` ("vision" or "audio"), from its downloaded weights."""
        mx = self.mx
        weights = part_file(self.path, part)
        if weights is None:
            raise ValueError(f"the model's {part} part isn't downloaded (python -m pensieve.gemma_mlx <model> {part})")
        text_hidden = self.cfg["text_config"]["hidden_size"]
        tower, proj = (_vision(self.cfg["vision_config"], text_hidden) if part == "vision"
                       else _audio(self.cfg["audio_config"], text_hidden))
        tw, pw = PARTS[part][1]
        w = mx.load(str(weights))
        tower_w = [(k[len(tw):], v) for k, v in w.items() if k.startswith(tw)]
        if part == "audio":  # torch's convolution weights are (out, in, size...); MLX's are (out, size..., in)
            tower_w = [(k, v.transpose(0, 2, 3, 1) if v.ndim == 4 else v.transpose(0, 2, 1) if v.ndim == 3 else v)
                       for k, v in tower_w]
        tower.load_weights(tower_w)
        proj.load_weights([(k[len(pw):], v) for k, v in w.items() if k.startswith(pw)])
        mx.eval(tower.parameters(), proj.parameters())
        return tower, proj

    def _load_vision(self):
        self.vision = self._load_part("vision")

    def encode_audio(self, samples, normalize_embeddings=True):
        """One vector for a sound of at most AUDIO_MAX_SECONDS: mono float samples at AUDIO_RATE. Its frames (one
        per 40 ms) between begin and end tokens, through the text model, mean-pooled."""
        import numpy as np
        mx = self.mx
        if getattr(self, "audio", None) is None:
            self.audio = self._load_part("audio")
        from transformers.models.gemma4.feature_extraction_gemma4 import Gemma4AudioFeatureExtractor
        fe = getattr(self, "_fe", None) or Gemma4AudioFeatureExtractor(**self._fe_config())
        self._fe = fe
        f = fe([np.asarray(samples, np.float32)[:AUDIO_RATE * AUDIO_MAX_SECONDS]], sampling_rate=AUDIO_RATE)  # a batch of 1
        mel, keep = mx.array(f["input_features"][0]), mx.array(f["input_features_mask"][0])
        tower, proj = self.audio
        h, keep = tower(mel, keep)
        soft = proj(h)[0][mx.array(np.flatnonzero(np.array(keep)))]  # the frames that are sound
        c = self.cfg
        e = self.model.embed(mx.array([[self.tok.bos_token_id, c["boa_token_id"], c["eoa_token_index"],
                                        self.tok.eos_token_id]]))[0]
        x = mx.concatenate([e[:2], soft.astype(e.dtype), e[2:]])[None]
        v = self.model(None, mx.ones(x.shape[:2], dtype=mx.bool_), x).astype(mx.float32).mean(axis=1)[0]
        if normalize_embeddings:
            v = v / mx.maximum(mx.linalg.norm(v), 1e-12)
        out = np.array(v)
        mx.clear_cache()
        return out

    def _fe_config(self):
        """The sound feature extractor's settings, from the model's processor_config.json."""
        p = self.path / "processor_config.json"
        fe = dict(json.loads(p.read_text()).get("feature_extractor", {})) if p.exists() else {}
        for k in ("feature_extractor_type", "processor_class"):
            fe.pop(k, None)
        return fe

    def encode_images(self, paths, normalize_embeddings=True):
        """One vector per image file, in the space of the text vectors: the image's soft tokens between its begin
        and end tokens, through the text model, mean-pooled. Each image runs alone: sizes differ."""
        import numpy as np
        mx = self.mx
        if self.vision is None:
            self._load_vision()
        tower, proj = self.vision
        vc, c = self.cfg["vision_config"], self.cfg
        out = np.zeros((len(paths), self.get_sentence_embedding_dimension()), np.float32)
        for i, p in enumerate(paths):
            pixels, pos, grid = image_patches(p, vc["patch_size"], vc["pooling_kernel_size"],
                                              c.get("vision_soft_tokens_per_image", 280))
            soft = proj(tower(mx.array(pixels)[None], mx.array(pos)[None], grid))  # (1, tokens, text hidden)
            ids = mx.array([[self.tok.bos_token_id, c["boi_token_id"], c["eoi_token_id"], self.tok.eos_token_id]])
            e = self.model.embed(ids)
            x = mx.concatenate([e[:, :2], soft.astype(e.dtype), e[:, 2:]], axis=1)
            h = self.model(ids, mx.ones(x.shape[:2], dtype=mx.bool_), x).astype(mx.float32)
            v = h.mean(axis=1)
            if normalize_embeddings:
                v = v / mx.maximum(mx.linalg.norm(v, axis=-1, keepdims=True), 1e-12)
            out[i] = np.array(v[0])
        mx.clear_cache()
        return out

    def encode_video(self, frames, normalize_embeddings=True):
        """One vector for a video, given its frames (RGB PIL images, sampled as VIDEO_FPS and VIDEO_MAX_FRAMES say):
        each frame's soft tokens between begin and end tokens, all frames in order, through the text model."""
        import numpy as np
        mx = self.mx
        if self.vision is None:
            self._load_vision()
        tower, proj = self.vision
        vc, c = self.cfg["vision_config"], self.cfg
        cut = [picture_patches(f, vc["patch_size"], vc["pooling_kernel_size"], VIDEO_FRAME_TOKENS) for f in frames]
        grid = cut[0][2]
        soft = []
        for s in range(0, len(cut), 8):  # 8 frames at a time: attention over 1,260 patches each
            pix = mx.array(np.stack([p for p, _, _ in cut[s:s + 8]]))
            pos = mx.array(np.stack([q for _, q, _ in cut[s:s + 8]]))
            soft.append(proj(tower(pix, pos, grid)))
        soft = mx.concatenate(soft)  # (frames, tokens, text hidden)
        e = self.model.embed(mx.array([[self.tok.bos_token_id, c["boi_token_id"], c["eoi_token_id"],
                                        self.tok.eos_token_id]]))[0]
        parts = [e[:1]]
        for f in soft.astype(e.dtype):
            parts += [e[1:2], f, e[2:3]]
        x = mx.concatenate(parts + [e[3:]])[None]
        h = self.model(None, mx.ones(x.shape[:2], dtype=mx.bool_), x).astype(mx.float32)
        v = h.mean(axis=1)[0]
        if normalize_embeddings:
            v = v / mx.maximum(mx.linalg.norm(v), 1e-12)
        out = np.array(v)
        mx.clear_cache()
        return out
