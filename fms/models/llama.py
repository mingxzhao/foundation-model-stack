import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

import fms.utils
from fms.modules.attention import MultiHeadAttention
from fms.modules.embedding import WordEmbedding
from fms.modules.feedforward import GatedLinearUnit
from fms.modules.layernorm import LayerNormParameterized
from fms.modules.positions import RotaryEmbedding
from fms.utils.activation import str_to_activation
from fms.utils.config import ModelConfig
from fms.utils.tokenizers import get_tokenizer


# params emb_dim heads layers lr
#  7B    4096    32    32     3.0E-04
# 13B    5120    40    40     3.0E-04
# 33B    6656    52    60     1.5.E-04
# 65B    8192    64    80     1.5.E-04


@dataclass
class LLaMAConfig(ModelConfig):
    src_vocab_size: int = 32_000  # can be set by tokenizer
    emb_dim: int = 4096
    norm_eps: float = 1e-6
    nheads: int = 32
    kvheads: int = 0
    nlayers: int = 32
    pad_id: int = -1
    hidden_grow_factor: float = 8 / 3
    multiple_of: float = 256
    activation_fn: str = "swish"
    p_dropout: float = 0.0
    max_expected_seq_len: int = 2048


class LLaMABlock(nn.Module):
    def __init__(self, config: LLaMAConfig, rotary_emb: RotaryEmbedding):
        super(LLaMABlock, self).__init__()
        self.config = config
        emb_kq = self.config.emb_dim // self.config.nheads
        emb_v = self.config.emb_dim // self.config.nheads

        self.ln = LayerNormParameterized(
            self.config.emb_dim,
            elementwise_scale=True,
            elementwise_shift=False,
            use_mean=False,
            eps=self.config.norm_eps,
            use_high_precision_pow=True,
        )
        self.ff_ln = LayerNormParameterized(
            self.config.emb_dim,
            elementwise_scale=True,
            elementwise_shift=False,
            use_mean=False,
            eps=self.config.norm_eps,
            use_high_precision_pow=True,
        )

        if self.config.kvheads == 0:
            kvheads = self.config.nheads
        else:
            assert self.config.nheads % self.config.kvheads == 0

        self.attn = MultiHeadAttention(
            self.config.emb_dim,
            emb_kq,
            emb_v,
            self.config.nheads,
            kvheads,
            p_dropout=self.config.p_dropout,
            use_bias=False,
            position_encoder=rotary_emb,
        )
        self.ff_sub_layer = GatedLinearUnit(
            self.config.emb_dim,
            hidden_grow_factor=self.config.hidden_grow_factor,
            multiple_of=self.config.multiple_of,
            activation_fn=fms.utils.str_to_activation(self.config.activation_fn),
            p_dropout=self.config.p_dropout,
            use_bias=False,
        )

        if self.config.p_dropout != 0:
            self.dropout = nn.Dropout(self.config.p_dropout)

    def forward(
        self,
        x,
        *,
        mask=None,
        past_key_value_state=None,
        use_cache=False,
        is_causal_mask=False,
        attn_algorithm=None,
    ):
        # if the cache is not empty, we need to get the kv cache for self and cross attention
        self_attn_past_key_value = past_key_value_state
        # if past_key_value_state is not None:
        #     self_attn_past_key_value = past_key_value_state[:2]
        # else:
        #     self_attn_past_key_value = None

        # first we do MHA and Add&Norm
        residual = x
        x = self.ln(x)
        x = self.attn(
            q=x,
            k=x,
            v=x,
            mask=mask,
            attn_algorithm=attn_algorithm,
            past_key_value_state=self_attn_past_key_value,
            use_cache=use_cache,
            is_self=True,
            is_causal_mask=is_causal_mask,
        )
        cache = None
        if use_cache:
            x, cache = x
        if self.config.p_dropout != 0:
            x = self.dropout(x)
        # residual connection
        x = x + residual

        # then we do FF and Add&Norm
        residual = x
        x = self.ff_ln(x)
        x = self.ff_sub_layer(x)
        if self.config.p_dropout != 0:
            x = self.dropout(x)
        # another residual
        x = x + residual

        if use_cache:
            return (x, cache)
        else:
            return x

class LLaMAStack(nn.Module):

    def __init__(self, config: LLaMAConfig):
        super().__init__()
        self.config = config

        self.rot_emb = RotaryEmbedding(
            self.config.emb_dim // self.config.nheads,
            self.config.max_expected_seq_len * 2,
        )

        self.stack = nn.ModuleList(
            [LLaMABlock(self.config, self.rot_emb) for _ in range(self.config.nlayers)]
        )

    def forward(
        self,
        x_in,
        mask=None,
        past_key_value_states=None,
        use_cache=False,
        attn_algorithm=None
    ):
        # Embed the given vocabulary indices using the given attention mask, with pre-/post-norm and dropout as specified
        # x_in: batch_size x seq_len
        # mask: batch_size x seq_len x seq_len
        # bias: nheads x seq_len x seq_len
        if past_key_value_states is None:
            past_key_value_states = [None for _ in range(len(self.stack))]

        qlen = x_in.size(1)
        klen = x_in.size(1)

        # if we are using the cache, the key length needs to be extended with the past keys length
        if use_cache and past_key_value_states[0] is not None:
            klen += past_key_value_states[0][0].size(-2)

        # if mask is none, we need to specify causal mask
        if mask is None:
            # we are caching and can assume all 1s in the mask
            if use_cache and klen != 1 and qlen == 1:
                # b x h x qlen x kvlen
                is_causal_mask = False
            else:
                is_causal_mask = True
        else:
            is_causal_mask = False

        x_in = self.shared(x_in)

        # this is the output cache for all the decoder layers
        present_key_value_states = []

        for i, layer in enumerate(self.stack):
            output = layer(
                x=x_in,
                mask=mask,
                past_key_value_state=past_key_value_states[i],
                use_cache=use_cache,
                is_causal_mask=is_causal_mask,
                attn_algorithm=attn_algorithm,
            )

            if use_cache:
                x_in, present_key_value_state = output
                present_key_value_states.append(present_key_value_state)

            else:
                x_in = output

        dec_out = x_in
        dec_out = self.dec_norm(dec_out)
        if self.p_dropout:
            dec_out = self.dropout(dec_out)

        return dec_out, present_key_value_states


class LLaMA(nn.Module):
    def __init__(
        self,
        config: Optional[LLaMAConfig] = None,
        **kwargs,
    ):
        super(LLaMA, self).__init__()
        if config is not None:
            self.config = config
        elif len(kwargs) != 0:
            self.config = LLaMAConfig()
            self.config.update_config(**kwargs)
        else:
            raise RuntimeError("need to specify either a config or kwargs")

        self.p_dropout = self.config.p_dropout
        self.width = self.config.emb_dim
        self.pad_id = self.config.pad_id
        self.max_expected_seq_len = self.config.max_expected_seq_len

        self.shared = WordEmbedding(
            self.config.src_vocab_size,
            self.config.emb_dim,
            padding_idx=self.config.pad_id,
            abs_pos=False,
            reversible=True,
            tie_weights=False,
            bias=False,
        )

        self.stack = LLaMAStack(config)

        self.dec_norm = LayerNormParameterized(
            self.config.emb_dim,
            elementwise_scale=True,
            elementwise_shift=False,
            use_mean=False,
            eps=self.config.norm_eps,
            use_high_precision_pow=True,
        )

        if self.p_dropout:
            self.dropout = nn.Dropout(self.config.p_dropout)

        self.reset_params()

    def get_config(self) -> LLaMAConfig:
        return self.config

    @classmethod
    def from_config(cls, config: LLaMAConfig) -> "LLaMA":
        config_dict = config.as_dict()
        config_dict["activation_fn"] = str_to_activation(config.activation_fn)
        return cls(**config_dict)

    def reset_params(self):
        # Modules are self-initializing, we're just going to down-scale the final prediction head to be
        # mixed-fan (inputs and gradients scale to the same inverse factors) if it isn't tied
        self.shared.head.weight.data.normal_(
            0, 1 / math.sqrt(math.sqrt(self.width * self.shared.vocab_size))
        )

    def forward(
        self,
        x,
        mask=None,
        past_key_value_states=None,
        use_cache=False,
        only_last_token=False,
        attn_algorithm=None,
    ):
        output, cache = self.stack(x, mask, past_key_value_states, use_cache, attn_algorithm)

        if only_last_token:
            output = output[:, -1, :]
        preds = self.shared(output, reverse=True)

        if use_cache:
            return preds, cache
        else:
            return preds

def _rename_weights_to_fms(orig_sd):
    replacements = [
        (r"^tok_embeddings", "shared.emb"),
        (r"^norm", "dec_norm"),
        (r"^output", "shared.head"),
        (r"^layers", "dec_process"),
        (r"\.attention\.", ".attn."),
        (r"attn\.wq", "attn.query"),
        (r"attn\.wk", "attn.key"),
        (r"attn\.wv", "attn.value"),
        (r"attn\.wo", "attn.dense"),
        (r"attention_norm", "ln"),
        (r"feed_forward\.w1", "ff_sub_layer.wg"),
        (r"feed_forward\.w2", "ff_sub_layer.w2"),
        (r"feed_forward\.w3", "ff_sub_layer.w1"),
        (r"ffn_norm", "ff_ln"),
    ]
    new_sd = {}
    for name, param in orig_sd.items():
        new_name = name
        for pattern, repl in replacements:
            new_name = re.sub(pattern, repl, new_name)
        new_sd[new_name] = param

    return new_sd

def load_fms_llama(model_path: str, tokenizer_path: str):
    # from llama.tokenizer import Tokenizer
    model_path = os.path.expanduser(model_path)
    tokenizer_path = os.path.expanduser(tokenizer_path)

    # Load tokenizer
    tokenizer = get_tokenizer(tokenizer_path)

    # Load Llama model from Meta's weights
    checkpoints = sorted(Path(model_path).glob("*.pth"))
    world_size = os.getenv("WORLD_SIZE", 1)
    local_rank = os.getenv("LOCAL_RANK", 0)

    assert world_size == len(
        checkpoints
    ), f"Loading a checkpoint for MP={len(checkpoints)} but world size is {world_size}"

    ckpt_path = checkpoints[local_rank]
    checkpoint_sd = torch.load(ckpt_path, map_location="cpu")
    with open(Path(model_path) / "params.json", "r") as f:
        params = json.loads(f.read())
    hidden_grow_factor = 8 / 3
    if "ffn_dim_multiplier" in params:
        hidden_grow_factor = hidden_grow_factor * params["ffn_dim_multiplier"]

    # IBM LLaMa
    fms_sd = _rename_weights_to_fms(checkpoint_sd)
    torch.set_default_tensor_type(torch.cuda.HalfTensor)
    ibm_model = LLaMA(
        src_vocab_size=tokenizer.vocab_size(),
        emb_dim=params["dim"],
        nheads=params["n_heads"],
        nlayers=params["n_layers"],
        hidden_grow_factor=hidden_grow_factor,
        multiple_of=params["multiple_of"],
        norm_eps=params["norm_eps"],
    )
    torch.set_default_tensor_type(torch.FloatTensor)
    ibm_model.load_state_dict(
        fms_sd, strict=False
    )  # the meta weights have some extra stuff

    return ibm_model, tokenizer