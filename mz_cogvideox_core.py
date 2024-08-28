

import os
import torch
import comfy.supported_models
import comfy.model_base
import comfy.ldm.flux.model
import comfy.model_patcher

import comfy.model_management
import folder_paths
import safetensors.torch

from .pipeline_cogvideox import CogVideoXPipeline
from diffusers.models import AutoencoderKLCogVideoX, CogVideoXTransformer3DModel
from diffusers.schedulers import CogVideoXDDIMScheduler


cogVideoXVaeConfig = {
    "act_fn": "silu",
    "block_out_channels": [
        128,
        256,
        256,
        512
    ],
    "down_block_types": [
        "CogVideoXDownBlock3D",
        "CogVideoXDownBlock3D",
        "CogVideoXDownBlock3D",
        "CogVideoXDownBlock3D"
    ],
    "force_upcast": True,
    "in_channels": 3,
    "latent_channels": 16,
    "latents_mean": None,
    "latents_std": None,
    "layers_per_block": 3,
    "mid_block_add_attention": True,
    "norm_eps": 1e-06,
    "norm_num_groups": 32,
    "out_channels": 3,
    "sample_size": 256,
    "scaling_factor": 1.15258426,
    "shift_factor": None,
    "temporal_compression_ratio": 4,
    "up_block_types": [
        "CogVideoXUpBlock3D",
        "CogVideoXUpBlock3D",
        "CogVideoXUpBlock3D",
        "CogVideoXUpBlock3D"
    ],
    "use_post_quant_conv": False,
    "use_quant_conv": False
}
cogVideoXTransformerConfig = {
    "activation_fn": "gelu-approximate",
    "attention_bias": True,
    "attention_head_dim": 64,
    "dropout": 0.0,
    "flip_sin_to_cos": True,
    "freq_shift": 0,
    "in_channels": 16,
    "max_text_seq_length": 226,
    "norm_elementwise_affine": True,
    "norm_eps": 1e-05,
    "num_attention_heads": 30,
    "num_layers": 30,
    "out_channels": 16,
    "patch_size": 2,
    "sample_frames": 49,
    "sample_height": 60,
    "sample_width": 90,
    "spatial_interpolation_scale": 1.875,
    "temporal_compression_ratio": 4,
    "temporal_interpolation_scale": 1.0,
    "text_embed_dim": 4096,
    "time_embed_dim": 512,
    "timestep_activation_fn": "silu"
}

cogVideoXTransformerConfig5B = {
    "activation_fn": "gelu-approximate",
    "attention_bias": True,
    "attention_head_dim": 64,
    "dropout": 0.0,
    "flip_sin_to_cos": True,
    "freq_shift": 0,
    "in_channels": 16,
    "max_text_seq_length": 226,
    "norm_elementwise_affine": True,
    "norm_eps": 1e-05,
    "num_attention_heads": 48,
    "num_layers": 42,
    "out_channels": 16,
    "patch_size": 2,
    "sample_frames": 49,
    "sample_height": 60,
    "sample_width": 90,
    "spatial_interpolation_scale": 1.875,
    "temporal_compression_ratio": 4,
    "temporal_interpolation_scale": 1.0,
    "text_embed_dim": 4096,
    "time_embed_dim": 512,
    "timestep_activation_fn": "silu",
    "use_rotary_positional_embeddings": True
}

cogVideoXDDIMSchedulerConfig = {
    "beta_end": 0.012,
    "beta_schedule": "scaled_linear",
    "beta_start": 0.00085,
    "clip_sample": False,
    "clip_sample_range": 1.0,
    "num_train_timesteps": 1000,
    "prediction_type": "v_prediction",
    "rescale_betas_zero_snr": True,
    "sample_max_value": 1.0,
    "set_alpha_to_one": True,
    "snr_shift_scale": 3.0,
    "steps_offset": 0,
    "timestep_spacing": "linspace",
    "trained_betas": None,
}


def fp8_linear_forward(cls, x):
    original_dtype = cls.weight.dtype
    if original_dtype == torch.float8_e4m3fn or original_dtype == torch.float8_e5m2:
        if len(x.shape) == 3:
            with torch.no_grad():
                if original_dtype == torch.float8_e4m3fn:
                    inn = x.reshape(-1, x.shape[2]).to(torch.float8_e5m2)
                else:
                    inn = x.reshape(-1, x.shape[2]).to(torch.float8_e4m3fn)
                w = cls.weight.t()

                scale_weight = torch.ones(
                    (1), device=x.device, dtype=torch.float32)
                scale_input = scale_weight

                bias = cls.bias.to(
                    torch.float16) if cls.bias is not None else None
                out_dtype = x.dtype if x.dtype in [
                    torch.float16, torch.float16] else torch.float16

                if bias is not None:
                    o = torch._scaled_mm(
                        inn, w, out_dtype=out_dtype, bias=bias, scale_a=scale_input, scale_b=scale_weight)
                else:
                    o = torch._scaled_mm(
                        inn, w, out_dtype=out_dtype, scale_a=scale_input, scale_b=scale_weight)

                if isinstance(o, tuple):
                    o = o[0]

                return o.reshape((-1, x.shape[1], cls.weight.shape[0]))

        else:
            cls.to(torch.float16)
            out = cls.original_forward(x.to(
                torch.float16
            ))
            cls.to(original_dtype)
            return out
    else:
        return cls.original_forward(x)


import torch.nn as nn
from types import MethodType


def convert_fp8_linear(module):
    for name, module in module.named_modules():
        if isinstance(module, nn.Linear):
            module.to(torch.float8_e4m3fn)
            original_forward = module.forward
            setattr(module, "original_forward", original_forward)
            setattr(module, "forward", MethodType(fp8_linear_forward, module))


def MZ_CogVideoXLoader_call(args={}):
    unet_name = args.get("unet_name")

    unet_path = folder_paths.get_full_path("unet", unet_name)

    device = comfy.model_management.get_torch_device()
    offload_device = comfy.model_management.unet_offload_device()
    comfy.model_management.soft_empty_cache()

    unet_sd = safetensors.torch.load_file(unet_path)
    transformerConfig = cogVideoXTransformerConfig5B
    if "transformer_blocks.30" in unet_sd:
        transformerConfig = cogVideoXTransformerConfig5B

    transformer = CogVideoXTransformer3DModel.from_config(
        transformerConfig)

    transformer.load_state_dict(unet_sd)

    dtype = torch.float16
    weight_dtype = args.get("weight_dtype")
    if weight_dtype == "fp8_e4m3fn":
        dtype = torch.float8_e4m3fn
    transformer.to(dtype).to(device)
    if weight_dtype == "fp8_e4m3fn":
        convert_fp8_linear(transformer)

    vae_name = args.get("vae_name")
    vae_path = folder_paths.get_full_path("vae", vae_name)
    vae = AutoencoderKLCogVideoX.from_config(cogVideoXVaeConfig)

    vae_sd = safetensors.torch.load_file(vae_path)
    vae.load_state_dict(vae_sd)
    vae.to(device).to(torch.float16)

    scheduler = CogVideoXDDIMScheduler.from_config(
        cogVideoXDDIMSchedulerConfig)

    pipe = CogVideoXPipeline(vae, transformer, scheduler)

    pipeline = {
        "pipe": pipe,
        "dtype": torch.float16,
        "base_path": os.path.join(
            os.path.dirname(__file__),
            "configs",
        ),
    }
    return (pipeline, )