"""Adapter Merge & GGUF Export — Convert fine-tuned LoRA adapters to Ollama-ready GGUF.

This script performs the final step of the fine-tuning pipeline:
  1. Load the INT4 base model with trained LoRA adapters
  2. Merge adapter weights into base model (ΔW → W + ΔW)
  3. Export merged model to GGUF format via llama.cpp
  4. Quantize GGUF to Q4_K_M (matching existing Ollama deployment)
  5. Generate an Ollama Modelfile for one-command deployment

Merge Mathematics:
  During LoRA training, only the low-rank matrices A ∈ R^{r×d_in} and
  B ∈ R^{d_out×r} are updated. The base weight W_0 stays frozen in INT4.
  At merge time we compute:

    W_merged = dequantize(W_0) + (α/r) · B · A

  where α/r = 64/32 = 2.0 (or α/√r = 64/√32 ≈ 11.3 for RS-LoRA).

  Since B·A ∈ R^{d_out×d_in} is dense (not quantized), we first
  dequantize W_0 to FP16, add the scaled adapter product, then
  re-quantize the merged weight to the target format (Q4_K_M).

  Memory during merge: ~18 GB (FP16 model) + ~18 MB (adapters) = ~18 GB.
  This exceeds 8 GB VRAM, so we use Unsloth's CPU merge path that
  performs the dequantize→add→requantize per-layer sequentially in
  system RAM (needs ~20 GB RAM, no GPU required).

Q4_K_M Quantization:
  Q4_K_M uses a mixed-precision scheme from llama.cpp:
    - Attention weights: Q6_K (6-bit, higher precision for attention)
    - Feed-forward weights: Q4_K (4-bit, acceptable for MLPs)
  This K-quant mixture achieves near-FP16 quality on reasoning tasks
  while fitting in 5.6 GB VRAM. The "M" in Q4_K_M means "medium" —
  balanced between Q4_K_S (small, more aggressive) and Q4_K_L (large,
  less compression on attention).

Usage:
  # Default: merge from GRPO checkpoint, export Q4_K_M GGUF
  python models/finetune/export_gguf.py

  # Custom paths
  python models/finetune/export_gguf.py \
      --adapter-dir models/finetune/checkpoints/grpo \
      --output-dir models/finetune/output \
      --quant-method q4_k_m

  # Create Ollama model after export
  ollama create qwen_vvip:9b-sft-grpo -f models/finetune/output/Modelfile
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
import yaml


def load_config(config_path: str) -> dict:
    """Load the training configuration for export settings."""
    with open(config_path) as f:
        return yaml.safe_load(f)


def merge_and_export_unsloth(
    model_name: str,
    adapter_dir: str,
    output_dir: str,
    max_seq_length: int,
    quant_method: str,
) -> Path:
    """Merge LoRA adapters into base model and export to GGUF via Unsloth.

    Unsloth provides a unified save_pretrained_gguf() method that handles
    the entire pipeline:
      1. Dequantize INT4 base weights to FP16 (layer by layer in CPU RAM)
      2. Compute ΔW = (α/r) · B · A for each LoRA adapter
      3. Add ΔW to dequantized weights: W_merged = W_0 + ΔW
      4. Call llama.cpp quantizer to convert FP16 → target quant format
      5. Write GGUF file with model metadata (architecture, tokenizer, etc.)

    The layer-by-layer approach means peak RAM usage is ~2× single layer
    size rather than 2× full model. For Qwen 9B (40 layers, 225M params/layer):
      Per-layer FP16: 225M × 2 bytes = 450 MB
      Peak RAM: ~1 GB (current + next layer) + overhead ≈ 2 GB above baseline
      Total RAM needed: ~12 GB (base model in 4-bit + working memory)

    Args:
        model_name: HuggingFace model identifier for the base model.
        adapter_dir: Directory containing trained LoRA adapter weights.
        output_dir: Directory to write the GGUF file and Modelfile.
        max_seq_length: Maximum sequence length for the model metadata.
        quant_method: GGUF quantization method (e.g., "q4_k_m").

    Returns:
        Path to the exported GGUF file.
    """
    import torch
    from unsloth import FastLanguageModel

    print(f"[MERGE] Loading base model: {model_name}")
    print(f"[MERGE] Loading adapter from: {adapter_dir}")

    # Load base model in 4-bit (same as training)
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=model_name,
        max_seq_length=max_seq_length,
        dtype=None,
        load_in_4bit=True,
    )

    # Load the trained LoRA adapter on top
    from peft import PeftModel
    model = PeftModel.from_pretrained(model, adapter_dir)
    print("[MERGE] Adapter loaded, merging weights...")

    # Export to GGUF with quantization
    # Unsloth handles: dequant base → add LoRA → requant to GGUF
    os.makedirs(output_dir, exist_ok=True)

    print(f"[EXPORT] Exporting to GGUF with quantization={quant_method}")
    print(f"[EXPORT] Output directory: {output_dir}")
    print("[EXPORT] This may take 10-20 minutes (CPU-bound merge + quantization)...")

    model.save_pretrained_gguf(
        output_dir,
        tokenizer,
        quantization_method=quant_method,
    )

    # Find the generated GGUF file
    gguf_files = list(Path(output_dir).glob("*.gguf"))
    if not gguf_files:
        print("[ERROR] No GGUF file generated. Check Unsloth/llama.cpp installation.")
        sys.exit(1)

    gguf_path = gguf_files[0]
    size_gb = gguf_path.stat().st_size / (1024 ** 3)
    print(f"[EXPORT] GGUF file: {gguf_path} ({size_gb:.2f} GB)")

    return gguf_path


def generate_modelfile(
    gguf_path: Path,
    output_dir: str,
    system_prompt_source: str | None,
    ollama_model_name: str,
) -> Path:
    """Generate an Ollama Modelfile for the fine-tuned GGUF model.

    The Modelfile tells Ollama how to load and configure the model.
    We reuse the system prompt from the existing Modelfile.qwen_vvip
    to maintain consistency with the production deployment.

    Args:
        gguf_path: Path to the GGUF model file.
        output_dir: Directory to write the Modelfile.
        system_prompt_source: Path to existing Modelfile to extract system prompt.
        ollama_model_name: Name for the Ollama model (e.g., "qwen_vvip:9b-sft-grpo").

    Returns:
        Path to the generated Modelfile.
    """
    # Extract system prompt from existing Modelfile if available
    system_prompt = ""
    if system_prompt_source and os.path.exists(system_prompt_source):
        with open(system_prompt_source) as f:
            content = f.read()
        # Extract SYSTEM block
        in_system = False
        system_lines = []
        for line in content.splitlines():
            if line.startswith('SYSTEM """'):
                in_system = True
                system_lines.append(line.removeprefix('SYSTEM """'))
                continue
            if in_system:
                if line.rstrip() == '"""':
                    in_system = False
                    continue
                system_lines.append(line)
        system_prompt = "\n".join(system_lines).strip()
        print(f"[MODELFILE] Extracted system prompt from {system_prompt_source}")
    else:
        print("[MODELFILE] No source Modelfile found, using minimal system prompt")
        system_prompt = (
            "You are the VVIP Convoy Orchestration Agent, the autonomous reasoning "
            "engine of a real-time traffic management platform. Always call tools "
            "for data — never fabricate numbers."
        )

    # Resolve GGUF path relative to the Modelfile location
    gguf_abs = str(gguf_path.resolve()).replace("\\", "/")

    modelfile_content = f"""# Ollama Modelfile — Fine-Tuned VVIP Convoy Agent
# Generated by export_gguf.py
# Model: Qwen 3.5 9B, fine-tuned with CPT → SFT → GRPO
# Quantization: Q4_K_M (~5.6 GB VRAM)
#
# Deploy:
#   ollama create {ollama_model_name} -f {output_dir}/Modelfile

FROM {gguf_abs}

SYSTEM \"\"\"{system_prompt}\"\"\"

# Inference parameters (tuned for convoy orchestration reasoning)
PARAMETER num_ctx 8192
PARAMETER num_predict 2048
PARAMETER temperature 0.6
PARAMETER top_p 0.85
PARAMETER top_k 35
PARAMETER repeat_penalty 1.15
PARAMETER presence_penalty 0.3
PARAMETER frequency_penalty 0.2
PARAMETER num_gpu 99
PARAMETER num_thread 8
PARAMETER mirostat 0
PARAMETER stop "<|endoftext|>"
PARAMETER stop "<|im_end|>"

TEMPLATE \"\"\"{{{{ if .System }}}}<|im_start|>system
{{{{ .System }}}}<|im_end|>
{{{{ end }}}}{{{{ if .Prompt }}}}<|im_start|>user
{{{{ .Prompt }}}}<|im_end|>
{{{{ end }}}}<|im_start|>assistant
{{{{ .Response }}}}<|im_end|>
\"\"\"
"""

    modelfile_path = Path(output_dir) / "Modelfile"
    with open(modelfile_path, "w") as f:
        f.write(modelfile_content)

    print(f"[MODELFILE] Written to {modelfile_path}")
    return modelfile_path


def register_with_ollama(modelfile_path: Path, ollama_model_name: str) -> bool:
    """Register the GGUF model with Ollama via `ollama create`.

    This is optional — the user can also run the command manually.
    We attempt it and report success/failure without blocking.

    Args:
        modelfile_path: Path to the Modelfile.
        ollama_model_name: Name for the Ollama model.

    Returns:
        True if registration succeeded, False otherwise.
    """
    try:
        result = subprocess.run(
            ["ollama", "create", ollama_model_name, "-f", str(modelfile_path)],
            capture_output=True,
            text=True,
            timeout=300,  # 5 min timeout for large models
        )
        if result.returncode == 0:
            print(f"[OLLAMA] Model registered as '{ollama_model_name}'")
            print(f"[OLLAMA] Test with: ollama run {ollama_model_name}")
            return True
        else:
            print(f"[OLLAMA] Registration failed: {result.stderr.strip()}")
            print(f"[OLLAMA] Register manually: ollama create {ollama_model_name} -f {modelfile_path}")
            return False
    except FileNotFoundError:
        print("[OLLAMA] ollama CLI not found. Install Ollama and run:")
        print(f"         ollama create {ollama_model_name} -f {modelfile_path}")
        return False
    except subprocess.TimeoutExpired:
        print("[OLLAMA] Registration timed out. Run manually:")
        print(f"         ollama create {ollama_model_name} -f {modelfile_path}")
        return False


@click.command()
@click.option(
    "--config", "config_path",
    default="models/finetune/config.yaml",
    type=click.Path(exists=True),
    help="Path to training configuration YAML",
)
@click.option(
    "--adapter-dir",
    default=None,
    type=click.Path(exists=True),
    help="Override adapter directory (default: last GRPO checkpoint from config)",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(),
    help="Override output directory (default: from config export section)",
)
@click.option(
    "--quant-method",
    default=None,
    type=click.Choice(["q4_k_m", "q4_k_s", "q5_k_m", "q5_k_s", "q8_0", "f16"]),
    help="GGUF quantization method (default: from config)",
)
@click.option(
    "--register-ollama/--no-register-ollama",
    default=True,
    help="Attempt to register model with Ollama after export",
)
def main(
    config_path: str,
    adapter_dir: str | None,
    output_dir: str | None,
    quant_method: str | None,
    register_ollama: bool,
) -> None:
    """Merge LoRA adapters and export fine-tuned model to GGUF for Ollama.

    Takes the trained LoRA adapter from the GRPO phase, merges it into
    the Qwen 3.5 9B base model, quantizes to Q4_K_M, and generates an
    Ollama-compatible Modelfile for deployment.
    """
    print("=" * 70)
    print("  VVIP Convoy Agent — Adapter Merge & GGUF Export")
    print("=" * 70)

    # Load config
    cfg = load_config(config_path)
    export_cfg = cfg.get("export", {})
    model_cfg = cfg["model"]

    # Resolve paths with config defaults
    if adapter_dir is None:
        # Default: use the GRPO output (final phase)
        adapter_dir = cfg.get("grpo", {}).get(
            "output_dir", "models/finetune/checkpoints/grpo"
        )
        # If GRPO wasn't run, fall back to SFT
        if not os.path.exists(adapter_dir):
            adapter_dir = cfg.get("sft", {}).get(
                "output_dir", "models/finetune/checkpoints/sft"
            )
        print(f"[CONFIG] Using adapter from: {adapter_dir}")

    if output_dir is None:
        output_dir = export_cfg.get("output_dir", "models/finetune/output")

    if quant_method is None:
        quant_method = export_cfg.get("quantization_method", "q4_k_m")

    print(f"[CONFIG] Adapter directory: {adapter_dir}")
    print(f"[CONFIG] Output directory:  {output_dir}")
    print(f"[CONFIG] Quantization:      {quant_method}")

    # Verify adapter exists
    adapter_config = Path(adapter_dir) / "adapter_config.json"
    if not adapter_config.exists():
        print(f"[ERROR] No adapter found at {adapter_dir}")
        print(f"        Expected: {adapter_config}")
        print(f"        Run train_qwen_vvip.py first to generate adapters.")
        sys.exit(1)

    # Step 1: Merge adapters and export GGUF
    gguf_path = merge_and_export_unsloth(
        model_name=model_cfg["name"],
        adapter_dir=adapter_dir,
        output_dir=output_dir,
        max_seq_length=model_cfg["max_seq_length"],
        quant_method=quant_method,
    )

    # Step 2: Generate Modelfile
    system_prompt_source = export_cfg.get(
        "modelfile_template", "models/Modelfile.qwen_vvip"
    )
    ollama_model_name = export_cfg.get(
        "ollama_model_name", "qwen_vvip:9b-sft-grpo"
    )

    modelfile_path = generate_modelfile(
        gguf_path=gguf_path,
        output_dir=output_dir,
        system_prompt_source=system_prompt_source,
        ollama_model_name=ollama_model_name,
    )

    # Step 3: Register with Ollama (optional)
    if register_ollama:
        print("\n[OLLAMA] Attempting to register model...")
        register_with_ollama(modelfile_path, ollama_model_name)

    # Summary
    print("\n" + "=" * 70)
    print("  EXPORT COMPLETE")
    print("=" * 70)
    print(f"  GGUF file:    {gguf_path}")
    print(f"  Modelfile:    {modelfile_path}")
    print(f"  Quant method: {quant_method}")
    print(f"  Model name:   {ollama_model_name}")
    print()
    print("  Deploy to Ollama:")
    print(f"    ollama create {ollama_model_name} -f {modelfile_path}")
    print(f"    ollama run {ollama_model_name}")
    print("=" * 70)


if __name__ == "__main__":
    main()
