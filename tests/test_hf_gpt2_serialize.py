import tempfile

import equinox
import fsspec
import jax
import jax.numpy as jnp
import jax.random as jrandom
import numpy as onp
from fsspec import AbstractFileSystem
from jax.random import PRNGKey
from test_utils import skip_if_no_torch
from transformers import AutoModelForCausalLM
from transformers import GPT2Config as HfGpt2Config
from transformers import GPT2LMHeadModel as HfGpt2LMHeadModel

import haliax as hax
from haliax import Axis
from levanter.compat.hf_checkpoints import HFCheckpointConverter, RepoRef
from levanter.models.gpt2 import Gpt2Config, Gpt2LMHeadModel
from levanter.models.loss import next_token_loss
from levanter.trainer import OptimizerConfig


@skip_if_no_torch
def test_hf_gpt2_roundtrip():
    _roundtrip_compare_gpt2_checkpoint("gpt2", None)


@skip_if_no_torch
def test_mistral_gpt2_roundtrip():
    _roundtrip_compare_gpt2_checkpoint("stanford-crfm/expanse-gpt2-small-x777", "checkpoint-60000")


def _rand_input(key: PRNGKey, seq_len: int, vocab_size) -> jnp.ndarray:
    return jrandom.randint(key, (seq_len,), 0, vocab_size)


def _roundtrip_compare_gpt2_checkpoint(model_id, revision):
    import torch

    converter = Gpt2Config.default_hf_checkpoint_converter

    torch_model: HfGpt2LMHeadModel = AutoModelForCausalLM.from_pretrained(model_id, revision=revision)
    torch_model.eval()

    model = converter.load_pretrained(Gpt2LMHeadModel, RepoRef(model_id, revision=revision))

    input = hax.random.randint(PRNGKey(0), model.Pos, 0, model.Vocab.size)

    # we compare softmaxes because the numerics are wonky and we usually just care about the softmax
    torch_out = torch_model(torch.from_numpy(onp.array(input.array)).to(torch.int32).unsqueeze(0))
    torch_out = torch_out.logits[0].detach().cpu().numpy()
    torch_out = jax.nn.softmax(torch_out, axis=-1)

    attn_mask = hax.nn.attention.causal_mask(model.Pos, model.config.KeyPos)

    def compute(input):
        return hax.nn.softmax(model(input, inference=True, key=None, attn_mask=attn_mask), axis=model.Vocab)

    compute = jax.jit(compute)
    jax_out = compute(input).array
    assert torch_out.shape == jax_out.shape, f"{torch_out.shape} != {jax_out.shape}"
    assert onp.isclose(torch_out, onp.array(jax_out), rtol=1e-2, atol=1e-2).all(), f"{torch_out} != {jax_out}"

    with tempfile.TemporaryDirectory() as tmpdir:
        converter.save_pretrained(model, tmpdir)

        torch_model2: HfGpt2LMHeadModel = AutoModelForCausalLM.from_pretrained(tmpdir)
        torch_model2.eval()

        torch_out2 = torch_model2(torch.from_numpy(onp.array(input.array)).to(torch.int32).unsqueeze(0))
        torch_out2 = torch_out2.logits[0].detach().cpu().numpy()
        torch_out2 = jax.nn.softmax(torch_out2, axis=-1)
        assert onp.isclose(torch_out2, onp.array(jax_out), rtol=1e-2, atol=1e-2).all(), f"{torch_out2} != {jax_out}"


# Gradient tests


@skip_if_no_torch
def test_hf_gradient():
    _compare_gpt2_checkpoint_gradients("gpt2", None)


def _compare_gpt2_checkpoint_gradients(model_id, revision):
    import torch

    converter = Gpt2Config.default_hf_checkpoint_converter
    torch_model: HfGpt2LMHeadModel = AutoModelForCausalLM.from_pretrained(model_id, revision=revision)
    torch_model.eval()

    model = converter.load_pretrained(Gpt2LMHeadModel, RepoRef(model_id, revision))

    input = hax.random.randint(PRNGKey(0), model.Pos, 0, model.Vocab.size)

    def torch_loss(model, input_ids) -> torch.Tensor:
        return model(input_ids, labels=input_ids)[0]

    torch_out = torch_loss(torch_model, torch.from_numpy(onp.array(input.array)).to(torch.int64).unsqueeze(0))
    causal_mask = hax.nn.attention.causal_mask(model.config.Pos, model.config.KeyPos)

    def compute_loss(model, input_ids):
        pred_y = model(input_ids, key=None, inference=True, attn_mask=causal_mask)

        return next_token_loss(model.Pos, model.Vocab, pred_y, input_ids).scalar()

    jax_compute_grad = jax.value_and_grad(compute_loss)
    jax_loss, jax_grad = jax_compute_grad(model, input)

    # gradients are kind of a pain to get at in torch, but we do it anyway
    torch_out.backward()
    state_dict = torch_model.transformer.state_dict(keep_vars=True)
    state_dict = {k: v.grad for k, v in state_dict.items()}

    jax_grad: Gpt2LMHeadModel

    jax_grad_dict = jax_grad.to_state_dict()

    for jax_key, jax_g in jax_grad_dict.items():
        if jax_key not in state_dict:
            assert jax_key == "token_out_embeddings"
            continue

        torch_g = state_dict[jax_key]
        assert onp.isclose(jax_g, torch_g.detach().cpu().numpy(), rtol=1e-2, atol=1e-2).all(), f"{jax_g} != {torch_g}"

    # now we also want to check that the optimizers do similar things
    optimizer_config = OptimizerConfig(weight_decay=0.0, learning_rate=1e-3, warmup_ratio=0.0, lr_schedule="constant")

    if optimizer_config.max_grad_norm is not None:
        torch.nn.utils.clip_grad_norm_(torch_model.parameters(), optimizer_config.max_grad_norm)
    torch_optimizer = torch.optim.AdamW(
        torch_model.parameters(),
        lr=optimizer_config.learning_rate,
        weight_decay=optimizer_config.weight_decay,
        betas=(optimizer_config.beta1, optimizer_config.beta2),
        eps=optimizer_config.epsilon,
    )

    torch_optimizer.step()

    jax_optimizer = optimizer_config.build(1000)
    state = jax_optimizer.init(model)
    updates, state = jax_optimizer.update(updates=jax_grad, state=state, params=model)
    new_model = equinox.apply_updates(model, updates)

    new_model_dict = new_model.to_state_dict()
    state_dict = torch_model.transformer.state_dict(keep_vars=True)

    # now compare new params
    for key, jax_p in new_model_dict.items():
        if key not in state_dict:
            assert key == "token_out_embeddings"
            continue
        torch_p = state_dict[key]
        assert onp.isclose(
            jax_p, torch_p.detach().cpu().numpy(), rtol=1e-3, atol=2e-3
        ).all(), f"{key}: {onp.linalg.norm(jax_p - torch_p.detach().cpu().numpy(), ord=onp.inf)}"


def test_hf_save_to_fs_spec():
    Vocab = Axis("Vocab", 128)
    config = Gpt2Config(hidden_dim=32, num_heads=2, num_layers=2)
    simple_model = Gpt2LMHeadModel.init(Vocab, config, key=PRNGKey(0))
    converter = HFCheckpointConverter(Gpt2Config, "gpt2", HfGpt2Config, ignore_prefix="transformer")

    converter.save_pretrained(simple_model, "memory://model")

    with tempfile.TemporaryDirectory() as tmpdir:

        # now copy the model to tmp because loading from memory doesn't work
        fs: AbstractFileSystem = fsspec.filesystem("memory")
        fs.get("model/", f"{tmpdir}/test", recursive=True)

        loaded_model = converter.load_pretrained(Gpt2LMHeadModel, ref=f"{tmpdir}/test")

        simple_dict = simple_model.to_state_dict()
        loaded_dict = loaded_model.to_state_dict()

        assert simple_dict.keys() == loaded_dict.keys()

        for key, simple_p in simple_dict.items():
            loaded_p = loaded_dict[key]
            assert onp.allclose(simple_p, loaded_p), f"{key}: {onp.linalg.norm(simple_p - loaded_p, ord=onp.inf)}"


# TODO: would be nice to have a test that tests hf upload?
