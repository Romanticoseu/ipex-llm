#
# Copyright 2016 The BigDL Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import os
import torch
import transformers
import time
import argparse
import torch.distributed as dist

from fastapi import FastAPI, HTTPException, StreamingResponse
from pydantic import BaseModel
import uvicorn
from ipex_llm.transformers.streamer import BatchTextIteratorStreamer

import asyncio, uuid
from collections import deque
from typing import Dict, List, Optional

from transformers.utils import logging
logger = logging.get_logger(__name__)

from ipex_llm.utils.benchmark_util import BenchmarkWrapper


def get_int_from_env(env_keys, default):
    """Returns the first positive env value found in the `env_keys` list or the default."""
    for e in env_keys:
        val = int(os.environ.get(e, -1))
        if val >= 0:
            return val
    return int(default)

global max_num_seqs
global max_num_batched_tokens

local_rank = get_int_from_env(["LOCAL_RANK","PMI_RANK"], "0")
world_size = get_int_from_env(["WORLD_SIZE","PMI_SIZE"], "1")
os.environ["RANK"] = str(local_rank)
os.environ["WORLD_SIZE"] = str(world_size)
os.environ["MASTER_PORT"] = os.environ.get("MASTER_PORT", "29500")

global model, tokenizer

rest_req_deque = deque(maxlen=128)
request_queue: asyncio.Queue = asyncio.Queue()
result_dict: Dict[str, str] = {}
streamer_dict = {}
empty_req = PromptRequest(prompt="", n_predict=0)

def load_model(model_path, low_bit):

    from ipex_llm import optimize_model

    import torch
    import time
    import argparse

    from transformers import AutoModelForCausalLM  # export AutoModelForCausalLM from transformers so that deepspeed use it
    from transformers import LlamaTokenizer, AutoTokenizer
    import deepspeed
    from deepspeed.accelerator.cpu_accelerator import CPU_Accelerator
    from deepspeed.accelerator import set_accelerator, get_accelerator
    from intel_extension_for_deepspeed import XPU_Accelerator

    # First use CPU as accelerator
    # Convert to deepspeed model and apply IPEX-LLM optimization on CPU to decrease GPU memory usage
    current_accel = CPU_Accelerator()
    set_accelerator(current_accel)
    global model, tokenizer
    model = AutoModelForCausalLM.from_pretrained(model_path,
                                                 device_map={"": "cpu"},
                                                 low_cpu_mem_usage=True,
                                                 torch_dtype=torch.float16,
                                                 trust_remote_code=True,
                                                 use_cache=True)

    model = deepspeed.init_inference(
        model,
        tensor_parallel={"tp_size": world_size},
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
    model = BenchmarkWrapper(model)

    # Modify backend related settings 
    if world_size > 1:
        get_accelerator().set_device(local_rank)
    dist_backend = get_accelerator().communication_backend_name()
    import deepspeed.comm.comm
    deepspeed.comm.comm.cdb = None
    from deepspeed.comm.comm import init_distributed
    init_distributed()

    # Load tokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True, padding_side='left')
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

def generate_text(prompt: List[str], n_predict = 32):
    while prompt[-1] == "":
        prompt = prompt[:-1]
    if isinstance(n_predict, list):
        n_predict = max(n_predict)

    inputs = tokenizer(prompt, return_tensors="pt", padding=True)
    input_ids = inputs.input_ids.to(f'xpu:{local_rank}')
    # print(input_ids)
    attention_mask = inputs.attention_mask.to(f'xpu:{local_rank}')
    output = model.generate(input_ids,
                            attention_mask=attention_mask,
                            max_new_tokens=n_predict,
                            use_cache=True)
    torch.xpu.synchronize()
    return output


async def generate_stream_gate(prompt: List[str], n_predict=32, request_ids=[]):
    while prompt[-1] == "":
        prompt = prompt[:-1]
    if isinstance(n_predict, list):
        n_predict = max(n_predict)

    logger.error(f"prompt: {prompt}")
    inputs = tokenizer(prompt, return_tensors="pt", padding=True)
    input_ids = inputs.input_ids.to(f"xpu:{local_rank}")
    attention_mask = inputs.attention_mask.to(f"xpu:{local_rank}")
    logger.error(f"local_rank: {local_rank}")

    for request_id in request_ids:
        if request_id not in streamer_dict:
            streamer_dict[request_id] = asyncio.Queue()

    streamer = BatchTextIteratorStreamer(
        tokenizer=tokenizer,
        timeout=60,
        skip_prompt=True,
        skip_special_tokens=True,
        batch_size=len(prompt),
    )

    generated_kwargs = dict(
        max_new_tokens=n_predict,
        streamer=streamer,
        attention_mask=attention_mask,
    )

    def model_generate():
        model.generate(input_ids, **generated_kwargs)
        torch.xpu.synchronize()

    t1 = Thread(target=model_generate)
    t1.start()

    partial_output = ""
    rfind_start = 0

    async def put_item(queue, item):
        logger.error(f"Producer: {item}")
        await queue.put(item)
        # await asyncio.sleep(1)

    for i in range(n_predict):
        tasks = []
        output_token = next(streamer)
        for index, request_id in enumerate(request_ids):
            # logger.error(f"request_id: {request_id}")
            task = asyncio.create_task(
                put_item(streamer_dict[request_id], output_token[index])
            )
            tasks.append(task)
        # logger.error(f"output_token: {output_token}")
        await asyncio.gather(*tasks)


class PromptRequest(BaseModel):
    prompt: str
    n_predict: int = 32  

app = FastAPI()

@app.post("/generate/")
async def generate(prompt_request: PromptRequest):
    request_id = str(uuid.uuid4())
    await request_queue.put((request_id, prompt_request))
    while True:
        await asyncio.sleep(0.1)
        if request_id in result_dict:
            output_str = result_dict.pop(request_id)
            return {"generated_text": output_str}


@app.post("/generate_stream/")
async def generate_stream(prompt_request: PromptRequest):
    request_id = str(uuid.uuid4())
    await request_queue.put((request_id, prompt_request))
    while True:
        await asyncio.sleep(0.1)
        if request_id in streamer_dict:
            token_queue = streamer_dict[request_id]
            async def output_generator(token_queue):
                i = 0
                while i < prompt_request.n_predict:
                    if not token_queue.empty():
                        token = await token_queue.get()
                        logger.error(f"Consumer: {token}")
                        # yield token
                        yield json.dumps(
                            {"request_id": request_id, "text": token}
                        ) + "\n"
                    else:
                        await asyncio.sleep(0.001)
                        # continue
                streamer_dict.pop(request_id, None)

            # return StreamingResponse(output_generator(token_queue))
            return StreamingResponse(
                output_generator(token_queue), media_type="application/json"
            )


async def process_requests():
    while True:
        request_ids, prompt_requests = [], []
        cur_batched_tokens = 0

        if local_rank == 0:
            while rest_req_deque:
                request_id, rest_request = rest_req_deque.popleft()
                prompt = rest_request.prompt
                cur_prompt_len = tokenizer(
                    prompt_request.prompt, return_tensors="pt"
                ).input_ids.size(1)
                cur_batched_tokens += cur_prompt_len
                if cur_batched_tokens > max_num_batched_tokens:
                    cur_batched_tokens -= cur_prompt_len
                    rest_req_deque.appendleft((request_id, rest_request))
                    break
                request_ids.append(request_id)
                prompt_requests.append(rest_request)
                if len(prompt_requests) == max_num_seqs:
                    break

            for _ in range(max_num_seqs - len(prompt_requests)):
                if request_queue.empty():
                    break
                request_id, prompt_request = await request_queue.get()
                # import pdb
                # pdb.set_trace()
                cur_prompt_len = tokenizer(
                    prompt_request.prompt, return_tensors="pt"
                ).input_ids.size(1)
                cur_batched_tokens += cur_prompt_len
                if cur_batched_tokens > max_num_batched_tokens:
                    cur_batched_tokens -= cur_prompt_len
                    rest_req_deque.appendleft((request_id, prompt_request))
                    break
                request_ids.append(request_id)
                prompt_requests.append(prompt_request)

        if local_rank == 0 and prompt_requests:
            object_list = prompt_requests
            if len(object_list) < max_num_seqs:
                object_list = object_list + [empty_req] * (
                    max_num_seqs - len(object_list)
                )
            logger.info(
                f"Running: {len(prompt_requests)}, Pending: {request_queue.qsize()}"
            )
            dist.broadcast_object_list(object_list, src=0)
            # start_time = time.time()

            # import pdb
            # pdb.set_trace()

            await generate_stream_gate(
                [req.prompt for req in object_list],
                [req.n_predict for req in object_list],
                request_ids,
            )
            # for request_id in request_ids:
            #     if request_id not in streamer_dict:
            #         streamer_dict[request_id] = asyncio.Queue()
            # async def put_item(queue, item):
            #     # 向队列中放入一个元素
            #     await queue.put(item)
            #     logger.error(f"Put {item} into queue {id(queue)}")
            # tasks = []
            # for item in generator:
            #     for index, request_id in enumerate(request_ids):
            #         tasks.append(put_item(streamer_dict[request_id], item[index]))

            # await asyncio.gather(*tasks)

            # outputs = generate_stream_gate([req.prompt for req in object_list], [req.n_predict for req in object_list])
            # generate_time = time.time() - start_time
            # outputs = outputs.cpu()
            # output_strs = tokenizer.batch_decode(outputs, skip_special_tokens=True)
            # output_strs = output_strs[:len(prompt_requests)]

            # # for request_id, output_str in zip(request_ids, outputs):
            # #     result_dict[request_id] = output_str
            # request_id = request_ids[0]
            # result_dict[request_id] = outputs
            # logger.info(f"First token latency: {model.first_cost}, next token latency: {model.rest_cost_mean}, generate time: {generate_time}")

        await asyncio.sleep(0.1)


@app.on_event("startup")
async def startup_event():
    if local_rank == 0:
        asyncio.create_task(process_requests())


async def main():
    parser = argparse.ArgumentParser(
        description="Predict Tokens using fastapi by leveraging DeepSpeed-AutoTP"
    )
    parser.add_argument(
        "--repo-id-or-model-path",
        type=str,
        default="meta-llama/Llama-2-7b-chat-hf",
        help="The huggingface repo id for the Llama2 (e.g. `meta-llama/Llama-2-7b-chat-hf`, `meta-llama/Llama-2-13b-chat-hf` and `meta-llama/Llama-2-70b-chat-hf`) to be downloaded"
        ", or the path to the huggingface checkpoint folder",
    )
    parser.add_argument(
        "--low-bit",
        type=str,
        default="sym_int4",
        help="The quantization type the model will convert to.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="The port number on which the server will run.",
    )
    parser.add_argument(
        "--max-num-batched-tokens",
        type=int,
        default=4096,
        help="Max tokens can be batched by this service.",
    )
    parser.add_argument(
        "--max-num-seqs",
        type=int,
        default=8,
        help="Max requests can be batched by this service.",
    )

    global max_num_seqs
    global max_num_batched_tokens

    args = parser.parse_args()
    model_path = args.repo_id_or_model_path
    low_bit = args.low_bit
    max_num_seqs = args.max_num_seqs
    max_num_batched_tokens = args.max_num_batched_tokens
    load_model(model_path, low_bit)

    config = uvicorn.Config(app=app, host="0.0.0.0", port=args.port)
    server = uvicorn.Server(config)

    if local_rank == 0:
        await server.serve()
    else:
        while True:
            object_list = [None] * max_num_seqs
            dist.broadcast_object_list(object_list, src=0)
            await generate_stream_gate(
                [req.prompt for req in object_list],
                [req.n_predict for req in object_list],
            )


if __name__ == "__main__":
    asyncio.run(main())
