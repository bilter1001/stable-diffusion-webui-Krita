import os
import threading

from modules.paths import script_path

import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image

import signal

from ldm.util import instantiate_from_config

from modules.shared import opts, cmd_opts, state
import modules.shared as shared
import modules.ui
from modules.ui import plaintext_to_html
import modules.scripts
import modules.processing as processing
import modules.sd_hijack
import modules.gfpgan_model as gfpgan
import modules.realesrgan_model as realesrgan
#import modules.esrgan_model as esrgan
import modules.images as images
import modules.lowvram
from modules.txt2img import *
from modules.img2img import *
from urllib import request
from flask import Flask, Response, request, send_file, jsonify
from flask_ngrok import run_with_ngrok
import json
from io import BytesIO
import base64
from modules.sd_samplers import samplers, samplers_for_img2img,samplers_k_diffusion


shared.sd_upscalers = {
    "RealESRGAN": lambda img: realesrgan.upscale_with_realesrgan(img, 2, 0),
    "Lanczos": lambda img: img.resize((img.width*2, img.height*2), resample=images.LANCZOS),
    "None": lambda img: img
}
#esrgan.load_models(cmd_opts.esrgan_models_path)
realesrgan.setup_realesrgan()
gfpgan.setup_gfpgan()


def load_model_from_config(config, ckpt, verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    model.eval()
    return model

cached_images = {}

def run_extras(image, gfpgan_strength, upscaling_resize, extras_upscaler_1, extras_upscaler_2, extras_upscaler_2_visibility):
    processing.torch_gc()

    image = image.convert("RGB")

    outpath = opts.outdir_samples or opts.outdir_extras_samples

    if gfpgan.have_gfpgan is not None and gfpgan_strength > 0:
        restored_img = gfpgan.gfpgan_fix_faces(np.array(image, dtype=np.uint8))
        res = Image.fromarray(restored_img)

        if gfpgan_strength < 1.0:
            res = Image.blend(image, res, gfpgan_strength)

        image = res

    if upscaling_resize != 1.0:
        def upscale(image, scaler_index, resize):
            small = image.crop((image.width // 2, image.height // 2, image.width // 2 + 10, image.height // 2 + 10))
            pixels = tuple(np.array(small).flatten().tolist())
            key = (resize, scaler_index, image.width, image.height) + pixels

            c = cached_images.get(key)
            if c is None:
                upscaler = shared.sd_upscalers[scaler_index]
                c = upscaler.upscale(image, image.width * resize, image.height * resize)
                cached_images[key] = c

            return c

        res = upscale(image, extras_upscaler_1, upscaling_resize)

        if extras_upscaler_2 != 0 and extras_upscaler_2_visibility>0:
            res2 = upscale(image, extras_upscaler_2, upscaling_resize)
            res = Image.blend(res, res2, extras_upscaler_2_visibility)

        image = res

    while len(cached_images) > 2:
        del cached_images[next(iter(cached_images.keys()))]

    images.save_image(image, outpath, "", None, '', opts.samples_format, short_filename=True, no_prompt=True)

    return image, '', ''


def run_pnginfo(image):
    info = ''
    for key, text in image.info.items():
        info += f"""
<div>
<p><b>{plaintext_to_html(str(key))}</b></p>
<p>{plaintext_to_html(str(text))}</p>
</div>
""".strip()+"\n"

    if len(info) == 0:
        message = "Nothing found in the image."
        info = f"<div><p>{message}<p></div>"

    return '', '', info


queue_lock = threading.Lock()




try:
    # this silences the annoying "Some weights of the model checkpoint were not used when initializing..." message at start.

    from transformers import logging

    logging.set_verbosity_error()
except Exception:
    pass

sd_config = OmegaConf.load(cmd_opts.config)
shared.sd_model = load_model_from_config(sd_config, cmd_opts.ckpt)
shared.sd_model = (shared.sd_model if cmd_opts.no_half else shared.sd_model.half())

if cmd_opts.lowvram or cmd_opts.medvram:
    modules.lowvram.setup_for_low_vram(shared.sd_model, cmd_opts.medvram)
else:
    shared.sd_model = shared.sd_model.to(shared.device)

modules.sd_hijack.model_hijack.hijack(shared.sd_model)

modules.scripts.load_scripts(os.path.join(script_path, "scripts"))


# make the program just exit at ctrl+c without waiting for anything
def sigint_handler(sig, frame):
    print(f'Interrupted with singal {sig} in {frame}')
    os._exit(0)


signal.signal(signal.SIGINT, sigint_handler)

app = Flask(__name__)
@app.route("/api/test")
def processTest():
    data={'prompt': 'test', 'mode': 'txt2img', 'initimage': {'image': '', 'mask': ''}, 'steps': 30, 'sampler': 'LMS', 'mask_blur': 4, 'inpainting_fill': 'latent noise', 'use_gfpgan': False, 'batch_count': 1, 'cfg_scale': 5.0, 'denoising_strength': 1.0, 'seed': -1, 'height': 512, 'width': 512, 'resize_mode': 0, 'upscaler': 'RealESRGAN', 'upscale_overlap': 64, 'inpaint_full_res': True, 'inpainting_mask_invert': 0} 
    oimages, oinfo, ohtml = txt2img(data['prompt'],'',data['steps'],2,data['use_gfpgan'],data['batch_count'],1,data['cfg_scale'],data['seed'],data['height'],data['width'],0)
    b64images = []
    for img in oimages:
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue())
        b64images.append(img_str.decode())
    return jsonify({'images':b64images,'info':oinfo})



@app.route("/api/", methods=["POST"])
def processAPI():
    r = request
    data = json.loads(r.data)
    print(data['mode'])

    oimages = []
    oinfo = []
    smp_index=0
    for i in range(0,len(samplers_k_diffusion)):
        if samplers_k_diffusion[i][0]=="LMS": smp_index=i    
    if data['mode'] == 'txt2img':
        oimages, oinfo, ohtml = txt2img(data['prompt'],'',data['steps'],smp_index,data['use_gfpgan'],data['batch_count'],1,data['cfg_scale'],data['seed'],data['height'],data['width'],0)

    if data['mode'] == 'img2img':
        switch_mode = 0
        buffer = BytesIO(base64.b64decode(data['initimage']['image']))
        initimg = Image.open(buffer)
        oimages, oinfo, ohtml = img2img(data['prompt'],\
                                initimg,\
                                {'image':'', 'mask':''},\
                                data['steps'],\
                                smp_index,\
                                data['mask_blur'],\
                                data['inpainting_fill'],\
                                data['use_gfpgan'],\
                                switch_mode, \
                                data['batch_count'], \
                                1, \
                                data['cfg_scale'],\
                                data['denoising_strength'],\
                                data['seed'],\
                                data['height'],\
                                data['width'],\
                                data['resize_mode'],\
                                data['upscaler'],\
                                data['upscale_overlap'],\
                                data['inpaint_full_res'],\
                                data['inpainting_mask_invert'],\
                                0)

    if data['mode'] == 'inpainting':

        buffer = BytesIO(base64.b64decode(data['initimage']['image']))
        initimg = Image.open(buffer)
        buffer = BytesIO(base64.b64decode(data['initimage']['mask']))
        initmask = Image.open(buffer)
        switch_mode = 1
        oimages, oinfo, ohtml = img2img(data['prompt'],\
                                initimg,\
                                {'image':initimg, 'mask':initmask},\
                                data['steps'],\
                                smp_index,\
                                data['mask_blur'],\
                                data['inpainting_fill'],\
                                data['use_gfpgan'],\
                                switch_mode, \
                                data['batch_count'], \
                                1, \
                                data['cfg_scale'],\
                                data['denoising_strength'],\
                                data['seed'],\
                                data['height'],\
                                data['width'],\
                                data['resize_mode'],\
                                data['upscaler'],\
                                data['upscale_overlap'],\
                                data['inpaint_full_res'],\
                                data['inpainting_mask_invert'],\
                                0)

    b64images = []
    for img in oimages:
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue())
        b64images.append(img_str.decode())

    return jsonify({'images':b64images,'info':oinfo})

if cmd_opts.share: run_with_ngrok(app)
app.run()
