import os
import torch
import transformers
import deepspeed

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

def get_int_from_env(env_keys, default):
    """Returns the first positive env value found in the `env_keys` list or the default."""
    for e in env_keys:
        val = int(os.environ.get(e, -1))
        if val >= 0:
            return val
    return int(default)

local_rank = get_int_from_env(["LOCAL_RANK","PMI_RANK"], "0")
world_size = get_int_from_env(["WORLD_SIZE","PMI_SIZE"], "1")
os.environ["RANK"] = str(local_rank)
os.environ["WORLD_SIZE"] = str(world_size)
os.environ["MASTER_PORT"] = os.environ.get("MASTER_PORT", "29500")

from ipex_llm import optimize_model

import torch
import time
import argparse

from transformers import AutoModelForCausalLM  # export AutoModelForCausalLM from transformers so that deepspeed use it
from transformers import LlamaTokenizer, AutoTokenizer
from deepspeed.accelerator.cpu_accelerator import CPU_Accelerator
from deepspeed.accelerator import set_accelerator, get_accelerator
from intel_extension_for_deepspeed import XPU_Accelerator
import torch.distributed as dist

model_path = "/mnt/disk1/models/Llama-2-7b-chat-hf"

low_bit = "sym_int4"

# First use CPU as accelerator
# Convert to deepspeed model and apply IPEX-LLM optimization on CPU to decrease GPU memory usage
current_accel = CPU_Accelerator()
set_accelerator(current_accel)
model = AutoModelForCausalLM.from_pretrained(model_path,
                                                device_map={"": "cpu"},
                                                low_cpu_mem_usage=True,
                                                torch_dtype=torch.float16,
                                                trust_remote_code=True,
                                                use_cache=True)

model = deepspeed.init_inference(
    model,
    mp_size=world_size,
    dtype=torch.bfloat16,
    replace_method="auto",
)

# Use IPEX-LLM `optimize_model` to convert the model into optimized low bit format
# Convert the rest of the model into float16 to reduce allreduce traffic
model = optimize_model(model.module.to(f'cpu'), low_bit=low_bit).to(torch.float16)

# Next, use XPU as accelerator to speed up inference
current_accel = XPU_Accelerator()
set_accelerator(current_accel)

# Move model back to xpu
model = model.to(f'xpu:{local_rank}')

# Modify backend related settings 
if world_size > 1:
    get_accelerator().set_device(local_rank)
dist_backend = get_accelerator().communication_backend_name()
import deepspeed.comm.comm
deepspeed.comm.comm.cdb = None
from deepspeed.comm.comm import init_distributed
init_distributed()

# Load tokenizer
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)

def generate_text(prompt: str, n_predict: int = 32):
    input_ids = tokenizer.encode(prompt, return_tensors="pt").to(f'xpu:{local_rank}')
    output = model.generate(input_ids,
                            max_new_tokens=n_predict,
                            use_cache=True)
    torch.xpu.synchronize()
    return output


class PromptRequest(BaseModel):
    prompt: str
    n_predict: int = 32  

app = FastAPI()

@app.post("/generate/")
async def generate(prompt_request: PromptRequest):
    if local_rank == 0:
        object_list = [prompt_request]
        dist.broadcast_object_list(object_list, src=0)
        output = generate_text(object_list[0].prompt, object_list[0].n_predict)
        output = output.cpu()
        output_str = tokenizer.decode(output[0], skip_special_tokens=True)
        return {"generated_text": output_str}

if __name__ == "__main__":
    if local_rank == 0:
        uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        while True:
            object_list = [None]
            dist.broadcast_object_list(object_list, src=0)
            output = generate_text(object_list[0].prompt, object_list[0].n_predict)
