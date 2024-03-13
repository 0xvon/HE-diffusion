#Modified by Yaojian Chen

"""SAMPLING ONLY."""

import torch
import numpy as np
from tqdm import tqdm
from functools import partial
import tenseal as ts
import time
import math
import cv2
from PIL import Image
from imwatermark import WatermarkEncoder
from einops import rearrange
from torchvision.utils import make_grid
from ldm.coo_sparse import COOSparseTensor, convert_dense_to_coo
from ldm.distortion import remove_points, count_zeros
import copy
import os


from ldm.modules.diffusionmodules.util import make_ddim_sampling_parameters, make_ddim_timesteps, noise_like

def put_watermark(img, wm_encoder=None):
    if wm_encoder is not None:
        img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        img = wm_encoder.encode(img, 'dwtDct')
        img = Image.fromarray(img[:, :, ::-1])
    return img

class ENC_PLMSSampler(object):
    def __init__(self, model, schedule="linear", **kwargs):
        super().__init__()
        self.model = model
        self.ddpm_num_timesteps = model.num_timesteps
        self.schedule = schedule

    def register_buffer(self, name, attr):
        if type(attr) == torch.Tensor:
            if attr.device != torch.device("cuda"):
                attr = attr.to(torch.device("cuda"))
        setattr(self, name, attr)

    def make_schedule(self, ddim_num_steps, ddim_discretize="uniform", ddim_eta=0., verbose=True):
        if ddim_eta != 0:
            raise ValueError('ddim_eta must be 0 for PLMS')
        self.ddim_timesteps = make_ddim_timesteps(ddim_discr_method=ddim_discretize, num_ddim_timesteps=ddim_num_steps,
                                                  num_ddpm_timesteps=self.ddpm_num_timesteps,verbose=verbose)
        alphas_cumprod = self.model.alphas_cumprod
        assert alphas_cumprod.shape[0] == self.ddpm_num_timesteps, 'alphas have to be defined for each timestep'
        to_torch = lambda x: x.clone().detach().to(torch.float32).to(self.model.device)

        self.register_buffer('betas', to_torch(self.model.betas))
        self.register_buffer('alphas_cumprod', to_torch(alphas_cumprod))
        self.register_buffer('alphas_cumprod_prev', to_torch(self.model.alphas_cumprod_prev))

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.register_buffer('sqrt_alphas_cumprod', to_torch(np.sqrt(alphas_cumprod.cpu())))
        self.register_buffer('sqrt_one_minus_alphas_cumprod', to_torch(np.sqrt(1. - alphas_cumprod.cpu())))
        self.register_buffer('log_one_minus_alphas_cumprod', to_torch(np.log(1. - alphas_cumprod.cpu())))
        self.register_buffer('sqrt_recip_alphas_cumprod', to_torch(np.sqrt(1. / alphas_cumprod.cpu())))
        self.register_buffer('sqrt_recipm1_alphas_cumprod', to_torch(np.sqrt(1. / alphas_cumprod.cpu() - 1)))

        # ddim sampling parameters
        ddim_sigmas, ddim_alphas, ddim_alphas_prev = make_ddim_sampling_parameters(alphacums=alphas_cumprod.cpu(),
                                                                                   ddim_timesteps=self.ddim_timesteps,
                                                                                   eta=ddim_eta,verbose=verbose)
        self.register_buffer('ddim_sigmas', ddim_sigmas)
        self.register_buffer('ddim_alphas', ddim_alphas)
        self.register_buffer('ddim_alphas_prev', ddim_alphas_prev)
        self.register_buffer('ddim_sqrt_one_minus_alphas', np.sqrt(1. - ddim_alphas))
        sigmas_for_original_sampling_steps = ddim_eta * torch.sqrt(
            (1 - self.alphas_cumprod_prev) / (1 - self.alphas_cumprod) * (
                        1 - self.alphas_cumprod / self.alphas_cumprod_prev))
        self.register_buffer('ddim_sigmas_for_original_num_steps', sigmas_for_original_sampling_steps)

    @torch.no_grad()
    def sample(self,
               S,
               batch_size,
               shape,
               conditioning=None,
               callback=None,
               normals_sequence=None,
               img_callback=None,
               quantize_x0=False,
               eta=0.,
               mask=None,
               x0=None,
               temperature=1.,
               noise_dropout=0.,
               score_corrector=None,
               corrector_kwargs=None,
               verbose=True,
               x_T=None,
               log_every_t=100,
               unconditional_guidance_scale=1.,
               unconditional_conditioning=None,
               # this has to come in the same format as the conditioning, # e.g. as encoded tokens, ...
               **kwargs
               ):
        if conditioning is not None:
            if isinstance(conditioning, dict):
                cbs = conditioning[list(conditioning.keys())[0]].shape[0]
                if cbs != batch_size:
                    print(f"Warning: Got {cbs} conditionings but batch-size is {batch_size}")
            else:
                if conditioning.shape[0] != batch_size:
                    print(f"Warning: Got {conditioning.shape[0]} conditionings but batch-size is {batch_size}")

        self.make_schedule(ddim_num_steps=S, ddim_eta=eta, verbose=verbose)
        # sampling
        C, H, W = shape
        size = (batch_size, C, H, W)
        print(f'Data shape for PLMS sampling is {size}')

        samples, intermediates = self.plms_sampling(conditioning, size,
                                                    callback=callback,
                                                    img_callback=img_callback,
                                                    quantize_denoised=quantize_x0,
                                                    mask=mask, x0=x0,
                                                    ddim_use_original_steps=False,
                                                    noise_dropout=noise_dropout,
                                                    temperature=temperature,
                                                    score_corrector=score_corrector,
                                                    corrector_kwargs=corrector_kwargs,
                                                    x_T=x_T,
                                                    log_every_t=log_every_t,
                                                    unconditional_guidance_scale=unconditional_guidance_scale,
                                                    unconditional_conditioning=unconditional_conditioning,
                                                    )
        return samples, intermediates

    @torch.no_grad()
    def plms_sampling(self, cond, shape,
                      x_T=None, ddim_use_original_steps=False,
                      callback=None, timesteps=None, quantize_denoised=False,
                      mask=None, x0=None, img_callback=None, log_every_t=100,
                      temperature=1., noise_dropout=0., score_corrector=None, corrector_kwargs=None,
                      unconditional_guidance_scale=1., unconditional_conditioning=None,):
        device = self.model.betas.device
        #device = "cuda"
        b = shape[0]
        if x_T is None:
            img = torch.randn(shape, device=device)
            img_cpu = torch.randn(shape, device="cpu")
        else:
            img = x_T
            img_cpu = x_T

        if timesteps is None:
            timesteps = self.ddpm_num_timesteps if ddim_use_original_steps else self.ddim_timesteps
        elif timesteps is not None and not ddim_use_original_steps:
            subset_end = int(min(timesteps / self.ddim_timesteps.shape[0], 1) * self.ddim_timesteps.shape[0]) - 1
            timesteps = self.ddim_timesteps[:subset_end]

        intermediates = {'x_inter': [img], 'pred_x0': [img]}
        time_range = list(reversed(range(0,timesteps))) if ddim_use_original_steps else np.flip(timesteps)
        total_steps = timesteps if ddim_use_original_steps else timesteps.shape[0]
        print(f"Running PLMS Sampling with {total_steps} timesteps")

        iterator = tqdm(time_range, desc='PLMS Sampler', total=total_steps)
        old_eps = []

        bits_scale = 26
        # Create TenSEAL context
        context = ts.context(
            ts.SCHEME_TYPE.CKKS,
            poly_modulus_degree=8192,
            coeff_mod_bit_sizes=[31, bits_scale, bits_scale, bits_scale, bits_scale, bits_scale, bits_scale, 31]
        )

        # set the scale
        context.global_scale = pow(2, bits_scale)

        # galois keys are required to do ciphertext rotations
        context.generate_galois_keys()
        '''
        T0 = time.time()
        enc_img = ts.ckks_tensor(context, img)
        T1 = time.time()
        print(f"encrypt needs: {T1-T0}s")
        '''
        wm = "StableDiffusionV1"
        wm_encoder = WatermarkEncoder()
        wm_encoder.set_watermark('bytes', wm.encode('utf-8'))
        sparse=True

        for i, step in enumerate(iterator):
            index = total_steps - i - 1
            tstep = torch.full((b,), step, device=device, dtype=torch.long)
            tstep_next = torch.full((b,), time_range[min(i + 1, len(time_range) - 1)], device=device, dtype=torch.long)

            if mask is not None:
                assert x0 is not None
                img_orig = self.model.q_sample(x0, tstep)  # TODO: deterministic forward pass?
                mask_img_orig = img_orig*mask
                img = mask_img_orig + (1. - mask) * img
                enc_img = mask_img_orig + (1. - mask) * enc_img

            if sparse:
                new_image = remove_points(img_cpu, threshold=0.01)
                remain_img = img_cpu - new_image
                print("zeros: ", count_zeros(new_image))
                coo_img = convert_dense_to_coo(new_image)
                T0 = time.time()
                coo_img.encrypt(context)
                T1 = time.time()
                print(f"encrypt needs: {T1-T0}s")


                outs = self.p_sample_plms_sp(img, coo_img, remain_img, cond, tstep, index=index, use_original_steps=ddim_use_original_steps,
                                      quantize_denoised=quantize_denoised, temperature=temperature,
                                      noise_dropout=noise_dropout, score_corrector=score_corrector,
                                      corrector_kwargs=corrector_kwargs,
                                      unconditional_guidance_scale=unconditional_guidance_scale,
                                      unconditional_conditioning=unconditional_conditioning,
                                      old_eps=old_eps, t_next=tstep_next)
                coo_img, remain_img, img_cpu, e_t = outs
                img = img_cpu
                #img = img_cpu.cuda()
            else:
                T0 = time.time()
                enc_img = ts.ckks_tensor(context, img)
                T1 = time.time()
                print(f"encrypt needs: {T1-T0}s")
                outs = self.p_sample_plms(img, enc_img, cond, tstep, index=index, use_original_steps=ddim_use_original_steps,
                                      quantize_denoised=quantize_denoised, temperature=temperature,
                                      noise_dropout=noise_dropout, score_corrector=score_corrector,
                                      corrector_kwargs=corrector_kwargs,
                                      unconditional_guidance_scale=unconditional_guidance_scale,
                                      unconditional_conditioning=unconditional_conditioning,
                                      old_eps=old_eps, t_next=tstep_next)
                enc_img, img, e_t = outs

            '''
            if i == 49:
                thresholds = [0.005, 0.01, 0.05, 0.1, 0.2, 0.5, 0.9, 0.99] 
                #print("save intermediate tensors: ", i, " ", step)
                for threshold in thresholds:
                    new_img = remove_points(img, threshold=threshold)
                    x_samples_ddim = self.model.decode_first_stage(new_img)
                    x_samples_ddim = torch.clamp((x_samples_ddim + 1.0) / 2.0, min=0.0, max=1.0)
                    grid = make_grid(x_samples_ddim, nrow=3)
                    grid = 255. * rearrange(grid, 'c h w -> h w c').to(torch.float).cpu().numpy()
                    img_new = Image.fromarray(grid.astype(np.uint8))
                    img_new = put_watermark(img_new, wm_encoder)
                    img_new.save(os.path.join("outputs/txt2img-samples/", f"thresholds_{threshold}.png"))
            '''
            old_eps.append(e_t)
            if len(old_eps) >= 4:
                old_eps.pop(0)
            if callback: callback(i)
            #if img_callback: img_callback(pred_x0, i)

            if index % log_every_t == 0 or index == total_steps - 1:
                intermediates['x_inter'].append(img)
                #intermediates['pred_x0'].append(pred_x0)

        return img, intermediates

    @torch.no_grad()
    def p_sample_plms(self, x, enc_x, c, t, index, repeat_noise=False, use_original_steps=False, quantize_denoised=False,
                      temperature=1., noise_dropout=0., score_corrector=None, corrector_kwargs=None,
                      unconditional_guidance_scale=1., unconditional_conditioning=None, old_eps=None, t_next=None):
        b, *_, device = *x.shape, x.device

        #self.model.cuda()
        '''
        context = ts.context(
                    ts.SCHEME_TYPE.CKKS,
                    poly_modulus_degree=16384,
                    coeff_mod_bit_sizes=[60, 40, 40, 40, 40, 60]
                )
        context.generate_galois_keys()
        context.global_scale = 2**40
        '''

        def get_model_output(x, t):
            if unconditional_conditioning is None or unconditional_guidance_scale == 1.:
                e_t = self.model.apply_model(x, t, c)
            else:
                e_t_uncond = self.model.apply_model(x, t, unconditional_conditioning)
                e_t = self.model.apply_model(x, t, c)
                e_t = e_t_uncond + unconditional_guidance_scale * (e_t - e_t_uncond)
                #print("got model output")

            if score_corrector is not None:
                assert self.model.parameterization == "eps"
                e_t = score_corrector.modify_score(self.model, e_t, x, t, c, **corrector_kwargs)

            return e_t

        alphas = self.model.alphas_cumprod if use_original_steps else self.ddim_alphas
        alphas_prev = self.model.alphas_cumprod_prev if use_original_steps else self.ddim_alphas_prev
        sqrt_one_minus_alphas = self.model.sqrt_one_minus_alphas_cumprod if use_original_steps else self.ddim_sqrt_one_minus_alphas
        sigmas = self.model.ddim_sigmas_for_original_num_steps if use_original_steps else self.ddim_sigmas

        def get_x_prev_and_pred_x0(e_t, index):
            # select parameters corresponding to the currently considered timestep
            a_t = torch.full((b, 1, 1, 1), alphas[index], device=device)
            a_prev = torch.full((b, 1, 1, 1), alphas_prev[index], device=device)
            sigma_t = torch.full((b, 1, 1, 1), sigmas[index], device=device)
            sqrt_one_minus_at = torch.full((b, 1, 1, 1), sqrt_one_minus_alphas[index],device=device)

            # current prediction for x_0
            pred_x0 = (x - sqrt_one_minus_at * e_t) * (1 / a_t.sqrt())
            if quantize_denoised:
                pred_x0, _, *_ = self.model.first_stage_model.quantize(pred_x0)
            # direction pointing to x_t
            dir_xt = (1. - a_prev - sigma_t**2).sqrt() * e_t
            noise = sigma_t * noise_like(x.shape, device, repeat_noise) * temperature
            if noise_dropout > 0.:
                noise = torch.nn.functional.dropout(noise, p=noise_dropout)
            x_prev = a_prev.sqrt() * pred_x0 + dir_xt + noise
            return x_prev, pred_x0

        def get_x_prev_and_pred_x0_enc(e_t, index):
            dir_xt = math.sqrt(1. - alphas_prev[index] - sigmas[index]**2) * e_t
            noise = float(sigmas[index]) * temperature * noise_like(x.shape, "cpu", repeat_noise)
            if noise_dropout > 0.:
                noise = torch.nn.functional.dropout(noise, p=noise_dropout)
            dir_xt = dir_xt + noise
            factor = math.sqrt(alphas_prev[index] / alphas[index])
            add_part =  dir_xt - factor * float(sqrt_one_minus_alphas[index]) * e_t  
            x_prev = factor * enc_x + add_part
            return x_prev

        T0 = time.time()
        e_t = get_model_output(x, t)
        T1 = time.time()
        if len(old_eps) == 0:
            # Pseudo Improved Euler (2nd order)
            x_prev, _ = get_x_prev_and_pred_x0(e_t, index)
            #enc_x_prev, _ = get_x_prev_and_pred_x0_enc(e_t, index)
            #x_prev = torch.tensor(enc_x_prev.decrypt().tolist())
            e_t_next = get_model_output(x_prev, t_next)
            e_t_prime = (e_t + e_t_next) / 2
        elif len(old_eps) == 1:
            # 2nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (3 * e_t - old_eps[-1]) / 2
        elif len(old_eps) == 2:
            # 3nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (23 * e_t - 16 * old_eps[-1] + 5 * old_eps[-2]) / 12
        elif len(old_eps) >= 3:
            # 4nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (55 * e_t - 59 * old_eps[-1] + 37 * old_eps[-2] - 9 * old_eps[-3]) / 24

        #model_cpu = self.model.cpu()
        #enc_e_t_prime = ts.ckks_tensor(context, e_t_prime)
        enc_x_prev = get_x_prev_and_pred_x0_enc(e_t_prime.cpu(), index)
        x_prev = torch.tensor(enc_x_prev.decrypt().tolist())
        T2 = time.time()
        print(f"model forward: {T1-T0}s, get prev: {T2-T1}s")

        return enc_x_prev, x_prev, e_t

    @torch.no_grad()
    def p_sample_plms_sp(self, x, coo_x, remain_x, c, t, index, repeat_noise=False, use_original_steps=False, quantize_denoised=False,
                      temperature=1., noise_dropout=0., score_corrector=None, corrector_kwargs=None,
                      unconditional_guidance_scale=1., unconditional_conditioning=None, old_eps=None, t_next=None):
        b, *_, device = *x.shape, x.device

        #self.model.cuda()
        '''
        context = ts.context(
                    ts.SCHEME_TYPE.CKKS,
                    poly_modulus_degree=16384,
                    coeff_mod_bit_sizes=[60, 40, 40, 40, 40, 60]
                )
        context.generate_galois_keys()
        context.global_scale = 2**40
        '''

        def get_model_output(x, t):
            if unconditional_conditioning is None or unconditional_guidance_scale == 1.:
                e_t = self.model.apply_model(x, t, c)
            else:
                e_t_uncond = self.model.apply_model(x, t, unconditional_conditioning)
                e_t = self.model.apply_model(x, t, c)
                e_t = e_t_uncond + unconditional_guidance_scale * (e_t - e_t_uncond)
                #print("got model output")

            if score_corrector is not None:
                assert self.model.parameterization == "eps"
                e_t = score_corrector.modify_score(self.model, e_t, x, t, c, **corrector_kwargs)

            return e_t

        alphas = self.model.alphas_cumprod if use_original_steps else self.ddim_alphas
        alphas_prev = self.model.alphas_cumprod_prev if use_original_steps else self.ddim_alphas_prev
        sqrt_one_minus_alphas = self.model.sqrt_one_minus_alphas_cumprod if use_original_steps else self.ddim_sqrt_one_minus_alphas
        sigmas = self.model.ddim_sigmas_for_original_num_steps if use_original_steps else self.ddim_sigmas

        def get_x_prev_and_pred_x0(e_t, index):
            # select parameters corresponding to the currently considered timestep
            a_t = torch.full((b, 1, 1, 1), alphas[index], device=device)
            a_prev = torch.full((b, 1, 1, 1), alphas_prev[index], device=device)
            sigma_t = torch.full((b, 1, 1, 1), sigmas[index], device=device)
            sqrt_one_minus_at = torch.full((b, 1, 1, 1), sqrt_one_minus_alphas[index],device=device)

            # current prediction for x_0
            pred_x0 = (x - sqrt_one_minus_at * e_t) * (1 / a_t.sqrt())
            if quantize_denoised:
                pred_x0, _, *_ = self.model.first_stage_model.quantize(pred_x0)
            # direction pointing to x_t
            dir_xt = (1. - a_prev - sigma_t**2).sqrt() * e_t
            noise = sigma_t * noise_like(x.shape, device, repeat_noise) * temperature
            if noise_dropout > 0.:
                noise = torch.nn.functional.dropout(noise, p=noise_dropout)
            x_prev = a_prev.sqrt() * pred_x0 + dir_xt + noise
            return x_prev, pred_x0

        def get_x_prev_and_pred_x0_enc(e_t, index):
            dir_xt = math.sqrt(1. - alphas_prev[index] - sigmas[index]**2) * e_t
            noise = float(sigmas[index]) * temperature * noise_like(x.shape, "cpu", repeat_noise)
            if noise_dropout > 0.:
                noise = torch.nn.functional.dropout(noise, p=noise_dropout)
            dir_xt = dir_xt + noise
            factor = math.sqrt(alphas_prev[index] / alphas[index])
            add_part =  dir_xt - factor * float(sqrt_one_minus_alphas[index]) * e_t  
            remain_x_prev = factor * remain_x + add_part
            print("got remain x_prev")
            coo_x_prev = factor * coo_x + add_part
            print("got coo x_prev")
            return remain_x_prev, coo_x_prev

        T0 = time.time()
        e_t = get_model_output(x, t)
        T1 = time.time()
        if len(old_eps) == 0:
            # Pseudo Improved Euler (2nd order)
            x_prev, _ = get_x_prev_and_pred_x0(e_t, index)
            #enc_x_prev, _ = get_x_prev_and_pred_x0_enc(e_t, index)
            #x_prev = torch.tensor(enc_x_prev.decrypt().tolist())
            e_t_next = get_model_output(x_prev, t_next)
            e_t_prime = (e_t + e_t_next) / 2
        elif len(old_eps) == 1:
            # 2nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (3 * e_t - old_eps[-1]) / 2
        elif len(old_eps) == 2:
            # 3nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (23 * e_t - 16 * old_eps[-1] + 5 * old_eps[-2]) / 12
        elif len(old_eps) >= 3:
            # 4nd order Pseudo Linear Multistep (Adams-Bashforth)
            e_t_prime = (55 * e_t - 59 * old_eps[-1] + 37 * old_eps[-2] - 9 * old_eps[-3]) / 24

        #model_cpu = self.model.cpu()
        #enc_e_t_prime = ts.ckks_tensor(context, e_t_prime)
        remain_x_prev, coo_x_prev = get_x_prev_and_pred_x0_enc(e_t_prime.cpu(), index)
        #x_prev, pred_x0 = get_x_prev_and_pred_x0(e_t_prime, index)
        #x_prev_ori, pred_x0_ori = get_x_prev_and_pred_x0(e_t_prime, index)
        print("copied coo tensor")
        coo_x_prev_d = coo_x_prev.decrypt()
        x_prev = coo_x_prev_d.merge_tensor(remain_x_prev)
        print("merged tensor")
        #print(torch.cosine_similarity(x_prev_ori, x_prev))
        #print(torch.cosine_similarity(pred_x0_ori, pred_x0))
        T2 = time.time()
        print(f"model forward: {T1-T0}s, get prev: {T2-T1}s")

        return coo_x_prev, remain_x_prev, x_prev, e_t