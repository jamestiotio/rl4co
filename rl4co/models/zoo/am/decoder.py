from dataclasses import dataclass

import torch
import torch.nn as nn

from einops import rearrange

from rl4co.models.nn.attention import LogitAttention
from rl4co.models.nn.env_embeddings import env_context_embedding, env_dynamic_embedding
from rl4co.models.nn.utils import decode_probs
from rl4co.utils.ops import batchify, select_start_nodes, unbatchify


@dataclass
class PrecomputedCache:
    node_embeddings: torch.Tensor
    graph_context: torch.Tensor
    glimpse_key: torch.Tensor
    glimpse_val: torch.Tensor
    logit_key: torch.Tensor


class Decoder(nn.Module):
    """Auto-regressive decoder for the Attention Model for constructing solutions
    We additionally include support for greedy multi-starts during inference (as in POMO)

    Args:
        env: Environment to solve
        embedding_dim: Dimension of the embeddings
        num_heads: Number of heads for the attention
    """

    def __init__(self, env, embedding_dim, num_heads, **logit_attn_kwargs):
        super(Decoder, self).__init__()

        self.env = env
        self.embedding_dim = embedding_dim
        self.num_heads = num_heads

        assert embedding_dim % num_heads == 0

        self.context = env_context_embedding(
            self.env.name, {"embedding_dim": embedding_dim}
        )
        self.dynamic_embedding = env_dynamic_embedding(
            self.env.name, {"embedding_dim": embedding_dim}
        )

        # For each node we compute (glimpse key, glimpse value, logit key) so 3 * embedding_dim
        self.project_node_embeddings = nn.Linear(
            embedding_dim, 3 * embedding_dim, bias=False
        )
        self.project_fixed_context = nn.Linear(embedding_dim, embedding_dim, bias=False)

        # MHA
        self.logit_attention = LogitAttention(
            embedding_dim, num_heads, **logit_attn_kwargs
        )

    def forward(
        self,
        td,
        embeddings,
        decode_type="sampling",
        softmax_temp=None,
        num_starts=None,
        calc_reward=True,
    ):
        # Greedy multi-start decoding if num_starts > 1
        num_starts = 0 if num_starts is None else num_starts
        assert not (
            "multistart" in decode_type and num_starts <= 1
        ), "Multi-start decoding requires `num_starts` > 1"

        # Compute keys, values for the glimpse and keys for the logits once as they can be reused in every step
        cached_embeds = self._precompute(embeddings, num_starts=num_starts)

        # Collect outputs
        outputs = []
        actions = []

        # Multi-start decoding: first action is chosen by ad-hoc node selection
        if num_starts > 1 or "multistart" in decode_type:
            action = select_start_nodes(td, num_starts, self.env)

            # Expand td to batch_size * num_starts
            td = batchify(td, num_starts)

            td.set("action", action)
            td = self.env.step(td)["next"]
            log_p = torch.zeros_like(
                td["action_mask"], device=td.device
            )  # first log_p is 0, so p = log_p.exp() = 1

            outputs.append(log_p)
            actions.append(action)

        # Main decoding
        while not td["done"].all():
            log_p, mask = self._get_log_p(cached_embeds, td, softmax_temp, num_starts)

            # Select the indices of the next nodes in the sequences, result (batch_size) long
            action = decode_probs(log_p.exp(), mask, decode_type=decode_type)

            td.set("action", action)
            td = self.env.step(td)["next"]

            # Collect output of step
            outputs.append(log_p)
            actions.append(action)

        outputs, actions = torch.stack(outputs, 1), torch.stack(actions, 1)
        if calc_reward:
            td.set("reward", self.env.get_reward(td, actions))

        return outputs, actions, td

    def _precompute(self, embeddings, num_starts=0):
        # The projection of the node embeddings for the attention is calculated once up front
        (
            glimpse_key_fixed,
            glimpse_val_fixed,
            logit_key_fixed,
        ) = self.project_node_embeddings(embeddings).chunk(3, dim=-1)

        # Batchify and unbatchify have no effect if num_starts = 0.
        # Otherwise, we need to batchify the embeddings to modify key value (i.e. for the lenght of queries)
        graph_context = unbatchify(
            batchify(self.project_fixed_context(embeddings.mean(1)), num_starts),
            num_starts,
        )

        # Organize in a dataclass for easy access
        cached_embeds = PrecomputedCache(
            node_embeddings=embeddings,
            graph_context=graph_context,
            glimpse_key=glimpse_key_fixed,
            glimpse_val=glimpse_val_fixed,
            logit_key=logit_key_fixed,
        )

        return cached_embeds

    def _get_log_p(self, cached, td, softmax_temp=None, num_starts=0):
        # Compute the query based on the context (computes automatically the first and last node context)

        # Unbatchify to [batch_size, num_starts, ...]. Has no effect if num_starts = 0
        td_unbatch = unbatchify(td, num_starts)

        step_context = self.context(cached.node_embeddings, td_unbatch)
        glimpse_q = step_context + cached.graph_context
        glimpse_q = glimpse_q.unsqueeze(1) if glimpse_q.ndim == 2 else glimpse_q

        # Compute keys and values for the nodes
        (
            glimpse_key_dynamic,
            glimpse_val_dynamic,
            logit_key_dynamic,
        ) = self.dynamic_embedding(td_unbatch)
        glimpse_k = cached.glimpse_key + glimpse_key_dynamic
        glimpse_v = cached.glimpse_val + glimpse_val_dynamic
        logit_k = cached.logit_key + logit_key_dynamic

        # Get the mask
        mask = ~td_unbatch["action_mask"]

        # Compute logits
        log_p = self.logit_attention(
            glimpse_q, glimpse_k, glimpse_v, logit_k, mask, softmax_temp
        )

        # Now we need to reshape the logits and log_p to [batch_size*num_starts, num_nodes]
        # Note that rearranging order is important here
        log_p = rearrange(log_p, "b s l -> (s b) l") if num_starts > 1 else log_p
        mask = rearrange(mask, "b s l -> (s b) l") if num_starts > 1 else mask
        return log_p, mask
